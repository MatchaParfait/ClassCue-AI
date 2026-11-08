"""Synthetic course-tool regression; no real services or user library."""

import json
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
import classcue.course_tools as tools
import classcue.video_pipeline as pipeline
from classcue.core import TranscriptSegment
from classcue.course_connection import connection_token
from classcue.course_tools import CourseTools, SCHEMAS
from classcue.study_courses import StudyCourseStore
from classcue.video_store import VideoStore


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(api_main, "DIRECTORY_PROJECT_ROOT", tmp_path / "app")
    store = VideoStore(tmp_path / "library" / "project.db")
    app = api_main.create_app(store)
    with TestClient(app) as client:
        service = CourseTools(app.state.jobs.provider, app.state.jobs, ensure_changes=lambda: None,
                              origin="http://127.0.0.1:9999")
        yield SimpleNamespace(store=store, client=client, app=app, service=service, root=tmp_path)


def video(f, name, folder=None, text=None):
    path = f.root / f"{name}.mp4"
    path.write_bytes(name.encode())
    identity = f.store.create_video(name, path.name, str(path), str(f.root / f"{name}.wav"), 120000)
    if folder is not None:
        f.store.set_video_folder(identity, folder)
    if text:
        f.store.mark_transcribed(identity, [TranscriptSegment(1, 10000, 20000, text)])
    return identity


def call(f, name, **args):
    return f.service.call(name, args)


def settle(f, identity):
    f.app.state.jobs.executor.submit(lambda: None).result(timeout=30)
    return call(f, "get_job", job_id=identity)


def fake_asr(monkeypatch, callback=None):
    monkeypatch.setattr(tools, "require_prepared_asr_model", lambda _: None)
    calls = []
    def run(path, *_args, **kwargs):
        calls.append((path.name, kwargs["asr_model_id"]))
        if callback:
            callback(kwargs)
        return [TranscriptSegment(1, 1000, 2000, "第三范式消除传递依赖")], 2000
    monkeypatch.setattr(pipeline, "_transcribe_video_with_fallback", run)
    return calls


def test_folder_course_root_and_search_share_recursive_scope(fixture):
    f = fixture
    parent = f.store.create_folder("数据库")
    child = f.store.create_folder("第二章", parent)
    binding = StudyCourseStore(f.store).create(folder_id=parent)
    one = video(f, "第一课", parent, "第三范式")
    two = video(f, "第二课", child, "第三范式函数依赖")
    outside = video(f, "库外课", text="第三范式")
    courses = call(f, "list_courses", query="数据库")["items"]
    assert len(courses) == 2 and courses[0]["course_id"] == binding["id"]
    for scope in [binding["id"], f"folder:{parent}"]:
        listed = call(f, "list_videos", course_id=scope)
        found = call(f, "search_subtitles", course_id=scope, query="范式")
        assert {v["video_id"] for v in listed["items"]} == {one, two}
        assert {v["video_id"] for v in found["items"]} == {one, two}
        assert found["coverage"]["with_subtitles"] == 2
    root = call(f, "search_subtitles", course_id="root", query="范式")
    assert [i["video_id"] for i in root["items"]] == [outside]
    assert call(f, "search_subtitles", query="不存在")["total"] == 0
    assert call(f, "list_videos", subtitle_status="missing")["items"] == []


def test_read_bounded_continuation_and_changed_subtitles(fixture):
    f = fixture
    identity = video(f, "长字幕")
    segments = [TranscriptSegment(i + 1, i * 100, (i + 1) * 100, "中" * 200) for i in range(200)]
    f.store.mark_transcribed(identity, segments)
    first = call(f, "read_transcript", video_id=identity, start_seconds=0, end_seconds=100)
    assert first["has_more"] and len(first["items"]) == 60
    second = call(f, "read_transcript", video_id=identity, start_seconds=0, end_seconds=100,
                  offset=first["next_offset"], transcript_version=first["transcript_version"])
    assert second["items"][0]["segment_id"] == 61
    f.store.mark_transcribed(identity, [TranscriptSegment(1, 0, 1000, "变化")])
    with pytest.raises(ValueError, match="字幕已变化"):
        call(f, "read_transcript", video_id=identity, start_seconds=0, end_seconds=100,
             offset=60, transcript_version=first["transcript_version"])


def test_root_search_keeps_match_in_long_snippet_and_paginates(fixture):
    f = fixture
    for index in range(3):
        video(f, f"根课{index}", text="前" * 2000 + "规范化" + "后" * 500)
    first = call(f, "search_subtitles", course_id="root", query="规范化", limit=2)
    assert first["total"] == 3 and len(first["items"]) == 2 and first["has_more"]
    assert all("规范化" in hit["text"] and len(hit["text"]) <= 202 for hit in first["items"])
    last = call(f, "search_subtitles", course_id="root", query="规范化", limit=2,
                offset=first["next_offset"])
    assert len(last["items"]) == 1 and not last["has_more"]


