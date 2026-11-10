import json
import os
from pathlib import Path
import shutil
import subprocess
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from classcue.desktop_startup import (
    MAX_ARTWORK_BYTES, MAX_MANIFEST_BYTES, load_startup_scenes,
    select_startup_scene, startup_background, startup_html, startup_transition_script,
)


@pytest.fixture
def project(tmp_path):
    assets = tmp_path / "assets"
    (assets / "startup").mkdir(parents=True)
    for name in ("desktop-startup.html", "desktop-startup-transition.js"):
        shutil.copy2(Path(__file__).parents[1] / "assets" / name, assets / name)
    scenes = [
        {"id": "first", "file": "first.jpg", "caption": "今天也有在练脑子。", "background": "#edf5fa"},
        {"id": "second", "file": "second.jpg", "caption": "重点划好了，整页都是。", "background": "#fcf0f2"},
    ]
    (assets / "startup" / "scenes.json").write_text(
        json.dumps({"version": 1, "scenes": scenes}, ensure_ascii=False), encoding="utf-8")
    for scene in scenes:
        Image.new("RGB", (90, 60), (220, 190, 80)).save(assets / "startup" / scene["file"], format="JPEG")
    return tmp_path


def test_each_new_launch_selects_one_local_scene_without_consecutive_repeats(project):
    previous = None
    for _ in range(6):
        scene = select_startup_scene(project)
        assert scene["id"] != previous
        assert scene["image"].startswith("data:image/jpeg;base64,")
        assert json.loads((project / ".runtime" / "startup-scene.json").read_text()) == {
            "version": 1, "id": scene["id"],
        }
        previous = scene["id"]


def test_bootstrap_choice_is_reused_and_one_html_document_never_rotates(project):
    scene = select_startup_scene(project, "second")
    assert scene["id"] == "second"
    html = startup_html(project, scene_id="second")
    assert 'data-scene-id="second"' in html
    assert html.count('class="startup-art"') == 1
    assert "重点划好了，整页都是。" in html
    assert "setInterval" not in html and "setTimeout" not in html
    assert startup_background(html) == "#fcf0f2"


def test_startup_reads_no_library_video_or_image_decoder_and_escapes_caption(project, monkeypatch):
    import classcue.video_store as video_store
    path = project / "assets" / "startup" / "scenes.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["scenes"][0]["caption"] = '<script>evil("name")</script>'
    path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(Image, "open", Mock(side_effect=AssertionError("No startup image decoding in Python")))
    monkeypatch.setattr(video_store, "VideoStore", Mock(side_effect=AssertionError("No library at startup")))
    html = startup_html(project, scene_id="first")
    assert "&lt;script&gt;" in html and "<script>evil" not in html
    assert 'aria-label="应用启动画面"' in html
    assert "data-library-key" not in html and "导入视频" not in html
    assert 'src="http' not in html and 'src="file:' not in html
    assert "__STARTUP_" not in html and "<!--STARTUP_" not in html


@pytest.mark.parametrize("kind", ["missing", "corrupt", "oversized", "version", "shape"])
def test_invalid_manifest_has_a_usable_brand_and_retry_fallback(project, kind):
    path = project / "assets" / "startup" / "scenes.json"
    if kind == "missing":
        path.unlink()
    elif kind == "corrupt":
        path.write_text("{bad")
    elif kind == "oversized":
        path.write_bytes(b" " * (MAX_MANIFEST_BYTES + 1))
    else:
        path.write_text(json.dumps({"version": 9 if kind == "version" else 1, "scenes": {}}))
    assert load_startup_scenes(project) == []
    html = startup_html(project)
    assert 'class="startup-art"' not in html
    assert "ClassCue AI" in html and "重试启动" in html and "打开日志目录" in html
    assert startup_background(html) == "#eef6f8"


@pytest.mark.parametrize("field,value", [
    ("id", "../escape"), ("file", "../secret.jpg"), ("file", "https://example.test/a.jpg"),
    ("background", "red;display:none"), ("caption", None),
])
def test_manifest_cannot_choose_external_files_or_inject_styles(project, field, value):
    path = project / "assets" / "startup" / "scenes.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["scenes"][0][field] = value
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert [scene["id"] for scene in load_startup_scenes(project)] == ["second"]
    assert select_startup_scene(project, "../escape")["id"] == "second"


