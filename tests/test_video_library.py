from pathlib import Path
from types import SimpleNamespace
import json
from threading import Event

from fastapi.testclient import TestClient
import pytest

import api.main as api_main
from core import TranscriptSegment
import video_library
import video_pipeline
from video_library import register_managed_video, video_filename
from video_store import VideoStore


@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setattr(video_pipeline, "video_duration_ms", lambda _: 1000)
    monkeypatch.setattr(api_main, "DIRECTORY_PROJECT_ROOT", tmp_path / "application")
    store = VideoStore(tmp_path / "workspace" / "project.db")
    source = tmp_path / "downloads" / "lesson.mp4"
    source.parent.mkdir()
    source.write_bytes(b"video fixture")
    root = tmp_path / "workspace" / "videos"
    application = api_main.create_app(store)
    client = TestClient(application)
    yield store, source, root, client
    application.state.jobs.shutdown(wait=True)


def move(library, filename=None):
    store, source, root, _ = library
    return register_managed_video(store, source, root.parent / "temporary_audio", root, filename)


def test_import_moves_verified_bytes_and_serves_library_file(library):
    store, source, root, client = library
    response = client.post("/api/videos/register", json={"path": str(source), "filename": "chapter"})
    assert response.status_code == 200
    video = response.json()["video"]
    destination = Path(video["video_path"])
    assert destination.is_relative_to(root)
    assert destination.name == "chapter.mp4"
    assert destination.read_bytes() == b"video fixture"
    assert not source.exists()
    assert video["display_name"] == "chapter"
    assert video["source_folder"] == str(source.parent)
    assert video["storage_warning"] is None
    assert client.get(f'/api/videos/{video["id"]}/stream').content == b"video fixture"
    assert len(store.list_videos()) == 1


def test_delete_moves_video_to_recycle_bin_and_undo_restores_it(library):
    store, source, root, client = library
    response = client.post("/api/videos/register", json={"path": str(source), "filename": "chapter"})
    video = response.json()["video"]
    managed = Path(video["video_path"])
    assert managed.exists()

    deleted = client.delete(f'/api/videos/{video["id"]}')
    assert deleted.status_code == 200
    assert deleted.json()["message"].startswith("已移入回收站")
    assert not managed.exists()
    assert store.list_videos() == []
    assert source.exists() is False

    restored = client.post("/api/recycle-bin/undo")
    assert restored.status_code == 200
    assert restored.json()["videos"] == 1
    assert managed.exists()
    assert managed.read_bytes() == b"video fixture"
    assert store.get_video(video["id"])["video_path"] == str(managed.resolve())


def test_upload_streams_browser_bytes_into_managed_library_without_deleting_original(library):
    store, source, root, client = library
    response = client.post("/api/videos/upload", params={"filename": "课堂 #1.MP4"}, content=source.read_bytes())
    assert response.status_code == 200
    video = response.json()["video"]
    destination = Path(video["video_path"])
    assert destination.is_relative_to(root)
    assert destination.name == "课堂 #1.MP4"
    assert destination.read_bytes() == source.read_bytes()
    assert not video["source_folder"]
    assert source.exists()
    assert response.json()["existed"] is False
    assert len(store.list_videos()) == 1
    assert client.get(f'/api/videos/{video["id"]}/stream').content == source.read_bytes()
    again = client.post("/api/videos/upload", params={"filename": destination.name}, content=source.read_bytes())
    assert again.status_code == 200
    assert again.json()["existed"] is True
    assert len(store.list_videos()) == 1


def test_upload_preserves_known_original_folder_for_existing_video(library):
    _, source, _, client = library
    content = source.read_bytes()
    original_folder = str(source.parent)
    assert client.post("/api/videos/register", json={"path": str(source)}).status_code == 200
    response = client.post("/api/videos/upload", params={"filename": "lesson.mp4"}, content=content)
    assert response.status_code == 200
    assert response.json()["video"]["source_folder"] == original_folder


@pytest.mark.parametrize("filename", ["notes.txt", "../lesson.mp4", r"..\lesson.mp4", "NUL.mp4"])
def test_upload_rejects_unsupported_formats_and_unsafe_names(library, filename):
    store, source, _, client = library
    response = client.post("/api/videos/upload", params={"filename": filename}, content=b"bytes")
    assert response.status_code == 400
    assert store.list_videos() == []
    assert source.exists()


