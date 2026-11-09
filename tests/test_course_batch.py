"""MCP batch operations execute the existing batch engine with synthetic ASR/AI."""

import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from threading import Event
from threading import Thread

import pytest

import classcue.video_pipeline as pipeline
from classcue.api_failure import ApiUnavailable
from classcue.course_connection import connection_token
from classcue.course_tools import CourseTools
from classcue.processing_control import ProcessingCancelled, ResourcePaused
from tests.test_web_batch import batch, folder, register_library_videos, wait


def invoke(batch, name, **arguments):
    response = batch.client.post(
        f"/api/course-tools/{name}",
        headers={"Authorization": f"Bearer {connection_token()}"},
        json={"library_id": batch.application.state.jobs.provider.library_id(), "arguments": arguments},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_preview_is_read_only_recursive_and_discloses_existing_effects(batch):
    ids = register_library_videos(batch, count=3)
    parent = batch.store.create_folder("course")
    child = batch.store.create_folder("chapter", parent)
    batch.store.set_video_folder(ids[0], parent)
    batch.store.set_video_folder(ids[1], child)
    preview = invoke(batch, "preview_batch", course_id=f"folder:{parent}")
    assert preview["valid"] and {item["video_id"] for item in preview["videos"]} == set(ids[:2])
    assert preview["effects"]["calls_ai_service"] and preview["effects"]["may_prepare_asr"]
    assert preview["options"]["analysis_modes"] == ["medium_recall"]
    assert preview["options"]["asr_model_id"] == "whisper-turbo"
    assert batch.store.latest_batch_job() is None
    assert batch.asr == batch.ai == []
    root_preview = invoke(batch, "preview_batch", course_id="root")
    assert [item["video_id"] for item in root_preview["videos"]] == [ids[2]]
    source = folder(batch, "to-import", 1)
    imported = invoke(batch, "preview_batch", folders=[str(source)], import_only=True)
    assert imported["import_count"] == 1 and imported["effects"]["moves_imported_sources"]
    assert not imported["effects"]["calls_ai_service"]
    assert (source / "lesson-0.mp4").is_file()


def test_mcp_full_batch_runs_existing_engine_and_replays_after_reopen(batch):
    ids = register_library_videos(batch, count=2)
    args = {"video_ids": ids, "options": {"analysis_modes": ["medium_recall", "question_extraction"]},
            "request_id": "run-batch-0001"}
    started = invoke(batch, "start_batch_processing", **args)
    assert started["job_type"] == "batch_video_process"
    assert wait(batch, started["job_id"])["status"] == "completed"
    result = invoke(batch, "get_job", job_id=started["job_id"])
    assert result["summary"]["completed"] == 2
    assert batch.modes == ["medium_recall", "question_extraction"] * 2
    assert len(batch.asr) == 2
    # New service instance, same persistent database; no in-memory deduplication.
    jobs = batch.application.state.jobs
    reopened = CourseTools(jobs.provider, jobs, ensure_changes=lambda: None,
                           origin="http://testserver", batch=batch.application.state.course_batch_tools)
    replay = reopened.call("start_batch_processing", args)
    assert replay["reused"] and replay["job_id"] == started["job_id"]
    assert len(batch.asr) == 2 and len(batch.ai) == 4
    changed = {**args, "options": {"analysis_modes": ["high_recall"]}}
    with pytest.raises(ValueError, match="request_id"):
        reopened.call("start_batch_processing", changed)
    with pytest.raises(ValueError, match="request_id"):
        reopened.call("retry_batch", {"job_id": started["job_id"], "request_id": args["request_id"]})


def test_retry_reuses_transcripts_and_completed_modes_and_is_idempotent(batch):
    ids = register_library_videos(batch, count=2)
    batch.outcomes.extend([None, ApiUnavailable("synthetic unavailable")])
    first = invoke(batch, "start_batch_processing", video_ids=ids,
                   options={"analysis_modes": ["medium_recall", "low_recall"]},
                   request_id="first-batch-0001")
    assert wait(batch, first["job_id"])["status"] == "awaiting_api"
    retry = invoke(batch, "retry_batch", job_id=first["job_id"], request_id="retry-batch-0001")
    assert wait(batch, retry["job_id"])["status"] == "completed"
    assert len(batch.asr) == 2
    assert list(zip(batch.ai, batch.modes)).count((ids[0], "medium_recall")) == 1
    repeated = invoke(batch, "retry_batch", job_id=first["job_id"], request_id="retry-batch-0001")
    assert repeated["reused"] and repeated["job_id"] == retry["job_id"]
    state = json.loads(batch.store.get_job(retry["job_id"])["request_json"])
    assert state["mcp_request"]["request_id"] == "retry-batch-0001"


def test_mcp_can_query_and_retry_existing_ui_batch(batch):
    ids = register_library_videos(batch, count=1)
    batch.outcomes.append(ApiUnavailable("synthetic unavailable"))
    ui = batch.client.post("/api/batch/process", json={"video_ids": ids}).json()
    assert wait(batch, ui["job_id"])["status"] == "awaiting_api"
    assert invoke(batch, "get_job", job_id=ui["job_id"])["next_action"] == "retry_batch"
    retry = invoke(batch, "retry_batch", job_id=ui["job_id"], request_id="retry-ui-batch-001")
    assert wait(batch, retry["job_id"])["status"] == "completed"


def test_resume_resource_paused_batch_uses_existing_resume_and_replays(batch, monkeypatch):
    ids = register_library_videos(batch, count=1)
    original = pipeline._transcribe_video_with_fallback
    def paused(*_args, **_kwargs):
        raise ResourcePaused("synthetic memory pressure")
    monkeypatch.setattr(pipeline, "_transcribe_video_with_fallback", paused)
    first = invoke(batch, "start_batch_processing", video_ids=ids, request_id="paused-batch-001")
    assert wait(batch, first["job_id"])["status"] == "resource_paused"
    monkeypatch.setattr(pipeline, "_transcribe_video_with_fallback", original)
    resumed = invoke(batch, "resume_batch", job_id=first["job_id"], request_id="resume-batch-001")
    assert wait(batch, resumed["job_id"])["status"] == "completed"
    assert batch.store.get_job(first["job_id"])["stage"] == "resumed"
    again = invoke(batch, "resume_batch", job_id=first["job_id"], request_id="resume-batch-001")
    assert again["reused"] and again["job_id"] == resumed["job_id"]


def test_cancel_full_batch_preserves_completed_item_and_duplicate_start(batch, monkeypatch):
    ids = register_library_videos(batch, count=2)
    original = pipeline._transcribe_video_with_fallback
    entered, release = Event(), Event()
    count = 0
    def transcribe(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            entered.set()
            assert release.wait(10)
            if kwargs["should_stop"]():
                raise ProcessingCancelled("synthetic cancel")
        return original(*args, **kwargs)
    monkeypatch.setattr(pipeline, "_transcribe_video_with_fallback", transcribe)
    arguments = {"video_ids": ids, "request_id": "cancel-batch-001"}
    try:
        first = invoke(batch, "start_batch_processing", **arguments)
        assert entered.wait(5)
        again = invoke(batch, "start_batch_processing", **arguments)
        assert again["reused"] and again["job_id"] == first["job_id"]
        invoke(batch, "cancel_job", job_id=first["job_id"])
    finally:
        release.set()
    assert wait(batch, first["job_id"])["status"] == "interrupted"
    assert batch.store.has_transcript(ids[0]) and not batch.store.has_transcript(ids[1])
    assert batch.store.get_analysis_run(ids[0], "medium_recall")["status"] == "completed"
    assert invoke(batch, "cancel_job", job_id=first["job_id"])["status"] == "interrupted"


def test_import_only_uses_existing_move_and_does_not_call_asr_or_ai(batch):
    source = folder(batch, "import-only", 1)
    args = {"folders": [str(source)], "import_only": True, "request_id": "import-batch-001"}
    result = invoke(batch, "start_batch_processing", **args)
    assert wait(batch, result["job_id"])["status"] == "completed"
    assert not (source / "lesson-0.mp4").exists()
    assert len(batch.store.list_videos()) == 1 and batch.asr == batch.ai == []
    assert invoke(batch, "start_batch_processing", **args)["reused"]


@pytest.mark.parametrize("tool,arguments", [
    ("preview_batch", {}),
    ("preview_batch", {"video_ids": [True]}),
    ("preview_batch", {"video_ids": [1], "course_id": "root"}),
    ("preview_batch", {"video_ids": [1], "options": {"force_retranscribe": "false"}}),
    ("preview_batch", {"folders": ["relative/path"]}),
    ("preview_batch", {"video_ids": [1], "import_only": True}),
    ("preview_batch", {"video_ids": [1], "options": {"analysis_modes": []}}),
    ("preview_batch", {"video_ids": [1], "options": {"analysis_modes": ["unknown"]}}),
    ("preview_batch", {"video_ids": [1], "options": {"asr_model_id": ""}}),
    ("preview_batch", {"video_ids": [1], "options": {"api_key": "unexpected"}}),
    ("start_batch_processing", {"video_ids": [1]}),
    ("retry_batch", {"job_id": "unknown", "request_id": "x"}),
])
def test_invalid_mcp_batch_parameters_fail_before_creating_jobs(batch, tool, arguments):
    response = batch.client.post(
        f"/api/course-tools/{tool}", headers={"Authorization": f"Bearer {connection_token()}"},
        json={"library_id": batch.application.state.jobs.provider.library_id(), "arguments": arguments})
    assert response.status_code == 422, response.text
    assert batch.store.latest_batch_job() is None and batch.asr == batch.ai == []


def test_batch_errors_and_job_type_boundaries(batch):
    ids = register_library_videos(batch, count=1)
    jobs = batch.application.state.jobs
    service = CourseTools(jobs.provider, jobs, ensure_changes=lambda: None,
                          origin="http://testserver", batch=batch.application.state.course_batch_tools)
    batch.store.create_job("other", "asr_prepare")
    for name in ["get_job", "cancel_job"]:
        with pytest.raises(KeyError):
            service.call(name, {"job_id": "other"})
    for name in ["retry_batch", "resume_batch"]:
        with pytest.raises(KeyError):
            service.call(name, {"job_id": "other", "request_id": "boundary-0001"})
    response = batch.client.post("/api/course-tools/preview_batch", headers={
        "Authorization": f"Bearer {connection_token()}"},
        json={"library_id": jobs.provider.library_id(),
              "arguments": {"video_ids": ids, "options": {"analysis_modes": ["custom_" + "f" * 32]}}})
    assert response.status_code == 400


def test_batch_query_paginates_and_handles_legacy_empty_request_rows(batch):
    ids = register_library_videos(batch, count=3)
    batch.store.create_job("legacy_empty", "batch_video_process")
    batch.store.update_job("legacy_empty", status="completed")
    result = invoke(batch, "start_batch_processing", video_ids=ids, request_id="paging-batch-001")
    wait(batch, result["job_id"])
    first = invoke(batch, "get_job", job_id=result["job_id"], limit=2)
    assert len(first["entries"]) == 2 and first["has_more"] and first["next_offset"] == 2
    second = invoke(batch, "get_job", job_id=result["job_id"], limit=2, offset=first["next_offset"])
    assert len(second["entries"]) == 1 and not second["has_more"]
    assert {entry["video_id"] for entry in first["entries"] + second["entries"]} == set(ids)
    state = json.loads(batch.store.get_job(result["job_id"])["request_json"])
    state["entries"][0]["error"] = "synthetic long failure " * 1000
    state["api_error"] = "synthetic API failure " * 1000
    batch.store.update_job(result["job_id"], request_json=json.dumps(state))
    bounded = invoke(batch, "get_job", job_id=result["job_id"])
    assert len(bounded["entries"][0]["error"]) == 1000
    assert len(bounded["api_error"]) == 2000


def test_ui_retry_does_not_copy_original_mcp_identity(batch):
    ids = register_library_videos(batch, count=1)
    batch.outcomes.append(ApiUnavailable("synthetic unavailable"))
    first = invoke(batch, "start_batch_processing", video_ids=ids, request_id="ui-retry-start-001")
    wait(batch, first["job_id"])
    ui_retry = batch.client.post(f"/api/jobs/{first['job_id']}/retry")
    assert ui_retry.status_code == 202
    new_id = ui_retry.json()["id"]
    wait(batch, new_id)
    assert "mcp_request" not in json.loads(batch.store.get_job(new_id)["request_json"])
    replay = invoke(batch, "start_batch_processing", video_ids=ids, request_id="ui-retry-start-001")
    assert replay["job_id"] == first["job_id"] and replay["reused"]


def test_real_stdio_mcp_batch_reaches_authenticated_http_and_returns_tool_errors(batch, monkeypatch):
    """Only service discovery is redirected; production MCP, bridge and batch run."""
    import uvicorn
    import api.main as api_main
    from tests.test_course_retrieval import seed_slides

    def synthetic_slides(store, video_id, root, **kwargs):
        seed_slides(store, root, video_id, length_ms=400)
        return {"status": "completed", "count": 2}

    monkeypatch.setattr(api_main, "extract_and_store_slides", synthetic_slides)
    monkeypatch.setattr(api_main, "video_preview_frame", lambda *_a, **_k:
                        api_main.asr_storage.slide_directory(
                            api_main.DIRECTORY_PROJECT_ROOT, batch.application.state.jobs.provider.data_root()) / "synthetic.jpg")

    node = shutil.which("node")
    project = Path(__file__).resolve().parents[1]
    client_script = project / "integrations/codex-desktop/test/batch-protocol-client.mjs"
    if not node or not (client_script.parent.parent / "node_modules").is_dir():
        pytest.skip("Node and installed MCP SDK are required")
    ids = register_library_videos(batch, count=1)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    # TestClient owns startup/teardown; this second HTTP listener shares that app.
    server = uvicorn.Server(uvicorn.Config(batch.application, lifespan="off", log_level="error"))
    thread = Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    fake_install = batch.root / "mcp-test-install"
    (fake_install / "scripts").mkdir(parents=True)
    fixture_file = fake_install / "connection-fixture.json"
    fixture_file.write_text(json.dumps({
        "origin": origin, "library_id": batch.application.state.jobs.provider.library_id(),
        "token": connection_token(),
    }), encoding="utf-8")
    (fake_install / "scripts/codex_course_tools.py").write_text(
        "import json, sys\nfrom pathlib import Path\n"
        f"sys.path.insert(0, {str(project)!r})\n"
        "import classcue.course_connection as connection\n"
        "fixture = json.loads((Path(__file__).resolve().parents[1] / 'connection-fixture.json').read_text(encoding='utf-8'))\n"
        "connection.ensure_service = lambda: (fixture['origin'], fixture['library_id'])\n"
        "connection.connection_token = lambda: fixture['token']\n"
        "from scripts.codex_course_tools import main\nmain()\n", encoding="utf-8")
    connection_file = fake_install / "connection.json"
    connection_file.write_text(json.dumps({
        "format": "classcue-codex-connection", "version": 1,
        "application": {"project_root": str(fake_install), "python": sys.executable},
    }), encoding="utf-8")
    try:
        result = subprocess.run(
            [node, str(client_script), str(connection_file), str(ids[0])],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        evidence = json.loads(result.stdout)
        assert evidence == {
            "version": "0.7.0", "preview": True, "completed": True,
            "reused": True, "validationError": True, "requestConflict": True,
            "sequenceCompleted": True, "sequenceReused": True,
            "retrieval": True, "nativeImages": True, "analysis": True,
            "jobDiscovery": True, "processingStatus": True,
        }
        assert len(batch.asr) == len(batch.ai) == 1
    finally:
        server.should_exit = True
        thread.join(10)
        listener.close()
        assert not thread.is_alive()
