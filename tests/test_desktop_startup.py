import json
from pathlib import Path
import shutil
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from desktop_startup import (
    MAX_BYTES, MAX_ITEMS, MAX_COVERS, MAX_COVER_BYTES,
    load_library_snapshot, save_library_snapshot, save_startup_cover, startup_html,
    startup_transition_script,
)


IDENTITY = "012345abcdef"


@pytest.fixture
def project(tmp_path):
    (tmp_path / "assets").mkdir()
    shutil.copy2(Path(__file__).parents[1] / "assets" / "desktop-startup.html", tmp_path / "assets")
    shutil.copy2(Path(__file__).parents[1] / "assets" / "desktop-startup-transition.js", tmp_path / "assets")
    return tmp_path


def listing(name="算法课程"):
    return {"parent_id": None, "items": [
        {"type": "folder", "folder": {"id": 2, "name": name, "video_count": 3}},
        {"type": "video", "video": {"id": 1, "display_name": name, "filename": "lesson.mp4",
                                  "video_path": "SECRET-PATH", "api_key": "SECRET-KEY", "text": "SECRET-TEXT"}},
    ]}


def test_snapshot_is_bounded_and_whitelists_metadata_without_modifying_source(project):
    original = listing()
    save_library_snapshot(project, IDENTITY, original)
    record = load_library_snapshot(project, IDENTITY)
    assert record["library_id"] == IDENTITY and len(record["items"]) == 2
    assert record["items"][1]["video"] == {"id": 1, "name": "算法课程"}
    assert "SECRET" not in json.dumps(record)
    assert original["items"][1]["video"]["video_path"] == "SECRET-PATH"
    many = listing()
    many["items"] = [listing()["items"][1]] * (MAX_ITEMS + 100)
    save_library_snapshot(project, IDENTITY, many)
    assert len(load_library_snapshot(project, IDENTITY)["items"]) == MAX_ITEMS
    assert len(json.dumps(load_library_snapshot(project, IDENTITY)).encode()) < MAX_BYTES


def test_preview_is_read_only_escapes_names_and_does_not_play_a_loading_animation(project):
    save_library_snapshot(project, IDENTITY, listing('"><script>window.evil=true</script>'))
    html = startup_html(project, IDENTITY)
    assert "上次同步的列表" in html
    assert "&lt;script&gt;" in html and "<script>window.evil" not in html
    assert "<button disabled>导入视频</button>" in html
    assert "SECRET" not in html and 'draggable="true"' not in html
    assert "@keyframes" not in html and "setTimeout" not in html
    assert "<!--LIBRARY_PREVIEW-->" not in html
    assert 'data-library-key="folder-2"' in html
    assert 'data-library-key="video-1"' in html


@pytest.mark.parametrize("kind", ["missing", "wrong-library", "corrupt", "oversized", "wrong-version"])
def test_untrusted_or_unrelated_snapshot_falls_back_without_opening_a_database(project, kind):
    save_library_snapshot(project, IDENTITY, listing())
    path = project / ".runtime" / "library-startup" / f"{IDENTITY}.json"
    if kind == "missing":
        path.unlink()
    elif kind == "corrupt":
        path.write_text("{bad")
    elif kind == "oversized":
        path.write_text(" " * (MAX_BYTES + 1))
    else:
        record = json.loads(path.read_text(encoding="utf-8"))
        record["library_id" if kind == "wrong-library" else "version"] = "other"
        path.write_text(json.dumps(record))
    assert load_library_snapshot(project, IDENTITY) is None
    assert "算法课程" not in startup_html(project, IDENTITY)
    assert "正在读取本地视频库" in startup_html(project, IDENTITY)


def test_nested_directory_cannot_replace_root_preview_and_disk_failure_is_nonfatal(project, monkeypatch):
    save_library_snapshot(project, IDENTITY, listing())
    nested = listing("Not root")
    nested["parent_id"] = 2
    save_library_snapshot(project, IDENTITY, nested)
    assert "Not root" not in json.dumps(load_library_snapshot(project, IDENTITY))
    monkeypatch.setattr(Path, "mkdir", Mock(side_effect=OSError("read-only")))
    save_library_snapshot(project, IDENTITY, listing())
    assert load_library_snapshot(project, "../escape") is None


