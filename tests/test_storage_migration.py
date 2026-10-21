import json
from pathlib import Path
from threading import Event
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

import api.main as api_main
import classcue.asr_storage as asr_storage
from classcue.core import TranscriptSegment
import classcue.storage_migration as migration
from classcue.video_store import VideoStore


@pytest.fixture
def library(tmp_path, monkeypatch):
    project = tmp_path / "app"
    root = tmp_path / "library"
    assets = tmp_path / "assets"
    store = VideoStore(root / "project.db")
    settings = project / "data" / "asr_storage.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"storage_root": str(assets), "unrelated": "preserve"}))
    video = root / "videos" / "hash" / "lesson.mp4"
    audio = root / "temporary_audio" / "hash.wav"
    model = assets / "models" / "asr" / "fixture" / "weights.bin"
    cache = assets / ".runtime" / "cache" / "wheel.bin"
    environment = assets / ".runtime" / "qwen-asr" / "ready.json"
    log = project / ".runtime" / "logs" / "old.log"
    for path, content in ((video, b"video"), (audio, b"audio"), (model, b"model"),
                          (cache, b"cache"), (environment, b"runtime"), (log, b"log")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    video_id = store.create_video("hash", video.name, str(video), str(audio), 1000)
    store.mark_transcribed(video_id, [TranscriptSegment(1, 0, 1000, "lesson")])
    folder_id = store.create_folder("folder")
    store.set_video_folder(video_id, folder_id)
    store.add_marker(video_id, 0, 1000, "lesson", "", "", "", .9, "high_recall")
    source_folder = store.get_video(video_id)["source_folder"]
    values = dict(asr_storage_root=str(tmp_path / "new-assets"),
                  audio_cache_dir=str(tmp_path / "new-audio"),
                  video_library_dir=str(tmp_path / "new-videos"), log_dir=str(tmp_path / "new-logs"))
    monkeypatch.setattr(api_main, "DIRECTORY_PROJECT_ROOT", project)
    monkeypatch.setattr(api_main, "ACTIVE_ASR_STORAGE_ROOT", assets)
    monkeypatch.setattr(api_main, "ACTIVE_LOG_DIR", log.parent)
    app = api_main.create_app(store)
    fixture = SimpleNamespace(project=project, root=root, store=store, values=values, settings=settings,
                              video=video, audio=audio, model=model, cache=cache, environment=environment,
                              log=log, source_folder=source_folder, video_id=video_id,
                              client=TestClient(app), app=app)
    yield fixture
    app.state.jobs.shutdown(wait=True)


def test_preview_is_observational_and_shows_excluded_environments(library):
    f = library
    before = f.settings.read_bytes()
    plan = migration.plan_migration(f.project, f.store, f.values)
    public = migration.public_plan(plan)
    assert public["files_total"] == 4
    assert public["bytes_total"] == 20
    assert len(public["sections"]) == 4
    assert any("旧运行环境保留" in warning for warning in public["warnings"])
    assert f.settings.read_bytes() == before
    assert all(not Path(value).exists() for value in f.values.values())
    assert not (f.project / "data" / "storage_migration.json").exists()


def test_verified_move_keeps_video_identity_content_and_metadata(library):
    f = library
    phases = []
    plan = migration.plan_migration(f.project, f.store, f.values)
    result = migration.migrate(f.project, f.store, f.values, expected_plan=plan["plan_id"],
                               report=lambda detail: phases.append(detail["phase"]))
    assert result["committed"]
    row = f.store.get_video(f.video_id)
    assert Path(row["video_path"]).read_bytes() == b"video"
    assert Path(row["audio_path"]).read_bytes() == b"audio"
    assert row["status"] == "transcribed"
    assert row["source_folder"] == f.source_folder
    assert row["library_folder_id"]
    assert len(f.store.get_segments(f.video_id)) == len(f.store.list_markers(f.video_id)) == 1
    assert (Path(f.values["asr_storage_root"]) / "models" / "asr" / "fixture" / "weights.bin").read_bytes() == b"model"
    assert (Path(f.values["asr_storage_root"]) / ".runtime" / "cache" / "wheel.bin").read_bytes() == b"cache"
    assert not any(path.exists() for path in (f.video, f.audio, f.model, f.cache))
    assert f.environment.read_bytes() == b"runtime"
    assert f.log.read_bytes() == b"log"
    assert Path(f.store.path).exists()
    assert json.loads(f.settings.read_text())["unrelated"] == "preserve"
    assert set(phases) >= {"copying", "verifying", "committing", "cleaning", "completed"}


def test_conflicting_target_does_not_overwrite_or_switch_anything(library):
    f = library
    destination = Path(f.values["video_library_dir"]) / "hash" / "lesson.mp4"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"unrelated")
    before = f.settings.read_bytes()
    with pytest.raises(ValueError, match="同名"):
        migration.migrate(f.project, f.store, f.values)
    assert destination.read_bytes() == b"unrelated"
    assert f.settings.read_bytes() == before
    assert f.video.read_bytes() == b"video"
    assert f.model.exists() and f.audio.exists() and f.cache.exists()
    assert f.store.get_video(f.video_id)["video_path"] == str(f.video)
    assert not (Path(f.values["asr_storage_root"]) / "models" / "asr" / "fixture" / "weights.bin").exists()