def test_upload_enforces_size_limit_and_cleans_up_temporary_file(library, monkeypatch):
    store, _, _, client = library
    monkeypatch.setattr(api_main, "MAX_VIDEO_BYTES", 4)
    response = client.post("/api/videos/upload", params={"filename": "large.mp4"}, content=b"12345")
    assert response.status_code == 413
    assert store.list_videos() == []
    assert client.post("/api/videos/upload", params={"filename": "empty.mp4"}, content=b"").status_code == 400


def test_upload_cleans_up_when_registration_fails(library, monkeypatch):
    _, _, _, client = library
    paths = []

    def fail(_store, source, *_args, **_kwargs):
        paths.append(Path(source))
        assert Path(source).read_bytes() == b"fixture"
        raise ValueError("fixture registration failure")

    monkeypatch.setattr(api_main, "register_managed_video", fail)
    response = client.post("/api/videos/upload", params={"filename": "lesson.mp4"}, content=b"fixture")
    assert response.status_code == 400
    assert "fixture registration failure" in response.json()["detail"]
    assert len(paths) == 1
    assert not paths[0].parent.exists()


def set_copy_mode(client, enabled=True):
    data = client.get("/api/settings/directories").json()
    payload = {key: data[key] for key in ("asr_storage_root", "audio_cache_dir", "log_dir", "video_library_dir")}
    payload["delete_source_after_import"] = not enabled
    assert client.put("/api/settings/directories", json=payload).status_code == 200


def test_copy_import_preserves_source_and_library_survives_source_cleanup(library):
    store, source, root, client = library
    set_copy_mode(client)
    response = client.post("/api/videos/register", json={"path": str(source), "filename": "copy"})
    assert response.status_code == 200
    video = response.json()["video"]
    path = Path(video["video_path"])
    assert path.is_relative_to(root)
    assert path.name == "copy.mp4"
    assert source.read_bytes() == path.read_bytes() == b"video fixture"
    assert video["storage_warning"] is None
    assert len(store.list_videos()) == 1
    source.unlink()
    assert client.get(f'/api/videos/{video["id"]}/stream').content == b"video fixture"


def test_rename_in_copy_mode_removes_old_library_filename_but_keeps_original(library):
    _, source, _, client = library
    set_copy_mode(client)
    video = client.post("/api/videos/register", json={"path": str(source)}).json()["video"]
    old_path = Path(video["video_path"])
    response = client.patch(f'/api/videos/{video["id"]}/filename', json={"filename": "renamed"})
    assert response.status_code == 200
    assert Path(response.json()["video_path"]).read_bytes() == b"video fixture"
    assert not old_path.exists()
    assert source.read_bytes() == b"video fixture"


def test_reenabling_move_applies_to_next_import(library):
    _, source, _, client = library
    set_copy_mode(client)
    first = client.post("/api/videos/register", json={"path": str(source)}).json()["video"]
    set_copy_mode(client, enabled=False)
    other = source.with_name("second.mp4")
    other.write_bytes(b"second fixture")
    response = client.post("/api/videos/register", json={"path": str(other)})
    assert response.status_code == 200
    assert not other.exists()
    assert source.read_bytes() == Path(first["video_path"]).read_bytes() == b"video fixture"


def test_rename_keeps_transcript_markers_folder_and_adjusted_time(library):
    store, source, root, client = library
    video_id, path, _, _ = move(library)
    store.mark_transcribed(video_id, [TranscriptSegment(1, 0, 1000, "exam")])
    folder_id = store.create_folder("chapter")
    store.set_video_folder(video_id, folder_id)
    marker_id = store.add_marker(video_id, 500, 900, "exam", "", "", "", .9, "high_recall")
    store.set_marker_jump_time(marker_id, 100)
    response = client.patch(f"/api/videos/{video_id}/filename", json={"filename": "renamed.mp4"})
    assert response.status_code == 200
    renamed = Path(response.json()["video_path"])
    assert renamed.name == "renamed.mp4"
    assert renamed.read_bytes() == b"video fixture"
    assert not path.exists()
    assert not source.exists()
    assert len(store.get_segments(video_id)) == 1
    assert store.list_markers(video_id)[0]["user_start_ms"] == 100
    assert store.get_video(video_id)["library_folder_id"] == folder_id
    assert store.get_video(video_id)["status"] == "transcribed"
    assert response.json()["display_name"] == "renamed"
    assert response.json()["source_folder"] == str(source.parent)
    assert renamed.is_relative_to(root)


