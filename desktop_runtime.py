"""Desktop lifecycle and authenticated single-instance IPC, independent of the GUI."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import socket
import errno
from threading import Event, Lock, Thread, Timer
import time
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener, urlopen

from filelock import FileLock, Timeout


APP_NAME = "课堂重点复习"
EXIT_FOREGROUND_SECONDS = 0.5


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def http_json(url, *, method="GET", payload=None, timeout=3, token=None):
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    # Desktop control is local IPC, including when a system/VPN proxy is set.
    open_url = (build_opener(ProxyHandler({})).open
                if urlsplit(url).hostname in {"127.0.0.1", "::1", "localhost"} else urlopen)
    with open_url(Request(url, data=data, headers=headers, method=method), timeout=timeout) as response:
        return json.load(response)


class SingleInstance:
    def __init__(self, runtime, dispatch):
        self.runtime = Path(runtime)
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.path = self.runtime / "desktop_instance.json"
        self.lock = FileLock(str(self.runtime / "desktop_instance.lock"), timeout=0)
        self.dispatch = dispatch
        self.server = None
        self.thread = None
        self.token = secrets.token_urlsafe(32)

    def acquire(self):
        try:
            self.lock.acquire()
        except Timeout:
            return False
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                if self.path != "/command" or not hmac.compare_digest(
                    self.headers.get("Authorization", ""), f"Bearer {owner.token}",
                ):
                    self.send_error(403)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 2048 or self.headers.get("Origin"):
                        raise ValueError("Invalid desktop request")
                    action = json.loads(self.rfile.read(length))["action"]
                    if action not in {"activate", "exit", "restart", "status"}:
                        raise ValueError("Unknown desktop action")
                    result = owner.dispatch(action)
                    encoded = json.dumps(result or {"ok": True}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)
                except (ValueError, KeyError, TypeError):
                    self.send_error(400)

        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            self.thread = Thread(target=self.server.serve_forever, name="desktop-ipc", daemon=True)
            self.thread.start()
            atomic_json(self.path, {
                "service": "classroom-desktop", "pid": os.getpid(),
                "port": self.server.server_port, "token": self.token,
            })
            return True
        except BaseException:
            self.close()
            raise

    def send(self, action, *, attempts=30):
        # The second launch can arrive before the first has written its port.
        for _ in range(attempts):
            state = read_json(self.path, {})
            if state.get("service") == "classroom-desktop":
                try:
                    return http_json(
                        f"http://127.0.0.1:{int(state['port'])}/command",
                        method="POST", payload={"action": action}, token=state["token"],
                    )
                except (OSError, ValueError, KeyError):
                    pass
            time.sleep(.1)
        raise RuntimeError("已有桌面实例正在启动或退出，请稍后重试。")

    def close(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            if self.thread is not None:
                self.thread.join(timeout=3)
        if self.lock.is_locked:
            if read_json(self.path, {}).get("token") == self.token:
                self.path.unlink(missing_ok=True)
            self.lock.release()


class Services:
    def __init__(self, project):
        self.project = Path(project)
        self.origin = ""
        self.instance_id = None
        self.update_token = secrets.token_urlsafe(32)

    def start(self, action="start"):
        from scripts import start_frontend
        os.environ.update(CLASSROOM_DESKTOP_MODE="1", CLASSROOM_APP_NO_BROWSER="1",
                          CLASSROOM_UPDATE_TOKEN=self.update_token)
        if start_frontend.main(action) != 0:
            raise RuntimeError("本地服务未能启动，请查看日志后重试。")
        state = start_frontend.read_state()
        self.origin = f"http://127.0.0.1:{state['api_port']}"
        self.instance_id = state["instance_id"]
        return self.origin

    def status(self):
        # A migration restarts the API with a new identity; accept it only when
        # the owned launcher's record confirms the same library and instance.
        deadline = time.monotonic() + 5
        while True:
            state = http_json(f"{self.origin}/api/library/status")
            health = http_json(f"{self.origin}/api/health")
            recorded = read_json(self.project / ".runtime" / "frontend_state.json", {})
            if (
                health.get("service") != "classroom-video-study"
                or not isinstance(health.get("instance_id"), str) or not health["instance_id"]
                or health.get("library_id") != recorded.get("library_id")
            ):
                raise RuntimeError("服务身份已变化，未执行进程控制。")
            if health.get("instance_id") == recorded.get("instance_id"):
                self.instance_id = health["instance_id"]
                return state
            # The new API can answer before the helper finishes recording its
            # process identities. Wait briefly for that atomic commit.
            if time.monotonic() >= deadline:
                raise RuntimeError("服务身份已变化，未执行进程控制。")
            time.sleep(.1)

    def can_close_offline(self):
        origin = urlsplit(self.origin)
        if origin.hostname != "127.0.0.1" or origin.port is None:
            return False
        try:
            # Windows can retry loopback SYNs for ~2s before reporting refusal.
            with socket.create_connection((origin.hostname, origin.port), timeout=3):
                return False
        except OSError as error:
            # A timeout is uncertainty, not evidence that a task has stopped.
            return error.errno == errno.ECONNREFUSED or getattr(error, "winerror", None) == 10061

    def shutdown_status(self):
        try:
            return self.status()
        except OSError:
            if not self.can_close_offline():
                raise
            logging.getLogger(__name__).info("API no longer listening; using verified launcher cleanup")
            return {"processing": False}

    def stop(self, *, cancel=False, progress=lambda _message: None, timeout=180):
        state = self.shutdown_status()
        if state.get("processing"):
            if not cancel:
                raise RuntimeError("仍有处理任务，请先停止任务或选择后台继续。")
            job_id = state.get("active_job_id")
            if not job_id:
                raise RuntimeError("无法确认正在处理的任务，请回到主窗口查看。")
            try:
                http_json(f"{self.origin}/api/jobs/{job_id}/cancel", method="POST", payload={})
            except OSError:
                if not self.can_close_offline():
                    raise
            deadline = time.monotonic() + timeout
            while self.shutdown_status().get("processing"):
                progress("正在安全停止任务并保存进度…")
                if time.monotonic() >= deadline:
                    raise RuntimeError("任务仍在安全停止中，请稍候再退出。")
                time.sleep(.25)
        from scripts import start_frontend
        if start_frontend.main("stop") != 0:
            raise RuntimeError("未能停止所属服务，请查看日志后重试。")


class Lifecycle:
    """All close/exit paths share the same job-aware state machine."""

    def __init__(self, services, ui, *, close_behavior="ask", save_close_behavior=None):
        self.services, self.ui = services, ui
        self.close_behavior = close_behavior if close_behavior in {"ask", "background", "exit"} else "ask"
        self.save_close_behavior = save_close_behavior or (lambda _value: None)
        self.ready = False
        self.starting = False
        self.exiting = False
        self.allow_destroy = False
        self.dialog = None
        self.tray_available = False
        self.hidden = False
        self.unsaved_changes = False
        self.pending_close_exit = False
        self.guard = Lock()
        self.closed = Event()

    def show(self):
        self.hidden = False
        self.ui.show()

    def prompt(self, kind, message):
        self.dialog = {"kind": kind, "message": message, "tray": self.tray_available}
        self.show()
        self.ui.dialog(self.dialog)

    def start(self, action="start"):
        with self.guard:
            if self.starting or self.exiting or self.allow_destroy:
                return
            self.starting = True
        try:
            self.ui.progress("正在启动本地服务…")
            origin = self.services.start(action)
            self.ready = True
            self.ui.open(origin)
        except Exception as error:
            logging.getLogger(__name__).exception("Desktop service startup failed")
            self.ready = False
            self.ui.error(str(error))
        finally:
            self.starting = False

    def close_requested(self, *, exit_app=False, ignore_unsaved=False):
        if self.exiting:
            return
        if self.starting:
            self.prompt("starting", "正在启动服务，请稍候再退出。")
            return
        if not self.ready:
            # Startup failure cleanup is owned by the existing launcher.
            self.destroy()
            return
        if self.unsaved_changes and not ignore_unsaved:
            if self.dialog and self.dialog["kind"] == "unsaved-close":
                self.pending_close_exit = self.pending_close_exit or exit_app
                self.show()
                return
            self.pending_close_exit = exit_app
            self.prompt("unsaved-close", "设置有未保存的修改，请先保存或放弃修改。")
            return
        if not exit_app:
            if self.close_behavior == "background" and self.tray_available:
                self.hidden = True
                self.ui.hide()
                return
            if self.close_behavior != "exit":
                self.prompt("close-choice", "关闭窗口后，你希望应用在托盘后台运行，还是直接退出？")
                return
        try:
            if self.services.status().get("processing"):
                self.prompt(
                    "exit-task", "当前仍有处理任务。退出应用将先停止任务并保存进度。",
                )
            else:
                self.exit()
        except Exception as error:
            if isinstance(error, OSError) and getattr(self.services, "can_close_offline", lambda: False)():
                self.exit()
            else:
                logging.getLogger(__name__).warning("Desktop close status check failed: %s", error)
                self.prompt("service-error", f"暂时无法确认任务状态：{error}")

    def set_close_behavior(self, value):
        if value not in {"ask", "background", "exit"}:
            raise ValueError("不支持的关闭窗口行为。")
        self.save_close_behavior(value)
        self.close_behavior = value

    def respond(self, action, remember=False):
        if action == "cancel":
            self.dialog = None
            return
        if action == "continue-close" and self.dialog and self.dialog["kind"] == "unsaved-close":
            self.dialog = None
            self.unsaved_changes = False
            self.close_requested(exit_app=self.pending_close_exit, ignore_unsaved=True)
            return
        if self.dialog and self.dialog["kind"] == "close-choice" and action in {"background", "exit"}:
            if action == "background" and not self.tray_available:
                raise RuntimeError("托盘不可用，请选择退出或取消关闭。")
            if remember:
                self.set_close_behavior(action)
            self.dialog = None
            if action == "background":
                self.hidden = True
                self.ui.hide()
            else:
                self.close_requested(exit_app=True)
            return
        if action == "background" and self.dialog and self.dialog["kind"] == "close-task":
            if not self.tray_available:
                raise RuntimeError("托盘不可用，无法在后台隐藏窗口。")
            self.dialog = None
            self.hidden = True
            self.ui.hide()
        elif action == "stop-exit" and self.dialog and self.dialog["kind"] in {"close-task", "exit-task"}:
            self.dialog = None
            self.exit(cancel=True)
        elif action == "close-window" and self.dialog and self.dialog["kind"] == "service-error":
            # Explicit escape for unreachable/unverified services. Do not kill
            # processes whose identity or active task cannot be established.
            with self.guard:
                if self.starting or self.exiting:
                    return
                self.exiting = True
            logging.getLogger(__name__).warning("User closed desktop window without stopping services")
            self.dialog = None
            self.destroy()
        elif action == "retry-close":
            self.close_requested(exit_app=True)
        else:
            raise ValueError("该操作不适用于当前窗口状态。")

    def exit(self, *, cancel=False):
        with self.guard:
            if self.exiting:
                return
            self.exiting = True
        finished = Event()

        def minimize_pending_exit():
            with self.guard:
                if finished.is_set() or not self.exiting or self.allow_destroy:
                    return
                try:
                    self.ui.minimize()
                except Exception:
                    logging.getLogger(__name__).exception("Could not minimize pending desktop exit")

        # Bound foreground waiting, not the time available for safe cleanup.
        timer = Timer(EXIT_FOREGROUND_SECONDS, minimize_pending_exit)
        timer.daemon = True
        timer.start()
        try:
            self.ui.progress("正在退出…" if not cancel else "正在安全停止任务…")
            self.services.stop(cancel=cancel, progress=self.ui.progress)
            with self.guard:
                finished.set()
            self.destroy()
        except Exception as error:
            logging.getLogger(__name__).exception("Desktop service shutdown failed")
            with self.guard:
                finished.set()
                self.exiting = False
            self.prompt("service-error", str(error))
        finally:
            timer.cancel()

    def destroy(self):
        self.allow_destroy = True
        self.closed.set()
        self.ui.destroy()
