from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from classcue import asr_storage, video_pipeline
from classcue.desktop_video_drop import VideoDropBridge
from classcue.video_library import import_local_video
from classcue.batch_processing import batch_source_paths, run_batch
from classcue.processing_control import ResourcePaused
from classcue.video_store import VideoStore


@pytest.fixture
def library(tmp_path, monkeypatch):
    project = tmp_path / "application"
    data = tmp_path / "library"
    monkeypatch.setattr(api_main, "DIRECTORY_PROJECT_ROOT", project)
    monkeypatch.setattr(video_pipeline, "video_duration_ms", lambda _: 1000)
    store = VideoStore(data / "project.db")
    app = api_main.create_app(store)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, store=store, project=project, data=data)


def policy(library, move):
    current = library.client.get("/api/settings/directories").json()
    payload = {key: current[key] for key in ("asr_storage_root", "audio_cache_dir", "log_dir", "video_library_dir")}
    payload["delete_source_after_import"] = move
    assert library.client.put("/api/settings/directories", json=payload).status_code == 200


@pytest.mark.parametrize("move", [False, True])
@pytest.mark.parametrize("entry", ["single", "secondary", "legacy"])
def test_local_entries_share_the_same_import_policy(library, tmp_path, move, entry):
    policy(library, move)
    source = tmp_path / "camera.mp4"
    source.write_bytes(b"synthetic camera")
    if entry == "legacy":
        identity, path, _, _ = import_local_video(
            library.store, source, library.data / "audio", project_root=library.project,
        )
    else:
        endpoint = "/api/videos/register"
        if entry == "secondary":
            primary = tmp_path / "primary.mp4"
            primary.write_bytes(b"synthetic primary")
            primary_id = library.client.post(endpoint, json={"path": str(primary)}).json()["video_id"]
            endpoint = f"/api/videos/{primary_id}/secondary-view/import"
        response = library.client.post(endpoint, json={"path": str(source)})
        assert response.status_code == 200, response.text
        identity = response.json()["video_id"]
        path = Path(response.json()["video"]["video_path"])
    assert path.is_relative_to(library.data / "videos")
    assert path.read_bytes() == b"synthetic camera"
    assert source.exists() is not move
    assert bool(library.store.get_video(identity)["is_secondary"]) is (entry == "secondary")


def test_duplicates_keep_name_folder_display_and_source_and_validate_library_copy(library, tmp_path):
    policy(library, True)
    source = tmp_path / "first.mp4"
    source.write_bytes(b"synthetic camera")
    first = library.client.post("/api/videos/register", json={"path": str(source)}).json()["video"]
    identity = first["id"]
    folder = library.store.create_folder("course")
    library.store.set_video_folder(identity, folder)
    library.store.update_video_display_name(identity, "custom label")
    duplicate = tmp_path / "second.mp4"
    duplicate.write_bytes(b"synthetic camera")
    response = library.client.post("/api/videos/register", json={"path": str(duplicate)})
    assert response.status_code == 200
    result = response.json()
    assert result["existed"]
    assert result["video"]["video_path"] == first["video_path"]
    assert result["video"]["filename"] == first["filename"]
    assert library.store.get_video(identity)["display_name"] == "custom label"
    assert library.store.get_video(identity)["library_folder_id"] == folder
    assert duplicate.exists()
    Path(first["video_path"]).write_bytes(b"damaged library copy")
    response = library.client.post("/api/videos/register", json={"path": str(duplicate)})
    assert response.status_code == 400
    assert duplicate.read_bytes() == b"synthetic camera"


@pytest.mark.parametrize("move", [False, True])
def test_upload_never_consumes_the_users_original(library, tmp_path, move):
    policy(library, move)
    source = tmp_path / "camera.mp4"
    source.write_bytes(b"synthetic upload")
    response = library.client.post("/api/videos/upload", params={"filename": source.name}, content=source.read_bytes())
    assert response.status_code == 200
    assert source.exists()
    assert Path(response.json()["video"]["video_path"]).read_bytes() == source.read_bytes()


@pytest.mark.parametrize("existing", [False, True])
def test_new_install_copies_and_old_install_retains_implicit_move_mode(tmp_path, existing):
    project, data = tmp_path / "application", tmp_path / "library"
    if existing:
        VideoStore(data / "project.db")
    asr_storage.initialize_import_preference(project, data)
    assert asr_storage.delete_source_after_import(project, data) is existing
    VideoStore(data / "project.db")
    asr_storage.initialize_import_preference(project, data)
    assert asr_storage.delete_source_after_import(project, data) is existing


def test_native_bridge_forwards_actual_paths_checks_origin_and_unbinds():
    element = Mock()
    window = SimpleNamespace(dom=SimpleNamespace(get_element=Mock(return_value=element)), run_js=Mock())
    check = Mock()
    bridge = VideoDropBridge(window, check)
    token = bridge.bind()
    handler = element.on.call_args.args[1]
    handler.callback({"dataTransfer": {"files": [
        {"name": "camera.mp4", "pywebviewFullPath": "D:/synthetic/camera.mp4"},
    ]}})
    assert "D:/synthetic/camera.mp4" in window.run_js.call_args.args[0]
    assert token in window.run_js.call_args.args[0]
    bridge.unbind(token)
    element.off.assert_called_once_with("drop", handler.callback)
    assert check.call_count == 3
    check.side_effect = PermissionError("external origin")
    with pytest.raises(PermissionError):
        handler.callback({"dataTransfer": {"files": []}})
    assert window.run_js.call_count == 1


