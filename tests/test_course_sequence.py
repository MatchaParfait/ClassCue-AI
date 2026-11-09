"""Shared preferences and sequential local processing, using synthetic media."""

import hashlib
import json
from threading import Event

import pytest

import api.main as api_main
import classcue.course_tools as tools
from classcue.core import TranscriptSegment
from classcue.processing_control import ResourcePaused
from classcue.video_store import VideoStore
from tests.test_course_batch import invoke
from tests.test_web_batch import batch, register_library_videos, wait


def save_selection(batch, modes, revision=0):
    return batch.client.put("/api/settings/recall-selection", json={
        "library_id": batch.application.state.jobs.provider.library_id(),
        "analysis_modes": modes, "expected_revision": revision,
    })


def test_preferences_persist_per_library_and_reject_stale_writes(batch):
    initial = batch.client.get("/api/settings/recall-selection").json()
    assert initial["analysis_modes"] == ["medium_recall"] and initial["revision"] == 0
    assert not initial["initialized"]
    saved = save_selection(batch, ["high_recall", "question_extraction"])
    assert saved.status_code == 200
    assert VideoStore(batch.store.path).get_recall_selection()["analysis_modes"] == [
        "high_recall", "question_extraction"]
    assert save_selection(batch, ["low_recall"]).status_code == 409
    other = VideoStore(batch.root / "other" / "project.db")
    assert not other.get_recall_selection()["initialized"]
    payload = {**saved.json(), "library_id": "different", "expected_revision": 1}
    payload.pop("initialized")
    payload.pop("revision")
    assert batch.client.put("/api/settings/recall-selection", json=payload).status_code == 409
    assert save_selection(batch, ["unknown"], 1).status_code == 422
    assert save_selection(batch, ["strict_exam_intent"], 1).status_code == 422
    assert save_selection(batch, ["custom_" + "a" * 32], 1).status_code == 409


def test_batch_inherits_saved_modes_and_explicit_override_does_not_save(batch):
    ids = register_library_videos(batch, count=1)
    assert save_selection(batch, ["high_recall", "question_extraction"]).status_code == 200
    preview = invoke(batch, "preview_batch", video_ids=ids)
    assert preview["analysis_modes_source"] == "app_saved"
    assert preview["options"]["analysis_modes"] == ["high_recall", "question_extraction"]
    assert preview["recall_selection_revision"] == 1
    start = invoke(batch, "start_batch_processing", video_ids=ids, expected_recall_revision=1,
                   request_id="saved-mode-sequence-001")
    assert wait(batch, start["job_id"])["status"] == "completed"
    assert batch.modes == ["high_recall", "question_extraction"]
    override = invoke(batch, "preview_batch", video_ids=ids, options={"analysis_modes": ["low_recall"]})
    assert override["analysis_modes_source"] == "explicit"
    assert batch.store.get_recall_selection()["analysis_modes"] == ["high_recall", "question_extraction"]
    save_selection(batch, ["low_recall"], 1)
    replay = invoke(batch, "start_batch_processing", video_ids=ids, expected_recall_revision=1,
                    request_id="saved-mode-sequence-001")
    assert replay["reused"] and replay["job_id"] == start["job_id"]
    with pytest.raises(ValueError, match="已变化"):
        tools.CourseTools(batch.application.state.jobs.provider, batch.application.state.jobs,
                          origin="http://localhost", ensure_changes=lambda: None,
                          batch=batch.application.state.course_batch_tools).call(
                              "start_batch_processing", {"video_ids": ids, "expected_recall_revision": 1,
                                                        "request_id": "stale-preview-001"})


def test_empty_saved_selection_never_falls_back_to_medium(batch):
    ids = register_library_videos(batch, count=1)
    assert save_selection(batch, []).status_code == 200
    response = batch.client.get("/api/settings/recall-selection").json()
    assert response["analysis_modes"] == []
    from classcue.course_connection import connection_token
    failed = batch.client.post("/api/course-tools/preview_batch", headers={
        "Authorization": f"Bearer {connection_token()}"}, json={
        "library_id": response["library_id"], "arguments": {"video_ids": ids}})
    assert failed.status_code == 409
    assert invoke(batch, "preview_batch", video_ids=ids,
                  options={"analysis_modes": ["low_recall"]})["valid"]
    assert batch.store.latest_batch_job() is None


