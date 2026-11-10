"""Connection and desktop IPC boundaries; synthetic local files only."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from classcue import course_connection as connection
from classcue.desktop_runtime import SingleInstance, http_json


def test_registration_preserves_workspaces_and_other_fields(tmp_path):
    path = tmp_path / "connection.json"
    path.write_text(json.dumps({"format": "classcue-codex-connection", "version": 1,
                                "roots": [str(tmp_path)], "other": "preserved"}))
    connection.register_application(path, tmp_path)
    value = json.loads(path.read_text())
    assert value["other"] == "preserved" and value["roots"] == [str(tmp_path)]
    assert Path(value["application"]["project_root"]) == tmp_path
    assert Path(value["application"]["python"]).is_absolute()
    first = connection.connection_token(tmp_path)
    assert first == connection.connection_token(tmp_path)
    assert connection.authenticated(f"Bearer {first}", first)
    assert not connection.authenticated("Bearer wrong", first)


def test_navigation_ipc_rejects_nonlocal_and_unexpected_paths(tmp_path):
    called = []
    instance = SingleInstance(tmp_path, lambda payload: called.append(payload) or {"ok": True})
    assert instance.acquire()
    try:
        for url in ["https://example.test/review/1", "http://127.0.0.1:1234/settings",
                    "http://user@127.0.0.1/review/1?start_ms=0&library_id=a"]:
            with pytest.raises(RuntimeError):
                instance.send({"action": "navigate", "url": url}, attempts=1)
        assert not called
        command = {"action": "navigate", "url": "http://127.0.0.1:1234/review/1?start_ms=5000&library_id=a"}
        assert instance.send(command)["ok"]
        assert called == [command]
    finally:
        instance.close()


def test_service_reuses_verified_owned_identity_without_starting(monkeypatch, tmp_path):
    from scripts import start_frontend
    import classcue.app_storage as storage
    import classcue.asr_storage as asr_storage
    monkeypatch.setattr(storage, "load_data_root", lambda: tmp_path)
    monkeypatch.setattr(asr_storage, "library_identity", lambda *_: "synthetic")
    monkeypatch.setattr(start_frontend, "read_state", lambda: {"api_port": 9988, "instance_id": "instance"})
    monkeypatch.setattr(connection, "http_json", lambda *_a, **_k: {
        "service": "classroom-video-study", "library_id": "synthetic", "instance_id": "instance"})
    monkeypatch.setattr(start_frontend, "main", lambda *_: pytest.fail("Must reuse"))
    assert connection.ensure_service() == ("http://127.0.0.1:9988", "synthetic")


def test_service_rejects_wrong_library_and_instance(monkeypatch, tmp_path):
    from scripts import start_frontend
    import classcue.app_storage as storage
    import classcue.asr_storage as asr_storage
    monkeypatch.setattr(storage, "load_data_root", lambda: tmp_path)
    monkeypatch.setattr(asr_storage, "library_identity", lambda *_: "synthetic")
    monkeypatch.setattr(start_frontend, "read_state", lambda: {"api_port": 9988, "instance_id": "instance"})
    monkeypatch.setattr(start_frontend, "main", lambda *_: pytest.fail("Must not replace"))
    for library, instance in [("other", "instance"), ("synthetic", "other")]:
        monkeypatch.setattr(connection, "http_json", lambda *_a, **_k: {
            "service": "classroom-video-study", "library_id": library, "instance_id": instance})
        with pytest.raises(RuntimeError):
            connection.ensure_service()


def test_service_starts_once_without_showing_window(monkeypatch, tmp_path):
    from scripts import start_frontend
    import classcue.app_storage as storage
    import classcue.asr_storage as asr_storage
    monkeypatch.setattr(storage, "load_data_root", lambda: tmp_path)
    monkeypatch.setattr(asr_storage, "library_identity", lambda *_: "synthetic")
    state, starts = {}, []
    monkeypatch.setattr(start_frontend, "read_state", lambda: state)
    monkeypatch.setattr(connection, "connection_token", lambda: "unused")
    monkeypatch.setenv("CLASSROOM_DESKTOP_MODE", "0")
    monkeypatch.setenv("CLASSROOM_APP_NO_BROWSER", "0")
    def start(action):
        import os
        assert os.environ["CLASSROOM_DESKTOP_MODE"] == "1" and os.environ["CLASSROOM_APP_NO_BROWSER"] == "1"
        starts.append(action)
        state.update(api_port=9988, instance_id="instance")
        return 0
    monkeypatch.setattr(start_frontend, "main", start)
    monkeypatch.setattr(connection, "http_json", lambda *_a, **_k: {
        "service": "classroom-video-study", "library_id": "synthetic", "instance_id": "instance"})
    assert connection.ensure_service() == ("http://127.0.0.1:9988", "synthetic")
    assert starts == ["start"]


def test_navigation_waits_until_initial_page_and_lifecycle_are_ready(monkeypatch, tmp_path):
    states = iter([
        {"ready": True, "starting": True, "window_ready": False},
        {"ready": True, "starting": False, "window_ready": False},
        {"ready": True, "starting": False, "window_ready": True},
    ])
    calls = []
    class Instance:
        def __init__(self, *_):
            pass
        def send(self, command, **_):
            calls.append(command)
            return next(states) if command == "status" else {"ok": True}
    monkeypatch.setattr(connection, "SingleInstance", Instance)
    monkeypatch.setattr(connection.time, "sleep", lambda _: None)
    result = connection.navigate_desktop("http://127.0.0.1:9999/review/1?start_ms=5000&library_id=a",
                                         project=tmp_path)
    assert result["navigation"] == "requested"
    assert calls[:3] == ["status", "status", "status"]
    assert calls[3]["action"] == "navigate"


def test_desktop_navigation_never_queues_another_video_behind_a_pending_open(tmp_path, monkeypatch):
    from filelock import FileLock
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    monkeypatch.setattr(connection, "SingleInstance", lambda *_: pytest.fail("must not launch or navigate"))
    with FileLock(str(runtime / "desktop_navigation.lock")):
        with pytest.raises(RuntimeError, match="另一段回放正在打开"):
            connection.navigate_desktop("http://127.0.0.1:9999/review/1?start_ms=0&library_id=a",
                                        project=tmp_path)


def test_closed_desktop_starts_the_bound_installation_before_loading_the_review(tmp_path, monkeypatch):
    commands, launched = [], []

    class Instance:
        def __init__(self, runtime, _dispatch):
            assert runtime == tmp_path / ".runtime"

        def send(self, command, **_kwargs):
            commands.append(command)
            if commands == ["status"]:
                raise RuntimeError("synthetic closed desktop")
            return {"ready": True, "window_ready": True} if command == "status" else {"ok": True}

    monkeypatch.setattr(connection, "SingleInstance", Instance)
    monkeypatch.setattr(connection.subprocess, "Popen", lambda args, **kwargs: launched.append((args, kwargs)))
    monkeypatch.setattr(connection.time, "sleep", lambda _: None)
    url = "http://127.0.0.1:9999/open/review/1?start_ms=12345&library_id=a"
    result = connection.navigate_desktop(url, project=tmp_path)
    assert len(launched) == 1
    assert launched[0][0] == [connection.sys.executable, str(tmp_path / "scripts/desktop_app.py")]
    assert launched[0][1]["cwd"] == tmp_path
    assert commands[-1] == {
        "action": "navigate", "url": "http://127.0.0.1:9999/review/1?start_ms=12345&library_id=a",
    }
    assert result["navigation"] == "requested" and result["play_url"] == url


@pytest.mark.parametrize("state", ["unsaved_changes", "starting", "exiting", "dialog"])
def test_window_navigation_rechecks_current_lifecycle_before_loading(state):
    from scripts.desktop_app import navigate_review
    loaded = []
    lifecycle = SimpleNamespace(ready=True, unsaved_changes=False, starting=False, exiting=False,
                                dialog=None, services=SimpleNamespace(origin="http://127.0.0.1:9999"),
                                show=lambda: None)
    ui = SimpleNamespace(library_id="synthetic", current_url="/settings",
                         window=SimpleNamespace(load_url=loaded.append,
                                                events=SimpleNamespace(loaded=SimpleNamespace(is_set=lambda: True))))
    setattr(lifecycle, state, True)
    assert not navigate_review(
        lifecycle, ui, "http://127.0.0.1:9999/review/1?start_ms=0&library_id=synthetic")["ok"]
    assert loaded == [] and ui.current_url == "/settings"


def test_window_navigation_failure_is_not_acknowledged_as_success():
    from scripts.desktop_app import navigate_review
    def fail(_url):
        raise RuntimeError("synthetic unavailable window")
    lifecycle = SimpleNamespace(ready=True, unsaved_changes=False, starting=False, exiting=False,
                                dialog=None, services=SimpleNamespace(origin="http://127.0.0.1:9999"),
                                show=lambda: None)
    ui = SimpleNamespace(library_id="synthetic", current_url="/settings",
                         window=SimpleNamespace(load_url=fail, get_current_url=lambda: "/settings",
                                                events=SimpleNamespace(loaded=SimpleNamespace(is_set=lambda: True))))
    assert not navigate_review(
        lifecycle, ui, "http://127.0.0.1:9999/review/1?start_ms=0&library_id=synthetic")["ok"]
    assert ui.current_url == "/settings"


@pytest.mark.parametrize("same_url", [False, True])
def test_window_navigation_reloads_identical_actual_url(same_url, monkeypatch):
    from scripts.desktop_app import navigate_review
    from scripts import desktop_app
    url = "http://127.0.0.1:9999/review/1?start_ms=12000&library_id=synthetic"
    loaded, reloaded, shown = [], [], []
    monkeypatch.setattr(desktop_app, "reload_window", lambda window: reloaded.append(window))
    lifecycle = SimpleNamespace(ready=True, unsaved_changes=False, starting=False, exiting=False,
                                dialog=None, services=SimpleNamespace(origin="http://127.0.0.1:9999"),
                                show=lambda: shown.append(True))
    ui = SimpleNamespace(library_id="synthetic", current_url="/stale",
                         window=SimpleNamespace(load_url=loaded.append,
                                                get_current_url=lambda: url if same_url else "/settings",
                                                events=SimpleNamespace(loaded=SimpleNamespace(is_set=lambda: True))))
    assert navigate_review(lifecycle, ui, url)["ok"]
    assert loaded == ([] if same_url else [url])
    assert reloaded == ([ui.window] if same_url else [])
    assert ui.current_url == url and shown == [True]