def test_desktop_library_response_saves_preview_after_returning_without_model_checks(project, monkeypatch):
    import api.main as api
    from video_store import VideoStore
    database = project / "library" / "project.db"
    store = VideoStore(database)
    store.create_video("test", "lesson.mp4", "unused.mp4", "unused.wav", 3000)
    monkeypatch.setattr(api, "DIRECTORY_PROJECT_ROOT", project)
    monkeypatch.setattr(api, "load_data_root", lambda: database.parent)
    monkeypatch.setenv("CLASSROOM_DESKTOP_MODE", "1")
    application = api.create_app()
    with TestClient(application) as client:
        body = client.get("/api/library").json()
        identity = client.get("/api/health").json()["library_id"]
        assert body["items"][0]["video"]["filename"] == "lesson.mp4"
        assert load_library_snapshot(project, identity)["items"][0]["video"]["name"] == "lesson"


def test_window_uses_expected_library_snapshot_and_replays_early_progress(project, monkeypatch):
    from scripts import desktop_app
    import app_storage
    import asr_storage
    monkeypatch.setattr(desktop_app, "PROJECT", project)
    monkeypatch.setattr(app_storage, "load_data_root", lambda: project / "library")
    monkeypatch.setattr(asr_storage, "library_identity", lambda *_args: IDENTITY)
    remove_transition = Mock()
    install = Mock(return_value=remove_transition)
    monkeypatch.setattr(desktop_app, "install_startup_transition", install)
    save_library_snapshot(project, IDENTITY, listing())
    ui = desktop_app.WindowUI()
    assert "算法课程" in ui.boot_html
    loaded = Event()
    ui.window = SimpleNamespace(events=SimpleNamespace(loaded=loaded), evaluate_js=Mock(), load_url=Mock())
    ui.progress("Connecting")
    assert ui.last_progress == "Connecting"
    ui.window.evaluate_js.assert_not_called()
    loaded.set()
    ui.progress("Connected")
    ui.window.evaluate_js.assert_called_once()
    view = {"scroll_y": 920, "anchor": "video-1", "offset": -40}
    ui.window.evaluate_js.return_value = view
    ui.open("http://127.0.0.1:8765")
    ui.window.load_url.assert_called_once_with("http://127.0.0.1:8765/")
    assert ui.startup_view == {**view, "library_id": IDENTITY}
    install.assert_called_once()
    assert "classroom-startup-cover" in install.call_args.args[1]
    ui.window.get_current_url = lambda: None
    ui.navigation_loaded()
    remove_transition.assert_not_called()
    ui.window.get_current_url = lambda: ui.current_url
    ui.navigation_loaded()
    ui.navigation_loaded()
    remove_transition.assert_called_once()


def test_transition_contains_only_a_read_only_preview_and_is_restricted_to_the_target_origin(project):
    save_library_snapshot(project, IDENTITY, listing('<b>&test "name"</b>'))
    view = {"scroll_y": 123, "anchor": "video-1", "offset": -40}
    script = startup_transition_script(project, startup_html(project, IDENTITY), "http://127.0.0.1:8765", view)
    # The payload is JSON, not executable strings interpolated into HTML.
    payload = json.JSONDecoder().raw_decode(script.split("const data = ", 1)[1])[0]
    assert payload["origin"] == "http://127.0.0.1:8765"
    assert payload["view"] == view
    assert "<script" not in payload["html"]
    assert "onclick" not in payload["html"]
    assert "&lt;b&gt;&amp;test" in payload["html"]
    assert "SECRET" not in payload["html"]
    assert "location.origin !== data.origin" in script
    assert 'location.pathname !== "/"' in script
    assert 'setAttribute("sandbox", "allow-same-origin")' in script