def test_identical_existing_destination_is_reused_and_does_not_block_retry(library):
    f = library
    destination = Path(f.values["audio_cache_dir"]) / "hash.wav"
    destination.parent.mkdir()
    destination.write_bytes(b"audio")
    assert migration.migrate(f.project, f.store, f.values)["committed"]
    assert destination.read_bytes() == b"audio"
    assert not f.audio.exists()


def test_insufficient_combined_disk_space_leaves_originals_and_settings(library, monkeypatch):
    f = library
    monkeypatch.setattr(migration.shutil, "disk_usage", lambda _: SimpleNamespace(free=19))
    with pytest.raises(ValueError, match="空间不足"):
        migration.plan_migration(f.project, f.store, f.values)
    assert f.video.exists() and f.model.exists()
    assert asr_storage.storage_root(f.project) == f.model.parents[3]


def test_failed_copy_or_verification_rolls_back_without_deleting_sources(library, monkeypatch):
    f = library
    before = f.settings.read_bytes()
    original = migration._hash
    monkeypatch.setattr(migration, "_hash", lambda path, *args: "wrong" if path.suffix == ".part" else original(path, *args))
    with pytest.raises(ValueError, match="校验失败"):
        migration.migrate(f.project, f.store, f.values)
    assert f.settings.read_bytes() == before
    assert f.audio.exists() and f.model.exists() and f.video.exists()
    assert not list(Path(f.values["audio_cache_dir"]).glob("*.part"))
    assert f.store.get_video(f.video_id)["video_path"] == str(f.video)


def test_config_write_failure_restores_database_configuration_and_sources(library, monkeypatch):
    f = library
    before = f.settings.read_bytes()
    def denied(*_args, **_kwargs):
        raise PermissionError("cannot save settings")
    monkeypatch.setattr(asr_storage, "save_directories", denied)
    with pytest.raises(PermissionError, match="cannot save"):
        migration.migrate(f.project, f.store, f.values)
    assert f.settings.read_bytes() == before
    assert f.store.get_video(f.video_id)["video_path"] == str(f.video)
    assert f.video.exists() and f.audio.exists() and f.model.exists()


@pytest.mark.parametrize("field,target", [
    ("audio_cache_dir", "inside-audio"),
    ("video_library_dir", "application"),
    ("audio_cache_dir", "database"),
    ("audio_cache_dir", "environment"),
])
def test_rejects_nested_and_protected_destinations(library, field, target):
    f = library
    path = {"inside-audio": f.audio.parent / "nested", "application": f.project,
            "database": f.root, "environment": f.environment.parent}[target]
    with pytest.raises(ValueError):
        migration.plan_migration(f.project, f.store, {**f.values, field: str(path)})
    assert f.video.exists()


def test_changed_preview_cannot_start_a_move(library):
    f = library
    plan = migration.plan_migration(f.project, f.store, f.values)
    f.audio.write_bytes(b"new audio content")
    with pytest.raises(ValueError, match="已变化"):
        migration.migrate(f.project, f.store, f.values, expected_plan=plan["plan_id"])
    assert f.video.exists() and f.audio.read_bytes() == b"new audio content"


def test_cleanup_failure_keeps_valid_new_library_and_reports_old_file(library, monkeypatch):
    f = library
    original = Path.unlink
    def unlink(path, *args, **kwargs):
        if path == f.video:
            raise PermissionError("playing")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", unlink)
    result = migration.migrate(f.project, f.store, f.values)
    assert result["committed"]
    assert any("原文件保留" in warning for warning in result["warnings"])
    assert f.video.exists()
    assert Path(f.store.get_video(f.video_id)["video_path"]).read_bytes() == b"video"


def wait_job(client, job_id):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in {"running", "queued"}:
            return job
        time.sleep(.02)
    raise AssertionError("migration did not finish")