@pytest.mark.parametrize("missing", [False, True])
def test_duplicate_repairs_legacy_or_missing_files_without_overwriting_names(library, tmp_path, missing):
    policy(library, True)
    original = tmp_path / "original.mp4"
    original.write_bytes(b"synthetic camera")
    if missing:
        identity, managed, _, _ = import_local_video(
            library.store, original, library.data / "audio", project_root=library.project,
        )
    else:
        identity, managed, _ = video_pipeline.register_local_video(library.store, original, library.data / "audio")
    library.store.update_video_display_name(identity, "saved custom name")
    if missing:
        managed.unlink()
    duplicate = tmp_path / "other-source" / "different.mp4"
    duplicate.parent.mkdir()
    duplicate.write_bytes(b"synthetic camera")
    response = library.client.post("/api/videos/register", json={"path": str(duplicate)})
    assert response.status_code == 200
    result = response.json()
    assert result["existed"]
    assert result["video"]["filename"] == "original.mp4"
    assert library.store.get_video(identity)["display_name"] == "saved custom name"
    assert Path(result["video"]["video_path"]).read_bytes() == duplicate.read_bytes()
    assert duplicate.exists()
    assert result["video"]["storage_warning"]
    assert library.store.get_video(identity)["source_folder"] == str(tmp_path)


def test_cli_recovers_archived_queue_after_resource_pause(library, tmp_path, monkeypatch):
    import classcue.batch_processing as batch
    import classcue.paths

    policy(library, True)
    monkeypatch.setattr(classcue.paths, "PROJECT_ROOT", library.project)
    source_root = tmp_path / "downloads"
    source_root.mkdir()
    source = source_root / "camera.mp4"
    source.write_bytes(b"synthetic camera")
    monkeypatch.setattr(batch, "prepare_video_for_analysis", Mock(side_effect=ResourcePaused("synthetic pause")))
    first = run_batch(library.store, batch_source_paths(library.store, source_root), library.data / "audio", object())
    assert first.paused
    assert not source.exists()
    recovered = batch_source_paths(library.store, source_root)
    assert len(recovered) == 1 and recovered[0].is_file()
    monkeypatch.setattr(batch, "prepare_video_for_analysis", Mock(return_value=("skipped", 0)))
    second = run_batch(library.store, recovered, library.data / "audio", object())
    assert second.skipped == 1 and not second.failed


@pytest.mark.parametrize("reload_page", [False, True, "during_unbind"])
def test_native_rebind_and_reload_keep_the_new_pywebview_element_registered(monkeypatch, reload_page):
    from itertools import count
    from webview.dom import _dnd_state
    from webview.dom.element import Element

    monkeypatch.setitem(_dnd_state, "num_listeners", 0)
    identities = count()
    window = SimpleNamespace(run_js=Mock(), dom=SimpleNamespace(_elements={}))
    window.evaluate_js = lambda code: [] if "getOwnPropertyNames" in code else f"handler-{next(identities)}"
    window.dom.get_element = lambda selector: Element(window, "same-node")
    bridge = VideoDropBridge(window, Mock())
    first = bridge.bind()
    if reload_page is True:
        window.dom._elements.clear()
    elif reload_page == "during_unbind":
        window.run_js.side_effect = lambda _code: window.dom._elements.clear()
    second = bridge.bind()
    window.run_js.side_effect = None
    bridge.unbind(first)
    assert list(bridge.bindings) == [second]
    element = window.dom._elements["same-node"]
    assert len(element._event_handlers["drop"]) == 1
    element._event_handlers["drop"][0]({"dataTransfer": {"files": [
        {"name": "camera.mp4", "pywebviewFullPath": "D:/synthetic/camera.mp4"},
    ]}})
    assert second in window.run_js.call_args.args[0]
    assert _dnd_state["num_listeners"] == 1
    bridge.unbind(second)
    assert not window.dom._elements
    assert _dnd_state["num_listeners"] == 0


def test_copy_scan_and_batch_deduplicate_stable_video_ids(library, tmp_path, monkeypatch):
    import classcue.batch_processing as batch
    import classcue.paths

    policy(library, False)
    monkeypatch.setattr(classcue.paths, "PROJECT_ROOT", library.project)
    source_root = tmp_path / "downloads"
    source_root.mkdir()
    source = source_root / "camera.mp4"
    source.write_bytes(b"synthetic camera")
    identity, managed, _, _ = import_local_video(
        library.store, source, library.data / "audio", project_root=library.project,
    )
    assert batch_source_paths(library.store, source_root) == [source]
    prepare = Mock(return_value=("skipped", 0))
    monkeypatch.setattr(batch, "prepare_video_for_analysis", prepare)
    result = run_batch(library.store, [source, managed], library.data / "audio", object())
    assert prepare.call_count == 1
    assert prepare.call_args.args[1] == identity
    assert result.skipped == 2 and result.failed == 0
