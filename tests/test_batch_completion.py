"""A finished web batch must stay completed when cancellation arrives late.

Every batch result is published durably (``publish_semantic_markers`` commits
before returning), so once the last entry is published a cancellation can no
longer prevent anything.  These regressions drive the real API, JobManager and
SQLite store with fake ASR/AI, synthetic subtitles and real temporary
placeholder videos:

* cancelling synchronously right after the final publish must not relabel the
  finished batch as interrupted;
* genuine cancellations between videos or between modes of one video still
  interrupt, keep published results and never start later work;
* an ordinary tail error still fails the job, and a batch left awaiting the API
  is never reported as committed.
"""
import faulthandler
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from classcue.ai_provider import ProviderProfile
from classcue.api_failure import ApiUnavailable
from classcue.core import TranscriptSegment
from classcue.processing_control import ProcessingCancelled
import classcue.video_pipeline as video_pipeline
from classcue.video_store import VideoStore
from classcue.web_batch import make_batch, run_web_batch
from tests.test_legacy_cancellation import ScriptedJudge, marker_titles, selection_for


@pytest.fixture
def batch(tmp_path, monkeypatch):
    store = VideoStore(tmp_path / "workspace" / "project.db")
    profile = ProviderProfile(base_url="https://unused.example/v1", model="fake", max_retries=0)
    settings = SimpleNamespace(active_profile=profile)
    monkeypatch.setattr(api_main, "DIRECTORY_PROJECT_ROOT", tmp_path / "application")
    monkeypatch.setattr(api_main, "selected_model_id", lambda: "whisper-turbo")
    monkeypatch.setattr(api_main, "require_selectable_model", lambda _: None)
    monkeypatch.setattr(api_main, "load_provider_settings", lambda: settings)
    monkeypatch.setattr(api_main, "resolve_interface_key", lambda *_: "fixture-key")
    monkeypatch.setattr(api_main, "interface_provider", lambda _: "openai_compatible")
    monkeypatch.setattr(video_pipeline, "video_duration_ms", lambda _: 1000)
    asr_calls = []

    def transcribe(path, *_args, **_kwargs):
        asr_calls.append(Path(path).name)
        return [TranscriptSegment(1, 0, 1000, "这个会考。")], 1000

    monkeypatch.setattr(video_pipeline, "_transcribe_video_with_fallback", transcribe)
    judge = ScriptedJudge(lambda _calls, chunk: selection_for(chunk, "新考点"))
    judge.close = lambda: None
    monkeypatch.setattr(api_main, "create_judge", lambda *_args, **_kwargs: judge)
    application = api_main.create_app(store)
    with TestClient(application) as client:
        yield SimpleNamespace(
            store=store, client=client, root=tmp_path, asr=asr_calls,
            application=application, judge=judge,
        )


def folder(batch, name="course", count=2):
    root = batch.root / name
    root.mkdir(parents=True)
    for index in range(count):
        (root / f"lesson-{index}.mp4").write_bytes(f"{name}-{index}".encode())
    return root


def wait(batch, job_id):
    barrier = batch.application.state.jobs.executor.submit(lambda: None)
    try:
        barrier.result(timeout=60)
    except TimeoutError:
        barrier.cancel()
        faulthandler.dump_traceback()
        row = batch.store.get_job(job_id)
        detail = {key: row[key] for key in ("status", "stage", "message", "error_message")} if row else None
        pytest.fail(f"Batch worker did not settle within 60s; job={job_id}; last_state={detail}")
    return batch.client.get(f"/api/jobs/{job_id}").json()


def cancel_after_publish(batch, ready, job_box):
    """Wrap the durable publish so the job is cancelled once it has committed."""
    publish = batch.store.publish_semantic_markers

    def commit_then_cancel(*args, **kwargs):
        assert ready.wait(10), "API did not return the batch job id in time"
        result = publish(*args, **kwargs)
        batch.application.state.jobs.cancel(job_box["job_id"])
        return result

    return commit_then_cancel


def start_and_unpark(batch, body):
    response = batch.client.post("/api/batch/process", json=body)
    assert response.status_code == 202, response.text
    return response.json()["job_id"]


def test_cancel_after_last_publish_keeps_completed_batch(batch, monkeypatch):
    root = folder(batch, count=1)
    ready, job_box = Event(), {}
    monkeypatch.setattr(
        batch.store, "publish_semantic_markers", cancel_after_publish(batch, ready, job_box),
    )
    job_box["job_id"] = start_and_unpark(batch, {"folders": [str(root)]})
    ready.set()
    job = wait(batch, job_box["job_id"])
    video, = batch.store.list_videos()
    assert job["status"] == job["stage"] == "completed", job
    assert job["progress"] == 1.0
    assert "批处理结束" in job["message"]
    assert job["batch_summary"]["completed"] == 1
    assert job["batch_summary"]["retryable"] == 0
    assert batch.judge.calls == 1
    assert marker_titles(batch.store, video["id"], "medium_recall") == ["新考点"]
    assert batch.store.get_analysis_run(video["id"], "medium_recall")["status"] == "completed"
    assert batch.store.get_video(video["id"])["status"] == "review_ready"