@pytest.mark.parametrize("kind", ["missing", "corrupt", "oversized"])
def test_bad_artwork_falls_back_to_the_previous_valid_scene(project, kind):
    select_startup_scene(project, "first")
    path = project / "assets" / "startup" / "second.jpg"
    if kind == "missing":
        path.unlink()
    else:
        path.write_bytes(b"bad" if kind == "corrupt" else b"\xff\xd8\xff" + b"X" * MAX_ARTWORK_BYTES)
    assert select_startup_scene(project)["id"] == "first"
    (project / "assets" / "startup" / "first.jpg").unlink()
    assert select_startup_scene(project) is None


def test_corrupt_or_unwritable_last_scene_record_never_blocks_startup(project, monkeypatch):
    state = project / ".runtime" / "startup-scene.json"
    state.parent.mkdir()
    state.write_text("{broken")
    monkeypatch.setattr(Path, "mkdir", Mock(side_effect=OSError("read-only")))
    assert select_startup_scene(project)["image"].startswith("data:image/jpeg;base64,")


def test_desktop_library_does_not_write_the_retired_course_preview(project, monkeypatch):
    import api.main as api
    from classcue.video_store import VideoStore
    database = project / "library" / "project.db"
    store = VideoStore(database)
    store.create_video("test", "lesson.mp4", "unused.mp4", "unused.wav", 3000)
    monkeypatch.setattr(api, "DIRECTORY_PROJECT_ROOT", project)
    monkeypatch.setattr(api, "load_data_root", lambda: database.parent)
    monkeypatch.setenv("CLASSROOM_DESKTOP_MODE", "1")
    with TestClient(api.create_app()) as client:
        assert client.get("/api/library").json()["items"][0]["video"]["filename"] == "lesson.mp4"
    assert not (project / ".runtime" / "library-startup").exists()


def test_window_preserves_one_artwork_across_navigation_retry_and_early_progress(project, monkeypatch):
    from scripts import desktop_app
    monkeypatch.setattr(desktop_app, "PROJECT", project)
    monkeypatch.setenv("CLASSROOM_STARTUP_SCENE", "second")
    remove_transition = Mock()
    install = Mock(return_value=remove_transition)
    monkeypatch.setattr(desktop_app, "install_startup_transition", install)
    ui = desktop_app.WindowUI()
    html = ui.boot_html
    assert 'data-scene-id="second"' in html
    loaded = Event()
    ui.window = SimpleNamespace(events=SimpleNamespace(loaded=loaded),
                                evaluate_js=Mock(return_value=None), load_url=Mock(), load_html=Mock(),
                                get_current_url=lambda: None)
    ui.progress("Connecting")
    ui.window.evaluate_js.assert_not_called()
    loaded.set()
    ui.progress("Connected")
    ui.open("http://127.0.0.1:8765")
    ui.window.load_url.assert_called_once_with("http://127.0.0.1:8765/")
    assert ui.startup_view is None
    assert 'data-scene-id=\\"second\\"' in install.call_args.args[1]
    ui.window.get_current_url = lambda: None
    ui.navigation_loaded()
    remove_transition.assert_not_called()
    ui.window.get_current_url = lambda: ui.current_url
    ui.navigation_loaded()
    ui.navigation_loaded()
    remove_transition.assert_called_once()
    ui.error("Retry")
    ui.window.load_html.assert_called_once_with(html)
    assert ui.boot_html == html


def test_transition_is_read_only_origin_restricted_and_keeps_the_same_image(project):
    html = startup_html(project, scene_id="first")
    script = startup_transition_script(project, html, "http://127.0.0.1:8765")
    payload = json.JSONDecoder().raw_decode(script.split("const data = ", 1)[1])[0]
    assert payload["origin"] == "http://127.0.0.1:8765" and payload["view"] is None
    assert payload["background"] == "#edf5fa"
    assert 'data-scene-id="first"' in payload["html"]
    assert "<script" not in payload["html"] and "onclick" not in payload["html"]
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
            assert inside_ui
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
                                evaluate_js=lambda _script: None, get_current_url=lambda: None)
    (project / "assets" / "desktop-startup-transition.js").unlink()
    ui.open("http://127.0.0.1:8765")
    ui.window.load_url.assert_called_once_with("http://127.0.0.1:8765/")
    assert ui.remove_startup_transition is None


