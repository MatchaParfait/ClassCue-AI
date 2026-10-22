import socket
import sys
from types import SimpleNamespace
import hashlib
import json
import logging
from http.client import BadStatusLine, IncompleteRead
from pathlib import Path
import os
import shutil
import subprocess
import time
import io
from concurrent.futures import ThreadPoolExecutor

import pytest

from scripts import start_app, start_frontend
from scripts import launcher_process
from scripts.service_ports import API_BIND_FAILED, bind_server_socket


def test_launcher_skips_an_occupied_ipv4_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        occupied = listener.getsockname()[1]
        assert start_app.find_free_port(occupied, attempts=2) == occupied + 1


def test_launcher_ignores_stale_state(tmp_path, monkeypatch):
    state_path = tmp_path / "app_state.json"
    state_path.write_text('{"pid": 99999999, "url": "http://127.0.0.1:8503"}', encoding="utf-8")
    monkeypatch.setattr(start_app, "STATE_PATH", state_path)
    monkeypatch.setattr(start_app, "url_is_ready", lambda _url: True)

    assert start_app.load_running_url() is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows app startup priority")
@pytest.mark.parametrize("launcher", ["streamlit", "frontend"])
def test_standard_launchers_explicitly_keep_hosted_whisper_at_normal_priority(
    tmp_path, monkeypatch, launcher,
):
    calls = []

    def popen(command, **options):
        calls.append(options)
        return SimpleNamespace()

    if launcher == "streamlit":
        monkeypatch.setattr(start_app, "LOG_DIR", tmp_path / "logs")
        monkeypatch.setattr(start_app.subprocess, "Popen", popen)
        start_app.start_server(8503)
    else:
        monkeypatch.setattr(start_frontend.subprocess, "Popen", popen)
        monkeypatch.setattr(start_frontend, "attach_cleanup_job", lambda _process: None)
        monkeypatch.setattr(start_frontend, "resume_owned_process", lambda _process: None)
        start_frontend.start_process(["python", "-m", "api.main"], tmp_path)
    options = calls[0]
    try:
        assert options["creationflags"] & start_app.subprocess.NORMAL_PRIORITY_CLASS
        assert options["creationflags"] & start_app.subprocess.CREATE_NO_WINDOW
        assert options["creationflags"] & start_app.subprocess.CREATE_NEW_PROCESS_GROUP
        assert not options["creationflags"] & start_app.subprocess.BELOW_NORMAL_PRIORITY_CLASS
    finally:
        if launcher == "streamlit":
            options["stdout"].close()
            options["stderr"].close()


def test_windows_probe_never_uses_kill(monkeypatch):
    calls = []
    monkeypatch.setattr(start_app, "os", SimpleNamespace(
        name="nt", kill=lambda *_args: pytest.fail("Windows probe sent a signal"),
    ))
    monkeypatch.setattr(start_app, "windows_process_is_alive", lambda pid: calls.append(pid) or True)
    assert start_app.process_is_alive(1234)
    assert not start_app.process_is_alive(0)
    assert calls == [1234]


@pytest.mark.parametrize("change", [
    {"service": "other"}, {"library_id": "other"}, {"instance_id": ""},
    {"status": "starting"}, {"instance_id": None},
])
def test_health_rejects_unrelated_or_unready_service(monkeypatch, change):
    payload = {
        "status": "ok", "service": "classroom-video-study",
        "library_id": "workspace", "instance_id": "instance",
    }
    payload.update(change)
    monkeypatch.setattr(start_frontend, "fetch", lambda _url: json.dumps(payload).encode())
    assert start_frontend.health("http://unused", "workspace") is None


def test_health_requires_project_identity(monkeypatch):
    payload = {"status": "ok", "service": "classroom-video-study",
               "library_id": "workspace", "instance_id": "instance"}
    monkeypatch.setattr(start_frontend, "fetch", lambda _url: json.dumps(payload).encode())
    assert start_frontend.health("http://unused", "workspace") == payload


@pytest.mark.parametrize("error", [BadStatusLine("GET / HTTP/1.1"), IncompleteRead(b"", 10)])
@pytest.mark.parametrize("probe", ["health", "frontend", "static", "live_data_root"])
def test_malformed_http_during_readiness_is_not_a_fatal_launch_error(monkeypatch, error, probe):
    def fetch(_url):
        raise error

    monkeypatch.setattr(start_frontend, "fetch", fetch)
    if probe == "health":
        assert start_frontend.health("http://unused", "workspace") is None
    elif probe == "frontend":
        assert not start_frontend.frontend_is_ready("http://unused", "workspace", "instance")
    elif probe == "static":
        assert not start_frontend.static_frontend_is_ready("http://unused", "workspace", "instance")
    else:
        assert start_frontend.live_data_root({"instance_id": "instance"}, "http://unused") is None


