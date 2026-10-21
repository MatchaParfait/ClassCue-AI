import json
from pathlib import Path
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

import api.main as api_main
import batch_processing
from core import TranscriptSegment
from processing_control import ResourcePaused
from video_store import VideoStore
import video_pipeline
import qwen_resilience
import web_batch


def wait_for(client, job_id, status):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = client.get(f"/api/jobs/{job_id}").json()
        if result["status"] == status:
            return result
        time.sleep(.02)
    raise AssertionError(result)


@pytest.fixture
def store(tmp_path):
    return VideoStore(tmp_path / "project.db")


def test_pipeline_resource_pause_is_not_a_media_decode_retry(tmp_path, store, monkeypatch):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")
    video_id = store.create_video("hash", path.name, str(path), str(tmp_path / "audio.wav"), 1000)
    calls = []

    def transcribe(*args, **kwargs):
        calls.append(kwargs)
        raise ResourcePaused("resources")

    monkeypatch.setattr(video_pipeline, "transcribe_audio", transcribe)
    monkeypatch.setattr(video_pipeline, "extract_audio", lambda *_: pytest.fail("not a decoding error"))
    with pytest.raises(ResourcePaused):
        video_pipeline.prepare_video_for_analysis(store, video_id, asr_model_id="qwen3-asr-1.7b")
    assert len(calls) == 1
    assert store.get_video(video_id)["status"] == "resource_paused"
    assert not store.get_segments(video_id)


def test_retranscription_pause_preserves_old_subtitles(tmp_path, store, monkeypatch):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")
    video_id = store.create_video("hash", path.name, str(path), str(tmp_path / "audio.wav"), 1000)
    old = [TranscriptSegment(1, 0, 1000, "old", [])]
    store.mark_transcribed(video_id, old, "whisper-turbo")
    monkeypatch.setattr(video_pipeline, "_transcribe_video_with_fallback",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(ResourcePaused("resources")))
    with pytest.raises(ResourcePaused):
        video_pipeline.retranscribe_video(store, video_id, "qwen3-asr-1.7b")
    assert store.get_segments(video_id) == old
    assert store.get_video(video_id)["transcription_model"] == "whisper-turbo"
    assert store.get_video(video_id)["status"] == "resource_paused"


def test_shared_batch_stops_new_asr_but_drains_ai(tmp_path, store, monkeypatch):
    paths = [tmp_path / f"{index}.mp4" for index in range(3)]
    ids = [store.create_video(str(i), path.name, str(path), "audio.wav", 1000)
           for i, path in enumerate(paths)]
    monkeypatch.setattr(batch_processing, "register_local_video",
                        lambda _store, path, _dir: (ids[paths.index(path)], path, True))
    prepared = []
    analyzed = []

    def prepare(_store, video_id, **kwargs):
        prepared.append(video_id)
        if video_id == ids[1]:
            raise ResourcePaused("resources")
        return "prepared", 1

    monkeypatch.setattr(batch_processing, "prepare_video_for_analysis", prepare)
    monkeypatch.setattr(batch_processing, "analyze_prepared_video_once",
                        lambda _store, video_id, *_: analyzed.append(video_id) or ("completed", 1))
    result = batch_processing.run_batch(store, paths, tmp_path, object())
    assert prepared == ids[:2]
    assert analyzed == ids[:1]
    assert result.paused
    assert result.remaining_paths == tuple(str(p) for p in paths[1:])
    assert result.completed == 1 and result.failed == 0
    assert json.loads((tmp_path / "batch_queue.json").read_text())["status"] == "resource_paused"


def configure_judge(monkeypatch):
    from ai_provider import ProviderProfile
    monkeypatch.setattr(api_main, "load_provider_settings",
                        lambda: SimpleNamespace(active_profile=ProviderProfile(base_url="https://unused.example/v1", model="fixture-model")))
    monkeypatch.setattr(api_main, "resolve_interface_key", lambda *_: "key")
    monkeypatch.setattr(api_main, "interface_provider", lambda *_: "provider")
    monkeypatch.setattr(api_main, "create_judge", lambda *_args, **_kwargs: SimpleNamespace(close=lambda: None))


def batch_videos(store, count):
    paths = [store.path.parent / f"{number}.mp4" for number in range(count)]
    for path in paths:
        path.write_bytes(b"mock video; decoding is replaced by the test")
    ids = [store.create_video(str(index), path.name, str(path), "audio.wav", 1000)
           for index, path in enumerate(paths)]
    return ids, paths


def test_shared_batch_resume_finishes_saved_queue(tmp_path, store, monkeypatch):
    path = tmp_path / "video.mp4"
    video_id = store.create_video("hash", path.name, str(path), "audio.wav", 1000)
    queue_path = tmp_path / "batch_queue.json"
    queue_path.write_text(json.dumps({"status": "resource_paused", "paths": [str(path)]}),
                          encoding="utf-8")
    monkeypatch.setattr(batch_processing, "register_local_video",
                        lambda *_: (video_id, path, True))
    monkeypatch.setattr(batch_processing, "prepare_video_for_analysis",
                        lambda *_args, **_kwargs: ("skipped", 1))
    result = batch_processing.run_batch(store, [path], tmp_path, object())
    assert not result.paused
    assert json.loads(queue_path.read_text())["status"] == "completed"


