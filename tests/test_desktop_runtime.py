import json
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from classcue.desktop_runtime import Lifecycle, Services, SingleInstance, http_json
from classcue.desktop_windows import normalize_hotkey
from classcue.frontend_host import mount_frontend


class UI:
    def __init__(self):
        self.events = []

    def __getattr__(self, name):
        return lambda *args: self.events.append((name, args))


@pytest.fixture
def lifecycle():
    calls = []
    state = {"processing": False, "active_job_id": None}
    services = SimpleNamespace(
        status=lambda: dict(state), start=lambda _action: "http://127.0.0.1:8765",
        stop=lambda **options: calls.append(options),
    )
    controller = Lifecycle(services, UI())
    controller.ready = True
    controller.tray_available = True
    return controller, state, calls


def test_idle_close_stops_services_before_window_is_destroyed(lifecycle):
    controller, _state, calls = lifecycle
    controller.close_requested()
    assert controller.dialog["kind"] == "close-choice" and not calls
    controller.respond("exit")
    assert calls and not calls[0]["cancel"]
    assert controller.allow_destroy and controller.closed.is_set()
    assert controller.ui.events[-1][0] == "destroy"


@pytest.fixture
def exit_timers(monkeypatch):
    timers = []

    class ControlledTimer:
        def __init__(self, interval, callback):
            self.interval, self.callback = interval, callback
            self.started = self.cancelled = False
            timers.append(self)

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

        def fire(self):
            # Also simulate a callback already queued when cancel() runs.
            self.callback()

    monkeypatch.setattr("classcue.desktop_runtime.Timer", ControlledTimer)
    return timers


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("tray", [False, True])
def test_pending_exit_minimizes_after_half_second_without_interrupting_cleanup(
    lifecycle, exit_timers, cancel, tray,
):
    controller, _state, calls = lifecycle
    controller.tray_available = tray

    def stop(**options):
        timer = exit_timers[0]
        assert timer.started and timer.daemon and timer.interval == 0.5
        assert not any(name == "minimize" for name, _args in controller.ui.events)
        options["progress"]("saving")
        timer.fire()
        assert controller.ui.events[-1] == ("minimize", ())
        assert controller.exiting and not controller.allow_destroy
        assert not controller.hidden
        calls.append(options)

    controller.services.stop = stop
    controller.exit(cancel=cancel)
    assert len(calls) == 1 and calls[0]["cancel"] is cancel
    assert controller.allow_destroy and controller.closed.is_set()
    assert exit_timers[0].cancelled
    assert controller.ui.events[-1] == ("destroy", ())
    exit_timers[0].fire()
    assert controller.ui.events[-1] == ("destroy", ())


def test_fast_exit_cancels_minimize_even_if_callback_is_already_queued(lifecycle, exit_timers):
    controller, _state, _calls = lifecycle
    controller.exit()
    assert exit_timers[0].cancelled
    exit_timers[0].fire()
    assert not any(name == "minimize" for name, _args in controller.ui.events)


def test_failed_exit_restores_window_and_old_timer_cannot_minimize_retry(lifecycle, exit_timers):
    controller, _state, _calls = lifecycle

    def fail(**_options):
        exit_timers[0].fire()
        raise RuntimeError("cleanup failed")

    controller.services.stop = fail
    controller.exit()
    assert controller.ui.events[-2][0] == "show"
    assert controller.ui.events[-1][0] == "dialog"
    assert controller.dialog["kind"] == "service-error"
    assert not controller.exiting and not controller.allow_destroy
    assert exit_timers[0].cancelled

    def retry(**_options):
        events = list(controller.ui.events)
        exit_timers[0].fire()
        assert controller.ui.events == events

    controller.services.stop = retry
    controller.respond("retry-close")
    assert controller.allow_destroy and len(exit_timers) == 2


def test_minimize_failure_does_not_abort_safe_exit(lifecycle, exit_timers):
    controller, _state, _calls = lifecycle
    controller.ui.minimize = lambda: (_ for _ in ()).throw(RuntimeError("window unavailable"))
    controller.services.stop = lambda **_options: exit_timers[0].fire()
    controller.exit()
    assert controller.allow_destroy and controller.closed.is_set()


