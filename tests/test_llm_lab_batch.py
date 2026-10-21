import json
from pathlib import Path
from threading import Barrier, Lock
from types import SimpleNamespace

import pytest

from ai_provider import ProviderProfile
from core import TranscriptSegment
from llm_experiments import LabConfig, execute_run, prepare_snapshot
from scripts import llm_lab_batch as batch
from video_store import VideoStore


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    store = VideoStore(tmp_path / "project.db")
    profile = ProviderProfile(base_url="https://not-called.example/v1", model="test-model")
    videos = []
    for index in range(1, 5):
        video_id = store.create_video(str(index), f"{index}.mp4", f"{index}.mp4", "", 10000)
        store.mark_transcribed(video_id, [
            TranscriptSegment(1, 1000, 2000, "This theorem must be mastered."),
        ], "fireredasr2-aed")
        videos.append({
            "video_id": video_id, "source": f"{index}.mp4",
            "status": "reused" if index == 1 else "completed",
            "summary": {
                "reused_database_transcript": index == 1,
                "transcript_sha256": batch.asr_hash(store, video_id),
            },
        })
    source = tmp_path / "asr.json"
    source.write_text(json.dumps({
        "batch_id": "test-asr", "status": "completed",
        "database": str(store.path), "videos": videos,
    }), encoding="utf-8")
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(prepare_snapshot(
        store, 1, LabConfig(scheme="few_shot"), profile,
    )), encoding="utf-8")
    monkeypatch.setattr(batch, "load_provider_settings", lambda: SimpleNamespace(active_profile=profile))
    monkeypatch.setattr(batch, "resolve_interface_key", lambda *_args: "test-secret")
    return store, source, baseline


def empty_response():
    return SimpleNamespace(
        id="test-response", model="test-model",
        choices=[SimpleNamespace(
            message=SimpleNamespace(content='{"candidates":[]}'), finish_reason="stop",
        )],
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=5, total_tokens=105),
    )


def install_executor(monkeypatch, *, failed_video=None, barrier=None, peaks=None):
    lock = Lock()
    active = 0

    def execute(store, run_id, snapshot, key, approved, **kwargs):
        def create(**_request):
            nonlocal active
            with lock:
                active += 1
                if peaks is not None:
                    peaks.append(active)
            try:
                if barrier is not None and snapshot["video_id"] in (2, 3):
                    barrier.wait(timeout=5)
                if snapshot["video_id"] == failed_video:
                    raise RuntimeError("test request failure")
                return empty_response()
            finally:
                with lock:
                    active -= 1
        client = SimpleNamespace(
            max_retries=0, chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        )
        return execute_run(store, run_id, snapshot, key, approved, client=client, **kwargs)
    monkeypatch.setattr(batch, "execute_run", execute)


def test_preview_uses_frozen_b_and_new_videos_only_without_keys(corpus, monkeypatch):
    store, source, baseline = corpus
    monkeypatch.setattr(batch, "load_provider_settings", lambda: pytest.fail("preview read credentials"))
    manifest = batch.run_batch(source, baseline)
    assert manifest["status"] == "preview_only"
    assert [item["video_id"] for item in manifest["videos"]] == [2, 3, 4]
    original = json.loads(baseline.read_text(encoding="utf-8"))
    for item in manifest["videos"]:
        snapshot = json.loads(
            (Path(manifest["directory"]) / f"video-{item['video_id']}" / "snapshot.json")
            .read_text(encoding="utf-8")
        )
        assert snapshot["config"] == original["config"]
        assert snapshot["prompt_bundle"] == original["prompt_bundle"]
        assert not store.list_experiments(item["video_id"])


def test_two_concurrent_calls_and_complete_independent_logs(corpus, monkeypatch):
    store, source, baseline = corpus
    peaks = []
    install_executor(monkeypatch, barrier=Barrier(2), peaks=peaks)
    manifest = batch.run_batch(source, baseline, execute=True, concurrency=2)
    assert manifest["status"] == "completed" and max(peaks) == 2
    assert manifest["concurrency"] == 2 and manifest["planned_extract_calls"] == 3
    assert not store.list_experiments(1)
    for item in manifest["videos"]:
        assert item["summary"]["complete"] and item["summary"]["candidate_count"] == 0
        assert item["summary"]["call_count"] == 1
        assert item["summary"]["usage"]["totals"]["total_tokens"] == 105
        assert batch.asr_hash(store, item["video_id"]) == item["asr_sha256"]
        assert not store.list_markers(item["video_id"])
        export = Path(manifest["directory"]) / f"video-{item['video_id']}"
        calls = json.loads((export / "calls.json").read_text(encoding="utf-8"))
        assert calls == store.list_experiment_calls(item["experiment_id"])
        assert calls[0]["status"] == "completed"
        assert calls[0]["raw_content"] == '{"candidates":[]}'
    assert (Path(manifest["directory"]) / "review.md").is_file()


def test_failure_is_not_zero_findings_and_other_videos_continue(corpus, monkeypatch):
    _store, source, baseline = corpus
    install_executor(monkeypatch, failed_video=2)
    manifest = batch.run_batch(source, baseline, execute=True)
    assert manifest["status"] == "completed_with_failures"
    failed = manifest["videos"][0]
    assert failed["status"] == "failed"
    assert failed["summary"]["candidate_count"] is None
    assert not failed["summary"]["complete"]
    assert all(item["summary"]["complete"] for item in manifest["videos"][1:])
    calls = json.loads(
        (Path(manifest["directory"]) / "video-2/calls.json").read_text(encoding="utf-8")
    )
    assert calls[0]["error"]["message"] == "test request failure"


def test_changed_asr_prevents_all_requests(corpus, monkeypatch):
    store, source, baseline = corpus
    store.mark_transcribed(3, [TranscriptSegment(1, 1000, 2000, "changed")], "fireredasr2-aed")
    monkeypatch.setattr(batch, "execute_run", lambda *_a, **_k: pytest.fail("API called"))
    with pytest.raises(ValueError, match="ASR changed"):
        batch.run_batch(source, baseline, execute=True)
    assert all(not store.list_experiments(video_id) for video_id in range(1, 5))


def test_another_batch_does_not_overwrite_previous_results(corpus, monkeypatch):
    store, source, baseline = corpus
    install_executor(monkeypatch)
    first = batch.run_batch(source, baseline, execute=True)
    first_calls = (Path(first["directory"]) / "video-2/calls.json").read_bytes()
    second = batch.run_batch(source, baseline, execute=True)
    assert first["batch_id"] != second["batch_id"]
    assert (Path(first["directory"]) / "video-2/calls.json").read_bytes() == first_calls
    assert len(store.list_experiments(2)) == 2