def test_legacy_startup_view_bridge_still_checks_origin():
    from scripts.desktop_app import NativeAPI
    view = {"library_id": "012345abcdef", "scroll_y": 920, "anchor": "video-1", "offset": -40}
    ui = SimpleNamespace(startup_view=view, boot_origin="about:blank",
                         window=SimpleNamespace(get_current_url=lambda: "http://127.0.0.1:8765/"))
    lifecycle = SimpleNamespace(ready=True, services=SimpleNamespace(origin="http://127.0.0.1:8765"))
    api = NativeAPI(lifecycle, ui)
    assert api.get_startup_view() == view
    ui.window.get_current_url = lambda: "https://unrelated.example/"
    with pytest.raises(PermissionError):
        api.clear_startup_view()
    assert ui.startup_view == view
    ui.window.get_current_url = lambda: "http://127.0.0.1:8765/"
    assert api.clear_startup_view() == {"ok": True} and api.get_startup_view() is None


def test_delayed_start_cannot_create_services_after_the_window_has_closed():
    from classcue.desktop_runtime import Lifecycle
    services = SimpleNamespace(start=Mock())
    lifecycle = Lifecycle(services, SimpleNamespace(destroy=Mock()))
    lifecycle.destroy()
    lifecycle.start()
    services.start.assert_not_called()


@pytest.mark.parametrize("actual_url", ["http://127.0.0.1:8765/", "http://127.0.0.1:8765/review/1"])
def test_retry_reloads_the_actual_root_page_and_finishes_startup(project, monkeypatch, actual_url):
    from scripts import desktop_app
    from classcue.desktop_runtime import Lifecycle
    monkeypatch.setattr(desktop_app, "PROJECT", project)
    monkeypatch.setattr(desktop_app, "install_startup_transition", Mock(return_value=Mock()))
    reload_window = Mock()
    monkeypatch.setattr(desktop_app, "reload_window", reload_window)
    loaded = Event()
    loaded.set()
    ui = desktop_app.WindowUI()
    # Cached state is intentionally different: navigation must use the real URL.
    ui.current_url = "http://127.0.0.1:8765/stale"
    ui.window = SimpleNamespace(events=SimpleNamespace(loaded=loaded), evaluate_js=Mock(return_value=None),
                                load_url=Mock(), get_current_url=lambda: actual_url)
    services = SimpleNamespace(start=Mock(return_value="http://127.0.0.1:8765"))
    lifecycle = Lifecycle(services, ui)
    lifecycle.ready = True
    lifecycle.start()
    assert lifecycle.ready and not lifecycle.starting
    assert ui.current_url == "http://127.0.0.1:8765/"
    if actual_url.endswith("/review/1"):
        ui.window.load_url.assert_called_once_with(ui.current_url)
        reload_window.assert_not_called()
    else:
        ui.window.load_url.assert_not_called()
        reload_window.assert_called_once_with(ui.window)


def test_blocked_page_script_cannot_keep_retry_starting_or_accumulate_workers(project, monkeypatch):
    from scripts import desktop_app
    from classcue.desktop_runtime import Lifecycle
    monkeypatch.setattr(desktop_app, "PROJECT", project)
    monkeypatch.setattr(desktop_app, "PAGE_SCRIPT_TIMEOUT_SECONDS", .01)
    monkeypatch.setattr(desktop_app, "install_startup_transition", Mock(return_value=None))
    reload_window = Mock()
    monkeypatch.setattr(desktop_app, "reload_window", reload_window)
    entered, release, finished, loaded = Event(), Event(), Event(), Event()
    loaded.set()
    ui = desktop_app.WindowUI()
    calls = []

    def stalled_script(expression):
        calls.append(expression)
        entered.set()
        try:
            assert release.wait(5)
            return {"scroll_y": 123}
        finally:
            finished.set()

    ui.window = SimpleNamespace(events=SimpleNamespace(loaded=loaded), evaluate_js=stalled_script,
                                get_current_url=lambda: "http://127.0.0.1:8765/", load_url=Mock())
    lifecycle = Lifecycle(SimpleNamespace(start=lambda _action: "http://127.0.0.1:8765"), ui)
    try:
        lifecycle.start()
        assert entered.is_set() and not finished.is_set()
        assert lifecycle.ready and not lifecycle.starting
        assert ui.startup_view is None
        lifecycle.start()
        assert len(calls) == 1
        assert reload_window.call_count == 2
    finally:
        release.set()
        assert finished.wait(3)
    # A stale snapshot returning after navigation must not overwrite the fallback.
    assert ui.startup_view is None