def test_native_exit_minimizes_to_taskbar_without_hiding_window():
    from scripts.desktop_app import WindowUI
    ui = WindowUI.__new__(WindowUI)
    events = []
    ui.window = SimpleNamespace(
        minimize=lambda: events.append("minimize"),
        hide=lambda: pytest.fail("Exit must not require a tray icon"),
    )
    ui.minimize()
    assert events == ["minimize"]


def test_busy_close_cancel_or_background_never_stops_task(lifecycle):
    controller, state, calls = lifecycle
    state["processing"] = True
    controller.close_requested()
    assert controller.dialog["kind"] == "close-choice"
    assert not calls
    controller.respond("cancel")
    assert controller.dialog is None and not controller.hidden
    controller.close_requested()
    controller.respond("background")
    assert controller.hidden and not calls and not controller.allow_destroy
    controller.show()
    assert not controller.hidden


def test_tray_exit_requires_confirmation_and_stops_active_task(lifecycle):
    controller, state, calls = lifecycle
    state["processing"] = True
    controller.close_requested(exit_app=True)
    assert controller.dialog["kind"] == "exit-task"
    controller.respond("stop-exit")
    assert calls[0]["cancel"]
    assert controller.allow_destroy


def test_tray_failure_does_not_allow_invisible_running_application(lifecycle):
    controller, state, calls = lifecycle
    state["processing"] = True
    controller.tray_available = False
    controller.close_requested()
    with pytest.raises(RuntimeError, match="托盘"):
        controller.respond("background")
    assert not controller.hidden and not calls


def test_service_uncertainty_or_stop_failure_keeps_window_and_processes(lifecycle):
    controller, _state, _calls = lifecycle
    controller.services.status = lambda: (_ for _ in ()).throw(OSError("offline"))
    controller.close_requested(exit_app=True)
    assert controller.dialog["kind"] == "service-error"
    assert not controller.allow_destroy
    controller.services.status = lambda: {"processing": False}
    controller.services.stop = lambda **_args: (_ for _ in ()).throw(RuntimeError("busy"))
    controller.respond("retry-close")
    assert not controller.exiting and not controller.allow_destroy


def test_startup_error_allows_retry_and_does_not_create_second_service(lifecycle):
    controller, _state, _calls = lifecycle
    controller.services.start = lambda _action: (_ for _ in ()).throw(RuntimeError("not ready"))
    controller.start()
    assert not controller.starting and not controller.ready
    controller.services.start = lambda _action: "http://127.0.0.1:8765"
    controller.start()
    assert controller.ready and controller.ui.events[-1] == ("open", ("http://127.0.0.1:8765",))
    controller.starting = True
    controller.close_requested()
    assert controller.dialog["kind"] == "starting" and not controller.allow_destroy


def test_single_instance_authenticates_commands_and_keeps_primary_record(tmp_path):
    received = []
    primary = SingleInstance(tmp_path, lambda action: received.append(action) or {"action": action})
    secondary = SingleInstance(tmp_path, lambda _action: pytest.fail("secondary must not dispatch"))
    try:
        assert primary.acquire()
        assert not secondary.acquire()
        assert secondary.send("activate", attempts=1) == {"action": "activate"}
        assert received == ["activate"]
        state = json.loads(primary.path.read_text())
        with pytest.raises(HTTPError) as error:
            http_json(
                f"http://127.0.0.1:{state['port']}/command", method="POST",
                payload={"action": "exit"}, token="wrong",
            )
        assert error.value.code == 403 and received == ["activate"]
        secondary.close()
        assert primary.path.exists()
    finally:
        primary.close()
        secondary.close()
    assert not primary.path.exists()


@pytest.mark.parametrize("source,expected", [
    ("", ""), ("ctrl + alt + k", "CTRL+ALT+K"), ("Ctrl+Shift+7", "CTRL+SHIFT+7"),
])
def test_hotkey_normalization(source, expected):
    assert normalize_hotkey(source) == expected


