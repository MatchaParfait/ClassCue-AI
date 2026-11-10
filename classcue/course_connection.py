"""Local MCP registration, authenticated API access and desktop navigation."""

from __future__ import annotations

import contextlib
import hmac
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from urllib.parse import urlsplit

from filelock import FileLock, Timeout

from classcue.paths import PROJECT_ROOT
from classcue.desktop_runtime import atomic_json, http_json, read_json, SingleInstance
from classcue.playback_links import desktop_review_url


def connection_token(project=PROJECT_ROOT):
    directory = Path(project) / ".runtime"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "course_tools_auth.json"
    with FileLock(str(path.with_suffix(".lock")), timeout=5):
        value = read_json(path, {})
        token = value.get("token")
        if not isinstance(token, str) or len(token) < 32:
            token = secrets.token_urlsafe(32)
            atomic_json(path, {"token": token})
    from classcue.diagnostics import register_secret
    register_secret(token)
    return token


def authenticated(header, token):
    return hmac.compare_digest(header or "", f"Bearer {token}")


def register_application(file, project=PROJECT_ROOT):
    path = Path(file)
    with FileLock(str(path.with_suffix(".lock")), timeout=5):
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if value.get("format") != "classcue-codex-connection" or value.get("version") != 1:
            raise ValueError("插件连接设置格式无效")
        value["application"] = {"project_root": str(Path(project).resolve()),
                                "python": sys.executable}
        atomic_json(path, value)


def ensure_service():
    from classcue.app_storage import load_data_root
    from classcue.asr_storage import library_identity
    from scripts import start_frontend
    expected = library_identity(PROJECT_ROOT, load_data_root() / "project.db")
    state = start_frontend.read_state()
    port = state.get("api_port")
    origin = f"http://127.0.0.1:{port}" if type(port) is int and 0 < port <= 65535 else ""
    health = None
    if origin:
        try:
            health = http_json(origin + "/api/health", timeout=2)
        except (OSError, ValueError):
            pass
    if not health or health.get("library_id") != expected:
        # Refuse to replace any live service recorded for another library.
        if health:
            raise RuntimeError("当前服务连接了其他视频库，请先在应用中检查连接")
        os.environ.update(CLASSROOM_DESKTOP_MODE="1", CLASSROOM_APP_NO_BROWSER="1")
        connection_token()
        with contextlib.redirect_stdout(sys.stderr):
            if start_frontend.main("start") != 0:
                raise RuntimeError("ClassCue 本地服务启动失败")
        state = start_frontend.read_state()
        origin = f"http://127.0.0.1:{int(state['api_port'])}"
        health = http_json(origin + "/api/health")
    if (health.get("service") != "classroom-video-study" or health.get("library_id") != expected
            or health.get("instance_id") != state.get("instance_id")):
        raise RuntimeError("ClassCue 服务身份不匹配")
    return origin, expected


def navigate_desktop(url, *, project=None):
    project = Path(project or PROJECT_ROOT)
    runtime = project / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(str(runtime / "desktop_navigation.lock"), timeout=0):
            return _navigate_desktop(url, project)
    except Timeout:
        raise RuntimeError("另一段回放正在打开，请稍后重试。") from None


def _navigate_desktop(url, project):
    original_url = url
    url = desktop_review_url(url)
    instance = SingleInstance(project / ".runtime", lambda _: None)
    try:
        state = instance.send("status", attempts=1)
    except RuntimeError:
        subprocess.Popen(
            [sys.executable, str(project / "scripts" / "desktop_app.py")],
            cwd=project, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        state = {}
    deadline = time.monotonic() + 45
    while not (state.get("ready") and state.get("window_ready")
               and not state.get("starting") and not state.get("exiting")):
        if time.monotonic() >= deadline:
            raise RuntimeError("桌面窗口尚未就绪，未确认播放定位")
        time.sleep(.25)
        try:
            state = instance.send("status", attempts=1)
        except RuntimeError:
            continue
    if state.get("unsaved_changes"):
        instance.send("activate", attempts=1)
        raise RuntimeError("桌面有未保存的修改，请先保存后再打开视频")
    parsed = urlsplit(url)
    result = instance.send({"action": "navigate", "url": url})
    if not result.get("ok"):
        raise RuntimeError(result.get("error", "桌面未接受导航请求"))
    return {"navigation": "requested", "play_url": original_url, "path": parsed.path}


def call_tool(name, arguments):
    origin, library = ensure_service()
    result = http_json(origin + "/api/course-tools/" + name, method="POST",
                       payload={"library_id": library, "arguments": arguments},
                       token=connection_token(), timeout=45)
    if name == "open_video":
        result.update(navigate_desktop(result["play_url"]))
    return result