def test_vite_requires_own_page_and_matching_proxy(tmp_path, monkeypatch):
    page = '<title>Classroom</title><script src="/src/main.tsx"></script>'
    (tmp_path / "index.html").write_text(page, encoding="utf-8")
    monkeypatch.setattr(start_frontend, "FRONTEND_ROOT", tmp_path)
    root_id = hashlib.sha256(os.path.normcase(str(start_frontend.PROJECT_ROOT.resolve())).encode()).hexdigest()[:12]

    def fetch(url):
        if url.endswith("/__classroom/frontend-identity"):
            return json.dumps({"service": "classroom-video-study-frontend", "root_id": root_id}).encode()
        return (page + '<script src="/@vite/client"></script>').encode()

    monkeypatch.setattr(start_frontend, "fetch", fetch)
    monkeypatch.setattr(start_frontend, "health", lambda *_args: {"instance_id": "instance"})
    assert start_frontend.frontend_is_ready("http://unused", "workspace", "instance")
    assert not start_frontend.frontend_is_ready("http://unused", "workspace", "other")
    monkeypatch.setattr(start_frontend, "fetch", lambda _url: page.encode())
    assert not start_frontend.frontend_is_ready("http://unused", "workspace", "instance")


@pytest.fixture
def frontend_launch(tmp_path, monkeypatch):
    monkeypatch.delenv("CLASSROOM_DESKTOP_MODE", raising=False)
    runtime = tmp_path / ".runtime"
    frontend = tmp_path / "frontend"
    vite = frontend / "node_modules" / "vite" / "bin" / "vite.js"
    vite.parent.mkdir(parents=True)
    vite.touch()
    monkeypatch.setattr(start_frontend, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(start_frontend, "RUNTIME_ROOT", runtime)
    monkeypatch.setattr(start_frontend, "LOG_DIR", runtime / "logs")
    monkeypatch.setattr(start_frontend, "STATE_PATH", runtime / "frontend_state.json")
    monkeypatch.setattr(start_frontend, "FRONTEND_ROOT", frontend)
    monkeypatch.setattr(start_frontend, "load_data_root", lambda: tmp_path)
    monkeypatch.setattr(start_frontend, "live_data_root", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(start_frontend.webui_process, "creation_time", lambda _pid: None)
    monkeypatch.setattr(start_frontend.shutil, "which", lambda _name: "node")
    monkeypatch.setenv("CLASSROOM_APP_NO_BROWSER", "1")
    monkeypatch.delenv("CLASSROOM_DATA_ROOT", raising=False)
    monkeypatch.delenv("CLASSROOM_API_PORT", raising=False)
    monkeypatch.delenv("CLASSROOM_FRONTEND_PORT", raising=False)
    state = {"api": False, "vite": False}
    spawned = []
    cleaned = []
    released = []
    library_id = hashlib.sha256(str((tmp_path / "project.db").resolve()).encode()).hexdigest()[:12]
    healthy = {"status": "ok", "service": "classroom-video-study",
               "library_id": library_id, "instance_id": "instance"}

    def spawn(command, cwd, service="api"):
        process = SimpleNamespace(pid=100 + len(spawned), returncode=None, poll=lambda: None)
        spawned.append((service, command, process))
        state[service] = True
        return process

    monkeypatch.setattr(start_frontend, "start_process", spawn)
    monkeypatch.setattr(start_frontend, "health", lambda *_args: healthy if state["api"] else None)
    monkeypatch.setattr(start_frontend, "port_available", lambda _host, port: state["api" if port == 8765 else "vite"])
    monkeypatch.setattr(start_frontend, "select_api_port", lambda preferred: preferred)
    monkeypatch.setattr(start_frontend, "frontend_is_ready", lambda *_args: state["vite"])
    monkeypatch.setattr(start_frontend, "terminate_owned_tree", lambda process: cleaned.append(process.pid))
    monkeypatch.setattr(start_frontend, "release_cleanup_job", lambda process: released.append(process.pid))
    return SimpleNamespace(
        state=state, spawned=spawned, cleaned=cleaned, released=released,
        runtime=runtime, spawn=spawn, healthy=healthy,
    )


def test_launch_reuses_services_and_does_not_kill_them(frontend_launch):
    context = frontend_launch
    context.state.update(api=True, vite=True)
    assert start_frontend.main() == 0
    assert not context.spawned
    assert not context.cleaned


def test_desktop_launch_starts_only_api_and_reuses_built_frontend(frontend_launch, monkeypatch):
    context = frontend_launch
    monkeypatch.setenv("CLASSROOM_DESKTOP_MODE", "1")
    dist = start_frontend.FRONTEND_ROOT / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>built</html>")
    monkeypatch.setattr(start_frontend, "static_frontend_is_ready", lambda *_args: context.state["api"])
    assert start_frontend.main() == 0
    assert [service for service, *_rest in context.spawned] == ["api"]
    saved = json.loads(start_frontend.STATE_PATH.read_text())
    assert saved["frontend_mode"] == "static"
    assert saved["frontend_url"] == "http://127.0.0.1:8765"


def test_static_shutdown_ignores_unowned_development_port(tmp_path, monkeypatch):
    monkeypatch.setattr(start_frontend.webui_process, "snapshot", lambda: {})
    monkeypatch.setattr(start_frontend.webui_process, "stop_verified", lambda _records: None)
    probes = []
    monkeypatch.setattr(start_frontend, "port_available", lambda _host, port: probes.append(port) or port == 5175)
    monkeypatch.setattr(start_frontend, "write_state", lambda _state: None)
    start_frontend.stop_services({"frontend_mode": "static"}, 8765, 5175)
    assert 5175 not in probes


def test_simultaneous_launches_create_only_one_pair(frontend_launch):
    context = frontend_launch
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: start_frontend.main(), range(2)))
    assert results == [0, 0]
    assert [service for service, *_rest in context.spawned] == ["api", "vite"]
    assert "--strictPort" in context.spawned[1][1]
    assert not context.cleaned
    assert context.released == [101, 100]
    state = json.loads((context.runtime / "frontend_state.json").read_text())
    assert state["owned_pids"] == {"api": 100, "vite": 101}