@pytest.mark.parametrize("value", ["Alt+F4", "Win+R", "Ctrl+Alt+Delete", "CTRL+ALT+K;evil", "hello"])
def test_unsupported_hotkey_is_rejected_before_touching_shortcuts(value):
    with pytest.raises(ValueError):
        normalize_hotkey(value)


def test_static_frontend_has_spa_fallback_without_masking_api_errors_or_leaking_files(tmp_path):
    root = tmp_path / "frontend" / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<html>classroom-app</html>")
    (root / "assets" / "app.js").write_text("console.log('app')")
    (tmp_path / "secret.txt").write_text("private")
    app = FastAPI()
    @app.get("/api/health")
    def health():
        return {"status": "ok"}
    @app.post("/api/post-only")
    def post_only():
        return {}
    mount_frontend(app, tmp_path)
    with TestClient(app) as client:
        assert "classroom-app" in client.get("/").text
        assert "classroom-app" in client.get("/review/1").text
        assert client.get("/settings?section=ai").headers["cache-control"] == "no-store"
        assert client.get("/assets/app.js").status_code == 200
        assert client.get("/assets/missing.js").status_code == 404
        assert client.get("/api/missing").status_code == 404
        assert client.get("/api/post-only").status_code == 405
        assert client.get("/api/post-only").headers["allow"] == "POST"
        assert client.get("/__classroom/missing").status_code == 404
        assert client.get("/%2E%2E/%2E%2E/secret.txt").status_code == 404
        assert client.get("/api/health").json() == {"status": "ok"}
        assert client.get("/__classroom/frontend-identity").json()["mode"] == "static"