def test_background_api_publishes_progress_and_serves_migrated_video(library):
    f = library
    before_id = f.client.get("/api/library/status").json()["library_id"]
    preview = f.client.post("/api/settings/directories/preview", json=f.values)
    assert preview.status_code == 200
    response = f.client.post("/api/settings/directories/migrate", json={**f.values, "plan_id": preview.json()["plan_id"]})
    assert response.status_code == 202
    job = wait_job(f.client, response.json()["job_id"])
    assert job["status"] == "completed", job
    assert job["migration"]["phase"] == "completed"
    assert job["migration"]["files_completed"] == 4
    assert f.client.get("/api/settings/directories").json()["migration_job"]["id"] == job["id"]
    assert f.client.get(f"/api/videos/{f.video_id}/stream").content == b"video"
    assert f.client.get("/api/library/status").json()["library_id"] == before_id
    assert not f.video.exists()


def test_mutations_blocked_during_migration_but_progress_is_readable(library, monkeypatch):
    f = library
    started, release = Event(), Event()
    original = migration.migrate
    def slow(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(migration, "migrate", slow)
    plan = f.client.post("/api/settings/directories/preview", json=f.values).json()
    response = f.client.post("/api/settings/directories/migrate", json={**f.values, "plan_id": plan["plan_id"]})
    job_id = response.json()["job_id"]
    try:
        assert started.wait(3)
        assert f.client.get(f"/api/jobs/{job_id}").status_code == 200
        assert f.client.post("/api/folders", json={"name": "blocked"}).status_code == 409
        assert f.client.get("/api/settings/directories").json()["busy"]
        assert f.client.get("/api/library/status").json()["processing"]
    finally:
        release.set()
    assert wait_job(f.client, job_id)["status"] == "completed"


def test_crash_before_commit_restores_old_settings_and_database(library):
    f = library
    plan = migration.plan_migration(f.project, f.store, f.values)
    migration._write_json(f.settings, plan["settings"])
    migration._update_paths(f.store, plan["updates"])
    journal = {**plan, "phase": "committing"}
    migration._write_json(f.project / "data" / "storage_migration.json", journal)
    migration.recover_migration(f.project, f.store)
    assert json.loads(f.settings.read_text()) == plan["old_settings"]
    assert f.store.get_video(f.video_id)["video_path"] == str(f.video)
    assert f.video.read_bytes() == b"video"


def test_crash_after_commit_finishes_cleanup_without_rolling_back(library, monkeypatch):
    f = library
    original = migration._cleanup_sources
    monkeypatch.setattr(migration, "_cleanup_sources", lambda _entries: (_ for _ in ()).throw(OSError("interrupted")))
    assert migration.migrate(f.project, f.store, f.values)["committed"]
    assert f.video.exists()
    monkeypatch.setattr(migration, "_cleanup_sources", original)
    migration.recover_migration(f.project, f.store)
    assert not f.video.exists()
    assert Path(f.store.get_video(f.video_id)["video_path"]).read_bytes() == b"video"


def test_startup_rollback_requires_restart_before_using_captured_model_paths(library):
    f = library
    plan = migration.plan_migration(f.project, f.store, f.values)
    migration._write_json(f.settings, plan["settings"])
    migration._write_json(f.project / "data" / "storage_migration.json", {**plan, "phase": "committing"})
    app = api_main.create_app(f.store)
    with TestClient(app):
        with pytest.raises(RuntimeError, match="重启"):
            app.state.jobs.submit("asr_prepare", None, lambda *_args: None)
    assert f.store.get_video(f.video_id)["video_path"] == str(f.video)
    assert f.video.read_bytes() == b"video"


def test_unregistered_files_in_video_directory_are_never_moved_or_removed(library):
    f = library
    unrelated = f.video.parent / "notes.txt"
    unrelated.write_bytes(b"keep")
    migration.migrate(f.project, f.store, f.values)
    assert unrelated.read_bytes() == b"keep"
    assert not (Path(f.values["video_library_dir"]) / "hash" / "notes.txt").exists()


def paired_values(f, destination):
    return {"asr_storage_root": str(f.model.parents[3]), "log_dir": str(f.log.parent),
            "media_storage_root": str(destination)}


def test_shared_root_moves_videos_and_audio_together_and_persists_binding(library):
    f = library
    destination = f.root.parent / "media"
    values = paired_values(f, destination)
    preview = f.client.post("/api/settings/directories/preview", json=values)
    assert preview.status_code == 200, preview.text
    assert {section["label"] for section in preview.json()["sections"]} == {"视频库", "临时音频与转写缓存"}
    assert not destination.exists()
    response = f.client.put("/api/settings/directories", json=values)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["media_storage_root"] == str(destination)
    assert result["media_storage_bound"]
    assert result["video_library_dir"] == str(destination / "videos")
    assert result["audio_cache_dir"] == str(destination / "audio")
    row = f.store.get_video(f.video_id)
    assert Path(row["video_path"]).is_relative_to(destination / "videos")
    assert Path(row["audio_path"]).is_relative_to(destination / "audio")
    assert Path(row["video_path"]).read_bytes() == b"video"
    assert Path(row["audio_path"]).read_bytes() == b"audio"
    assert not f.video.exists() and not f.audio.exists()
    assert f.model.exists() and f.cache.exists()
    assert row["source_folder"] == f.source_folder
    assert len(f.store.list_markers(f.video_id)) == 1
    saved = json.loads(f.settings.read_text())
    assert saved["media_storage_root"] == str(destination)
    assert asr_storage.video_directory(f.project, f.root) == destination / "videos"
    assert asr_storage.audio_directory(f.project, f.root) == destination / "audio"


def test_shared_root_creates_both_children_even_without_files(library):
    f = library
    f.video.unlink()
    f.audio.unlink()
    with f.store.connect() as connection:
        connection.execute("DELETE FROM videos")
    destination = f.root.parent / "empty-media"
    assert f.client.put("/api/settings/directories", json=paired_values(f, destination)).status_code == 200
    assert (destination / "videos").is_dir()
    assert (destination / "audio").is_dir()


def test_old_default_layout_is_not_changed_until_user_requests_binding(library):
    f = library
    current = f.client.get("/api/settings/directories").json()
    assert current["media_storage_root"] == str(f.root)
    assert not current["media_storage_bound"]
    assert current["audio_cache_dir"] == str(f.audio.parent)
    assert f.audio.exists()
    response = f.client.put("/api/settings/directories", json=paired_values(f, f.root))
    assert response.status_code == 200, response.text
    assert response.json()["media_storage_bound"]
    assert Path(f.store.get_video(f.video_id)["video_path"]) == f.video
    assert (f.root / "audio" / "hash.wav").read_bytes() == b"audio"
    assert not f.audio.exists()


@pytest.mark.parametrize("child", ["audio_cache_dir", "video_library_dir"])
def test_bound_subdirectories_cannot_be_changed_independently(library, child):
    f = library
    destination = f.root.parent / "media"
    values = paired_values(f, destination)
    assert f.client.put("/api/settings/directories", json=values).status_code == 200
    before = f.settings.read_bytes()
    legacy_payload = {"asr_storage_root": values["asr_storage_root"], "log_dir": values["log_dir"],
                      child: str(f.root.parent / "separate")}
    response = f.client.put("/api/settings/directories", json=legacy_payload)
    assert response.status_code == 400
    assert "已绑定" in response.json()["detail"]
    assert f.settings.read_bytes() == before
    assert (destination / "audio" / "hash.wav").exists()


def test_bound_library_can_move_again_and_preference_save_keeps_the_pair(library):
    f = library
    first, second = f.root.parent / "first", f.root.parent / "second"
    assert f.client.put("/api/settings/directories", json=paired_values(f, first)).status_code == 200
    settings = paired_values(f, second)
    assert f.client.put("/api/settings/directories", json=settings).status_code == 200
    settings.pop("media_storage_root")
    settings["delete_source_after_import"] = False
    assert f.client.put("/api/settings/directories", json=settings).status_code == 200
    assert not (first / "videos").exists() and not (first / "audio").exists()
    assert asr_storage.audio_directory(f.project, f.root) == second / "audio"
    assert asr_storage.video_directory(f.project, f.root) == second / "videos"
    assert Path(f.store.get_video(f.video_id)["video_path"]).read_bytes() == b"video"


def test_conflict_in_either_child_preserves_both_originals(library):
    f = library
    destination = f.root.parent / "media"
    conflict = destination / "videos" / "hash" / "lesson.mp4"
    conflict.parent.mkdir(parents=True)
    conflict.write_bytes(b"unrelated")
    before = f.settings.read_bytes()
    response = f.client.put("/api/settings/directories", json=paired_values(f, destination))
    assert response.status_code == 400
    assert f.video.read_bytes() == b"video" and f.audio.read_bytes() == b"audio"
    assert conflict.read_bytes() == b"unrelated"
    assert f.settings.read_bytes() == before
    assert not (destination / "audio" / "hash.wav").exists()