def test_resource_exhaustion_reason_is_not_lost_as_user_cancellation(fixture, monkeypatch):
    f = fixture
    identity = video(f, "资源不足")
    def fail(_):
        raise tools.ResourcePaused("synthetic CUDA OOM")
    fake_asr(monkeypatch, fail)
    result = call(f, "start_transcription", video_ids=[identity])
    job = settle(f, result["job_id"])
    assert job["status"] == "interrupted"
    assert "synthetic CUDA OOM" in job["error_message"]
    assert "释放资源" in job["message"] and job["summary"]["pending"] == 1


def test_transcribe_skips_saved_and_never_calls_analysis(fixture, monkeypatch):
    f = fixture
    one, two = video(f, "已转写", text="原文"), video(f, "待转写")
    calls = fake_asr(monkeypatch)
    monkeypatch.setattr(api_main, "create_judge", lambda *_a, **_k: pytest.fail("No AI"))
    result = call(f, "start_transcription", video_ids=[one, two])
    job = settle(f, result["job_id"])
    assert job["status"] == "completed"
    assert job["summary"] == {"completed": 1, "skipped": 1, "failed": 0, "pending": 0}
    assert len(calls) == 1 and f.store.get_segments(one)[0].text == "原文"
    assert f.store.list_markers(two) == []
    assert call(f, "start_transcription", video_ids=[one, two])["job_id"] is None


def test_course_task_is_visible_with_entries_and_model_in_desktop_api(fixture, monkeypatch):
    f = fixture
    one, two = video(f, "已转写", text="原文"), video(f, "待转写")
    fake_asr(monkeypatch)
    job_id = call(f, "start_transcription", video_ids=[one, two])["job_id"]
    finished = settle(f, job_id)
    desktop = f.client.get(f"/api/jobs/{job_id}").json()
    recent = f.client.get("/api/jobs/recent").json()[0]
    assert desktop["model_id"] == finished["model_id"]
    assert desktop["job_type"] == recent["job_type"] == "course_transcribe"
    assert desktop["batch_summary"] == {
        "total": 2, "completed": 1, "skipped": 1, "failed": 0, "awaiting_ai": 0, "pending_asr": 0,
    }
    assert {entry["label"] for entry in desktop["batch_entries"]} == {"已转写", "待转写"}
    assert recent["batch_summary"] == desktop["batch_summary"]


def test_empty_transcription_is_failed_and_later_video_still_completes(fixture, monkeypatch):
    f = fixture
    silent, spoken = video(f, "无语音"), video(f, "有语音")
    fake_asr(monkeypatch)
    def run(path, *_args, **_kwargs):
        if path.stem == "无语音":
            return [], 0
        return [TranscriptSegment(1, 1000, 2000, "有效语音")], 2000
    monkeypatch.setattr(pipeline, "_transcribe_video_with_fallback", run)
    job_id = call(f, "start_transcription", video_ids=[silent, spoken])["job_id"]
    job = settle(f, job_id)
    assert job["status"] == "failed"
    assert job["summary"] == {"completed": 1, "skipped": 0, "failed": 1, "pending": 0}
    assert job["failures"][0]["video_id"] == silent
    assert "未识别到可保存的字幕" in job["failures"][0]["error"]
    assert not f.store.has_transcript(silent)
    assert f.store.get_video(silent)["status"] == "failed"
    assert f.store.get_segments(spoken)[0].text == "有效语音"


def test_duplicate_start_cancel_and_file_lock(fixture, monkeypatch):
    f = fixture
    one, two = video(f, "甲"), video(f, "乙")
    entered, release = Event(), Event()
    def block(kwargs):
        entered.set()
        release.wait(10)
        if kwargs["should_stop"]():
            from classcue.processing_control import ProcessingCancelled
            raise ProcessingCancelled("stop")
    fake_asr(monkeypatch, block)
    try:
        first = call(f, "start_transcription", video_ids=[one, two])
        assert entered.wait(5)
        again = call(f, "start_transcription", video_ids=[two, one])
        assert again["job_id"] == first["job_id"] and again["reused"]
        assert f.client.patch(f"/api/videos/{two}/filename", json={"filename": "changed.mp4"}).status_code == 409
        call(f, "cancel_job", job_id=first["job_id"])
    finally:
        release.set()
    assert settle(f, first["job_id"])["status"] == "interrupted"
    assert not f.store.has_transcript(one)


