import json
from pathlib import Path
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
import asr_storage
import video_pipeline
from video_store import VideoStore


@pytest.fixture
def directories(tmp_path, monkeypatch):
    project = tmp_path / "application"
    workspace = tmp_path / "workspace"
    store = VideoStore(workspace / "project.db")
    monkeypatch.setattr(api_main, "DIRECTORY_PROJECT_ROOT", project)
    monkeypatch.setattr(api_main, "ACTIVE_ASR_STORAGE_ROOT", project)
    monkeypatch.setattr(api_main, "ACTIVE_LOG_DIR", project / ".runtime" / "logs")
    application = api_main.create_app(store)
    client = TestClient(application)
    yield client, store, project, workspace, application
    application.state.jobs.shutdown(wait=True)


def values(project, workspace):
    return {
        "asr_storage_root": str(project.parent / "assets"),
        "audio_cache_dir": str(project.parent / "audio-cache"),
        "log_dir": str(project.parent / "logs"),
    }


def test_read_is_observational_and_reports_effective_defaults(directories):
    client, store, project, workspace, _ = directories
    response = client.get("/api/settings/directories")
    assert response.status_code == 200
    data = response.json()
    assert data["audio_cache_dir"] == str(workspace / "temporary_audio")
    assert data["asr_storage_root"] == str(project)
    assert data["package_cache_dir"] == str(project / ".runtime" / "cache")
    assert not data["restart_required"]
    assert data["delete_source_after_import"] is True
    assert not project.exists()
    assert not (workspace / "temporary_audio").exists()
    assert store.list_videos() == []


