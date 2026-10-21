import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

import api.main as api_main
import application_data_migration as migration
import asr_storage
from core import TranscriptSegment
from database_lease import DatabaseBusyError, DatabaseLease
from video_store import VideoStore


@pytest.fixture
def library(tmp_path, monkeypatch):
    project, old = tmp_path / "application", tmp_path / "old-library"
    data = project / "data"
    data.mkdir(parents=True)
    (data / "asr_storage.json").write_text(json.dumps({"library_data_dir": str(old)}))
    (data / "asr_settings.json").write_text('{"model_id":"whisper-turbo"}')
    key = data / "ai_keys" / "fixture.dpapi"
    key.parent.mkdir()
    key.write_bytes(b"encrypted-fixture-never-decrypt")
    store = VideoStore(old / "project.db")
    video = store.create_video("hash", "lesson.mp4", str(old / "videos/lesson.mp4"), str(old / "temporary_audio/lesson.wav"), 1000)
    store.mark_transcribed(video, [TranscriptSegment(1, 0, 1000, "lesson")])
    store.add_marker(video, 0, 1000, "lesson", "", "", "", .9, "high_recall")
    monkeypatch.setattr(api_main, "DIRECTORY_PROJECT_ROOT", project)
    return SimpleNamespace(project=project, data=data, old=old, store=store, video=video,
                           destination=tmp_path / "new-data", key=key)


def stage(f):
    plan = migration.plan(f.project, Path(f.store.path), str(f.destination))
    record = migration.schedule(f.project, Path(f.store.path), str(f.destination), plan["plan_id"])
    return plan, record


def test_preview_is_read_only_and_excludes_unrelated_or_large_data(library):
    f = library
    (f.data / "legacy.db").write_bytes(b"unrelated")
    plan = migration.plan(f.project, Path(f.store.path), str(f.destination))
    assert plan["files_total"] == 3
    assert not f.destination.exists()
    assert migration.status(f.project, Path(f.store.path))["config_bytes"] < 1024
    assert not migration.journal_path(f.project).exists()


def test_preview_rejects_reusing_the_current_database_location(library):
    f = library
    current = f.old.parent / "partial-data"
    current.mkdir()
    with pytest.raises(ValueError, match="目标数据库与当前数据库相同"):
        migration.plan(f.project, current / "library" / "project.db", str(current))
    assert not migration.journal_path(f.project).exists()


def test_offline_backup_moves_all_small_data_and_keeps_identity_and_large_paths(library):
    f = library
    old_id = asr_storage.library_identity(f.project, Path(f.store.path))
    stage(f)
    root = migration.apply_pending(f.project, Path(f.store.path))
    assert root == f.destination / "library"
    moved = VideoStore(root / "project.db")
    row = moved.get_video(f.video)
    assert row["video_path"] == str(f.old / "videos/lesson.mp4")
    assert row["audio_path"] == str(f.old / "temporary_audio/lesson.wav")
    assert len(moved.list_markers(f.video)) == len(moved.get_segments(f.video)) == 1
    assert asr_storage.library_identity(f.project, root / "project.db") == old_id
    assert asr_storage.library_scope(f.project, root / "project.db") == str(f.old)
    assert asr_storage.application_config_directory(f.project) == f.destination / "config"
    assert asr_storage.video_directory(f.project, root) == f.old / "videos"
    assert asr_storage.audio_directory(f.project, root) == f.old / "temporary_audio"
    assert (f.destination / "config/ai_keys/fixture.dpapi").read_bytes() == f.key.read_bytes()
    assert (f.destination / "config/asr_settings.json").read_bytes() == (f.data / "asr_settings.json").read_bytes()
    assert Path(f.store.path).exists() and f.key.exists()
    assert migration.read_journal(f.project)["status"] == "completed"


def test_live_database_lease_prevents_copy_or_relocation(library):
    f = library
    stage(f)
    lease = DatabaseLease()
    lease.acquire(Path(f.store.path))
    try:
        with pytest.raises(DatabaseBusyError):
            migration.apply_pending(f.project, Path(f.store.path))
        assert not (f.destination / "library/project.db").exists()
        assert not asr_storage.read_configuration(f.project).get("application_data_root")
    finally:
        lease.release()