def test_reregister_managed_file_is_idempotent(library):
    store, _, _, client = library
    video_id, path, _, _ = move(library)
    response = client.post("/api/videos/register", json={"path": str(path)})
    assert response.status_code == 200
    assert response.json()["existed"]
    assert response.json()["video_id"] == video_id
    assert path.read_bytes() == b"video fixture"
    assert len(store.list_videos()) == 1


def test_different_videos_with_same_filename_do_not_overwrite(library):
    store, source, root, _ = library
    first_id, first, _, _ = move(library)
    source.write_bytes(b"different video")
    second_id, second, _, _ = move(library)
    assert first_id != second_id
    assert first != second
    assert first.name == second.name == "lesson.mp4"
    assert first.read_bytes() == b"video fixture"
    assert second.read_bytes() == b"different video"
    assert len(store.list_videos()) == 2


def test_identical_reimport_reuses_record(library):
    store, source, _, _ = library
    video_id, path, _, _ = move(library)
    source.write_bytes(b"video fixture")
    second_id, second, existed, _ = move(library)
    assert second_id == video_id and existed
    assert path == second
    assert len(store.list_videos()) == 1
    assert not source.exists()


@pytest.mark.parametrize("name", ["", "../escape", r"..\escape", "CON", "AUX.mp4", "a:b", "movie.mov"])
def test_invalid_names_preserve_source(library, name):
    store, source, _, client = library
    response = client.post("/api/videos/register", json={"path": str(source), "filename": name})
    assert response.status_code in (400, 422)
    assert source.read_bytes() == b"video fixture"
    assert store.list_videos() == []