def test_save_preserves_old_files_and_existing_preferences(directories):
    client, store, project, workspace, _ = directories
    configuration = project / "data" / "asr_storage.json"
    configuration.parent.mkdir(parents=True)
    old = project.parent / "old-assets"
    old.mkdir()
    (old / "keep.bin").write_bytes(b"original")
    configuration.write_text(json.dumps({"storage_root": str(old), "unrelated": "keep"}))
    payload = values(project, workspace)
    response = client.put("/api/settings/directories", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["restart_required"]
    assert data["active_asr_storage_root"] == str(project)
    assert data["asr_storage_root"] == payload["asr_storage_root"]
    assert data["model_dir"] == str(Path(payload["asr_storage_root"]) / "models" / "asr")
    assert (old / "keep.bin").read_bytes() == b"original"
    assert store.list_videos() == []
    assert json.loads(configuration.read_text())["unrelated"] == "keep"
    assert client.get("/api/library/status").json()["temporary_audio_dir"] == payload["audio_cache_dir"]
    assert not list(configuration.parent.glob("storage-*.tmp"))
    restarted = TestClient(api_main.create_app(store))
    assert restarted.get("/api/settings/directories").json()["audio_cache_dir"] == payload["audio_cache_dir"]
    result = subprocess.run(
        [sys.executable, "-c",
         "from pathlib import Path; from asr_storage import active_storage_root, log_directory; "
         "import sys,json; p=Path(sys.argv[1]); print(json.dumps([str(active_storage_root(p)),str(log_directory(p))]))",
         str(project)], capture_output=True, text=True, check=True,
    )
    assert json.loads(result.stdout) == [payload["asr_storage_root"], payload["log_dir"]]


def test_audio_cache_is_application_wide_and_can_restore_default(directories, tmp_path):
    client, _, project, root, _ = directories
    payload = values(project, root)
    assert client.put("/api/settings/directories", json=payload).status_code == 200
    assert asr_storage.audio_directory(project, root) == Path(payload["audio_cache_dir"])
    settings = json.loads((project / "data/asr_storage.json").read_text())
    assert settings["audio_cache_dir"] == payload["audio_cache_dir"]
    assert "audio_directories" not in settings
    assert "video_directories" not in settings
    payload["audio_cache_dir"] = str(root / "temporary_audio")
    assert client.put("/api/settings/directories", json=payload).status_code == 200
    assert asr_storage.audio_directory(project, root) == root / "temporary_audio"


@pytest.mark.parametrize("invalid", ["", "relative/folder", "   "])
def test_invalid_paths_do_not_change_saved_configuration(directories, invalid):
    client, _, project, workspace, _ = directories
    payload = values(project, workspace)
    assert client.put("/api/settings/directories", json=payload).status_code == 200
    before = (project / "data" / "asr_storage.json").read_bytes()
    payload["audio_cache_dir"] = invalid
    assert client.put("/api/settings/directories", json=payload).status_code in (400, 422)
    assert (project / "data" / "asr_storage.json").read_bytes() == before


def test_disk_roots_and_files_are_not_directory_destinations(directories, tmp_path):
    client, _, project, workspace, _ = directories
    payload = values(project, workspace)
    file = tmp_path / "file.txt"
    file.write_text("untouched")
    for invalid in (str(file), str(Path(tmp_path.anchor))):
        payload["log_dir"] = invalid
        assert client.put("/api/settings/directories", json=payload).status_code == 400
    assert file.read_text() == "untouched"
    assert not (project / "data" / "asr_storage.json").exists()


def test_write_permission_errors_do_not_persist_changes(directories, monkeypatch):
    client, _, project, workspace, _ = directories
    def denied(**_kwargs):
        raise PermissionError("Access denied")
    monkeypatch.setattr(asr_storage, "NamedTemporaryFile", denied)
    response = client.put("/api/settings/directories", json=values(project, workspace))
    assert response.status_code == 400
    assert "Access denied" in response.json()["detail"]
    assert not (project / "data" / "asr_storage.json").exists()


@pytest.mark.parametrize("status", ["running", "queued", "resource_paused"])
def test_running_or_paused_jobs_block_path_changes(directories, status):
    client, store, project, workspace, _ = directories
    store.create_job("busy", "video_process", None)
    store.update_job("busy", status=status)
    response = client.put("/api/settings/directories", json=values(project, workspace))
    assert response.status_code == 409
    assert not (project / "data" / "asr_storage.json").exists()
    assert client.get("/api/settings/directories").json()["busy"]


def test_wrong_application_identity_cannot_change_storage(directories):
    client, _, project, workspace, _ = directories
    payload = values(project, workspace)
    payload["library_id"] = "different-application"
    assert client.put("/api/settings/directories", json=payload).status_code == 409
    assert not project.exists()


def test_directory_updates_cannot_relocate_the_database(directories):
    client, _, project, root, _ = directories
    payload = {**values(project, root), "data_root": str(root.parent / "other")}
    assert client.put("/api/settings/directories", json=payload).status_code == 422
    assert client.get("/api/library/status").json()["data_root"] == str(root)
    assert not project.exists()


def test_new_and_existing_videos_use_configured_audio_cache(directories, monkeypatch, tmp_path):
    client, store, project, workspace, _ = directories
    payload = values(project, workspace)
    assert client.put("/api/settings/directories", json=payload).status_code == 200
    source = tmp_path / "lesson.mp4"
    source.write_bytes(b"video fixture")
    monkeypatch.setattr(video_pipeline, "video_duration_ms", lambda _: 1000)
    registered = client.post("/api/videos/register", json={"path": str(source)})
    assert registered.status_code == 200
    video = store.get_video(registered.json()["video_id"])
    assert Path(video["audio_path"]).parent == Path(payload["audio_cache_dir"])
    monkeypatch.setattr(video_pipeline, "configured_audio_directory",
                        lambda _project, root: asr_storage.configured_audio_directory(project, root))
    old_video = {"audio_path": str(workspace / "temporary_audio" / "old.wav")}
    assert video_pipeline._audio_path(store, old_video) == Path(payload["audio_cache_dir"]) / "old.wav"
    payload["audio_cache_dir"] = None
    assert client.put("/api/settings/directories", json=payload).status_code == 200
    assert video_pipeline._audio_path(store, video) == workspace / "temporary_audio" / Path(video["audio_path"]).name


def test_model_runtime_paths_stay_at_startup_location_until_restart(tmp_path, monkeypatch):
    project = tmp_path / "application"
    assert asr_storage.active_storage_root(project) == project
    asr_storage.save_directories(project, tmp_path / "workspace", asset_root=str(tmp_path / "assets"),
                                audio_cache_dir=None, log_dir=str(tmp_path / "logs"))
    assert asr_storage.storage_root(project) == tmp_path / "assets"
    assert asr_storage.active_storage_root(project) == project
    import qwen_runtime
    environment = project / ".runtime" / "qwen-asr"
    environment.mkdir(parents=True)
    (environment / "fixture.bin").write_bytes(b"runtime")
    monkeypatch.setattr(qwen_runtime, "ROOT", project)
    monkeypatch.setattr(qwen_runtime, "ENVIRONMENT", environment)
    assert qwen_runtime.clear_runtime() == 7
    assert not environment.exists()


def test_import_preference_is_application_wide_and_partial_updates_preserve_it(directories, tmp_path):
    client, store, project, workspace, _ = directories
    payload = values(project, workspace)
    payload["delete_source_after_import"] = False
    assert client.put("/api/settings/directories", json=payload).status_code == 200
    assert client.get("/api/settings/directories").json()["delete_source_after_import"] is False
    assert asr_storage.delete_source_after_import(project, workspace) is False
    payload.pop("delete_source_after_import")
    assert client.put("/api/settings/directories", json=payload).status_code == 200
    assert asr_storage.delete_source_after_import(project, workspace) is False
    reopened = api_main.create_app(store)
    try:
        assert TestClient(reopened).get("/api/settings/directories").json()["delete_source_after_import"] is False
    finally:
        reopened.state.jobs.shutdown(wait=True)
    payload["delete_source_after_import"] = True
    assert client.put("/api/settings/directories", json=payload).status_code == 200
    assert asr_storage.delete_source_after_import(project, workspace) is True


@pytest.mark.parametrize("invalid", ["false", 0, 1, [], {}])
def test_import_preference_requires_strict_boolean(directories, invalid):
    client, _, project, workspace, _ = directories
    payload = {**values(project, workspace), "delete_source_after_import": invalid}
    assert client.put("/api/settings/directories", json=payload).status_code == 422
    assert not project.exists()


def test_invalid_saved_import_preference_fails_closed(directories, tmp_path, monkeypatch):
    client, store, project, workspace, _ = directories
    config = project / "data" / "asr_storage.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({
        "storage_root": str(project),
        "delete_source_after_import": "false",
    }))
    source = tmp_path / "lesson.mp4"
    source.write_bytes(b"video fixture")
    monkeypatch.setattr(video_pipeline, "video_duration_ms", lambda _: 1000)
    assert client.post("/api/videos/register", json={"path": str(source)}).status_code == 400
    assert source.read_bytes() == b"video fixture"
    assert store.list_videos() == []