def test_saved_custom_scheme_is_validated_again_when_archived(batch):
    ids = register_library_videos(batch, count=1)
    scheme = batch.store.save_recall_scheme({
        "name": "synthetic scheme", "base_mode": "medium_recall", "rules": [], "notes": "",
    })
    assert save_selection(batch, [scheme["id"]]).status_code == 200
    assert invoke(batch, "preview_batch", video_ids=ids)["options"]["analysis_modes"] == [scheme["id"]]
    batch.store.archive_recall_scheme(scheme["id"])
    service = tools.CourseTools(batch.application.state.jobs.provider, batch.application.state.jobs,
                                origin="http://localhost", ensure_changes=lambda: None,
                                batch=batch.application.state.course_batch_tools)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:
        service.call("preview_batch", {"video_ids": ids})
    assert error.value.status_code == 400 and "停用" in error.value.detail


def test_legacy_idempotency_marker_replays_after_preferences_upgrade(batch):
    ids = register_library_videos(batch, count=1)
    request = {"video_ids": ids, "request_id": "old-version-request"}
    first = invoke(batch, "start_batch_processing", **request)
    wait(batch, first["job_id"])
    old_body = tools.BatchStart.model_validate(request).model_dump(exclude={"request_id", "expected_recall_revision"})
    state = json.loads(batch.store.get_job(first["job_id"])["request_json"])
    state["mcp_request"] = {
        "action": "start_batch_processing", "request_id": request["request_id"],
        "digest": hashlib.sha256(json.dumps(old_body, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
    }
    batch.store.update_job(first["job_id"], request_json=json.dumps(state))
    save_selection(batch, ["low_recall"])
    replay = invoke(batch, "start_batch_processing", **request)
    assert replay["reused"] and replay["job_id"] == first["job_id"]


@pytest.fixture
def sequence(batch, monkeypatch):
    monkeypatch.setattr(tools, "require_prepared_asr_model", lambda _: None)
    monkeypatch.setattr(tools, "selection_snapshot", lambda: {
        "model_id": "whisper-turbo", "firered_device": "gpu", "paraformer_device": "cpu",
        "nano_device": "cpu", "dolphin_device": "cpu"})
    slides = []
    def extract(store, video_id, _root, **kwargs):
        assert store.has_transcript(video_id)
        slides.append(video_id)
        kwargs["progress"](0.5, "synthetic slides", "detecting")
        return {"status": "completed", "count": 3}
    monkeypatch.setattr(api_main, "extract_and_store_slides", extract)
    batch.slides = slides
    return batch


def test_sequence_runs_after_transcription_without_ai_and_replays(sequence):
    b = sequence
    ids = register_library_videos(b, count=2)
    args = {"video_ids": ids, "request_id": "local-sequence-001"}
    result = invoke(b, "start_processing_sequence", **args)
    assert wait(b, result["job_id"])["status"] == "completed"
    assert len(b.asr) == 2 and b.slides == ids and b.ai == []
    state = invoke(b, "get_job", job_id=result["job_id"], limit=1)
    assert state["job_type"] == "course_sequence" and state["has_more"]
    assert [s["status"] for s in state["entries"][0]["steps"]] == ["completed", "completed"]
    assert invoke(b, "start_processing_sequence", **args)["reused"]
    assert len(b.asr) == 2
    second = invoke(b, "start_processing_sequence", video_ids=ids, request_id="local-sequence-002")
    wait(b, second["job_id"])
    assert len(b.asr) == 2
    assert invoke(b, "get_job", job_id=second["job_id"])["entries"][0]["steps"][0]["status"] == "skipped"


@pytest.mark.parametrize("error", [ValueError("synthetic ASR error"), ResourcePaused("synthetic memory")])
def test_asr_failure_stops_all_later_steps_and_videos(sequence, monkeypatch, error):
    b = sequence
    ids = register_library_videos(b, count=2)
    def fail(*_args, **_kwargs):
        raise error
    monkeypatch.setattr(tools.CourseTools, "_transcribe_video", fail)
    result = invoke(b, "start_processing_sequence", video_ids=ids, request_id="sequence-failure-001")
    assert wait(b, result["job_id"])["status"] in {"failed", "interrupted"}
    assert b.slides == [] and b.ai == []
    state = json.loads(b.store.get_job(result["job_id"])["request_json"])
    assert state["entries"][0]["steps"][1]["status"] == "pending"
    assert state["entries"][1]["status"] == "pending"


def test_ppt_failure_preserves_saved_transcript_for_next_submission(sequence, monkeypatch):
    b = sequence
    ids = register_library_videos(b, count=1)
    original = b.application.state.course_batch_tools["slides"]
    def fail(*_args):
        raise ValueError("synthetic slides error")
    b.application.state.course_batch_tools["slides"] = fail
    first = invoke(b, "start_processing_sequence", video_ids=ids, request_id="ppt-failure-001")
    assert wait(b, first["job_id"])["status"] == "failed"
    assert b.store.has_transcript(ids[0])
    b.application.state.course_batch_tools["slides"] = original
    second = invoke(b, "start_processing_sequence", video_ids=ids, request_id="ppt-recovery-001")
    assert wait(b, second["job_id"])["status"] == "completed"
    assert len(b.asr) == 1


def test_cancel_between_steps_does_not_extract_ppt(sequence, monkeypatch):
    b = sequence
    ids = register_library_videos(b, count=1)
    entered, release = Event(), Event()
    original = tools.CourseTools._transcribe_video
    def transcribe(self, *args):
        original(self, *args)
        entered.set()
        assert release.wait(10)
    monkeypatch.setattr(tools.CourseTools, "_transcribe_video", transcribe)
    try:
        started = invoke(b, "start_processing_sequence", video_ids=ids, request_id="sequence-cancel-001")
        assert entered.wait(5)
        response = b.client.patch(f"/api/videos/{ids[0]}/filename", json={"filename": "changed.mp4"})
        assert response.status_code == 409
        invoke(b, "cancel_job", job_id=started["job_id"])
    finally:
        release.set()
    assert wait(b, started["job_id"])["status"] == "interrupted"
    assert b.store.has_transcript(ids[0]) and b.slides == []
    assert invoke(b, "get_job", job_id=started["job_id"])["current_video_id"] is None


def test_sequence_restart_preserves_completed_step_and_resets_running(sequence):
    b = sequence
    ids = register_library_videos(b, count=1)
    b.store.create_job("old_sequence", "course_sequence")
    b.store.update_job("old_sequence", status="running", request_json=json.dumps({
        "entries": [{"video_id": ids[0], "status": "running", "steps": [
            {"name": "transcribe", "status": "completed"}, {"name": "extract_slides", "status": "running"}]}]}))
    b.store.mark_jobs_interrupted()
    old = json.loads(b.store.get_job("old_sequence")["request_json"])
    assert old["entries"][0]["steps"] == [
        {"name": "transcribe", "status": "completed"}, {"name": "extract_slides", "status": "pending"}]


def test_slides_only_does_not_load_asr_and_respects_explicit_step_order(sequence, monkeypatch):
    b = sequence
    ids = register_library_videos(b, count=1)
    def no_asr():
        pytest.fail("slides-only must not inspect ASR selection")
    monkeypatch.setattr(tools, "selection_snapshot", no_asr)
    order = []
    b.application.state.course_batch_tools["slides"] = lambda *_: order.append("slides") or {"count": 2}
    first = invoke(b, "start_processing_sequence", video_ids=ids, steps=["extract_slides"],
                   request_id="slides-only-request")
    assert wait(b, first["job_id"])["status"] == "completed"
    assert b.asr == b.ai == [] and order == ["slides"]
    result = invoke(b, "get_job", job_id=first["job_id"])
    assert result["selection"] is None


def test_sequence_reports_reused_ppt_cache(sequence):
    b = sequence
    ids = register_library_videos(b, count=1)
    b.application.state.course_batch_tools["slides"] = lambda *_: {"status": "cached", "count": 4}
    first = invoke(b, "start_processing_sequence", video_ids=ids, steps=["extract_slides"],
                   request_id="slides-cache-request")
    assert wait(b, first["job_id"])["status"] == "completed"
    step = invoke(b, "get_job", job_id=first["job_id"])["entries"][0]["steps"][0]
    assert step == {"name": "extract_slides", "status": "skipped", "count": 4}


@pytest.mark.parametrize("arguments", [
    {"video_ids": [1], "steps": []},
    {"video_ids": [1], "steps": ["transcribe", "transcribe"]},
    {"video_ids": [1], "steps": ["analyze"]},
    {"video_ids": [1], "course_id": "root"},
])
def test_sequence_rejects_invalid_targets_and_steps(sequence, arguments):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        tools.SequenceStart.model_validate({**arguments, "request_id": "invalid-sequence"})
