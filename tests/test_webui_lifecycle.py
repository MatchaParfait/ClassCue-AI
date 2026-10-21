"""End-to-end lifecycle checks against a disposable application checkout."""

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

from scripts import start_frontend, webui_process


pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("node") is None
    or not (start_frontend.FRONTEND_ROOT / "node_modules/vite/bin/vite.js").is_file(),
    reason="Requires Windows and installed local WebUI dependencies",
)


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@pytest.mark.parametrize("occupied_mode,desktop", [
    ("free", False), ("bound", False), ("listening", False), ("bound", True),
])
def test_real_frontend_api_start_reuse_restart_stop(tmp_path, occupied_mode, desktop):
    root = tmp_path / "application"
    root.mkdir()
    for source in start_frontend.PROJECT_ROOT.glob("*.py"):
        shutil.copy2(source, root / source.name)
    for name in ("pyproject.toml", "release_config.json", "uv.lock"):
        shutil.copy2(start_frontend.PROJECT_ROOT / name, root / name)
    for directory in ("api", "scripts"):
        shutil.copytree(
            start_frontend.PROJECT_ROOT / directory, root / directory,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
    shutil.copytree(
        start_frontend.FRONTEND_ROOT, root / "frontend",
        ignore=shutil.ignore_patterns("node_modules", "dist", "*.tsbuildinfo"),
    )
    if desktop:
        dist = root / "frontend" / "dist"
        dist.mkdir()
        (dist / "index.html").write_text("<html>desktop fixture</html>", encoding="utf-8")
    # Reuse read-only installed packages; all writable source, logs and data are isolated.
    subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(root / "frontend/node_modules"),
         str(start_frontend.FRONTEND_ROOT / "node_modules")],
        check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(root / ".venv"), sys.prefix],
        check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    initial_workspace = tmp_path / "initial-workspace"
    data = root / "data"
    data.mkdir()
    (data / "asr_storage.json").write_text(json.dumps({
        "storage_root": str(tmp_path / "assets"), "log_dir": str(tmp_path / "logs"),
        "library_data_dir": str(initial_workspace),
    }), encoding="utf-8")
    api_port, frontend_port = free_port(), free_port()
    assert api_port != frontend_port
    blocker = socket.socket()
    if occupied_mode != "free":
        if os.name == "nt":
            blocker.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        blocker.bind(("127.0.0.1", api_port))
        if occupied_mode == "listening":
            blocker.listen()
    else:
        blocker.close()
    requested_port = api_port
    environment = {**os.environ, "CLASSROOM_APP_NO_BROWSER": "1", "PYTHONUTF8": "1"}
    for key in (
        "CLASSROOM_DATA_ROOT", "CLASSROOM_API_PORT", "CLASSROOM_FRONTEND_PORT",
        "CLASSROOM_VIDEO_DB", "CLASSROOM_DESKTOP_MODE",
    ):
        environment.pop(key, None)
    if desktop:
        environment["CLASSROOM_DESKTOP_MODE"] = "1"
    first_environment = {
        **environment, "CLASSROOM_DATA_ROOT": str(initial_workspace),
        "CLASSROOM_API_PORT": str(api_port), "CLASSROOM_FRONTEND_PORT": str(frontend_port),
    }
    state_path = root / ".runtime/frontend_state.json"
    launcher = root / "scripts/webui.ps1"
    page_port = frontend_port

    def control(action, env=environment):
        result = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(launcher), "-Action", action], cwd=root, env=env,
            capture_output=True, encoding="utf-8", timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def request(path, payload=None):
        url = f"http://127.0.0.1:{page_port}{path}"
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)

    def state():
        return json.loads(state_path.read_text(encoding="utf-8"))

    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import sys;sys.stdin.buffer.read()"],
        stdin=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        control("start", first_environment)
        first = state()
        api_port = first["api_port"]
        if desktop:
            page_port = api_port
            assert first["frontend_url"] == f"http://127.0.0.1:{api_port}"
            assert set(first["owned_pids"]) == {"api"}
        if occupied_mode != "free":
            assert api_port != requested_port
            assert blocker.getsockname()[1] == requested_port
        assert first["data_root"] == str(initial_workspace)
        assert all(first["owned_processes"].values())
        assert request("/api/health")["instance_id"] == first["instance_id"]
        assert request("/__classroom/frontend-identity")["service"] == "classroom-video-study-frontend"
        control("start")
        assert state()["owned_pids"] == first["owned_pids"]
        assert state()["instance_id"] == first["instance_id"]

        # The single library persists across a restart without env overrides.
        control("restart")
        restarted = state()
        assert restarted["data_root"] == str(initial_workspace)
        assert restarted["instance_id"] != first["instance_id"]
        assert restarted["api_port"] == api_port
        assert restarted["frontend_port"] == frontend_port
        assert request("/api/library/status")["data_root"] == str(initial_workspace)
        for identities in first["owned_processes"].values():
            for identity in identities:
                assert webui_process.creation_time(identity["pid"]) != identity["created"]
        assert unrelated.poll() is None

        # Real API-triggered, immediate relocation/restart. All files and
        # processes are in this disposable installation.
        source_key = data / "ai_keys" / "fixture.dpapi"
        source_key.parent.mkdir()
        source_key.write_bytes(b"encrypted-fixture")
        destination = tmp_path / "small-application-data"
        previous_instance = restarted["instance_id"]
        payload = {"destination": str(destination), "library_id": restarted["library_id"]}
        preview = request("/api/settings/application-data/preview", payload)
        assert not destination.exists()
        accepted = request("/api/settings/application-data/migrate", {**payload, "plan_id": preview["plan_id"]})
        assert accepted["status"] == "restarting"
        deadline = time.monotonic() + 45
        while True:
            try:
                current = request("/api/health")
                if current["instance_id"] != previous_instance and state()["data_root"] == str(destination / "library"):
                    break
            except (OSError, ValueError):
                pass
            assert time.monotonic() < deadline, "Automatic data relocation restart timed out"
            time.sleep(.2)
        assert current["library_id"] == restarted["library_id"]
        assert request("/api/library/status")["library_scope"] == str(initial_workspace)
        assert (destination / "config/ai_keys/fixture.dpapi").read_bytes() == b"encrypted-fixture"
        assert (initial_workspace / "project.db").exists()  # Retained backup.
        assert request("/api/settings/directories")["application_data"]["migration"]["status"] == "completed"
        assert unrelated.poll() is None

        control("stop")
        assert state()["status"] == "stopped"
        assert state()["owned_pids"] == {}
        assert not start_frontend.port_available("127.0.0.1", api_port)
        assert not start_frontend.port_available("127.0.0.1", frontend_port)
        control("stop")  # Repeated stops are harmless.
        control("start")
        assert request("/api/library/status")["data_root"] == str(destination / "library")
        assert state()["frontend_port"] == frontend_port
        control("stop")
        assert unrelated.poll() is None
    finally:
        # Even failed assertions only clean fixture-owned services, never the live app.
        records = webui_process.snapshot()
        roots = [
            pid for pid, record in records.items()
            if webui_process.matches_service(record, "api", root, api_port)
            or webui_process.matches_service(record, "vite", root, frontend_port)
        ]
        members = webui_process.descendants(roots, records)
        webui_process.stop_verified([records[pid] for pid in members])
        blocker.close()
        unrelated.kill()
        unrelated.wait(timeout=5)
