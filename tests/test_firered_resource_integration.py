import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

import api.main as api_main
import classcue.batch_processing as batch_processing
from classcue.core import TranscriptSegment
import classcue.firered_resilience as fr
from classcue.processing_control import ResourcePaused
from tests.test_qwen_resource_integration import configure_judge, wait_for
import classcue.video_pipeline as video_pipeline
from classcue.video_store import VideoStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(api_main, "DIRECTORY_PROJECT_ROOT", tmp_path / "application")
    monkeypatch.setattr(video_pipeline, "configured_audio_directory", lambda *_: tmp_path)
    return VideoStore(tmp_path / "project.db")


def test_resource_profile_does_not_scan_or_load_models(store, monkeypatch):
    monkeypatch.setattr(api_main, "resource_profile", lambda: {
        "engine": "firered", "cuda_available": True, "gpu_total_mib": 8188,
        "initial_batch_size": 6,
    })
    monkeypatch.setattr(api_main, "model_is_downloaded", lambda *_: pytest.fail("no scan"))
    monkeypatch.setattr(api_main, "cache_size", lambda *_: pytest.fail("no scan"))
    monkeypatch.setattr(api_main, "prepare_asr_model", lambda *_: pytest.fail("no model loading"))
    monkeypatch.setattr(api_main, "selected_model_id", lambda: "fireredasr2-aed")
    with TestClient(api_main.create_app(store)) as client:
        result = client.get("/api/asr/models/fireredasr2-aed/resource-profile")
        assert result.status_code == 200 and result.json()["initial_batch_size"] == 6
        assert client.get("/api/asr/selection").json()["engine"] == "firered"
        assert client.get("/api/asr/models/missing/resource-profile").status_code == 404
        assert client.get("/api/asr/models/whisper-turbo/resource-profile").status_code == 400


@pytest.mark.parametrize("kind", ["resource", "ordinary"])
def test_firered_pause_or_failure_preserves_old_subtitles_and_review_markers(tmp_path, store, monkeypatch, kind):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"fixture")
    video_id = store.create_video("hash", path.name, str(path), str(tmp_path / "audio.wav"), 1000)
    old = [TranscriptSegment(1, 0, 1000, "old", [])]
    store.mark_transcribed(video_id, old, "whisper-turbo")
    marker_id = store.add_marker(video_id, 0, 1000, "old", "", "old", "old", .9, "high_recall")
    store.set_marker_review_status(marker_id, "mastered")
    calls = []
    error = ResourcePaused("resources") if kind == "resource" else RuntimeError("inference error")
    def transcribe(*args, **kwargs):
        calls.append(kwargs)
        raise error
    monkeypatch.setattr(video_pipeline, "transcribe_audio", transcribe)
    monkeypatch.setattr(video_pipeline, "extract_audio", lambda *_: pytest.fail("no decode retry"))
    with pytest.raises(type(error)):
        video_pipeline.retranscribe_video(store, video_id, "fireredasr2-aed")
    assert len(calls) == 1
    assert calls[0]["cache_directory"] == tmp_path / "firered_jobs"
    assert store.get_segments(video_id) == old
    with store.connect() as connection:
        assert connection.execute("SELECT review_status FROM video_markers WHERE id = ?",
                                  (marker_id,)).fetchone()[0] == "mastered"
    assert store.get_video(video_id)["transcription_model"] == "whisper-turbo"


def test_api_metadata_survives_restart_and_saved_request_resumes(store, monkeypatch):
    configure_judge(monkeypatch)
    video_id = store.create_video("hash", "video.mp4", "video.mp4", "audio.wav", 1000)
    calls = []
    value = {"engine": "firered", "initial_batch_size": 6, "current_batch_size": 1,
             "completed_segments": 12, "total_segments": 30, "recovery_count": 5,
             "recovery_reason": "inference: oom", "status": "resource_paused"}

    def process(_store, vid, _judge, **kwargs):
        calls.append(kwargs["asr_model_id"])
        fr._RESOURCE.get()(value)
        if len(calls) == 1:
            raise ResourcePaused("resources")
        return "completed", 1

    monkeypatch.setattr(api_main, "process_video_once", process)
    with TestClient(api_main.create_app(store)) as client:
        response = client.post(f"/api/videos/{video_id}/process", json={"asr_model_id": "fireredasr2-aed"})
        assert response.status_code == 202
        job_id = response.json()["job_id"]
        result = wait_for(client, job_id, "resource_paused")
        assert result["resource"] == value
    with TestClient(api_main.create_app(store)) as client:
        assert client.get(f"/api/jobs/{job_id}").json()["resource"] == value
        options = client.get(f"/api/videos/{video_id}/processing-options").json()
        assert options["active_job"]["resource"] == value
        assert '"current_batch_size": 1' in client.get(f"/api/jobs/{job_id}/events").text
        resumed = client.post(f"/api/jobs/{job_id}/resume", json={})
        assert resumed.status_code == 202
        wait_for(client, resumed.json()["job_id"], "completed")
    assert calls == ["fireredasr2-aed", "fireredasr2-aed"]