def test_shutdown_cancels_job_and_waits_for_idle_before_owned_launcher_stops(tmp_path, monkeypatch):
    import classcue.desktop_runtime as desktop_runtime
    from scripts import start_frontend
    service = Services(tmp_path)
    service.origin = "http://127.0.0.1:8765"
    states = iter([
        {"processing": True, "active_job_id": "job"},
        {"processing": True, "active_job_id": "job"},
        {"processing": False},
    ])
    events = []
    service.status = lambda: next(states)
    monkeypatch.setattr(desktop_runtime, "http_json", lambda url, **kwargs: events.append(("cancel", url)))
    monkeypatch.setattr(desktop_runtime.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(start_frontend, "main", lambda action: events.append(("launcher", action)) or 0)
    service.stop(cancel=True)
    assert events == [
        ("cancel", "http://127.0.0.1:8765/api/jobs/job/cancel"), ("launcher", "stop"),
    ]


def test_desktop_start_uses_launcher_resolved_port(tmp_path, monkeypatch):
    from scripts import start_frontend

    calls = []
    monkeypatch.setattr(start_frontend, "main", lambda action: calls.append(action) or 0)
    monkeypatch.setattr(start_frontend, "read_state", lambda: {
        "api_port": 18765, "instance_id": "fallback-instance",
    })
    service = Services(tmp_path)
    assert service.start() == "http://127.0.0.1:18765"
    assert service.instance_id == "fallback-instance"
    assert calls == ["start"]


def test_busy_shutdown_without_explicit_cancel_never_stops_service(tmp_path, monkeypatch):
    from scripts import start_frontend
    service = Services(tmp_path)
    service.status = lambda: {"processing": True}
    monkeypatch.setattr(start_frontend, "main", lambda _action: pytest.fail("Must not stop a running task"))
    with pytest.raises(RuntimeError):
        service.stop()


def test_migration_status_waits_for_owned_launcher_identity_commit(tmp_path, monkeypatch):
    import classcue.desktop_runtime as desktop_runtime
    service = Services(tmp_path)
    service.origin = "http://127.0.0.1:8765"
    records = iter([
        {"library_id": "same", "instance_id": "old"},
        {"library_id": "same", "instance_id": "new"},
    ])
    monkeypatch.setattr(desktop_runtime, "read_json", lambda *_args: next(records))
    monkeypatch.setattr(desktop_runtime.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(desktop_runtime, "http_json", lambda url: (
        {"service": "classroom-video-study", "library_id": "same", "instance_id": "new"}
        if url.endswith("/health") else {"processing": True, "active_job_id": "task"}
    ))
    assert service.status()["processing"]
    assert service.instance_id == "new"


def test_dead_api_can_exit_using_verified_launcher_cleanup(lifecycle):
    controller, _state, calls = lifecycle
    controller.services.status = lambda: (_ for _ in ()).throw(OSError("connection refused"))
    controller.services.can_close_offline = lambda: True
    controller.close_requested(exit_app=True)
    assert calls and controller.allow_destroy


def test_real_refused_connection_exits_through_verified_launcher(tmp_path, monkeypatch):
    import socket
    from scripts import start_frontend
    # Reserve a non-listening loopback socket, without touching the user's API.
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        service = Services(tmp_path)
        service.origin = f"http://127.0.0.1:{reserved.getsockname()[1]}"
        assert service.can_close_offline()
        calls = []
        monkeypatch.setattr(start_frontend, "main", lambda action: calls.append(action) or 0)
        controller = Lifecycle(service, UI())
        controller.ready = True
        controller.close_requested(exit_app=True)
        assert calls == ["stop"]
        assert controller.allow_destroy and controller.closed.is_set()
        assert controller.dialog is None


def test_unknown_listening_server_is_not_treated_as_stopped(tmp_path):
    import socket
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        service = Services(tmp_path)
        service.origin = f"http://127.0.0.1:{server.getsockname()[1]}"
        assert not service.can_close_offline()


def test_network_timeout_does_not_authorize_offline_shutdown(tmp_path, monkeypatch):
    import classcue.desktop_runtime as desktop_runtime
    monkeypatch.setattr(desktop_runtime.socket, "create_connection", lambda *_a, **_kw:
                        (_ for _ in ()).throw(TimeoutError("unreachable")))
    service = Services(tmp_path)
    service.origin = "http://127.0.0.1:8765"
    assert not service.can_close_offline()


def test_loopback_ipc_ignores_system_proxy(tmp_path, monkeypatch):
    import urllib.request
    # Simulate a VPN/system proxy even for loopback, with bypass disabled.
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"http": "http://127.0.0.1:1"})
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda _host: False)
    primary = SingleInstance(tmp_path, lambda action: {"action": action})
    try:
        assert primary.acquire()
        assert primary.send("status", attempts=1) == {"action": "status"}
    finally:
        primary.close()


@pytest.mark.parametrize("phase", ["cancel", "poll"])
def test_backend_death_during_cancel_still_finishes_owned_cleanup(tmp_path, monkeypatch, phase):
    import classcue.desktop_runtime as desktop_runtime
    from scripts import start_frontend
    service = Services(tmp_path)
    service.origin = "http://127.0.0.1:8765"
    states = iter([{"processing": True, "active_job_id": "job"}, OSError("refused")])
    def status():
        state = next(states)
        if isinstance(state, Exception): raise state
        return state
    service.status = status
    service.can_close_offline = lambda: True
    def cancel(*_args, **_kwargs):
        if phase == "cancel": raise OSError("refused")
    monkeypatch.setattr(desktop_runtime, "http_json", cancel)
    calls = []
    monkeypatch.setattr(start_frontend, "main", lambda action: calls.append(action) or 0)
    service.stop(cancel=True)
    assert calls == ["stop"]


def test_only_explicit_service_error_escape_closes_window_without_stopping_tasks(lifecycle):
    controller, state, calls = lifecycle
    state["processing"] = True
    controller.close_requested()
    with pytest.raises(ValueError):
        controller.respond("close-window")
    controller.services.status = lambda: (_ for _ in ()).throw(OSError("offline"))
    controller.close_requested(exit_app=True)
    assert controller.dialog["kind"] == "service-error"
    controller.respond("close-window")
    assert controller.allow_destroy and controller.closed.is_set()
    assert calls == [] and state["processing"]


