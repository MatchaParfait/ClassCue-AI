"""Native Windows entry point. All application services remain separately owned."""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
from pathlib import Path
import sys
from threading import Thread
import time
from urllib.parse import urlsplit
from urllib.error import HTTPError

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from desktop_runtime import APP_NAME, Lifecycle, Services, SingleInstance
from desktop_startup import startup_html, startup_transition_script
from scripts.desktop_transition import install_startup_transition


def background(function, *args, **kwargs):
    thread = Thread(target=function, args=args, kwargs=kwargs, daemon=True)
    thread.start()
    return thread


class WindowUI:
    def __init__(self):
        self.window = None
        try:
            from app_storage import load_data_root
            from asr_storage import library_identity
            library_id = library_identity(PROJECT, load_data_root() / "project.db")
        except (OSError, ValueError):
            library_id = None
        self.boot_html = startup_html(PROJECT, library_id)
        self.library_id = library_id
        self.startup_view = None
        self.remove_startup_transition = None
        self.boot_origin = None
        self.current_url = None
        self.last_progress = None

    def script(self, expression):
        try:
            return self.window.evaluate_js(expression)
        except Exception:
            logging.getLogger(__name__).exception("Desktop page notification failed")

    def progress(self, message):
        self.last_progress = message
        if self.window.events.loaded.is_set():
            self.script(f"window.__classroomDesktopProgress?.({json.dumps(message)})")

    def error(self, message):
        self.current_url = None
        # Store the message for the loaded event instead of racing navigation.
        self.last_error = message
        self.window.load_html(self.boot_html)

    def open(self, origin):
        # Service preparation runs concurrently with WebView's first load.
        self.window.events.loaded.wait(15)
        view = self.script("window.__classroomDesktopView?.() ?? null")
        if isinstance(view, dict):
            self.startup_view = {**view, "library_id": self.library_id}
            if self.remove_startup_transition:
                self.remove_startup_transition()
            try:
                self.remove_startup_transition = install_startup_transition(
                    self.window, startup_transition_script(PROJECT, self.boot_html, origin, self.startup_view))
            except (OSError, ValueError):
                self.remove_startup_transition = None
                logging.getLogger(__name__).warning("Startup preview transition unavailable", exc_info=True)
        self.last_error = None
        self.current_url = origin + "/"
        self.window.load_url(origin + "/")

    def navigation_loaded(self):
        if self.current_url and self.remove_startup_transition and self.window.get_current_url() == self.current_url:
            self.remove_startup_transition()
            self.remove_startup_transition = None

    def show(self):
        self.window.show()
        try:
            if ctypes.windll.user32.IsIconic(self.window.native.Handle.ToInt64()):
                self.window.restore()
        except Exception:
            self.window.restore()

    def hide(self):
        self.script("document.querySelectorAll('video,audio').forEach(media=>media.pause())")
        self.window.hide()

    def minimize(self):
        self.window.minimize()

    def dialog(self, value):
        self.script("window.dispatchEvent(new CustomEvent('classroom-desktop-dialog',"
                    f"{{detail:{json.dumps(value)}}}))")

    def destroy(self):
        self.window.destroy()