def test_restart_api_reuses_existing_matching_vite(frontend_launch):
    context = frontend_launch
    context.state["vite"] = True
    assert start_frontend.main() == 0
    assert [service for service, *_rest in context.spawned] == ["api"]
    assert not context.cleaned


def test_explicit_workspace_override_changes_expected_health(frontend_launch, monkeypatch, tmp_path):
    workspace = tmp_path / "independent"
    expected = hashlib.sha256(str((workspace / "project.db").resolve()).encode()).hexdigest()[:12]
    monkeypatch.setenv("CLASSROOM_DATA_ROOT", str(workspace))
    seen = []
    monkeypatch.setattr(start_frontend, "health", lambda _url, library_id: (
        seen.append(library_id) or {**frontend_launch.healthy, "library_id": library_id}
    ))
    frontend_launch.state.update(api=True, vite=True)
    assert start_frontend.main() == 0
    assert seen and set(seen) == {expected}


def test_launcher_respects_explicit_ports(frontend_launch, monkeypatch):
    context = frontend_launch
    monkeypatch.setenv("CLASSROOM_API_PORT", "18765")
    monkeypatch.setenv("CLASSROOM_FRONTEND_PORT", "15173")
    monkeypatch.setattr(start_frontend, "port_available", lambda *_args: False)
    seen_urls = []
    monkeypatch.setattr(start_frontend, "health", lambda url, _workspace: (
        seen_urls.append(url) or (context.healthy if context.state["api"] else None)
    ))
    assert start_frontend.main() == 0
    assert "--port" in context.spawned[0][1] and "18765" in context.spawned[0][1]
    assert "15173" in context.spawned[1][1]
    assert set(seen_urls) == {"http://127.0.0.1:18765"}
    state = json.loads((context.runtime / "frontend_state.json").read_text())
    assert (state["api_port"], state["frontend_port"]) == (18765, 15173)


@pytest.mark.parametrize("port", ["not-a-port", "0", "65536"])
def test_launcher_rejects_invalid_port_without_spawning(frontend_launch, monkeypatch, port):
    monkeypatch.setenv("CLASSROOM_FRONTEND_PORT", port)
    assert start_frontend.main() == 1
    assert not frontend_launch.spawned


def test_vite_rejects_another_checkout_even_with_same_project_page(monkeypatch):
    monkeypatch.setattr(start_frontend, "fetch", lambda _url: json.dumps({
        "service": "classroom-video-study-frontend", "root_id": "different",
    }).encode())
    assert not start_frontend.frontend_is_ready("http://unused", "workspace", "instance")


