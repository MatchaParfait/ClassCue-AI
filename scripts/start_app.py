"""Start Streamlit in the background and open the local app in a browser."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.launcher_process import windows_process_is_alive
from asr_storage import log_directory


RUNTIME_ROOT = PROJECT_ROOT / ".runtime"
LOG_DIR = log_directory(PROJECT_ROOT)
STATE_PATH = RUNTIME_ROOT / "app_state.json"
START_PORT = 8503


def url_is_ready(url: str, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 500
    except (OSError, urllib.error.URLError):
        return False


def streamlit_url_is_ready(url: str, timeout: float = 0.25) -> bool:
    """Check Streamlit's health endpoint so unrelated local services are ignored."""
    return url_is_ready(f"{url.rstrip('/')}/_stcore/health", timeout=timeout)


def process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return windows_process_is_alive(pid)
    try:
        os.kill(pid, 0)
        return True
    except (OSError, SystemError):
        return False


def load_running_url() -> str | None:
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        pid = int(state["pid"])
        url = str(state["url"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return url if process_is_alive(pid) and streamlit_url_is_ready(url) else None


def find_existing_app(start: int = START_PORT, attempts: int = 100) -> str | None:
    """Recover an app whose state file was overwritten or removed by another launch."""
    for port in range(start, start + attempts):
        url = f"http://127.0.0.1:{port}"
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.05)
            try:
                if probe.connect_ex(("127.0.0.1", port)) != 0:
                    continue
            except OSError:
                continue
        if streamlit_url_is_ready(url):
            return url
    return None


def find_free_port(start: int = START_PORT, attempts: int = 100) -> int:
    for port in range(start, start + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("没有找到可用的本地端口，请关闭部分本地服务后重试。")


def open_browser(url: str) -> None:
    if os.getenv("CLASSROOM_APP_NO_BROWSER") != "1":
        webbrowser.open(url, new=2)


def start_server(port: int) -> subprocess.Popen[bytes]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stdout = (LOG_DIR / "streamlit.stdout.log").open("ab")
    stderr = (LOG_DIR / "streamlit.stderr.log").open("ab")
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(PROJECT_ROOT / "video_app.py"),
        "--server.address",
        "127.0.0.1",
        "--server.port",
        str(port),
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    ]
    creationflags = 0
    if os.name == "nt":
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            | subprocess.NORMAL_PRIORITY_CLASS
        )
    return subprocess.Popen(
        command,
        cwd=PROJECT_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        creationflags=creationflags,
        close_fds=True,
    )


def main() -> int:
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    running_url = load_running_url()
    if not running_url:
        running_url = find_existing_app()
    if running_url:
        print(f"应用已经运行：{running_url}")
        open_browser(running_url)
        return 0

    STATE_PATH.unlink(missing_ok=True)
    port = find_free_port()
    url = f"http://127.0.0.1:{port}"
    process = start_server(port)
    STATE_PATH.write_text(
        json.dumps({"pid": process.pid, "port": port, "url": url}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            STATE_PATH.unlink(missing_ok=True)
            print(f"Streamlit 提前退出，退出码：{process.returncode}", file=sys.stderr)
            return 1
        if url_is_ready(url):
            print(f"应用已启动：{url}")
            open_browser(url)
            return 0
        time.sleep(0.25)

    process.terminate()
    STATE_PATH.unlink(missing_ok=True)
    print("应用启动超过 60 秒。请查看 .runtime\\logs\\streamlit.stderr.log。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
