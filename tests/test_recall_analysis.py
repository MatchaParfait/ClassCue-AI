import json
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
import classcue.video_pipeline as video_pipeline
from api.main import VideoProcessPayload
from classcue.core import TranscriptSegment
from classcue.video_store import VideoStore


@pytest.fixture
def recall(tmp_path, monkeypatch):
    store = VideoStore(tmp_path / "workspace" / "video.db")
    source = tmp_path / "lesson.mp4"
    source.write_bytes(b"fixture-video")
    video_id = store.create_video("hash", source.name, str(source), "audio.wav", 3000)
    store.mark_transcribed(video_id, [TranscriptSegment(1, 0, 3000, "考试会考这个知识点。")])
    store.set_status(video_id, "review_ready")
    folder = store.create_folder("保留课程归属")
    store.set_video_folder(video_id, folder)
    store.publish_exam_results(video_id, "medium_recall", [{
        "start_ms": 0, "end_ms": 2000, "title": "中召回原考点",
        "reason": "fixture", "source_quote": "考试会考", "signal_type": "exam",
    }], "medium-signature", "fixture-medium")
    marker_id = store.list_markers(video_id, "medium_recall")[0]["id"]
    store.set_marker_review_status(marker_id, "mastered")
    profile = api_main.ProviderProfile(base_url="https://unused.example/v1", model="fixture-model")
    monkeypatch.setattr(api_main, "DIRECTORY_PROJECT_ROOT", tmp_path / "application")
    monkeypatch.setattr(api_main, "load_provider_settings", lambda: SimpleNamespace(active_profile=profile))
    monkeypatch.setattr(api_main, "resolve_interface_key", lambda *_: "fixture-key")
    monkeypatch.setattr(api_main, "interface_provider", lambda *_: "openai_compatible")
    monkeypatch.setattr(api_main, "require_selectable_model", lambda *_: None)
    monkeypatch.setattr(video_pipeline, "_transcribe_video_with_fallback", lambda *_args, **_kwargs: pytest.fail("Must reuse saved subtitles"))
    calls, closed, outcomes = [], [], []

    class Judge:
        def __init__(self, mode):
            self.mode = mode

        def analyze_video(self, current_store, current_id, **kwargs):
            calls.append((self.mode, profile.model))
            outcome = outcomes.pop(0) if outcomes else None
            if isinstance(outcome, BaseException):
                current_store.set_analysis_run(current_id, self.mode, "failed")
                raise outcome
            if callable(outcome):
                outcome()
            current_store.publish_exam_results(current_id, self.mode, [], "signature", f"fixture-{self.mode}")
            return "completed", 0

        def close(self):
            closed.append(self.mode)

    monkeypatch.setattr(api_main, "create_judge", lambda _profile, *_args, mode: Judge(mode))
    application = api_main.create_app(store)
    with TestClient(application) as client:
        yield SimpleNamespace(store=store, client=client, video_id=video_id, folder=folder,
                              application=application, calls=calls, closed=closed, outcomes=outcomes,
                              profile=profile)


def wait(recall, job_id):
    recall.application.state.jobs.executor.submit(lambda: None).result(timeout=5)
    return recall.client.get(f"/api/jobs/{job_id}").json()


def test_multiple_modes_validation_and_legacy_defaults():
    assert VideoProcessPayload().analysis_modes == ["medium_recall"]
    assert VideoProcessPayload(analysis_mode="low_recall").analysis_modes == ["low_recall"]
    value = VideoProcessPayload(analysis_modes=["high_recall", "medium_recall", "high_recall", "low_recall"])
    assert value.analysis_modes == ["high_recall", "medium_recall", "low_recall"]
    for invalid in [[], ["unknown"]]:
        with pytest.raises(ValueError):
            VideoProcessPayload(analysis_modes=invalid)


def test_additional_recall_reuses_subtitles_preserves_other_modes_and_folder(recall):
    before = dict(recall.store.list_markers(recall.video_id, "medium_recall")[0])
    response = recall.client.post(f"/api/videos/{recall.video_id}/analysis", json={"analysis_mode": "high_recall"})
    assert response.status_code == 202
    job = wait(recall, response.json()["job_id"])
    assert job["status"] == "completed"
    assert job["job_type"] == "video_analysis"
    assert job["analysis_modes"] == ["high_recall"]
    assert recall.calls == [("high_recall", "fixture-model")]
    assert recall.closed == ["high_recall"]
    assert dict(recall.store.list_markers(recall.video_id, "medium_recall")[0]) == before
    video = recall.store.get_video(recall.video_id)
    assert video["library_folder_id"] == recall.folder
    assert video["status"] == "review_ready"
    assert len(recall.store.get_segments(recall.video_id)) == 1
    run = recall.store.get_analysis_run(recall.video_id, "high_recall")
    assert run["status"] == "completed" and run["candidate_count"] == 0
    # Completed zero-result runs must not be mistaken for never-run modes.
    assert recall.client.post(f"/api/videos/{recall.video_id}/analysis", json={"analysis_mode": "high_recall"}).status_code == 409
    assert len(recall.calls) == 1


