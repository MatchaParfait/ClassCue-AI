"""Windows shortcuts, tray menu and per-user desktop preferences."""

from pathlib import Path
import re
import subprocess

from classcue.asr_storage import application_config_directory
from classcue.desktop_runtime import APP_NAME, atomic_json, read_json


def normalize_hotkey(value):
    value = str(value or "").upper().replace(" ", "")
    if not value:
        return ""
    if not re.fullmatch(r"CTRL\+(?:ALT|SHIFT)\+[A-Z0-9]", value):
        raise ValueError("快捷键请使用 Ctrl+Alt+字母/数字，或 Ctrl+Shift+字母/数字。")
    return value


def preferences(project):
    return read_json(application_config_directory(Path(project)) / "desktop_settings.json", {})


def save_close_behavior(project, value):
    if value not in {"ask", "background", "exit"}:
        raise ValueError("不支持的关闭窗口行为。")
    atomic_json(application_config_directory(Path(project)) / "desktop_settings.json", {
        **preferences(project), "close_behavior": value,
    })


def install_shortcuts(project, hotkey=None):
    project = Path(project).resolve()
    hotkey = normalize_hotkey(preferences(project).get("hotkey") if hotkey is None else hotkey)
    command = [
        "powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(project / "scripts" / "install_desktop_shortcuts.ps1"),
        "-ProjectRoot", str(project), "-AppName", APP_NAME, "-Hotkey", hotkey,
    ]
    result = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW, timeout=20,
    )
    if result.returncode:
        raise RuntimeError("无法更新桌面/开始菜单快捷方式，请检查目录权限。")
    atomic_json(application_config_directory(project) / "desktop_settings.json", {
        **preferences(project), "hotkey": hotkey, "shortcuts_installed": True,
    })
    return {"hotkey": hotkey, "shortcuts_installed": True}


class Tray:
    def __init__(self, project, *, show, settings, exit_app):
        import pystray
        from PIL import Image
        self.icon = pystray.Icon(
            "classroom-video-study", Image.open(Path(project) / "assets" / "desktop.ico"),
            APP_NAME, menu=pystray.Menu(
                pystray.MenuItem("打开主窗口", lambda *_args: show(), default=True),
                pystray.MenuItem("快捷键设置…", lambda *_args: settings()),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出", lambda *_args: exit_app()),
            ),
        )

    def start(self):
        self.icon.run_detached()
        self.icon.visible = True

    def stop(self):
        self.icon.stop()