def test_selection_frozen_and_failure_is_visible(fixture, monkeypatch):
    f = fixture
    import classcue.asr_models as models
    monkeypatch.setattr(models, "SETTINGS_PATH", f.root / "selection.json")
    models.SETTINGS_PATH.write_text('{"model_id":"dolphin-small-cn","dolphin_device":"cpu"}')
    one, two = video(f, "甲"), video(f, "乙")
    devices = []
    def callback(_):
        devices.append(models.selected_dolphin_device())
        models.SETTINGS_PATH.write_text('{"model_id":"whisper-turbo","dolphin_device":"gpu"}')
    fake_asr(monkeypatch, callback)
    job = settle(f, call(f, "start_transcription", video_ids=[one, two])["job_id"])
    assert devices == ["cpu", "cpu"] and job["model_id"] == "dolphin-small-cn"
    assert models.selected_model_id() == "whisper-turbo"


def test_rejects_missing_media_unready_model_unknown_scope_and_other_jobs(fixture, monkeypatch):
    f = fixture
    identity = video(f, "目标")
    with pytest.raises(KeyError):
        call(f, "list_videos", course_id="folder:999")
    monkeypatch.setattr(tools, "require_prepared_asr_model", lambda _: (_ for _ in ()).throw(RuntimeError("未就绪")))
    with pytest.raises(RuntimeError, match="未就绪"):
        call(f, "start_transcription", video_ids=[identity])
    assert f.store.list_active_jobs() == []
    from pathlib import Path
    Path(f.store.get_video(identity)["video_path"]).unlink()
    with pytest.raises(ValueError, match="文件"):
        call(f, "start_transcription", video_ids=[identity])
    f.store.create_job("other", "video_process", identity)
    with pytest.raises(KeyError):
        call(f, "cancel_job", job_id="other")


def test_http_authorization_library_and_validation(fixture):
    f = fixture
    identity = video(f, "课", text="原文")
    payload = {"library_id": f.app.state.jobs.provider.library_id(), "arguments": {}}
    assert f.client.post("/api/course-tools/list_courses", json=payload).status_code == 403
    headers = {"Authorization": f"Bearer {connection_token()}"}
    assert f.client.post("/api/course-tools/list_courses", json=payload, headers=headers).status_code == 200
    invalid = {**payload, "library_id": "wrong"}
    assert f.client.post("/api/course-tools/list_courses", json=invalid, headers=headers).status_code == 409
    invalid_limit = f.client.post("/api/course-tools/search_subtitles",
                                 json={**payload, "arguments": {"query": "原文", "limit": 100}}, headers=headers)
    assert invalid_limit.status_code == 422
    assert "limit" in invalid_limit.json()["detail"] and "50" in invalid_limit.json()["detail"]
    for args in [{"video_ids": [True]}, {"video_ids": ["1"]}, {"course_id": "root", "video_ids": [identity]}]:
        response = f.client.post("/api/course-tools/start_transcription", json={**payload, "arguments": args},
                                 headers=headers)
        assert response.status_code == 422
    assert call(f, "open_video", video_id=identity, start_seconds=10)["navigation"] == "requested"


def test_interrupted_persistent_state_survives_reopen(fixture):
    f = fixture
    identity = video(f, "课")
    f.store.create_job("restart", tools.JOB_TYPE)
    f.store.update_job("restart", status="running", request_json=json.dumps({
        "entries": [{"video_id": identity, "status": "running", "error": ""}]}))
    f.store.set_status(identity, "processing")
    reopened = VideoStore(f.store.path)
    reopened.mark_jobs_interrupted()
    result = call(f, "get_job", job_id="restart")
    assert result["status"] == "interrupted" and result["current_video_id"] is None
    assert result["summary"]["pending"] == 1
    assert json.loads(reopened.get_job("restart")["request_json"])["entries"][0]["status"] == "pending"
    assert f.client.get("/api/jobs/restart").json()["batch_entries"][0]["status"] == "pending"
    assert f.store.get_video(identity)["status"] == "failed"


def test_legacy_terminal_job_does_not_show_video_as_still_running(fixture):
    f = fixture
    identity = video(f, "旧中断任务")
    f.store.create_job("old_restart", tools.JOB_TYPE)
    f.store.update_job("old_restart", status="interrupted", request_json=json.dumps({
        "entries": [{"video_id": identity, "status": "running", "error": ""}]}))
    assert call(f, "get_job", job_id="old_restart")["current_video_id"] is None
    assert f.client.get("/api/jobs/old_restart").json()["batch_entries"][0]["status"] == "pending"


@pytest.mark.parametrize("tool,args", [
    ("search_subtitles", {"query": "  "}),
    ("read_transcript", {"video_id": 1, "start_seconds": 10, "end_seconds": 5}),
    ("read_transcript", {"video_id": 1, "start_seconds": float("inf"), "end_seconds": 5}),
    ("read_transcript", {"video_id": 1, "start_seconds": 0, "end_seconds": 5, "offset": 1}),
    ("list_courses", {"limit": True}),
])
def test_invalid_arguments(fixture, tool, args):
    with pytest.raises(ValueError):
        call(fixture, tool, **args)