def test_cancel_between_videos_interrupts_and_keeps_published_result(batch, monkeypatch):
    root = folder(batch, count=2)
    ready, job_box = Event(), {}
    monkeypatch.setattr(
        batch.store, "publish_semantic_markers", cancel_after_publish(batch, ready, job_box),
    )
    job_box["job_id"] = start_and_unpark(batch, {"folders": [str(root)]})
    ready.set()
    job = wait(batch, job_box["job_id"])
    assert job["status"] == "interrupted", job
    assert job["batch_summary"]["completed"] == 1
    assert job["batch_summary"]["retryable"] == 1
    assert len(batch.asr) == 1  # the second video never started
    done, pending = job["batch_entries"]
    assert done["status"] == "completed" and pending["status"] == "pending_asr"
    assert marker_titles(batch.store, done["video_id"], "medium_recall") == ["新考点"]
    assert batch.store.get_analysis_run(done["video_id"], "medium_recall")["status"] == "completed"
    assert batch.store.get_video(pending["video_id"])["status"] == "uploaded"


def test_cancel_between_modes_of_one_video_interrupts_and_keeps_first_mode(batch, monkeypatch):
    root = folder(batch, count=1)
    ready, job_box = Event(), {}
    monkeypatch.setattr(
        batch.store, "publish_semantic_markers", cancel_after_publish(batch, ready, job_box),
    )
    job_box["job_id"] = start_and_unpark(batch, {
        "folders": [str(root)], "analysis_modes": ["medium_recall", "high_recall"],
    })
    ready.set()
    job = wait(batch, job_box["job_id"])
    assert job["status"] == "interrupted", job
    video_id = job["batch_entries"][0]["video_id"]
    assert marker_titles(batch.store, video_id, "medium_recall") == ["新考点"]
    assert batch.store.get_analysis_run(video_id, "medium_recall")["status"] == "completed"
    assert batch.store.get_analysis_run(video_id, "high_recall") is None
    assert len(batch.asr) == 1
    retry = batch.client.post(f'/api/jobs/{job["id"]}/retry')
    assert retry.status_code == 202, retry.text
    finished = wait(batch, retry.json()["id"])
    assert finished["status"] == "completed", finished
    assert finished["batch_summary"]["completed"] == 1
    assert len(batch.asr) == 1  # subtitles reused; only the missing mode was screened
    assert marker_titles(batch.store, video_id, "high_recall") == ["新考点"]


def test_tail_failure_is_not_masked_by_completed_entries(batch, monkeypatch):
    import classcue.web_batch as web_batch

    def broken_summary(state, store=None):
        raise RuntimeError("fixture tail failure")

    monkeypatch.setattr(web_batch, "batch_summary", broken_summary)
    root = folder(batch, count=1)
    job_id = start_and_unpark(batch, {"folders": [str(root)]})
    job = wait(batch, job_id)
    video, = batch.store.list_videos()
    assert job["status"] == "failed", job
    assert "fixture tail failure" in job["error_message"]
    assert marker_titles(batch.store, video["id"], "medium_recall") == ["新考点"]


def test_awaiting_api_tail_cancel_still_interrupts(tmp_path):
    video_path = tmp_path / "lesson.mp4"
    video_path.write_bytes(b"placeholder")
    store = VideoStore(tmp_path / "workspace" / "project.db")
    video_id = store.create_video("hash", "lesson.mp4", str(video_path), "lesson.wav", 3000)
    store.mark_transcribed(video_id, [TranscriptSegment(1, 0, 3000, "这个会考。")])
    options = api_main.VideoProcessPayload(asr_model_id="whisper-turbo")
    state = make_batch({"folders": []}, options.model_dump(), registered=[
        {"video_id": video_id, "source_path": "", "source_root": "", "status": "pending_ai", "error": ""},
    ])

    def judge_factory(_mode):
        raise ApiUnavailable("fixture offline")

    def update(_progress, _message, stage=None):
        if stage == "awaiting_api":
            # JobManager.update checkpoints before writing; a set cancel raises here.
            raise ProcessingCancelled("用户已请求停止处理")

    with pytest.raises(ProcessingCancelled):
        run_web_batch(
            store, state, options, save=lambda: None, update=update,
            should_cancel=lambda: False,
            import_video=lambda *_args, **_kwargs: pytest.fail("No import"),
            judge_factory=judge_factory,
        )