def test_additional_recall_failure_and_retry_preserve_old_results(recall):
    recall.outcomes.append(ValueError("fixture invalid evidence"))
    request = {"analysis_mode": "low_recall"}
    response = recall.client.post(f"/api/videos/{recall.video_id}/analysis", json=request)
    assert wait(recall, response.json()["job_id"])["status"] == "failed"
    assert recall.store.get_analysis_run(recall.video_id, "low_recall")["status"] == "failed"
    assert recall.store.get_video(recall.video_id)["status"] == "review_ready"
    assert len(recall.store.list_markers(recall.video_id, "medium_recall")) == 1
    retry = recall.client.post(f"/api/videos/{recall.video_id}/analysis", json=request)
    assert wait(recall, retry.json()["job_id"])["status"] == "completed"
    assert len(recall.calls) == 2


def test_failed_video_recovers_when_retry_uses_another_provider(recall):
    recall.store.set_status(recall.video_id, "failed")
    recall.profile.model = "gpt-test"
    recall.outcomes.append(ValueError("429 WEEKLY_LIMIT_EXCEEDED"))
    request = {"analysis_mode": "low_recall"}
    before = dict(recall.store.list_markers(recall.video_id, "medium_recall")[0])
    failed = recall.client.post(f"/api/videos/{recall.video_id}/analysis", json=request).json()
    assert wait(recall, failed["job_id"])["status"] == "failed"
    assert recall.store.get_video(recall.video_id)["status"] == "failed"

    recall.profile.model = "deepseek-test"
    retry = recall.client.post(f"/api/videos/{recall.video_id}/analysis", json=request).json()
    assert wait(recall, retry["job_id"])["status"] == "completed"
    video = recall.client.get(f"/api/videos/{recall.video_id}").json()
    assert video["status"] == "review_ready"
    assert video["folder_id"] == recall.folder
    assert dict(recall.store.list_markers(recall.video_id, "medium_recall")[0]) == before
    assert recall.store.get_job(failed["job_id"])["status"] == "failed"
    assert recall.calls == [("low_recall", "gpt-test"), ("low_recall", "deepseek-test")]


def test_additional_recall_requires_transcript_and_valid_mode(recall):
    empty_id = recall.store.create_video("empty", "empty.mp4", "empty.mp4", "empty.wav", 3000)
    assert recall.client.post(f"/api/videos/{empty_id}/analysis", json={"analysis_mode": "high_recall"}).status_code == 409
    assert recall.client.post("/api/videos/999/analysis", json={"analysis_mode": "high_recall"}).status_code == 404
    assert recall.client.post(f"/api/videos/{recall.video_id}/analysis", json={"analysis_mode": "unknown"}).status_code == 422
    assert recall.calls == []


def test_additional_recall_rejects_double_submission(recall):
    gate, entered = Event(), Event()
    def hold():
        entered.set()
        gate.wait(timeout=5)
    recall.outcomes.append(hold)
    try:
        first = recall.client.post(f"/api/videos/{recall.video_id}/analysis", json={"analysis_mode": "high_recall"})
        assert entered.wait(timeout=3)
        assert first.status_code == 202
        assert recall.client.post(f"/api/videos/{recall.video_id}/analysis", json={"analysis_mode": "high_recall"}).status_code == 409
        active = recall.client.get(f"/api/videos/{recall.video_id}/processing-options").json()["active_job"]
        assert active["analysis_modes"] == ["high_recall"]
    finally:
        gate.set()
    assert wait(recall, first.json()["job_id"])["status"] == "completed"


def test_single_processing_runs_each_mode_and_retranscribes_only_once(recall, monkeypatch):
    seen = []
    def process(_store, _id, judge, **kwargs):
        seen.append((judge.mode, kwargs["analysis_mode"], kwargs["force_retranscribe"]))
        return "completed", 0
    monkeypatch.setattr(api_main, "process_video_once", process)
    response = recall.client.post(f"/api/videos/{recall.video_id}/process", json={
        "analysis_modes": ["high_recall", "medium_recall", "low_recall"],
        "force_retranscribe": True,
    })
    assert response.status_code == 202
    assert wait(recall, response.json()["job_id"])["status"] == "completed"
    assert seen == [("high_recall", "high_recall", True), ("medium_recall", "medium_recall", False), ("low_recall", "low_recall", False)]
    request = json.loads(recall.store.get_job(response.json()["job_id"])["request_json"])
    assert request["options"]["analysis_modes"] == ["high_recall", "medium_recall", "low_recall"]
