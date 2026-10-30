"""Bounded, authenticated JSON exchange with a Windows desktop plugin."""

from __future__ import annotations

import json
import os
import re
import struct
import time
from pathlib import Path

MAX_FRAME = 32 * 1024 * 1024
DESKTOP_VERSIONS = {"0.2.1-rc.3", "0.2.1-rc.5"}
PIPE_PATTERN = re.compile(r"\\\\\.\\pipe\\classcue-dsh-[a-f0-9-]{36}")


class BridgeFailure(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def descriptor(path: Path) -> dict:
    try:
        if path.stat().st_size > 4096:
            raise ValueError()
        value = json.loads(path.read_text(encoding="utf-8"))
        if (value.get("protocol") != 1 or value.get("desktop_version") not in DESKTOP_VERSIONS
                or not PIPE_PATTERN.fullmatch(value.get("pipe", ""))
                or not re.fullmatch(r"[a-f0-9]{64}", value.get("token", ""))
                or not re.fullmatch(r"[a-f0-9-]{36}", value.get("instance_id", ""))):
            raise ValueError()
        return value
    except (OSError, ValueError, TypeError, AttributeError):
        raise BridgeFailure("plugin_unavailable", "请开启已安装桥接插件的 DSH 桌面版") from None


def exchange(pipe: str, frame: bytes, timeout: float) -> bytes:
    if os.name != "nt":
        raise BridgeFailure("desktop_only", "此桥接仅支持 Windows 桌面版")
    import ctypes
    from ctypes import wintypes as w

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)

    class Overlapped(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                    ("Offset", w.DWORD), ("OffsetHigh", w.DWORD), ("hEvent", w.HANDLE)]

    kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p,
                                  w.DWORD, w.DWORD, w.HANDLE]
    kernel.CreateFileW.restype = w.HANDLE
    kernel.CreateEventW.argtypes = [ctypes.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR]
    kernel.CreateEventW.restype = w.HANDLE
    kernel.WaitNamedPipeW.argtypes = [w.LPCWSTR, w.DWORD]
    kernel.WaitNamedPipeW.restype = w.BOOL
    kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    kernel.WaitForSingleObject.restype = w.DWORD
    kernel.GetOverlappedResult.argtypes = [w.HANDLE, ctypes.POINTER(Overlapped), ctypes.POINTER(w.DWORD), w.BOOL]
    kernel.GetOverlappedResult.restype = w.BOOL
    kernel.CancelIoEx.argtypes = [w.HANDLE, ctypes.POINTER(Overlapped)]
    kernel.CancelIoEx.restype = w.BOOL
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.CloseHandle.restype = w.BOOL
    for name in ("ReadFile", "WriteFile"):
        function = getattr(kernel, name)
        function.argtypes = [w.HANDLE, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.POINTER(Overlapped)]
        function.restype = w.BOOL

    deadline = time.monotonic() + timeout

    def remaining():
        milliseconds = int((deadline - time.monotonic()) * 1000)
        if milliseconds <= 0:
            raise BridgeFailure("timeout", "DSH 桥接响应超时，可使用同一请求继续查看")
        return milliseconds

    handle = kernel.CreateFileW(pipe, 0xC0000000, 0, None, 3, 0x40000000, None)
    if handle == ctypes.c_void_p(-1).value:
        if ctypes.get_last_error() == 231 and kernel.WaitNamedPipeW(pipe, remaining()):
            handle = kernel.CreateFileW(pipe, 0xC0000000, 0, None, 3, 0x40000000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise BridgeFailure("plugin_unavailable", "DSH 桌面桥接未连接")

    def io(buffer, write=False):
        overlapped = Overlapped(hEvent=kernel.CreateEventW(None, True, False, None))
        if not overlapped.hEvent:
            raise ctypes.WinError(ctypes.get_last_error())
        count = w.DWORD()
        try:
            ok = (kernel.WriteFile if write else kernel.ReadFile)(
                handle, buffer, len(buffer), ctypes.byref(count), ctypes.byref(overlapped))
            if not ok:
                if ctypes.get_last_error() != 997:
                    raise ctypes.WinError(ctypes.get_last_error())
                if kernel.WaitForSingleObject(overlapped.hEvent, remaining()) != 0:
                    raise BridgeFailure("timeout", "DSH 桥接响应超时，可使用同一请求继续查看")
                if not kernel.GetOverlappedResult(handle, ctypes.byref(overlapped), ctypes.byref(count), False):
                    raise ctypes.WinError(ctypes.get_last_error())
            return count.value
        finally:
            # Complete cancellation before releasing the OVERLAPPED memory or its event.
            kernel.CancelIoEx(handle, ctypes.byref(overlapped))
            kernel.GetOverlappedResult(handle, ctypes.byref(overlapped), ctypes.byref(count), True)
            kernel.CloseHandle(overlapped.hEvent)

    def read_exact(length):
        result = bytearray()
        while len(result) < length:
            buffer = ctypes.create_string_buffer(length - len(result))
            count = io(buffer)
            if not count:
                raise BridgeFailure("disconnected", "DSH 桥接连接已断开")
            result.extend(buffer.raw[:count])
        return bytes(result)

    try:
        offset = 0
        while offset < len(frame):
            buffer = (ctypes.c_char * (len(frame) - offset)).from_buffer_copy(frame[offset:])
            count = io(buffer, True)
            if not count:
                raise BridgeFailure("disconnected", "DSH 桥接连接已断开")
            offset += count
        size = struct.unpack(">I", read_exact(4))[0]
        if size > MAX_FRAME:
            raise BridgeFailure("invalid_response", "DSH 桥接响应过大")
        return read_exact(size)
    except OSError:
        raise BridgeFailure("disconnected", "DSH 桥接连接已断开，请检查桌面插件") from None
    finally:
        kernel.CloseHandle(handle)


class PipeClient:
    def __init__(self, connection_path: Path, transport=exchange):
        self.path = connection_path
        self.transport = transport

    def call(self, method: str, params: dict | None = None, timeout=12.0):
        connection = descriptor(self.path)
        body = json.dumps({"protocol": 1, "token": connection["token"], "method": method,
                           "params": params or {}}, ensure_ascii=False, separators=(",", ":")).encode()
        if len(body) > MAX_FRAME:
            raise BridgeFailure("content_too_large", "单题传输内容过大")
        try:
            result = json.loads(self.transport(connection["pipe"], struct.pack(">I", len(body)) + body, timeout))
            if result["ok"]:
                value = result.get("value")
                if isinstance(value, dict) and "instance_id" in value and value["instance_id"] != connection["instance_id"]:
                    raise BridgeFailure("instance_mismatch", "DSH 实例身份已变化，请重新选择连接")
                return value
            error = result["error"]
            raise BridgeFailure(str(error["code"]), str(error["message"]))
        except (ValueError, KeyError, TypeError):
            raise BridgeFailure("invalid_response", "DSH 桥接返回了无效响应") from None