def test_new_vite_receives_selected_api_proxy_target(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv("CLASSROOM_API_PORT", "18765")
    monkeypatch.setenv("VITE_API_PROXY_TARGET", "http://wrong:1")
    monkeypatch.setattr(start_frontend.subprocess, "Popen", lambda _command, **options: calls.append(options) or SimpleNamespace())
    monkeypatch.setattr(start_frontend, "attach_cleanup_job", lambda _process: None)
    monkeypatch.setattr(start_frontend, "resume_owned_process", lambda _process: None)
    start_frontend.start_process(["node", "vite"], tmp_path, "vite")
    assert calls[0]["env"]["VITE_API_PROXY_TARGET"] == "http://127.0.0.1:18765"


def test_spawn_error_cleans_only_new_api(frontend_launch, monkeypatch, capsys):
    context = frontend_launch

    def spawn(command, cwd, service="api"):
        if service == "vite":
            raise FileNotFoundError("node missing")
        return context.spawn(command, cwd, service)

    monkeypatch.setattr(start_frontend, "start_process", spawn)
    assert start_frontend.main() == 1
    assert context.cleaned == [100]
    assert "phase=vite spawn" in capsys.readouterr().err
    assert "Traceback" in (context.runtime / "logs" / "launcher.log").read_text()


def test_foreign_port_is_not_killed(frontend_launch, monkeypatch):
    context = frontend_launch
    context.state["api"] = True
    monkeypatch.setattr(start_frontend, "port_available", lambda *_args: True)
    assert start_frontend.main() == 1
    assert not context.spawned
    assert not context.cleaned


@pytest.mark.parametrize("listening", [False, True])
@pytest.mark.parametrize("host", ["127.0.0.1", "0.0.0.0"])
def test_api_port_selection_detects_bound_and_listening_sockets(listening, host):
    with bind_server_socket(host, 0) as foreign:
        port = foreign.getsockname()[1]
        if listening:
            foreign.listen()
        assert start_frontend.port_available("127.0.0.1", port) == listening
        selected = start_frontend.select_api_port(port)
        assert selected != port
        with bind_server_socket("127.0.0.1", selected):
            pass
        assert foreign.getsockname()[1] == port


def test_api_port_selection_handles_permission_denied(monkeypatch):
    def bind(host, port):
        if port == 8765:
            raise PermissionError(13, "permission denied")
        return bind_server_socket(host, port)

    monkeypatch.setattr(start_frontend, "port_available", lambda *_args: False)
    monkeypatch.setattr(start_frontend, "bind_server_socket", bind)
    assert start_frontend.select_api_port(8765) != 8765


def test_desktop_fallback_persists_actual_port_and_restores_environment(frontend_launch, monkeypatch):
    context = frontend_launch
    monkeypatch.setenv("CLASSROOM_DESKTOP_MODE", "1")
    monkeypatch.setenv("CLASSROOM_API_PORT", "8765")
    dist = start_frontend.FRONTEND_ROOT / "dist"
    dist.mkdir()
    (dist / "index.html").touch()
    monkeypatch.setattr(start_frontend, "select_api_port", lambda _preferred: 18765)
    seen = []
    monkeypatch.setattr(start_frontend, "static_frontend_is_ready", lambda url, *_args:
                        seen.append(url) or context.state["api"])
    assert start_frontend.main() == 0
    saved = start_frontend.read_state()
    assert saved["api_port"] == 18765
    assert saved["frontend_url"] == "http://127.0.0.1:18765"
    assert "18765" in context.spawned[0][1]
    assert set(seen) == {"http://127.0.0.1:18765"}
    assert os.environ["CLASSROOM_API_PORT"] == "8765"
    assert not context.cleaned
    assert start_frontend.main() == 0
    assert len(context.spawned) == 1


def test_api_fallback_does_not_reuse_vite_with_stale_proxy(frontend_launch, monkeypatch):
    context = frontend_launch
    context.state["vite"] = True
    monkeypatch.setattr(start_frontend, "select_api_port", lambda _preferred: 18765)
    monkeypatch.setattr(start_frontend, "frontend_is_ready",
                        lambda url, *_args: not url.endswith(":5173"))
    assert start_frontend.main() == 0
    assert [service for service, *_args in context.spawned] == ["api", "vite"]
    saved = start_frontend.read_state()
    assert saved["frontend_port"] != 5173
    assert saved["api_port"] == 18765
    assert not context.cleaned


@pytest.mark.parametrize("exitcode", [API_BIND_FAILED, 1, 3])
def test_only_socket_bind_failures_retry_on_another_port(frontend_launch, monkeypatch, exitcode):
    context = frontend_launch
    selections = []

    def select(preferred):
        selections.append(preferred)
        return 18765 if preferred == 0 else preferred

    def spawn(command, cwd, service="api"):
        process = context.spawn(command, cwd, service)
        if len(context.spawned) == 1:
            process.returncode = exitcode
            process.poll = lambda: exitcode
            context.state["api"] = False
        return process

    monkeypatch.setattr(start_frontend, "select_api_port", select)
    monkeypatch.setattr(start_frontend, "start_process", spawn)
    assert start_frontend.main() == (0 if exitcode == API_BIND_FAILED else 1)
    assert context.cleaned == [100]
    assert selections == ([8765, 0] if exitcode == API_BIND_FAILED else [8765])
    if exitcode == API_BIND_FAILED:
        assert start_frontend.read_state()["api_port"] == 18765


def test_api_bind_race_retry_is_bounded(frontend_launch, monkeypatch):
    context = frontend_launch

    def spawn(command, cwd, service="api"):
        process = context.spawn(command, cwd, service)
        context.state["api"] = False
        process.returncode = API_BIND_FAILED
        process.poll = lambda: API_BIND_FAILED
        return process

    monkeypatch.setattr(start_frontend, "select_api_port", lambda preferred: preferred or 18765)
    monkeypatch.setattr(start_frontend, "start_process", spawn)
    assert start_frontend.main() == 1
    assert len(context.spawned) == start_frontend.API_BIND_ATTEMPTS
    assert context.cleaned == [100, 101, 102]
    assert not start_frontend.STATE_PATH.exists()


@pytest.mark.parametrize("failure", ["exit", "timeout"])
def test_failed_readiness_cleans_new_pair(frontend_launch, monkeypatch, failure):
    context = frontend_launch
    monkeypatch.setattr(start_frontend, "frontend_is_ready", lambda *_args: False)
    if failure == "timeout":
        monkeypatch.setattr(start_frontend, "STARTUP_TIMEOUT", 0)
    else:
        def spawn(command, cwd, service="api"):
            process = context.spawn(command, cwd, service)
            if service == "vite":
                process.returncode = 7
                process.poll = lambda: 7
            return process
        monkeypatch.setattr(start_frontend, "start_process", spawn)
    assert start_frontend.main() == 1
    assert context.cleaned == [101, 100]


def test_python_service_output_rotates_and_redacts(tmp_path):
    from classcue.diagnostics import register_secret

    secret = "launcher-secret-value"
    register_secret(secret)
    logger = logging.getLogger(f"test.launcher.{tmp_path}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = launcher_process.RedactingRotatingFileHandler(
        tmp_path / "output.log", maxBytes=1024, backupCount=3, encoding="utf-8",
    )
    logger.addHandler(handler)
    output = launcher_process.BoundedLogStream(logger, logging.INFO)
    try:
        for _index in range(100):
            output.write(f"{secret} " + "x" * 120 + "\n")
        output.write("unterminated")
        output.flush()
        logs = list(tmp_path.glob("output.log*"))
        assert len(logs) <= 4
        assert all(path.stat().st_size <= 1024 for path in logs)
        assert all(secret not in path.read_text() for path in logs)
        assert "unterminated" in (tmp_path / "output.log").read_text()
        assert output.pending == ""
    finally:
        handler.close()
        logger.removeHandler(handler)


def test_oversized_unicode_exception_cannot_exceed_log_limit(tmp_path):
    logger = logging.getLogger(f"test.oversized.{tmp_path}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = launcher_process.RedactingRotatingFileHandler(
        tmp_path / "oversized.log", maxBytes=1024, backupCount=3, encoding="utf-8",
    )
    logger.addHandler(handler)
    try:
        try:
            raise RuntimeError("\u8bfe" * 100000)
        except RuntimeError:
            logger.exception("Failure context")
        text = (tmp_path / "oversized.log").read_text(encoding="utf-8")
        assert "Failure context" in text and "truncated" in text
        assert all(path.stat().st_size <= 1024 for path in tmp_path.glob("oversized.log*"))
    finally:
        handler.close()
        logger.removeHandler(handler)


def test_log_disk_failure_does_not_recurse_into_captured_stderr(tmp_path, monkeypatch):
    logger = logging.getLogger(f"test.diskerror.{tmp_path}")
    logger.setLevel(logging.INFO)
    handler = launcher_process.RedactingRotatingFileHandler(
        tmp_path / "failure.log", maxBytes=128, backupCount=3, encoding="utf-8",
    )
    logger.addHandler(handler)
    emergency = io.StringIO()
    monkeypatch.setattr(sys, "__stderr__", emergency)
    captured_stderr = launcher_process.BoundedLogStream(logger, logging.ERROR)
    monkeypatch.setattr(sys, "stderr", captured_stderr)
    calls = []

    def rollover_failure():
        calls.append(True)
        raise OSError("disk full")

    monkeypatch.setattr(handler, "doRollover", rollover_failure)
    try:
        for _index in range(4):
            logger.error("x" * 100)
        assert 1 <= len(calls) <= 4
        assert "disk full" in emergency.getvalue()
        assert captured_stderr.pending == ""
    finally:
        handler.close()
        logger.removeHandler(handler)


@pytest.mark.parametrize("newline", [True, False])
def test_python_output_redacts_secret_across_chunks_and_writes(tmp_path, newline):
    from classcue.diagnostics import register_secret

    secret = "private-split-launcher-api-key"
    register_secret(secret)
    logger = launcher_process.service_logger(tmp_path, "split-test")
    output = launcher_process.BoundedLogStream(logger, logging.INFO)
    output.write("x" * 8180 + secret[:12])
    output.write(secret[12:] + "x" * 20000 + ("\n" if newline else ""))
    assert len(output.pending) < 16384
    output.flush()
    text = (tmp_path / "split-test.log").read_text()
    assert secret not in text
    assert secret[:12] not in text
    assert secret[12:] not in text
    assert "[REDACTED]" in text


def test_service_logger_does_not_duplicate_handlers(tmp_path):
    first = launcher_process.service_logger(tmp_path, "same")
    second = launcher_process.service_logger(tmp_path, "same")
    assert first is second
    assert len(first.handlers) == 1


def test_api_import_failure_is_logged_without_extra_supervisor(tmp_path):
    code = (
        "import sys, types;"
        "def_placeholder=None;"
        "sys.modules['uvicorn']=types.SimpleNamespace(Config=lambda *a, **kw: "
        "(_ for _ in ()).throw(RuntimeError('fake import failed')));"
        "from scripts import api_service;"
        f"sys.argv=['api_service','--port','0','--log-dir',{str(tmp_path)!r}];"
        "raise SystemExit(api_service.main())"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=start_frontend.PROJECT_ROOT,
        capture_output=True, timeout=10,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert result.returncode == 1
    text = (tmp_path / "api.log").read_text(encoding="utf-8")
    assert "Traceback" in text and "fake import failed" in text


def test_api_child_reports_bind_failure_before_loading_application(tmp_path):
    with bind_server_socket("127.0.0.1", 0) as foreign:
        port = foreign.getsockname()[1]
        result = subprocess.run(
            [sys.executable, str(start_frontend.PROJECT_ROOT / "scripts/api_service.py"),
             "--port", str(port), "--log-dir", str(tmp_path)],
            capture_output=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        assert result.returncode == API_BIND_FAILED
        text = (tmp_path / "api.log").read_text(encoding="utf-8")
        assert "phase=bind" in text
        assert "Application startup" not in text
        assert foreign.getsockname()[1] == port


@pytest.mark.skipif(shutil.which("node") is None, reason="Node not installed")
def test_vite_config_identity_matches_python_checkout_hash():
    code = (
        "import {loadConfigFromFile} from 'vite';"
        "const loaded=await loadConfigFromFile({command:'serve',mode:'development'});"
        "const plugin=loaded.config.plugins.find(p=>p.name==='classroom-frontend-identity');"
        "let middleware;"
        "plugin.configureServer({middlewares:{use(fn){middleware=fn}}});"
        "const headers={};let body;"
        "middleware({method:'GET',url:'/__classroom/frontend-identity'},"
        "{setHeader(k,v){headers[k]=v},end(v){body=v}},()=>{throw Error('identity not handled')});"
        "console.log(JSON.stringify({headers,body:JSON.parse(body)}));"
    )
    result = subprocess.run(
        [shutil.which("node"), "--input-type=module", "-e", code],
        cwd=start_frontend.FRONTEND_ROOT, timeout=15, capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert result.returncode == 0, result.stderr
    result_payload = json.loads(result.stdout)
    expected = hashlib.sha256(os.path.normcase(str(start_frontend.PROJECT_ROOT.resolve())).encode()).hexdigest()[:12]
    assert result_payload["body"] == {
        "service": "classroom-video-study-frontend", "root_id": expected,
    }
    assert result_payload["headers"]["Cache-Control"] == "no-store"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node not installed")
def test_vite_logging_has_hard_runtime_bounds_and_redaction(tmp_path):
    code = (
        "for(let i=0;i<16000;i++)process.stdout.write('token=super-secret-value '+ 'x'.repeat(700)+'\\n');"
        "process.stdout.write('x'.repeat(8180)+'super-secret');"
        "process.stdout.write('-value'+'x'.repeat(20000)+'\\n');"
        "process.stderr.write('Bearer private-value\\n');"
    )
    result = subprocess.run(
        [shutil.which("node"), "--require",
         str(start_frontend.PROJECT_ROOT / "scripts" / "vite_logging.cjs"), "-e", code],
        cwd=start_frontend.PROJECT_ROOT, timeout=30, capture_output=True,
        env={**os.environ, "CLASSROOM_LOG_DIR": str(tmp_path), "TEST_API_KEY": "super-secret-value"},
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert result.returncode == 0, result.stderr
    logs = list(tmp_path.glob("vite.stdout.log*"))
    assert len(logs) <= 4
    assert all(path.stat().st_size <= launcher_process.MAX_LOG_BYTES for path in logs)
    assert all("super-secret-value" not in path.read_text() for path in logs)
    assert all("super-secret" not in path.read_text() for path in logs)
    assert "private-value" not in (tmp_path / "vite.stderr.log").read_text()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process ownership")
def test_releasing_job_keeps_successful_background_service_alive(tmp_path, monkeypatch):
    monkeypatch.setattr(start_frontend, "LOG_DIR", tmp_path)
    process = start_frontend.start_process(
        [sys.executable, "-c", "import time;time.sleep(60)"], tmp_path,
    )
    try:
        launcher_process.release_cleanup_job(process)
        time.sleep(.1)
        assert process.poll() is None
        assert launcher_process.windows_process_is_alive(process.pid)
    finally:
        launcher_process.terminate_owned_tree(process)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process ownership")
def test_owned_windows_job_cleans_child_after_parent_exit(tmp_path, monkeypatch):
    child_pid_path = tmp_path / "child.pid"
    code = (
        "import subprocess,sys,time,pathlib;"
        "time.sleep(.2);"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid));"
        "time.sleep(.2)"
    )
    monkeypatch.setattr(start_frontend, "LOG_DIR", tmp_path)
    process = start_frontend.start_process([sys.executable, "-c", code], tmp_path)
    child_pid = None
    try:
        deadline = time.monotonic() + 5
        while not child_pid_path.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        child_pid = int(child_pid_path.read_text())
        process.wait(timeout=5)
        assert launcher_process.windows_process_is_alive(child_pid)
        launcher_process.terminate_owned_tree(process)
        deadline = time.monotonic() + 5
        while launcher_process.windows_process_is_alive(child_pid) and time.monotonic() < deadline:
            time.sleep(.05)
        assert not launcher_process.windows_process_is_alive(child_pid)
    finally:
        launcher_process.terminate_owned_tree(process)
        if child_pid and launcher_process.windows_process_is_alive(child_pid):
            # Only this test-created PID, never an application process.
            subprocess.run(["taskkill.exe", "/PID", str(child_pid), "/T", "/F"], capture_output=True)


def test_unified_start_remembers_ports_and_workspace(frontend_launch, monkeypatch, tmp_path):
    context = frontend_launch
    workspace = tmp_path / "remembered"
    context.runtime.mkdir()
    start_frontend.write_state({
        "data_root": str(workspace), "api_port": 18765, "frontend_port": 15175,
    })
    monkeypatch.setattr(start_frontend, "port_available", lambda *_args: False)
    assert start_frontend.main() == 0
    state = start_frontend.read_state()
    assert state["data_root"] == str(workspace)
    assert state["api_port"] == 18765
    assert state["frontend_port"] == 15175
    assert "18765" in context.spawned[0][1]
    assert "15175" in context.spawned[1][1]
    assert "CLASSROOM_DATA_ROOT" not in os.environ
    assert "CLASSROOM_API_PORT" not in os.environ


def test_api_recovery_keeps_ownership_of_reused_vite(frontend_launch):
    context = frontend_launch
    context.runtime.mkdir()
    start_frontend.write_state({
        "instance_id": "old-api", "api_port": 8765, "frontend_port": 5173,
        "owned_pids": {"api": 10, "vite": 11},
        "owned_processes": {"vite": [{"pid": 11, "created": "123"}]},
    })
    context.state["vite"] = True
    assert start_frontend.main() == 0
    state = start_frontend.read_state()
    assert state["owned_pids"] == {"api": 100, "vite": 11}
    assert state["owned_processes"]["vite"] == [{"pid": 11, "created": "123"}]


def test_unified_restart_preserves_live_data_root(frontend_launch, monkeypatch, tmp_path):
    context = frontend_launch
    context.runtime.mkdir()
    start_frontend.write_state({
        "instance_id": "old", "api_port": 8765, "frontend_port": 5175,
        "data_root": str(tmp_path / "saved"), "owned_pids": {"api": 10, "vite": 11},
    })
    current = tmp_path / "live"
    monkeypatch.setattr(start_frontend, "live_data_root", lambda *_args, **_kwargs: current)
    calls = []

    def stop(state, api_port, frontend_port):
        calls.append((state["data_root"], api_port, frontend_port))
        state.update(owned_pids={}, owned_processes={})
        context.state.update(api=False, vite=False)

    monkeypatch.setattr(start_frontend, "stop_services", stop)
    assert start_frontend.main("restart") == 0
    assert calls == [(str(current), 8765, 5175)]
    state = start_frontend.read_state()
    assert state["data_root"] == str(current)
    assert state["owned_pids"] == {"api": 100, "vite": 101}
    assert [entry[0] for entry in context.spawned] == ["api", "vite"]


def test_unified_stop_without_state_does_not_start_anything(frontend_launch):
    assert start_frontend.main("stop") == 0
    assert not frontend_launch.spawned


def test_unified_stop_does_not_touch_untracked_ports(frontend_launch, monkeypatch):
    monkeypatch.setattr(start_frontend, "port_available", lambda *_args: True)
    assert start_frontend.main("stop") == 1
    assert not frontend_launch.spawned
    assert not frontend_launch.cleaned


def test_unified_restart_does_not_start_after_stop_failure(frontend_launch, monkeypatch):
    frontend_launch.runtime.mkdir()
    start_frontend.write_state({"api_port": 8765, "frontend_port": 5175})

    def fail(*_args):
        raise RuntimeError("Cannot verify process")

    monkeypatch.setattr(start_frontend, "stop_services", fail)
    assert start_frontend.main("restart") == 1
    assert not frontend_launch.spawned


@pytest.mark.parametrize("processing", [True, False])
def test_live_data_root_allows_reopening_but_blocks_busy_shutdown(monkeypatch, tmp_path, processing):
    library_id = hashlib.sha256(str((tmp_path / "project.db").resolve()).encode()).hexdigest()[:12]
    payloads = {
        "health": {"service": "classroom-video-study", "instance_id": "instance"},
        "workspace": {"data_root": str(tmp_path), "library_id": library_id, "processing": processing},
    }
    monkeypatch.setattr(start_frontend, "fetch", lambda url: json.dumps(
        payloads["health" if url.endswith("health") else "workspace"],
    ).encode())
    assert start_frontend.live_data_root({"instance_id": "instance"}, "http://unused") == tmp_path
    if processing:
        with pytest.raises(RuntimeError, match="processing task"):
            start_frontend.live_data_root({"instance_id": "instance"}, "http://unused", require_idle=True)
    else:
        assert start_frontend.live_data_root(
            {"instance_id": "instance"}, "http://unused", require_idle=True,
        ) == tmp_path
    with pytest.raises(RuntimeError, match="identity changed"):
        start_frontend.live_data_root({"instance_id": "other"}, "http://unused")


@pytest.mark.parametrize("failure", ["reused", "unrelated", "untracked"])
def test_stop_preflights_entire_pair_before_terminating(frontend_launch, monkeypatch, failure):
    records = {
        10: {"pid": 10, "parent": 1, "created": "100", "command": "api"},
        11: {"pid": 11, "parent": 1, "created": "101", "command": "vite"},
    }
    state = {"owned_processes": {
        "api": [{"pid": 10, "created": "100"}],
        "vite": [{"pid": 11, "created": "99" if failure == "reused" else "101"}],
    }}
    if failure == "untracked":
        del records[11]
    monkeypatch.setattr(start_frontend.webui_process, "snapshot", lambda: records)
    monkeypatch.setattr(start_frontend.webui_process, "matches_service", lambda record, *_args: (
        not (failure == "unrelated" and record["pid"] == 11)
    ))
    monkeypatch.setattr(start_frontend, "port_available", lambda *_args: failure == "untracked")
    monkeypatch.setattr(start_frontend.webui_process, "stop_verified", lambda *_args: pytest.fail("Unsafe stop"))
    with pytest.raises(RuntimeError):
        start_frontend.stop_services(state, 8765, 5175)


def test_stop_cleans_descendants_and_keeps_configuration(frontend_launch, monkeypatch, tmp_path):
    frontend_launch.runtime.mkdir()
    records = {
        10: {"pid": 10, "parent": 1, "created": "100"},
        11: {"pid": 11, "parent": 1, "created": "101"},
        12: {"pid": 12, "parent": 10, "created": "102"},
        13: {"pid": 13, "parent": 10, "created": "90"},  # Stale parent PID.
    }
    state = {
        "data_root": str(tmp_path), "api_port": 8765, "frontend_port": 5175,
        "owned_pids": {"api": 10, "vite": 11},
    }
    stopped = []
    monkeypatch.setattr(start_frontend.webui_process, "snapshot", lambda: records)
    monkeypatch.setattr(start_frontend.webui_process, "matches_service", lambda *_args: True)
    monkeypatch.setattr(start_frontend.webui_process, "stop_verified", lambda targets: stopped.extend(targets))
    start_frontend.stop_services(state, 8765, 5175)
    assert [record["pid"] for record in stopped] == [10, 11, 12]
    saved = start_frontend.read_state()
    assert saved["status"] == "stopped"
    assert saved["owned_pids"] == {}
    assert saved["data_root"] == str(tmp_path)
    assert saved["frontend_port"] == 5175


@pytest.mark.skipif(sys.platform != "win32", reason="Windows saved process ownership")
def test_native_saved_identity_handles_wmi_precision_and_stops_only_fixture(tmp_path):
    from scripts import webui_process

    process = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(60)"],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        records = webui_process.snapshot()
        assert records[process.pid]["created"] == webui_process.creation_time(process.pid)
        changed = {**records[process.pid], "created": "1"}
        with pytest.raises(RuntimeError, match="reused"):
            webui_process.stop_verified([changed])
        assert process.poll() is None
        webui_process.stop_verified([records[process.pid]])
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows command line parsing")
def test_service_identity_requires_exact_script_and_port(tmp_path):
    from scripts import webui_process

    script = tmp_path / "scripts" / "api_service.py"
    record = {"command": f'python.exe "{script}" --host 127.0.0.1 --port 8765'}
    assert webui_process.matches_service(record, "api", tmp_path, 8765)
    assert not webui_process.matches_service(record, "api", tmp_path, 8766)
    assert not webui_process.matches_service(
        {"command": f'python.exe -c "print(\'{script}\')" --host 127.0.0.1 --port 8765'},
        "api", tmp_path, 8765,
    )
