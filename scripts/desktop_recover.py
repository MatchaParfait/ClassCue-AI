"""Explicit one-click recovery for an idle, failed desktop preparation window."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from filelock import FileLock, Timeout

from classcue.desktop_runtime import SingleInstance
from scripts import start_frontend, webui_process


def bootstrap_records(project):
    records = webui_process.snapshot()
    expected = os.path.normcase(str((project / "scripts" / "desktop_bootstrap.ps1").resolve()))
    roots = []
    for pid, record in records.items():
        args = webui_process.command_args(record.get("command") or "")
        if not args or Path(args[0]).name.lower() not in {"powershell.exe", "pwsh.exe"}:
            continue
        options = [arg.lower() for arg in args]
        try:
            target = args[options.index("-file") + 1]
        except (ValueError, IndexError):
            continue
        if not os.path.isabs(target) or os.path.normcase(os.path.abspath(target)) != expected:
            continue
        roots.append(pid)
    members = webui_process.descendants(roots, records)
    console = os.path.normcase(str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/conhost.exe"))
    for pid in set(members) - set(roots):
        args = webui_process.command_args(records[pid].get("command") or "")
        executable = args[0].removeprefix("\\??\\") if args else ""
        if os.path.normcase(executable) != console:
            raise RuntimeError("桌面环境仍在安装或构建，请等待准备结束后再运行恢复；不会中断安装进程。")
    return [records[pid] for pid in members]


def require_idle_services():
    state = start_frontend.read_state()
    if not state:
        return
    port = int(state.get("api_port", start_frontend.API_PORT))
    active = start_frontend.live_data_root(state, f"http://127.0.0.1:{port}", require_idle=True)
    if active is None and start_frontend.port_available("127.0.0.1", port):
        raise RuntimeError("服务仍在监听但无法确认任务状态，恢复已停止；不会强杀可能正在处理的任务。")


def wait_ready(instance, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            state = instance.send("status", attempts=1)
            if state.get("ready") and state.get("visible"):
                return
        except (OSError, ValueError, KeyError, RuntimeError):
            pass
        time.sleep(.5)
    raise RuntimeError("启动请求已发送，但尚未确认桌面窗口就绪。请查看准备窗口或 .runtime/logs/desktop.log；不要反复点击。")


def recover(project=PROJECT):
    runtime = project / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    with FileLock(str(runtime / "desktop_recovery.lock"), timeout=0):
        instance = SingleInstance(runtime, lambda _action: None)
        try:
            instance.send("activate", attempts=2)
            instance.send("restart", attempts=2)
        except (OSError, ValueError, KeyError, RuntimeError):
            pass
        else:
            print("已向现有桌面窗口发送重启请求；有处理任务时由应用提示，不会强制停止。")
            return
        try:
            with FileLock(str(runtime / "desktop_instance.lock"), timeout=0):
                bootstrap_records(project)
                print("正在检查任务并停止本安装的服务……")
                require_idle_services()
                if start_frontend.main("stop") != 0:
                    raise RuntimeError("未能安全停止服务。请先结束处理任务，或检查启动日志；恢复已停止。")
                # Refresh identities after service shutdown; never act on stale PIDs.
                idle = bootstrap_records(project)
                if idle:
                    webui_process.stop_verified(idle)
        except Timeout:
            raise RuntimeError("桌面实例仍在运行但暂未响应，请稍后重试；不会强杀窗口或丢弃未保存内容。") from None
        subprocess.Popen(
            ["wscript.exe", str(project / "scripts" / "desktop_launcher.vbs")],
            cwd=project, creationflags=subprocess.CREATE_NO_WINDOW,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        print("已重新发起桌面启动，正在等待窗口就绪……", flush=True)
        wait_ready(instance)
        print("桌面窗口已就绪。资料、数据库、模型和配置均保留。")


def main():
    try:
        recover()
        return 0
    except Timeout:
        print("已有恢复操作正在执行，请等待，不必重复点击。", file=sys.stderr)
    except Exception as error:
        print(f"恢复未完成：{error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