def test_transition_registration_and_removal_access_webview_only_on_its_ui_thread(monkeypatch):
    import sys
    from scripts.desktop_transition import install_startup_transition
    inside_ui = False
    registered, removed = [], []

    class Delegate:
        def __class_getitem__(cls, _value):
            return lambda callback: callback

    class WebView:
        @property
        def CoreWebView2(self):
            assert inside_ui, "WebView2 properties cannot be read from the service thread"
            return self

        def AddScriptToExecuteOnDocumentCreatedAsync(self, script):
            registered.append(script)
            return SimpleNamespace(GetAwaiter=lambda: SimpleNamespace(GetResult=lambda: "one-shot"))

        def RemoveScriptToExecuteOnDocumentCreated(self, script_id):
            removed.append(script_id)

    def invoke(callback):
        nonlocal inside_ui
        inside_ui = True
        try:
            return callback()
        finally:
            inside_ui = False

    monkeypatch.setitem(sys.modules, "System", SimpleNamespace(Func=Delegate, Object=object))
    window = SimpleNamespace(native=SimpleNamespace(browser=SimpleNamespace(webview=WebView()), Invoke=invoke))
    cleanup = install_startup_transition(window, "read-only script")
    assert registered == ["read-only script"] and cleanup is not None
    cleanup()
    assert removed == ["one-shot"]


def test_missing_transition_asset_cannot_prevent_the_real_library_from_opening(project, monkeypatch):
    from scripts import desktop_app
    monkeypatch.setattr(desktop_app, "PROJECT", project)
    ui = desktop_app.WindowUI()
    loaded = Event()
    loaded.set()
    ui.window = SimpleNamespace(events=SimpleNamespace(loaded=loaded), load_url=Mock(),
                                evaluate_js=lambda _script: {"scroll_y": 0, "anchor": None, "offset": 0})
    (project / "assets" / "desktop-startup-transition.js").unlink()
    ui.open("http://127.0.0.1:8765")
    ui.window.load_url.assert_called_once_with("http://127.0.0.1:8765/")
    assert ui.remove_startup_transition is None


def test_startup_view_bridge_is_read_only_until_acknowledged_and_origin_checked():
    from scripts.desktop_app import NativeAPI
    view = {"library_id": IDENTITY, "scroll_y": 920, "anchor": "video-1", "offset": -40}
    ui = SimpleNamespace(startup_view=view, boot_origin="about:blank",
                         window=SimpleNamespace(get_current_url=lambda: "http://127.0.0.1:8765/"))
    lifecycle = SimpleNamespace(ready=True, services=SimpleNamespace(origin="http://127.0.0.1:8765"))
    api = NativeAPI(lifecycle, ui)
    assert api.get_startup_view() == view
    assert api.get_startup_view() == view
    ui.window.get_current_url = lambda: "https://unrelated.example/"
    with pytest.raises(PermissionError):
        api.get_startup_view()
    with pytest.raises(PermissionError):
        api.clear_startup_view()
    assert ui.startup_view == view
    ui.window.get_current_url = lambda: "http://127.0.0.1:8765/"
    assert api.clear_startup_view() == {"ok": True}
    assert api.get_startup_view() is None


def test_delayed_start_cannot_create_services_after_the_window_has_closed():
    from desktop_runtime import Lifecycle
    services = SimpleNamespace(start=Mock())
    ui = SimpleNamespace(destroy=Mock())
    lifecycle = Lifecycle(services, ui)
    lifecycle.destroy()
    lifecycle.start()
    services.start.assert_not_called()


def cached_cover(project, source):
    from PIL import Image
    from video_thumbnail import _thumbnail_path
    directory = project / "cache" / "thumbnails"
    path, _identity = _thumbnail_path(source, directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (1920, 1080), (220, 80, 30)).save(path, format="JPEG")
    return directory, path