def test_no_free_space_preserves_source(library, monkeypatch):
    store, source, _, _ = library
    monkeypatch.setattr(video_library.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    with pytest.raises(ValueError, match="空间不足"):
        move(library)
    assert source.read_bytes() == b"video fixture"
    assert not store.list_videos()


def test_failed_verification_preserves_source_and_removes_partial(library, monkeypatch):
    store, source, root, _ = library
    monkeypatch.setattr(video_library.shutil, "copyfileobj", lambda _source, output, **_kwargs: output.write(b"broken"))
    with pytest.raises(ValueError, match="校验失败"):
        move(library)
    assert source.read_bytes() == b"video fixture"
    assert not list(root.rglob("*.part"))
    assert not list(root.rglob("*.mp4"))
    assert not store.list_videos()


def test_failed_db_registration_preserves_source(library, monkeypatch):
    store, source, root, _ = library
    def denied(*_args, **_kwargs):
        raise PermissionError("DB denied")
    monkeypatch.setattr(store, "create_video", denied)
    with pytest.raises(PermissionError, match="DB denied"):
        move(library)
    assert source.read_bytes() == b"video fixture"
    assert not list(root.rglob("*.mp4"))


def test_failed_source_deletion_retains_valid_library_and_returns_warning(library, monkeypatch):
    _, source, _, client = library
    unlink = Path.unlink
    def denied(path, *args, **kwargs):
        if path == source:
            raise PermissionError("File in use")
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", denied)
    response = client.post("/api/videos/register", json={"path": str(source)})
    assert response.status_code == 200
    video = response.json()["video"]
    assert "无法删除" in video["storage_warning"]
    assert source.read_bytes() == Path(video["video_path"]).read_bytes()


def test_changed_source_is_not_deleted(library, monkeypatch):
    store, source, _, _ = library
    update = store.update_video_file
    def mutate(*args, **kwargs):
        update(*args, **kwargs)
        source.write_bytes(b"new source")
    monkeypatch.setattr(store, "update_video_file", mutate)
    _, path, _, warning = move(library)
    assert path.read_bytes() == b"video fixture"
    assert source.read_bytes() == b"new source"
    assert "发生变化" in warning


def test_collision_does_not_overwrite_or_delete_source(library):
    store, source, root, _ = library
    target = root / store.file_hash(source) / source.name
    target.parent.mkdir(parents=True)
    target.write_bytes(b"do not overwrite")
    with pytest.raises(ValueError, match="未覆盖"):
        move(library)
    assert target.read_bytes() == b"do not overwrite"
    assert source.read_bytes() == b"video fixture"


@pytest.mark.parametrize("status", ["running", "resource_paused"])
def test_active_jobs_block_import_and_rename(library, status):
    store, source, _, client = library
    video_id, path, _, _ = move(library)
    source.write_bytes(b"another")
    store.create_job("busy", "video_process", video_id)
    store.update_job("busy", status=status)
    assert client.post("/api/videos/register", json={"path": str(source)}).status_code == 409
    assert client.post("/api/videos/upload", params={"filename": "lesson.mp4"}, content=b"fixture").status_code == 409
    assert client.patch(f"/api/videos/{video_id}/filename", json={"filename": "new"}).status_code == 409
    assert source.read_bytes() == b"another"
    assert path.exists()

@pytest.mark.parametrize("status", ["queued", "running", "resource_paused"])
@pytest.mark.parametrize("kind", ["single", "batch", "legacy_batch"])
def test_only_task_members_are_locked_for_rename(library, status, kind):
    store, source, root, client = library
    first_id, first_path, _, _ = move(library)
    source.write_bytes(b"unrelated fixture")
    other_id, other_path, _, _ = move(library)
    store.mark_transcribed(other_id, [TranscriptSegment(1, 0, 1000, "saved subtitles")])
    folder = store.create_folder("chapter")
    store.set_video_folder(other_id, folder)
    marker = store.add_marker(other_id, 0, 900, "exam", "", "", "", .9, "medium_recall")
    store.set_marker_jump_time(marker, 300)
    job_type = "video_process" if kind == "single" else "batch_video_process"
    store.create_job("busy", job_type, first_id if kind == "single" else None)
    request = (
        {"kind": "single", "video_id": first_id} if kind == "single" else
        {"kind": "batch", "version": 2, "entries": [{"video_id": first_id, "status": "pending_asr"}]} if kind == "batch" else
        {"kind": "batch", "items": [[first_id, str(first_path)]]}
    )
    store.update_job("busy", status=status, request_json=json.dumps(request))
    videos = {row["id"]: row for row in client.get("/api/videos").json()}
    assert videos[first_id]["file_change_block_reason"]
    assert videos[other_id]["file_change_block_reason"] is None
    assert client.get(f"/api/videos/{first_id}").json()["file_change_block_reason"]
    assert client.patch(f"/api/videos/{first_id}/filename", json={"filename": "blocked"}).status_code == 409
    result = client.patch(f"/api/videos/{other_id}/filename", json={"filename": "allowed"})
    assert result.status_code == 200
    assert result.json()["file_change_block_reason"] is None
    assert Path(result.json()["video_path"]).read_bytes() == b"unrelated fixture"
    assert not other_path.exists()
    assert first_path.read_bytes() == b"video fixture"
    assert store.get_video(other_id)["library_folder_id"] == folder
    assert store.get_segments(other_id)[0].text == "saved subtitles"
    assert store.list_markers(other_id)[0]["user_start_ms"] == 300
    assert store.get_job("busy")["status"] == status
    assert json.loads(store.get_job("busy")["request_json"]) == request


def test_rename_does_not_interrupt_an_actual_running_job(library):
    store, source, _, client = library
    busy_id, busy_path, _, _ = move(library)
    source.write_bytes(b"unrelated video")
    free_id, _, _, _ = move(library)
    started, release = Event(), Event()

    def operation(update, should_cancel):
        started.set()
        assert release.wait(10)
        assert not should_cancel()
        assert busy_path.read_bytes() == b"video fixture"

    manager = client.app.state.jobs
    job_id = manager.submit("video_process", busy_id, operation)
    future = manager._futures[job_id]
    try:
        assert started.wait(5)
        assert client.patch(f"/api/videos/{free_id}/filename", json={"filename": "allowed"}).status_code == 200
        assert client.patch(f"/api/videos/{busy_id}/filename", json={"filename": "blocked"}).status_code == 409
        assert store.get_job(job_id)["status"] == "running"
    finally:
        release.set()
        future.result(timeout=10)
    assert store.get_job(job_id)["status"] == "completed"


@pytest.mark.parametrize("status", ["completed", "failed", "interrupted", "awaiting_api"])
def test_finished_or_awaiting_api_queues_release_file_locks(library, status):
    store, _, _, client = library
    video_id, path, _, _ = move(library)
    store.create_job("old-batch", "batch_video_process")
    store.update_job("old-batch", status=status, request_json=json.dumps({
        "kind": "batch", "version": 2,
        "entries": [{"video_id": video_id, "status": "pending_ai"}],
    }))
    assert client.get("/api/videos").json()[0]["file_change_block_reason"] is None
    result = client.patch(f"/api/videos/{video_id}/filename", json={"filename": "allowed"})
    assert result.status_code == 200
    assert not path.exists()


def test_paused_task_can_be_stopped_to_release_its_video(library):
    store, _, _, client = library
    video_id, _, _, _ = move(library)
    store.create_job("paused", "video_process", video_id)
    store.update_job("paused", status="resource_paused")
    store.set_status(video_id, "resource_paused")
    assert client.patch(f"/api/videos/{video_id}/filename", json={"filename": "new"}).status_code == 409
    assert client.post("/api/jobs/paused/cancel").status_code == 200
    assert client.get("/api/videos").json()[0]["file_change_block_reason"] is None
    assert client.patch(f"/api/videos/{video_id}/filename", json={"filename": "new"}).status_code == 200


@pytest.mark.parametrize("status", ["running", "resource_paused"])
def test_model_tasks_do_not_lock_unrelated_video_files(library, status):
    store, _, _, client = library
    video_id, _, _, _ = move(library)
    store.create_job("model", "asr_prepare")
    store.update_job("model", status=status, request_json='{"model_id":"whisper-turbo"}')
    assert client.patch(f"/api/videos/{video_id}/filename", json={"filename": "allowed"}).status_code == 200


@pytest.mark.parametrize("entry_kind", ["source_path", "content_hash"])
def test_pending_import_of_an_existing_video_protects_its_file(library, entry_kind):
    store, _, _, client = library
    video_id, path, _, _ = move(library)
    entry = {"video_id": None, "status": "pending_import", entry_kind:
             str(path) if entry_kind == "source_path" else store.get_video(video_id)["content_hash"]}
    store.create_job("importing", "batch_video_process")
    store.update_job("importing", status="running", request_json=json.dumps({
        "kind": "batch", "version": 2, "entries": [entry],
    }))
    assert client.patch(f"/api/videos/{video_id}/filename", json={"filename": "blocked"}).status_code == 409
    assert path.exists()


def test_storage_migration_still_locks_all_file_changes(library):
    store, _, _, client = library
    video_id, _, _, _ = move(library)
    client.app.state.storage_migrating.set()
    try:
        assert client.get("/api/videos").json()[0]["file_change_block_reason"]
        assert client.patch(f"/api/videos/{video_id}/filename", json={"filename": "blocked"}).status_code == 409
    finally:
        client.app.state.storage_migrating.clear()


def test_renaming_legacy_reference_migrates_it_without_resetting_results(library):
    store, source, root, client = library
    video_id, _, _ = video_pipeline.register_local_video(store, source, root.parent / "temporary_audio")
    store.mark_transcribed(video_id, [TranscriptSegment(1, 0, 1000, "exam")])
    response = client.patch(f"/api/videos/{video_id}/filename", json={"filename": "new"})
    assert response.status_code == 200
    assert Path(response.json()["video_path"]).is_relative_to(root)
    assert not source.exists()
    assert store.get_segments(video_id)[0].text == "exam"


def test_changed_legacy_content_cannot_be_renamed_as_old_video(library):
    store, source, root, client = library
    video_id, _, _ = video_pipeline.register_local_video(store, source, root.parent / "temporary_audio")
    source.write_bytes(b"changed")
    response = client.patch(f"/api/videos/{video_id}/filename", json={"filename": "new"})
    assert response.status_code == 400
    assert "内容已变化" in response.json()["detail"]
    assert source.read_bytes() == b"changed"


def test_custom_library_survives_reopening_as_an_application_setting(library, tmp_path):
    store, source, root, client = library
    custom = tmp_path / "durable-videos"
    directories = client.get("/api/settings/directories").json()
    response = client.put("/api/settings/directories", json={
        "library_id": directories["library_id"], "video_library_dir": str(custom),
        "asr_storage_root": directories["asr_storage_root"],
        "audio_cache_dir": directories["audio_cache_dir"], "log_dir": directories["log_dir"],
    })
    assert response.status_code == 200
    response = client.post("/api/videos/register", json={"path": str(source)})
    assert Path(response.json()["video"]["video_path"]).is_relative_to(custom)
    assert client.get("/api/settings/directories").json()["video_library_dir"] == str(custom)
    reopened = api_main.create_app(store)
    try:
        assert TestClient(reopened).get("/api/settings/directories").json()["video_library_dir"] == str(custom)
    finally:
        reopened.state.jobs.shutdown(wait=True)


def test_batch_import_rejects_mixed_directory_without_moving_files(library, monkeypatch):
    store, source, root, client = library
    keep = source.parent / "notes.txt"
    keep.write_text("keep")
    monkeypatch.setattr(api_main, "require_selectable_model", lambda _: None)
    # Mixed directories must be rejected before registration or job submission.
    monkeypatch.setattr(client.app.state.jobs, "submit", lambda *_args, **_kwargs: "fake-job")
    response = client.post("/api/batch/process", json={"folders": [str(source.parent)]})
    assert response.status_code == 400
    assert source.exists()
    assert keep.read_text() == "keep"
    assert not store.list_videos()


def test_registration_receipt_failure_never_deletes_source(library):
    store, source, root, _ = library

    def fail(_video_id):
        raise OSError("receipt persistence failed")

    with pytest.raises(OSError, match="receipt persistence"):
        register_managed_video(store, source, root.parent / "audio", root, on_registered=fail)
    assert source.read_bytes() == b"video fixture"
    assert Path(store.list_videos()[0]["video_path"]).read_bytes() == b"video fixture"


@pytest.mark.parametrize("field", ["audio_cache_dir", "log_dir"])
def test_cache_directories_cannot_overlap_video_library(library, field):
    _, source, root, client = library
    values = client.get("/api/settings/directories").json()
    payload = {key: values[key] for key in ("asr_storage_root", "audio_cache_dir", "log_dir", "video_library_dir")}
    payload[field] = str(root)
    assert client.put("/api/settings/directories", json=payload).status_code == 400
    assert source.exists()


def test_filename_keeps_extension_without_changing_media_format():
    assert video_filename("chapter", Path("lesson.MP4")) == "chapter.MP4"


def test_case_only_rename_changes_the_actual_disk_filename(library):
    _, _, _, client = library
    video_id, path, _, _ = move(library)
    response = client.patch(f"/api/videos/{video_id}/filename", json={"filename": "LESSON.mp4"})
    assert response.status_code == 200
    names = [entry.name for entry in path.parent.iterdir() if entry.suffix.lower() == ".mp4"]
    assert names == ["LESSON.mp4"]


def test_changing_library_root_moves_managed_videos_and_protects_the_new_library(library, tmp_path):
    store, source, root, client = library
    video_id, old_path, _, _ = move(library)
    current = client.get("/api/settings/directories").json()
    payload = {key: current[key] for key in ("asr_storage_root", "audio_cache_dir", "log_dir", "video_library_dir")}
    payload["video_library_dir"] = str(tmp_path / "new-library")
    assert client.put("/api/settings/directories", json=payload).status_code == 200
    new_path = Path(store.get_video(video_id)["video_path"])
    assert new_path.is_relative_to(tmp_path / "new-library")
    assert not old_path.exists()
    assert new_path.read_bytes() == b"video fixture"
    payload["audio_cache_dir"] = str(tmp_path / "new-library")
    assert client.put("/api/settings/directories", json=payload).status_code == 400
    assert new_path.exists()
    assert client.get(f"/api/videos/{video_id}/stream").status_code == 200