@pytest.mark.parametrize("processing", [False, True])
def test_remembered_background_hides_without_stopping_tasks(lifecycle, processing):
    controller, state, calls = lifecycle
    state["processing"] = processing
    saved = []
    controller.save_close_behavior = saved.append
    controller.close_requested()
    controller.respond("background", remember=True)
    assert saved == ["background"] and controller.hidden and not calls
    controller.show()
    controller.close_requested()
    assert controller.hidden and controller.dialog is None and not calls


def test_remembered_exit_still_confirms_active_task(lifecycle):
    controller, state, calls = lifecycle
    state["processing"] = True
    saved = []
    controller.save_close_behavior = saved.append
    controller.close_requested()
    controller.respond("exit", remember=True)
    assert saved == ["exit"] and controller.dialog["kind"] == "exit-task" and not calls
    controller.respond("cancel")
    controller.close_requested()
    assert controller.dialog["kind"] == "exit-task" and not calls
    controller.respond("stop-exit")
    assert calls[0]["cancel"]


def test_remembered_background_falls_back_to_question_without_tray(lifecycle):
    controller, _state, calls = lifecycle
    controller.set_close_behavior("background")
    controller.tray_available = False
    controller.close_requested()
    assert controller.dialog["kind"] == "close-choice"
    assert not controller.hidden and not calls


def test_preference_write_failure_keeps_window_visible(lifecycle):
    controller, _state, calls = lifecycle
    controller.save_close_behavior = lambda _value: (_ for _ in ()).throw(OSError("disk full"))
    controller.close_requested()
    with pytest.raises(OSError):
        controller.respond("background", remember=True)
    assert controller.close_behavior == "ask" and controller.dialog["kind"] == "close-choice"
    assert not controller.hidden and not calls


def test_close_preference_persists_without_overwriting_hotkey(tmp_path, monkeypatch):
    import classcue.desktop_windows as desktop_windows
    monkeypatch.setattr(desktop_windows, "application_config_directory", lambda _project: tmp_path)
    path = tmp_path / "desktop_settings.json"
    path.write_text(json.dumps({"hotkey": "CTRL+ALT+K", "shortcuts_installed": True}))
    desktop_windows.save_close_behavior(tmp_path, "background")
    stored = desktop_windows.preferences(tmp_path)
    assert stored["hotkey"] == "CTRL+ALT+K" and stored["close_behavior"] == "background"
    controller = Lifecycle(SimpleNamespace(), UI(), close_behavior=stored["close_behavior"])
    assert controller.close_behavior == "background"
    desktop_windows.save_close_behavior(tmp_path, "ask")
    assert desktop_windows.preferences(tmp_path)["close_behavior"] == "ask"
    with pytest.raises(ValueError):
        desktop_windows.save_close_behavior(tmp_path, "unknown")


@pytest.mark.parametrize("behavior", ["ask", "background", "exit"])
def test_unsaved_settings_guard_precedes_every_remembered_close_behavior(lifecycle, behavior):
    controller, _state, calls = lifecycle
    controller.close_behavior = behavior
    controller.unsaved_changes = True
    controller.close_requested()
    assert controller.dialog["kind"] == "unsaved-close"
    assert not controller.hidden and not controller.allow_destroy and not calls
    controller.respond("cancel")
    assert controller.unsaved_changes and not calls
    controller.close_requested()
    controller.respond("continue-close")
    assert not controller.unsaved_changes
    if behavior == "ask": assert controller.dialog["kind"] == "close-choice"
    elif behavior == "background": assert controller.hidden and not calls
    else: assert controller.allow_destroy and calls


def test_unsaved_tray_exit_keeps_task_confirmation(lifecycle):
    controller, state, calls = lifecycle
    state["processing"] = True
    controller.unsaved_changes = True
    controller.close_requested(exit_app=True)
    assert controller.dialog["kind"] == "unsaved-close"
    controller.respond("continue-close")
    assert controller.dialog["kind"] == "exit-task" and not calls
    controller.respond("stop-exit")
    assert calls[0]["cancel"]