def test_conflicting_database_is_never_overwritten(library):
    f = library
    target = VideoStore(f.destination / "library/project.db")
    target.create_video("different", "other.mp4", "other.mp4", "other.wav", 1000)
    stage(f)
    with pytest.raises(ValueError, match="目标数据库"):
        migration.apply_pending(f.project, Path(f.store.path))
    assert len(target.list_videos()) == 1
    assert target.list_videos()[0]["filename"] == "other.mp4"
    assert len(f.store.list_markers(f.video)) == 1
    assert not asr_storage.read_configuration(f.project).get("application_data_root")


def test_config_copy_failure_keeps_locator_and_original_data(library, monkeypatch):
    f = library
    before = (f.data / "asr_storage.json").read_bytes()
    stage(f)
    monkeypatch.setattr(migration.shutil, "copy2", lambda *_args: (_ for _ in ()).throw(PermissionError("fixture denied")))
    with pytest.raises(PermissionError):
        migration.apply_pending(f.project, Path(f.store.path))
    assert (f.data / "asr_storage.json").read_bytes() == before
    assert f.key.exists() and Path(f.store.path).exists()
    assert not (f.destination / "library/project.db").exists()


def test_restart_after_locator_commit_finishes_without_reverting_to_old_database(library):
    f = library
    stage(f)
    root = migration.apply_pending(f.project, Path(f.store.path))
    record = migration.read_journal(f.project)
    record["status"] = "copying"  # Simulate process death just after the atomic locator update.
    migration._write_json(migration.journal_path(f.project), record)
    assert migration.apply_pending(f.project, Path(f.store.path)) == root
    assert migration.read_journal(f.project)["status"] == "completed"
    assert asr_storage.library_identity(f.project, root / "project.db") == record["library_id"]


def test_restart_with_stale_launcher_path_reuses_committed_location_without_rewriting_journal(library):
    f = library
    stage(f)
    root = migration.apply_pending(f.project, Path(f.store.path))
    before = migration.journal_path(f.project).read_bytes()
    assert migration.apply_pending(f.project, Path(f.store.path)) == root
    assert migration.journal_path(f.project).read_bytes() == before


@pytest.mark.parametrize("status", ["pending", "copying", "completed"])
def test_independent_database_is_not_redirected_by_installation_migration(library, status):
    f = library
    stage(f)
    if status != "pending":
        migration.apply_pending(f.project, Path(f.store.path))
    record = migration.read_journal(f.project)
    record["status"] = status
    migration._write_json(migration.journal_path(f.project), record)
    before = migration.journal_path(f.project).read_bytes()
    independent = f.project / "isolated-test" / "project.db"
    assert migration.apply_pending(f.project, independent) == independent.parent
    assert migration.journal_path(f.project).read_bytes() == before
    assert not independent.parent.exists()


def test_api_checks_ownership_and_does_not_schedule_a_delayed_migration(library):
    f = library
    app = api_main.create_app(f.store)
    try:
        client = TestClient(app)
        payload = dict(destination=str(f.destination), library_id=client.get("/api/health").json()["library_id"])
        preview = client.post("/api/settings/application-data/preview", json=payload)
        assert preview.status_code == 200
        response = client.post("/api/settings/application-data/migrate", json={**payload, "plan_id": preview.json()["plan_id"]})
        assert response.status_code == 400
        assert "请使用应用启动入口" in response.json()["detail"]
        assert not migration.journal_path(f.project).exists()
        assert not f.destination.exists()
    finally:
        app.state.jobs.shutdown(wait=True)


def test_api_dispatches_immediate_restart_only_after_explicit_request(library, monkeypatch):
    f = library
    calls = []
    monkeypatch.setattr(migration, "require_owned_launcher", lambda *_args: {})
    monkeypatch.setattr(migration, "restart_owned_application", lambda *args: calls.append(args) or SimpleNamespace(wait=lambda: 0))
    app = api_main.create_app(f.store)
    try:
        client = TestClient(app)
        payload = dict(destination=str(f.destination), library_id=client.get("/api/health").json()["library_id"])
        preview = client.post("/api/settings/application-data/preview", json=payload).json()
        assert not calls
        response = client.post("/api/settings/application-data/migrate", json={**payload, "plan_id": preview["plan_id"]})
        assert response.status_code == 202
        assert len(calls) == 1 and calls[0][2] == response.json()["id"]
        assert client.post("/api/folders", json={"name": "blocked"}).status_code == 409
    finally:
        app.state.jobs.shutdown(wait=True)