def test_old_database_gains_nullable_resource_column(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as connection:
        connection.execute("""CREATE TABLE processing_jobs (
            id TEXT PRIMARY KEY, video_id INTEGER, job_type TEXT, status TEXT,
            stage TEXT, progress REAL, message TEXT, error_message TEXT,
            started_at TEXT, finished_at TEXT, created_at TEXT)""")
        connection.execute("INSERT INTO processing_jobs(id,job_type,status) VALUES ('old','video_process','failed')")
    store = VideoStore(path)
    assert store.get_job("old")["resource_json"] is None
    assert api_main.JobManager._row_payload(store.get_job("old"))["resource"] is None


def test_shared_batch_retains_firered_limits_and_resets_next_queue(tmp_path, store, monkeypatch):
    paths = [tmp_path / f"{index}.mp4" for index in range(2)]
    ids = [store.create_video(str(i), path.name, str(path), "audio.wav", 1000)
           for i, path in enumerate(paths)]
    monkeypatch.setattr(batch_processing, "register_local_video",
                        lambda _store, path, _dir: (ids[paths.index(path)], path, True))
    seen = []
    def prepare(*_args, **_kwargs):
        queue = fr._QUEUE.get()
        seen.append(queue.batch_size)
        queue.batch_size = 3
        return "skipped", 1
    monkeypatch.setattr(batch_processing, "prepare_video_for_analysis", prepare)
    for _ in range(2):
        batch_processing.run_batch(store, paths, tmp_path, object(), asr_model_id="fireredasr2-aed")
    assert seen == [None, 3, None, 3]
    assert fr._QUEUE.get() is None


def test_api_batch_resume_resets_firered_limits_and_supports_skip(store, monkeypatch, tmp_path):
    import classcue.web_batch as web_batch
    from classcue.ai_provider import ProviderProfile
    configure_judge(monkeypatch)
    monkeypatch.setattr(api_main, "load_provider_settings", lambda: SimpleNamespace(
        active_profile=ProviderProfile(base_url="https://unused.example/v1", model="fixture"),
    ))
    paths = [tmp_path / f"{i}.mp4" for i in range(3)]
    for path in paths:
        path.write_bytes(b"fixture")
    ids = [store.create_video(str(i), path.name, str(path), "audio.wav", 1000) for i, path in enumerate(paths)]
    seen = []
    def prepare(_store, vid, **_kwargs):
        queue = fr._QUEUE.get()
        seen.append((vid, queue.batch_size))
        queue.batch_size = 3
        return "completed", 1
    monkeypatch.setattr(web_batch, "prepare_video_for_analysis", prepare)
    monkeypatch.setattr(web_batch, "analyze_prepared_video_once", lambda *_a, **_kw: ("completed", 1))
    with TestClient(api_main.create_app(store)) as client:
        for index in range(2):
            store.create_job(f"paused-{index}", "batch_video_process")
            store.update_job(f"paused-{index}", status="resource_paused", request_json=json.dumps({
                "kind": "batch", "items": [[vid, str(paths[i])] for i, vid in enumerate(ids)],
                "options": {"asr_model_id": "fireredasr2-aed"},
            }))
            result = client.post(f"/api/jobs/paused-{index}/resume", json={"skip_current": True})
            wait_for(client, result.json()["job_id"], "completed")
    assert seen == [(ids[1], None), (ids[2], 3), (ids[1], None), (ids[2], 3)]