def test_shared_batch_uses_one_recovery_scope_and_resets_on_next_start(tmp_path, store, monkeypatch):
    paths = [tmp_path / f"{i}.mp4" for i in range(2)]
    ids = [store.create_video(str(i), path.name, str(path), "audio.wav", 1000)
           for i, path in enumerate(paths)]
    monkeypatch.setattr(batch_processing, "register_local_video",
                        lambda _store, path, _dir: (ids[paths.index(path)], path, True))
    seen = []

    def prepare(_store, _id, **_kwargs):
        queue = qwen_resilience._QUEUE.get()
        seen.append((queue, queue.asr_batch_size))
        queue.retain("asr", 1, 30, "low-memory")
        return "skipped", 1

    monkeypatch.setattr(batch_processing, "prepare_video_for_analysis", prepare)
    batch_processing.run_batch(store, paths, tmp_path, object())
    batch_processing.run_batch(store, paths, tmp_path, object())
    assert [batch for _queue, batch in seen] == [None, 1, None, 1]
    assert seen[0][0] is seen[1][0]
    assert seen[0][0] is not seen[2][0]
    assert qwen_resilience._QUEUE.get() is None


def test_api_batch_uses_one_recovery_scope_and_resets_on_resume(store, monkeypatch):
    configure_judge(monkeypatch)
    ids, paths = batch_videos(store, 2)
    seen = []

    def prepare(_store, _id, **_kwargs):
        queue = qwen_resilience._QUEUE.get()
        seen.append((queue, queue.asr_batch_size))
        queue.retain("asr", 1, 30, "low-memory")
        return "completed", 1

    monkeypatch.setattr(web_batch, "prepare_video_for_analysis", prepare)
    monkeypatch.setattr(web_batch, "analyze_prepared_video_once", lambda *_args, **_kwargs: ("completed", 1))
    with TestClient(api_main.create_app(store)) as client:
        for number in range(2):
            previous_id = f"paused-{number}"
            store.create_job(previous_id, "batch_video_process")
            store.update_job(previous_id, status="resource_paused", request_json=json.dumps({
                "kind": "batch", "items": [[vid, str(path)] for vid, path in zip(ids, paths)],
                "options": {"asr_model_id": "qwen3-asr-1.7b"},
            }))
            response = client.post(f"/api/jobs/{previous_id}/resume", json={})
            assert response.status_code == 202
            wait_for(client, response.json()["job_id"], "completed")
    assert [batch for _queue, batch in seen] == [None, 1, None, 1]
    assert seen[0][0] is seen[1][0]
    assert seen[0][0] is not seen[2][0]


def test_api_resource_pause_survives_restart_and_resumes_without_new_input(store, monkeypatch):
    configure_judge(monkeypatch)
    video_id = store.create_video("hash", "video.mp4", "video.mp4", "audio.wav", 1000)
    seen = []

    def process(_store, vid, _judge, **kwargs):
        seen.append((vid, kwargs["qwen_options"]))
        if len(seen) == 1:
            _store.set_status(vid, "resource_paused")
            raise ResourcePaused("resources")
        return "completed", 1

    monkeypatch.setattr(api_main, "process_video_once", process)
    with TestClient(api_main.create_app(store)) as client:
        response = client.post(f"/api/videos/{video_id}/process",
                               json={"asr_model_id": "qwen3-asr-1.7b", "qwen_options": {"mode": "low-resource"}})
        job_id = response.json()["job_id"]
        wait_for(client, job_id, "resource_paused")
        assert client.get("/api/jobs/paused").json()[0]["id"] == job_id
    with TestClient(api_main.create_app(store)) as client:
        assert client.get(f"/api/jobs/{job_id}").json()["status"] == "resource_paused"
        assert client.get(f"/api/videos/{video_id}/processing-options").json()["active_job"]["id"] == job_id
        resumed = client.post(f"/api/jobs/{job_id}/resume", json={})
        assert resumed.status_code == 202
        wait_for(client, resumed.json()["job_id"], "completed")
        assert seen[0][1] == seen[1][1]
        assert client.get(f"/api/jobs/{job_id}").json()["status"] == "interrupted"


def test_api_batch_pause_can_skip_current_and_keep_remaining_queue(store, monkeypatch):
    configure_judge(monkeypatch)
    ids, paths = batch_videos(store, 3)
    store.create_job("paused", "batch_video_process")
    store.update_job("paused", status="resource_paused", request_json=json.dumps({
        "kind": "batch", "items": [[vid, str(path)] for vid, path in zip(ids, paths)],
        "options": {"asr_model_id": "qwen3-asr-1.7b"},
    }))
    seen = []
    monkeypatch.setattr(web_batch, "prepare_video_for_analysis",
                        lambda _store, vid, **kwargs: seen.append(vid) or ("prepared", 1))
    monkeypatch.setattr(web_batch, "analyze_prepared_video_once", lambda *_args, **_kwargs: ("completed", 1))
    with TestClient(api_main.create_app(store)) as client:
        response = client.post("/api/jobs/paused/resume", json={"skip_current": True})
        assert response.status_code == 202
        wait_for(client, response.json()["job_id"], "completed")
    assert seen == ids[1:]


def test_cancel_paused_job_does_not_restart_worker(store):
    store.create_job("paused", "batch_video_process")
    store.update_job("paused", status="resource_paused")
    with TestClient(api_main.create_app(store)) as client:
        result = client.post("/api/jobs/paused/cancel")
        assert result.json()["status"] == "interrupted"
        assert not store.list_active_jobs()


def test_restart_clears_processing_video_owned_by_batch_job(store):
    video_id = store.create_video("hash", "video.mp4", "video.mp4", "audio.wav", 1000)
    store.set_status(video_id, "processing")
    store.create_job("batch", "batch_video_process")
    store.update_job("batch", status="running", request_json=json.dumps({
        "kind": "batch", "items": [[video_id, "video.mp4"]],
    }))
    assert store.mark_jobs_interrupted() == 1
    assert store.get_video(video_id)["status"] == "failed"