@pytest.mark.parametrize("cancel", [False, True])
def test_stalled_snapshot_replays_latest_dialog_and_media_pause_without_extra_workers(project, monkeypatch, cancel):
    from scripts import desktop_app
    from classcue.desktop_runtime import Lifecycle
    monkeypatch.setattr(desktop_app, "PROJECT", project)
    monkeypatch.setattr(desktop_app, "PAGE_SCRIPT_TIMEOUT_SECONDS", .01)
    ui = desktop_app.WindowUI()
    entered, release, drained, loaded = Event(), Event(), Event(), Event()
    loaded.set()
    calls, workers = [], []
    original_background = desktop_app.background

    def track_worker(function):
        thread = original_background(function)
        workers.append(thread)
        return thread

    def evaluate(expression):
        calls.append(expression)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
        elif "classroom-desktop-dialog" in expression:
            drained.set()

    monkeypatch.setattr(desktop_app, "background", track_worker)
    ui.window = SimpleNamespace(
        events=SimpleNamespace(loaded=loaded), evaluate_js=evaluate, hide=Mock(), show=Mock(), restore=Mock(),
        get_current_url=lambda: "http://127.0.0.1:8765/",
    )
    lifecycle = Lifecycle(SimpleNamespace(origin="http://127.0.0.1:8765"), ui)
    lifecycle.ready = lifecycle.unsaved_changes = True
    try:
        assert ui.script("optional snapshot") is None and entered.is_set()
        ui.hide()
        for index in range(20):
            ui.progress(f"step {index}")
        lifecycle.close_requested()
        assert lifecycle.dialog["kind"] == "unsaved-close"
        if cancel:
            api = desktop_app.NativeAPI(lifecycle, ui)
            assert api.respond("cancel") == {"ok": True}
            assert lifecycle.dialog is None
        assert len(workers) == 1 and len(calls) == 1 and len(ui.script_pending) == 3
        release.set()
        assert drained.wait(3)
        workers[0].join(3)
        assert not workers[0].is_alive()
        assert len(calls) == 4
        assert "media.pause()" in calls[1]
        assert "step 19" in calls[2]
        if cancel:
            assert "detail:null" in calls[3]
        else:
            assert '"kind": "unsaved-close"' in calls[3]
        assert not ui.script_running and not ui.script_pending
    finally:
        release.set()
        for thread in workers:
            thread.join(3)


def test_same_url_reload_uses_the_native_ui_thread_without_page_scripts(monkeypatch):
    import sys
    from scripts.desktop_transition import reload_window
    inside_ui = False
    calls = []

    class Delegate:
        def __class_getitem__(cls, _value):
            return lambda callback: callback

    class WebView:
        def Reload(self):
            assert inside_ui
            calls.append("reload")

    def invoke(callback):
        nonlocal inside_ui
        inside_ui = True
        try:
            return callback()
        finally:
            inside_ui = False

    monkeypatch.setitem(sys.modules, "System", SimpleNamespace(Func=Delegate, Object=object))
    window = SimpleNamespace(native=SimpleNamespace(browser=SimpleNamespace(webview=SimpleNamespace(
        CoreWebView2=WebView())), Invoke=invoke))
    reload_window(window)
    assert calls == ["reload"]


def test_startup_and_transition_dialogs_close_without_errors_in_a_real_browser(project):
    source = Path(__file__).resolve().parents[1]
    node = shutil.which("node")
    playwright = os.getenv("CLASSROOM_PLAYWRIGHT_MODULE") or os.getenv("PLAYWRIGHT_MODULE") or str(
        source / "frontend/node_modules/playwright")
    if not node or not Path(playwright).exists():
        pytest.skip("Node and installed Playwright are required")
    html = startup_html(project, scene_id="first")
    payload = project / "browser.json"
    payload.write_text(json.dumps({
        "html": html, "transition": startup_transition_script(project, html, "http://127.0.0.1:8765"),
    }), encoding="utf-8")
    result = subprocess.run([node, str(source / "tests/desktop_startup_browser.cjs"), str(payload)],
                            env={**os.environ, "CLASSROOM_PLAYWRIGHT_MODULE": playwright},
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {"passed": True, "surfaces": 2}
