"""Bounded RPC client for the Windows Portable Devices worker."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path, PurePosixPath
from queue import Empty, Queue
import subprocess
import sys
from threading import Lock, Thread

from classcue.device_protocol import BLOCK_BYTES


def relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or "\\" in value or any(part in {".", ".."} for part in value.split("/")):
        raise ValueError("传输目录路径无效")
    if len(value) > 600 or any(ord(char) < 32 for char in value):
        raise ValueError("传输目录路径无效")
    return str(path) if value else ""


class WpdTransport:
    def __init__(self):
        self._process = None
        self._responses: Queue = Queue()
        self._lock = Lock()

    def _start(self):
        if self._process and self._process.poll() is None:
            return
        if os.name != "nt":
            raise RuntimeError("有线设备传输仅支持 Windows")
        self._responses = Queue()
        self._process = subprocess.Popen(
            [sys.executable, "-m", "classcue.device_wpd_worker"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
            cwd=Path(__file__).resolve().parents[1], creationflags=subprocess.CREATE_NO_WINDOW,
        )
        process = self._process
        responses = self._responses

        def read():
            try:
                for line in process.stdout:
                    responses.put(json.loads(line))
            except Exception:
                pass
            finally:
                responses.put({"ok": False, "error": "设备连接进程已停止"})
        Thread(target=read, daemon=True, name="classcue-wpd-output").start()

    def close(self):
        process = self._process
        self._process = None
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)

    def call(self, operation: str, *, timeout=45, **data):
        with self._lock:
            self._start()
            self._process.stdin.write(json.dumps({"op": operation, **data}, ensure_ascii=True) + "\n")
            self._process.stdin.flush()
            try:
                response = self._responses.get(timeout=timeout)
            except Empty:
                self.close()
                raise TimeoutError("设备响应超时，请检查数据线和文件传输模式") from None
            if not response.get("ok"):
                if response.get("missing"):
                    raise FileNotFoundError("设备文件尚不存在")
                raise OSError(response.get("error", "设备操作失败"))
            return response.get("result")

    def devices(self):
        return self.call("devices")

    def list(self, device: str, path=""):
        return self.call("list", device=device, path=relative_path(path))

    def read(self, device: str, path: str, maximum=BLOCK_BYTES):
        result = self.call("read", device=device, path=relative_path(path), maximum=maximum)
        return base64.b64decode(result, validate=True)

    def write(self, device: str, path: str, content: bytes):
        if len(content) > BLOCK_BYTES:
            raise ValueError("数据块过大")
        return self.call("write", device=device, path=relative_path(path), content=base64.b64encode(content).decode("ascii"))

    def delete(self, device: str, path: str):
        return self.call("delete", device=device, path=relative_path(path))


class FolderTransport:
    """Protocol test adapter. Never exposed as a substitute for USB discovery."""
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, value):
        resolved = (self.root / relative_path(value)).resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError("传输路径越界")
        return resolved

    def devices(self):
        return [{"id": "synthetic", "name": "Synthetic USB device", "root_available": True}]

    def list(self, device, path=""):
        target = self._path(path)
        if not target.is_dir():
            return []
        return [{"name": entry.name, "folder": entry.is_dir(), "size": entry.stat().st_size}
                for entry in target.iterdir()]

    def read(self, device, path, maximum=BLOCK_BYTES):
        with self._path(path).open("rb") as stream:
            result = stream.read(maximum + 1)
        if len(result) > maximum:
            raise ValueError("设备文件超过限制")
        return result

    def write(self, device, path, content):
        target = self._path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".partial")
        with temporary.open("wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(target)

    def delete(self, device, path):
        self._path(path).unlink(missing_ok=True)

    def close(self):
        pass
