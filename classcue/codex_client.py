"""Bounded stdio client for the public Codex App Server protocol."""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
from pathlib import Path

from classcue.dsh_pipe import BridgeFailure


def find_codex(executable: str | None = None) -> str:
    candidate = executable or os.getenv("CLASSCUE_CODEX_EXECUTABLE") or os.getenv("CODEX_CLI_PATH")
    if candidate:
        path = Path(candidate).expanduser()
        if path.is_absolute() and path.is_file():
            return str(path.resolve())
        raise BridgeFailure("codex_unavailable", "Codex CLI 路径不可用，请在连接设置中重新选择")
    found = shutil.which("codex.exe") or shutil.which("codex")
    if found:
        return found
    # The desktop installer puts its CLI alongside the application.
    base = Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData/Local")) / "OpenAI/Codex/bin"
    candidates = sorted(base.glob("*/codex.exe"), key=lambda p: p.stat().st_mtime, reverse=True) if base.exists() else []
    if candidates:
        return str(candidates[0])
    raise BridgeFailure("codex_unavailable", "未找到 Codex CLI，请安装 Codex 或选择 codex.exe")


class CodexClient:
    def __init__(self, executable=None, home=None, timeout=20):
        self.executable = find_codex(executable)
        self.home = Path(home or os.getenv("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()
        self.timeout = timeout
        self.process = None
        self.messages = queue.Queue()
        self.counter = 0

    def __enter__(self):
        self.home.mkdir(parents=True, exist_ok=True)
        self.process = subprocess.Popen(
            [self.executable, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={**os.environ, "CODEX_HOME": str(self.home)},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            self.identity = self.call("initialize", {
                "clientInfo": {"name": "classcue", "title": "ClassCue", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            })
            self._send({"method": "initialized", "params": {}})
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def _read(self):
        try:
            while line := self.process.stdout.readline(32 * 1024 * 1024 + 1):
                if len(line) > 32 * 1024 * 1024:
                    break
                try:
                    self.messages.put(json.loads(line))
                except (ValueError, UnicodeError):
                    break
        finally:
            self.messages.put(None)

    def _send(self, value):
        result = queue.Queue()
        payload = (json.dumps(value, ensure_ascii=True) + "\n").encode("utf-8")

        def write():
            try:
                self.process.stdin.write(payload)
                self.process.stdin.flush()
                result.put(True)
            except (BrokenPipeError, OSError, ValueError):
                result.put(False)

        writer = threading.Thread(target=write, daemon=True)
        writer.start()
        try:
            success = result.get(timeout=self.timeout)
        except queue.Empty:
            self.process.terminate()
            writer.join(timeout=1)
            raise BridgeFailure("codex_timeout", "Codex 接收超时，原讨论绑定已保留") from None
        if not success:
            raise BridgeFailure("codex_disconnected", "Codex 连接中断，原讨论绑定已保留")

    def call(self, method, params):
        self.counter += 1
        self._send({"id": self.counter, "method": method, "params": params})
        import time
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                value = self.messages.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                raise BridgeFailure("codex_timeout", "Codex 响应超时，请重试；不会自动创建替代讨论") from None
            if value is None:
                raise BridgeFailure("codex_disconnected", "Codex 已断开，原讨论绑定已保留")
            if value.get("id") != self.counter:
                if "id" in value and "method" in value:
                    self._send({"id": value["id"], "error": {"code": -32601, "message": "Unsupported request"}})
                continue
            if "error" in value:
                raise BridgeFailure("codex_rpc_error", "Codex 拒绝此操作，请检查版本及原讨论状态")
            return value.get("result", {})

    def __exit__(self, *_):
        if self.process:
            if self.process.stdin:
                self.process.stdin.close()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
            if self.process.stdout:
                self.process.stdout.close()
            self.reader.join(timeout=1)