class NativeAPI:
    def __init__(self, lifecycle, ui):
        self._lifecycle, self._ui = lifecycle, ui

    def _check_origin(self):
        current = self._ui.window.get_current_url()
        origin = self._lifecycle.services.origin
        if current:
            parsed = urlsplit(current)
            if origin and f"{parsed.scheme}://{parsed.netloc}" == origin:
                return
        if not self._lifecycle.ready and current in {None, "about:blank", self._ui.boot_origin}:
            return
        raise PermissionError("桌面控制仅允许应用自己的页面调用。")

    def describe(self):
        self._check_origin()
        from desktop_windows import preferences
        return {
            "name": APP_NAME, "tray": self._lifecycle.tray_available,
            "dialog": self._lifecycle.dialog, "hotkey": preferences(PROJECT).get("hotkey", ""),
            "close_behavior": self._lifecycle.close_behavior,
        }

    def respond(self, action, remember=False):
        self._check_origin()
        self._lifecycle.respond(action, remember)
        return {"ok": True}

    def get_startup_view(self):
        self._check_origin()
        return self._ui.startup_view

    def clear_startup_view(self):
        self._check_origin()
        self._ui.startup_view = None
        return {"ok": True}

    def set_close_behavior(self, value):
        self._check_origin()
        self._lifecycle.set_close_behavior(value)
        return {"close_behavior": self._lifecycle.close_behavior}

    def set_unsaved_changes(self, value):
        self._check_origin()
        self._lifecycle.unsaved_changes = bool(value)
        return {"ok": True}

    def retry(self):
        self._check_origin()
        from application_data_migration import read_journal
        migration = read_journal(PROJECT)
        if migration and migration.get("status") in {"pending", "copying", "committed"}:
            raise RuntimeError("应用数据正在迁移，请等待原重启流程完成。")
        background(self._lifecycle.start)
        return {"ok": True}

    def set_hotkey(self, hotkey):
        self._check_origin()
        from desktop_windows import install_shortcuts
        return install_shortcuts(PROJECT, hotkey)

    def hide_to_tray(self):
        self._check_origin()
        if not self._lifecycle.tray_available or not self._lifecycle.ready:
            raise RuntimeError("托盘或服务尚未就绪。")
        self._lifecycle.hidden = True
        self._ui.hide()
        return {"ok": True}

    def exit_application(self):
        self._check_origin()
        background(self._lifecycle.close_requested, exit_app=True)
        return {"ok": True}

    def open_logs(self):
        self._check_origin()
        from asr_storage import log_directory
        os.startfile(str(log_directory(PROJECT)))
        return {"ok": True}

    def install_update(self):
        self._check_origin()
        lifecycle = self._lifecycle
        with lifecycle.guard:
            if lifecycle.unsaved_changes or not lifecycle.ready or lifecycle.starting or lifecycle.exiting:
                raise RuntimeError("请先保存设置，并等待服务就绪。")
            lifecycle.exiting = True
        from desktop_runtime import http_json, read_json
        from app_update_install import prepare_helper, spawn_helper
        from app_updates import updates_dir
        services = lifecycle.services
        process = None
        reserved = False
        try:
            # Validate launcher identity before the native-only reservation.
            services.status()
            http_json(f"{services.origin}/api/updates/prepare-install", method="POST",
                      payload={}, token=services.update_token, timeout=15)
            reserved = True
            directory = updates_dir(PROJECT)
            plan = read_json(directory / "install-plan.json")
            helper = prepare_helper(PROJECT, plan)
            process = spawn_helper(PROJECT, plan, helper)
            deadline = time.monotonic() + 120
            while read_json(directory / "helper-ready.json", {}).get("id") != plan["id"]:
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("更新包预检未完成，请查看 .runtime/updates/worker-error.json。")
                time.sleep(.1)
            services.stop(cancel=False)
            lifecycle.destroy()
            return {"ok": True}
        except Exception as error:
            lifecycle.exiting = False
            if process is not None and process.poll() is None:
                # This is our staged helper, still waiting for this host's lock.
                process.terminate()
                process.wait(timeout=10)
            if reserved:
                try:
                    http_json(f"{services.origin}/api/updates/cancel-install", method="POST",
                              payload={}, token=services.update_token)
                except OSError:
                    background(lifecycle.start, "start")
            if isinstance(error, HTTPError):
                try:
                    detail = json.load(error).get("detail", "")
                except (ValueError, OSError):
                    detail = ""
                raise RuntimeError(detail or "更新准备失败，请完整退出并重新启动桌面程序后重试。") from error
            raise