def test_snapshot_reuses_existing_cover_and_startup_never_needs_source_or_decoder(project, monkeypatch):
    import video_thumbnail as thumbnails
    from PIL import Image
    source = project / "source.mp4"
    source.write_bytes(b"not a real video")
    directory, cached = cached_cover(project, source)
    record = listing()
    record["items"][1]["video"]["video_path"] = str(source)
    monkeypatch.setattr(thumbnails, "_encode_frame", Mock(side_effect=AssertionError("must not decode")))
    save_library_snapshot(project, IDENTITY, record, directory)
    cover = project / ".runtime" / "library-startup" / IDENTITY / "covers" / "1.jpg"
    with Image.open(cover) as image:
        assert image.size == (480, 270)
    assert cover.stat().st_size <= MAX_COVER_BYTES
    assert "video_path" not in json.dumps(load_library_snapshot(project, IDENTITY))
    # An unchanged cached JPEG is not resized on every list refresh.
    monkeypatch.setattr(Image, "open", Mock(side_effect=AssertionError("already prepared")))
    save_library_snapshot(project, IDENTITY, record, directory)
    source.unlink()
    cached.unlink()
    html = startup_html(project, IDENTITY)
    assert 'src="data:image/jpeg;base64,' in html
    assert str(source) not in html
    assert 'src="http' not in html and 'src="file:' not in html
    assert "data:image/jpeg" not in startup_html(project, "aaaaaaaaaaaa")
    thumbnails._encode_frame.assert_not_called()


def test_missing_or_invalid_cover_is_only_a_placeholder_and_cannot_block_startup(project, monkeypatch):
    import video_thumbnail as thumbnails
    source = project / "source.mp4"
    source.write_bytes(b"not a real video")
    record = listing()
    record["items"][1]["video"]["video_path"] = str(source)
    monkeypatch.setattr(thumbnails, "_encode_frame", Mock(side_effect=AssertionError("must not decode")))
    save_library_snapshot(project, IDENTITY, record, project / "no-cache")
    assert "data:image/jpeg" not in startup_html(project, IDENTITY)
    broken = project / "broken.jpg"
    broken.write_bytes(b"corrupt")
    save_startup_cover(project, IDENTITY, 1, broken)
    assert "data:image/jpeg" not in startup_html(project, IDENTITY)
    thumbnails._encode_frame.assert_not_called()


def test_embedded_covers_have_a_count_and_byte_limit(project):
    from PIL import Image
    source = project / "cached.jpg"
    Image.new("RGB", (480, 270), (100, 160, 90)).save(source, format="JPEG")
    record = listing()
    record["items"] = [{"type": "video", "video": {"id": index, "name": str(index)}}
                       for index in range(1, MAX_COVERS + 4)]
    save_library_snapshot(project, IDENTITY, record)
    for item in record["items"]:
        save_startup_cover(project, IDENTITY, item["video"]["id"], source)
    assert startup_html(project, IDENTITY).count('src="data:image/jpeg;base64,') == MAX_COVERS
    cover = project / ".runtime" / "library-startup" / IDENTITY / "covers" / "1.jpg"
    cover.write_bytes(b"\xff\xd8\xff" + b"X" * MAX_COVER_BYTES)
    record["items"] = record["items"][:1]
    save_library_snapshot(project, IDENTITY, record)
    assert "data:image/jpeg" not in startup_html(project, IDENTITY)


def test_normal_thumbnail_response_updates_startup_cover_even_after_snapshot_was_saved(project, monkeypatch):
    import api.main as api
    from video_store import VideoStore
    source = project / "source.mp4"
    source.write_bytes(b"not a real video")
    _directory, jpeg = cached_cover(project, source)
    database = project / "library" / "project.db"
    store = VideoStore(database)
    video_id = store.create_video("hash", source.name, str(source), "unused.wav", 3000)
    monkeypatch.setattr(api, "DIRECTORY_PROJECT_ROOT", project)
    monkeypatch.setattr(api, "load_data_root", lambda: database.parent)
    monkeypatch.setattr(api, "video_thumbnail", lambda *_args: jpeg)
    monkeypatch.setenv("CLASSROOM_DESKTOP_MODE", "1")
    with TestClient(api.create_app()) as client:
        client.get("/api/library").raise_for_status()
        identity = client.get("/api/health").json()["library_id"]
        assert "data:image/jpeg" not in startup_html(project, identity)
        response = client.get(f"/api/videos/{video_id}/thumbnail")
        assert response.status_code == 200
        assert response.content == jpeg.read_bytes()
        assert "data:image/jpeg" in startup_html(project, identity)
