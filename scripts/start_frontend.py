"""Start or reuse the matching local API and Vite frontend."""

from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser

from filelock import FileLock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from classcue.app_storage import load_data_root
from classcue.asr_storage import log_directory, library_identity
import classcue.application_data_migration as application_data_migration
from scripts.launcher_process import (
    attach_cleanup_job, launcher_logger, release_cleanup_job, resume_owned_process,
    terminate_owned_tree,
)
from scripts import webui_process
from scripts.service_ports import API_BIND_FAILED, bind_server_socket


FRONTEND_ROOT = PROJECT_ROOT / "frontend"
RUNTIME_ROOT = PROJECT_ROOT / ".runtime"
LOG_DIR = log_directory(PROJECT_ROOT)
STATE_PATH = RUNTIME_ROOT / "frontend_state.json"
API_HOST = "127.0.0.1"
API_PORT = 8765
FRONTEND_HOST = "127.0.0.1"
FRONTEND_PORT = 5173
STARTUP_TIMEOUT = 30.0
API_BIND_ATTEMPTS = 3


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=1) as response:
        if response.status != 200:
            raise OSError(f"Unexpected HTTP status: {response.status}")
        return response.read(64 * 1024)


def health(url: str, library_id: str) -> dict[str, str] | None:
    try:
        payload = json.loads(fetch(f"{url}/api/health"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if (
        payload.get("status") != "ok"
        or payload.get("service") != "classroom-video-study"
        or payload.get("library_id") != library_id
        or not isinstance(payload.get("instance_id"), str)
        or not payload["instance_id"].strip()
    ):
        return None
    return payload


class PageIdentity(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.scripts: set[str] = set()
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs) -> None:
        if tag == "title":
            self._in_title = True
        elif tag == "script":
            source = dict(attrs).get("src")
            if source:
                self.scripts.add(source)

    def handle_endtag(self, tag) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data) -> None:
        if self._in_title:
            self.title += data


def frontend_is_ready(url: str, library_id: str, instance_id: str) -> bool:
    try:
        identity = json.loads(fetch(f"{url}/__classroom/frontend-identity"))
        root_id = hashlib.sha256(os.path.normcase(str(PROJECT_ROOT.resolve())).encode("utf-8")).hexdigest()[:12]
        if (
            not isinstance(identity, dict)
            or identity.get("service") != "classroom-video-study-frontend"
            or identity.get("root_id") != root_id
        ):
            return False
        expected = PageIdentity()
        expected.feed((FRONTEND_ROOT / "index.html").read_text(encoding="utf-8"))
        actual = PageIdentity()
        actual.feed(fetch(f"{url}/").decode("utf-8"))
        if (
            actual.title != expected.title
            or "/src/main.tsx" not in actual.scripts
            or "/@vite/client" not in actual.scripts
        ):
            return False
        proxied = health(url, library_id)
        return proxied is not None and proxied["instance_id"] == instance_id
    except (OSError, UnicodeError, ValueError):
        return False


def static_frontend_is_ready(url: str, library_id: str, instance_id: str) -> bool:
    try:
        identity = json.loads(fetch(f"{url}/__classroom/frontend-identity"))
        root_id = hashlib.sha256(os.path.normcase(str(PROJECT_ROOT.resolve())).encode()).hexdigest()[:12]
        return (
            identity.get("service") == "classroom-video-study-frontend"
            and identity.get("mode") == "static" and identity.get("root_id") == root_id
            and health(url, library_id)["instance_id"] == instance_id
        )
    except (OSError, ValueError, TypeError, KeyError):
        return False


def port_available(host: str, port: int) -> bool:
    """Compatibility name: return whether a listener already occupies the port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        return probe.connect_ex((host, port)) == 0


def select_api_port(preferred: int) -> int:
    if preferred and not port_available(API_HOST, preferred):
        try:
            with bind_server_socket(API_HOST, preferred):
                return preferred
        except OSError:
            pass
    with bind_server_socket(API_HOST, 0) as available:
        return available.getsockname()[1]


def start_process(command: list[str], cwd: Path, service: str = "api") -> subprocess.Popen[bytes]:
    creationflags = (
        subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        | subprocess.NORMAL_PRIORITY_CLASS | 0x00000004  # CREATE_SUSPENDED
    ) if os.name == "nt" else 0
    environment = {**os.environ, "CLASSROOM_LOG_DIR": str(LOG_DIR.resolve())}
    environment.pop("CLASSROOM_EXPECT_INSTANCE_ID", None)
    environment.pop("CLASSROOM_DATA_MIGRATION_ID", None)
    if service == "vite":
        api_port = os.getenv("CLASSROOM_API_PORT", str(API_PORT))
        environment["VITE_API_PROXY_TARGET"] = f"http://{API_HOST}:{api_port}"
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        start_new_session=os.name != "nt",
        env=environment,
    )
    try:
        attach_cleanup_job(process)
        resume_owned_process(process)
    except (OSError, RuntimeError):
        try:
            terminate_owned_tree(process)
        finally:
            raise
    return process


def read_state() -> dict:
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("Invalid frontend runtime state")
        return state
    except FileNotFoundError:
        return {}


def write_state(state: dict) -> None:
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temporary.replace(STATE_PATH)


def live_data_root(state: dict, api_url: str, *, require_idle: bool = False) -> Path | None:
    """Read and verify the database used by the recorded server instance."""
    try:
        payload = json.loads(fetch(f"{api_url}/api/health"))
    except (OSError, ValueError):
        return None
    if payload.get("service") != "classroom-video-study" or payload.get("instance_id") != state.get("instance_id"):
        raise RuntimeError("API identity changed; refusing to control an unrelated service")
    data_root = json.loads(fetch(f"{api_url}/api/library/status"))
    if require_idle and data_root.get("processing"):
        raise RuntimeError("A processing task is running. Stop it in the app before stopping/restarting.")
    path = Path(data_root["data_root"]).resolve()
    expected = library_identity(PROJECT_ROOT, path / "project.db")
    if data_root.get("library_id") != expected:
        raise RuntimeError("Live data_root identity does not match its database")
    return path


def stop_services(state: dict, api_port: int, frontend_port: int) -> None:
    records = webui_process.snapshot()
    roots = []
    saved_records = state.get("owned_processes", {})
    manages_vite = state.get("frontend_mode") != "static" or bool(
        state.get("owned_processes", {}).get("vite") or state.get("owned_pids", {}).get("vite")
    )
    services = [("api", api_port)] + ([("vite", frontend_port)] if manages_vite else [])
    for service, port in services:
        service_roots = []
        saved = saved_records.get(service, [])
        if not saved:
            pid = state.get("owned_pids", {}).get(service)
            saved = [{"pid": pid}] if pid else []
        for identity in saved:
            record = records.get(identity["pid"])
            if record is None:
                continue
            if (
                not webui_process.matches_service(record, service, PROJECT_ROOT, port)
                or (identity.get("created") and identity["created"] != record["created"])
            ):
                raise RuntimeError(f"Saved {service} process identity changed; no service was stopped")
            service_roots.append(record["pid"])
        if not service_roots and port_available(API_HOST, port):
            raise RuntimeError(f"{service} port is occupied without a verified owned process")
        roots.extend(service_roots)
    members = webui_process.descendants(roots, records)
    # A verified API may launch this restart helper. Keep the helper and any
    # venv redirector ancestors alive while terminating the old API tree.
    controller_chain = set()
    controller = os.getpid()
    while controller in records and controller not in roots and controller not in controller_chain:
        controller_chain.add(controller)
        controller = records[controller]["parent"]
    members = [pid for pid in members if pid not in controller_chain]
    webui_process.stop_verified([records[pid] for pid in members])
    deadline = time.monotonic() + 5
    while any(port_available(API_HOST, port) for _service, port in services):
        if time.monotonic() >= deadline:
            raise RuntimeError("A service port is still occupied; restart was cancelled")
        time.sleep(.1)
    state.update(owned_pids={}, owned_processes={}, status="stopped")
    write_state(state)


def main(action: str = "start") -> int:
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    logger = launcher_logger(LOG_DIR)
    owned: list[tuple[str, subprocess.Popen]] = []
    successful = False
    previous_environment = {}
    stage = "lock"
    library_id = "unknown"
    api_url = f"http://{API_HOST}:{API_PORT}"
    frontend_url = f"http://{FRONTEND_HOST}:{FRONTEND_PORT}"
    api_port = API_PORT
    frontend_port = FRONTEND_PORT
    desktop = os.getenv("CLASSROOM_DESKTOP_MODE") == "1"
    is_frontend_ready = static_frontend_is_ready if desktop else frontend_is_ready
    launch_lock = FileLock(str(RUNTIME_ROOT / "frontend_start.lock"), timeout=STARTUP_TIMEOUT + 10)
    try:
        stage = "configuration"
        if action not in {"start", "stop", "restart"}:
            raise ValueError(f"Unknown action: {action}")
        launch_lock.acquire()
        previous = read_state()
        expected_instance = os.getenv("CLASSROOM_EXPECT_INSTANCE_ID")
        if expected_instance and previous.get("instance_id") != expected_instance:
            raise RuntimeError("Server identity changed; automatic data relocation restart was cancelled")
        expected_migration = os.getenv("CLASSROOM_DATA_MIGRATION_ID")
        if expected_migration and (application_data_migration.read_journal(PROJECT_ROOT) or {}).get("id") != expected_migration:
            raise RuntimeError("Data relocation request changed; automatic restart was cancelled")
        # Stop always targets the recorded pair, even if the caller sets new ports.
        api_port = int(previous.get("api_port", API_PORT))
        frontend_port = int(previous.get("frontend_port", FRONTEND_PORT))
        if action == "start":
            api_port = int(os.getenv("CLASSROOM_API_PORT", str(api_port)))
            frontend_port = int(os.getenv("CLASSROOM_FRONTEND_PORT", str(frontend_port)))
        if not (1 <= api_port <= 65535 and 1 <= frontend_port <= 65535):
            raise ValueError("API/frontend ports must be between 1 and 65535")
        api_url = f"http://{API_HOST}:{api_port}"
        frontend_url = f"http://{FRONTEND_HOST}:{frontend_port}"
        if desktop:
            frontend_url = api_url
            if action != "stop" and not (FRONTEND_ROOT / "dist" / "index.html").is_file():
                raise RuntimeError("桌面页面尚未构建。请先运行桌面准备入口，或在 frontend 中执行 npm run build。")
        stage = "data_root"
        active_data_root = None
        if previous and (action != "start" or api_port == previous.get("api_port")):
            active_data_root = live_data_root(previous, api_url, require_idle=action != "start")
            if desktop and action == "start" and active_data_root is not None:
                current = health(api_url, previous["library_id"])
                if current and not static_frontend_is_ready(api_url, previous["library_id"], current["instance_id"]):
                    live_data_root(previous, api_url, require_idle=True)
                    stop_services(previous, api_port, frontend_port)
                    active_data_root = None
        data_override = os.getenv("CLASSROOM_DATA_ROOT")
        if data_override:
            data_root = Path(data_override).expanduser().resolve()
        else:
            data_root = active_data_root or (
                Path(previous["data_root"]).resolve()
                if previous.get("data_root") else load_data_root()
            )
        # Set the resolved data_root for both spawned services, not just identity checks.
        library_id = library_identity(PROJECT_ROOT, data_root / "project.db")
        if action in {"stop", "restart"}:
            stage = "stop"
            if previous:
                previous["data_root"] = str(active_data_root or data_root)
                previous["library_id"] = library_identity(PROJECT_ROOT, (active_data_root or data_root) / "project.db")
                write_state(previous)
                stop_services(previous, api_port, frontend_port)
            elif port_available(API_HOST, api_port) or port_available(FRONTEND_HOST, frontend_port):
                raise RuntimeError("No runtime ownership record; refusing to stop untracked services")
            if action == "stop":
                print("Frontend and API stopped.")
                return 0
        if active_data_root is None or action == "restart":
            try:
                data_root = application_data_migration.apply_pending(PROJECT_ROOT, data_root / "project.db")
                library_id = library_identity(PROJECT_ROOT, data_root / "project.db")
            except Exception as exc:
                logger.exception("Application data relocation failed; restarting the original library")
                record = application_data_migration.read_journal(PROJECT_ROOT)
                if record and record["status"] != "completed":
                    record.update(status="failed", message=str(exc))
                    application_data_migration._write_json(application_data_migration.journal_path(PROJECT_ROOT), record)
                print("应用数据迁移失败，继续使用原目录启动。请在设置中查看原因。")
        previous_environment = {
            key: os.environ.get(key) for key in ("CLASSROOM_DATA_ROOT", "CLASSROOM_API_PORT")
        }
        os.environ["CLASSROOM_DATA_ROOT"] = str(data_root)
        os.environ["CLASSROOM_API_PORT"] = str(api_port)
        deadline = time.monotonic() + STARTUP_TIMEOUT
        stage = "api identity"
        api_health = health(api_url, library_id)
        initial_api_port = api_port
        if api_health is None:
            for attempt in range(API_BIND_ATTEMPTS):
                selected = select_api_port(api_port if attempt == 0 else 0)
                if selected != api_port:
                    logger.info("service=api phase=port-fallback old_port=%s port=%s", api_port, selected)
                api_port = selected
                api_url = f"http://{API_HOST}:{api_port}"
                os.environ["CLASSROOM_API_PORT"] = str(api_port)
                if desktop:
                    frontend_url = api_url
                stage = "api spawn"
                process = start_process(
                    [sys.executable, str(PROJECT_ROOT / "scripts" / "api_service.py"),
                     "--host", API_HOST, "--port", str(api_port), "--log-dir", str(LOG_DIR)],
                    PROJECT_ROOT, "api",
                )
                owned.append(("api", process))
                logger.info("service=api pid=%s phase=spawn port=%s log=%s", process.pid, api_port, LOG_DIR)
                stage = "api readiness"
                api_health = health(api_url, library_id)
                while api_health is None:
                    exitcode = process.poll()
                    if exitcode is not None:
                        if exitcode == API_BIND_FAILED and attempt + 1 < API_BIND_ATTEMPTS:
                            terminate_owned_tree(process)
                            owned.remove(("api", process))
                            break
                        raise RuntimeError(f"service=api pid={process.pid} exitcode={exitcode}")
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"API did not become ready within {STARTUP_TIMEOUT:g}s")
                    time.sleep(0.25)
                    api_health = health(api_url, library_id)
                if api_health is not None:
                    break

        stage = "vite identity"
        frontend_occupied = desktop or port_available(FRONTEND_HOST, frontend_port)
        if (
            not desktop and api_port != initial_api_port and frontend_occupied
            and not frontend_is_ready(frontend_url, library_id, api_health["instance_id"])
        ):
            # An existing Vite still proxies the old API. Leave it untouched.
            with bind_server_socket(FRONTEND_HOST, 0) as available:
                frontend_port = available.getsockname()[1]
            frontend_url = f"http://{FRONTEND_HOST}:{frontend_port}"
            frontend_occupied = False
        if frontend_occupied:
            if not is_frontend_ready(frontend_url, library_id, api_health["instance_id"]):
                raise RuntimeError(f"Vite port {frontend_port} is occupied by a different or unready service")
        else:
            stage = "vite spawn"
            node = shutil.which("node")
            vite = FRONTEND_ROOT / "node_modules" / "vite" / "bin" / "vite.js"
            if node is None or not vite.is_file():
                raise RuntimeError("Node/Vite is missing; install Node and run npm install in frontend")
            process = start_process(
                [node, "--require", str(PROJECT_ROOT / "scripts" / "vite_logging.cjs"),
                 str(vite), "--host", FRONTEND_HOST, "--port", str(frontend_port), "--strictPort"],
                FRONTEND_ROOT, "vite",
            )
            owned.append(("vite", process))
            logger.info("service=vite pid=%s phase=spawn port=%s log=%s", process.pid, frontend_port, LOG_DIR)

        stage = "readiness"
        while time.monotonic() < deadline:
            for service, process in owned:
                if process.poll() is not None:
                    raise RuntimeError(f"service={service} pid={process.pid} exitcode={process.returncode}")
            api_health = health(api_url, library_id)
            if api_health and is_frontend_ready(frontend_url, library_id, api_health["instance_id"]):
                same_instance = previous.get("instance_id") == api_health["instance_id"]
                previous_pids = {
                    service: pid for service, pid in previous.get("owned_pids", {}).items()
                    if same_instance or (
                        service == "vite" and previous.get("frontend_port") == frontend_port
                    )
                }
                previous_processes = {
                    service: identities for service, identities in previous.get("owned_processes", {}).items()
                    if service in previous_pids
                }
                process_records = {}
                for service, process in owned:
                    if webui_process.creation_time(process.pid):
                        process_records[service] = webui_process.service_records(
                            process.pid, service, PROJECT_ROOT,
                            api_port if service == "api" else frontend_port,
                        )
                        if not process_records[service]:
                            raise RuntimeError(f"Unable to record {service} process ownership")
                state = {
                    "library_id": library_id, "instance_id": api_health["instance_id"],
                    "data_root": str(data_root), "status": "running",
                    "frontend_mode": "static" if desktop else "vite",
                    "frontend_url": frontend_url,
                    "api_port": api_port, "frontend_port": frontend_port,
                    "owned_pids": {**previous_pids, **{service: process.pid for service, process in owned}},
                    "owned_processes": {**previous_processes, **process_records},
                }
                write_state(state)
                successful = True
                logger.info("phase=ready data_root=%s instance=%s url=%s", library_id, api_health["instance_id"], frontend_url)
                print(f"Frontend: {frontend_url}/")
                print(f"API: {api_url}")
                if os.getenv("CLASSROOM_APP_NO_BROWSER") != "1":
                    try:
                        webbrowser.open(frontend_url, new=2)
                    except Exception:
                        logger.exception("phase=browser url=%s", frontend_url)
                return 0
            time.sleep(0.25)
        raise TimeoutError(f"Services did not become ready within {STARTUP_TIMEOUT:g}s")
    except (Exception, KeyboardInterrupt) as exc:
        logger.exception("phase=%s data_root=%s api_port=%s vite_port=%s log=%s", stage, library_id, api_port, frontend_port, LOG_DIR)
        print(f"Launch failed: phase={stage}; {exc}; logs={LOG_DIR}", file=sys.stderr)
        return 1
    finally:
        for service, process in reversed(owned):
            if successful:
                release_cleanup_job(process)
            else:
                try:
                    terminate_owned_tree(process)
                    logger.info("service=%s pid=%s phase=cleanup exitcode=%s", service, process.pid, process.returncode)
                except (OSError, subprocess.SubprocessError):
                    logger.exception("service=%s pid=%s phase=cleanup", service, process.pid)
        for key, value in previous_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        launch_lock.release()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Control the local frontend and API together.")
    parser.add_argument("action", nargs="?", default="start", choices=("start", "stop", "restart"))
    raise SystemExit(main(parser.parse_args().action))