def main(action="activate"):
    if os.name != "nt":
        raise RuntimeError("当前桌面入口面向 Windows；其他平台请使用网页入口。")
    from asr_storage import log_directory
    from scripts.launcher_process import BoundedLogStream, service_logger
    logger = service_logger(log_directory(PROJECT), "desktop")
    sys.stdout = BoundedLogStream(logger, logging.INFO)
    sys.stderr = BoundedLogStream(logger, logging.ERROR)
    ui = WindowUI()
    from desktop_windows import preferences, save_close_behavior
    lifecycle = Lifecycle(Services(PROJECT), ui,
                          close_behavior=preferences(PROJECT).get("close_behavior", "ask"),
                          save_close_behavior=lambda value: save_close_behavior(PROJECT, value))
    tray = None

    def dispatch(command):
        if command == "status":
            return {
                "ready": lifecycle.ready, "starting": lifecycle.starting,
                "exiting": lifecycle.exiting, "visible": not lifecycle.hidden,
                "dialog": lifecycle.dialog, "tray": lifecycle.tray_available,
                "unsaved_changes": lifecycle.unsaved_changes,
                "pid": os.getpid(),
                "url": ui.current_url,
            }
        if command == "activate":
            if ui.window is not None:
                background(lifecycle.show)
        elif command == "exit":
            background(lifecycle.close_requested, exit_app=True)
        elif command == "restart":
            def restart():
                try:
                    if lifecycle.services.status().get("processing"):
                        lifecycle.prompt("starting", "仍有处理任务，请完成或停止任务后再重启。")
                    else:
                        lifecycle.start("restart")
                except Exception as error:
                    lifecycle.prompt("service-error", str(error))
            background(restart)
        return {"ok": True}

    instance = SingleInstance(PROJECT / ".runtime", dispatch)
    if not instance.acquire():
        try:
            instance.send(action)
            return 0
        finally:
            instance.close()
    if action == "exit":
        instance.close()
        from scripts import start_frontend
        os.environ["CLASSROOM_APP_NO_BROWSER"] = "1"
        return start_frontend.main("stop")

    try:
        import webview
        from desktop_windows import Tray, install_shortcuts, preferences
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ClassroomVideoStudy.Desktop")
        webview.settings.update(ALLOW_DOWNLOADS=True, ALLOW_FILE_URLS=False)
        if os.getenv("CLASSROOM_DESKTOP_DEBUG_PORT"):
            webview.settings["REMOTE_DEBUGGING_PORT"] = int(os.environ["CLASSROOM_DESKTOP_DEBUG_PORT"])
            webview.settings["OPEN_DEVTOOLS_IN_DEBUG"] = False
        window = webview.create_window(
            APP_NAME, html=ui.boot_html, js_api=NativeAPI(lifecycle, ui),
            width=1280, height=850, min_size=(800, 600), text_select=True,
            background_color="#F8F5E9",
        )
        ui.window = window

        def on_loaded():
            ui.navigation_loaded()
            ui.current_url = window.get_current_url()
            if not lifecycle.ready:
                ui.boot_origin = window.get_current_url()
            if getattr(ui, "last_error", None):
                ui.script(f"window.__classroomDesktopProgress?.({json.dumps(ui.last_error)},true)")
            elif not lifecycle.ready and ui.last_progress:
                ui.script(f"window.__classroomDesktopProgress?.({json.dumps(ui.last_progress)})")

        def on_closing():
            if lifecycle.allow_destroy:
                return True
            background(lifecycle.close_requested)
            return False

        window.events.loaded += on_loaded
        window.events.closing += on_closing

        def run():
            nonlocal tray
            window.events.loaded.wait(15)
            try:
                tray = Tray(
                    PROJECT, show=lambda: background(lifecycle.show),
                    settings=lambda: lifecycle.prompt("hotkey", "设置启动或唤起应用的快捷键。"),
                    exit_app=lambda: background(lifecycle.close_requested, exit_app=True),
                )
                tray.start()
                lifecycle.tray_available = True
            except Exception:
                logger.exception("Tray unavailable; hiding the window is disabled")
            if not preferences(PROJECT).get("shortcuts_installed"):
                try:
                    install_shortcuts(PROJECT)
                except Exception:
                    logger.exception("Shortcut installation failed")

        # Prepare the API while WebView2 itself is initializing, not after its first load.
        background(lifecycle.start, "restart" if action == "restart" else "start")
        webview.start(
            run, gui="edgechromium", private_mode=False,
            storage_path=str(PROJECT / ".runtime" / "webview"),
            icon=str(PROJECT / "assets" / "desktop.ico"),
        )
        return 0
    except Exception:
        logger.exception("Desktop host failed")
        ctypes.windll.user32.MessageBoxW(
            None, "桌面窗口未能打开。请通过桌面启动入口重试，并查看日志。\n"
            "需要 Windows WebView2 Runtime；网页启动入口仍可使用。",
            APP_NAME, 0x10,
        )
        return 1
    finally:
        if tray is not None:
            tray.stop()
        instance.close()
        sys.stdout.flush()
        sys.stderr.flush()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--action", choices=["activate", "exit", "restart"], default="activate")
    raise SystemExit(main(parser.parse_args().action))
