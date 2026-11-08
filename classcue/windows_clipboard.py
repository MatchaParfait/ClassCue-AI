"""Copy local image files using the Windows file-drop clipboard format."""

from __future__ import annotations

import ctypes
import os
import struct
import time
from ctypes import wintypes
from pathlib import Path
from uuid import UUID


def file_drop_data(paths):
    files = [str(Path(path).resolve(strict=True)) for path in paths]
    if not files or any("\0" in path or not Path(path).is_file() for path in files):
        raise ValueError("请选择可用的题图文件")
    # DROPFILES uses five 32-bit fields, followed by a double-NUL-terminated UTF-16 list.
    return struct.pack("<IiiII", 20, 0, 0, 0, 1) + ("\0".join(files) + "\0\0").encode("utf-16-le")


def copy_files(paths):
    _copy_data(15, file_drop_data(paths))


def copy_text(text):
    if not isinstance(text, str) or "\0" in text:
        raise ValueError("原题文字格式无效")
    _copy_data(13, (text + "\0").encode("utf-16-le"))


def _copy_data(format_id, data):
    if os.name != "nt":
        raise OSError("题图复制需要 Windows 桌面")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.DestroyWindow.restype = wintypes.BOOL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL
    # EmptyClipboard needs a real owner for the subsequent SetClipboardData call.
    window = user32.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, -3, None, None, None)
    if not window:
        raise ctypes.WinError(ctypes.get_last_error())
    memory = None
    opened = False
    try:
        memory = kernel32.GlobalAlloc(0x0042, len(data))
        if not memory:
            raise ctypes.WinError(ctypes.get_last_error())
        pointer = kernel32.GlobalLock(memory)
        if not pointer:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            ctypes.memmove(pointer, data, len(data))
        finally:
            kernel32.GlobalUnlock(memory)
        for attempt in range(6):
            if user32.OpenClipboard(window):
                opened = True
                break
            if attempt < 5:
                time.sleep(0.03)
        if not opened or not user32.EmptyClipboard() or not user32.SetClipboardData(format_id, memory):
            raise ctypes.WinError(ctypes.get_last_error())
        memory = None  # Windows owns the allocation after SetClipboardData succeeds.
    finally:
        if opened:
            user32.CloseClipboard()
        if memory:
            kernel32.GlobalFree(memory)
        user32.DestroyWindow(window)


class _Format(ctypes.Structure):
    _fields_ = [("format", wintypes.WORD), ("target", ctypes.c_void_p), ("aspect", wintypes.DWORD),
                ("index", wintypes.LONG), ("medium", wintypes.DWORD)]


class _Medium(ctypes.Structure):
    _fields_ = [("medium", wintypes.DWORD), ("handle", ctypes.c_void_p), ("release", ctypes.c_void_p)]


def _method(pointer, index, result, *arguments):
    table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(result, ctypes.c_void_p, *arguments)(table[index])


def _release(pointer):
    if pointer:
        _method(pointer, 2, wintypes.ULONG)(pointer)


def _snapshot_data_object(source, ole):
    """Materialize data before EmptyClipboard invalidates OleGetClipboard's live wrapper."""
    shell = ctypes.OleDLL("shell32")
    kernel = ctypes.WinDLL("kernel32")
    shell.SHCreateDataObject.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p,
                                        ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    ole.ReleaseStgMedium.argtypes = [ctypes.POINTER(_Medium)]
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    kernel.GlobalSize.argtypes = [ctypes.c_void_p]
    kernel.GlobalSize.restype = ctypes.c_size_t
    iid = (ctypes.c_byte * 16).from_buffer_copy(UUID("0000010e-0000-0000-c000-000000000046").bytes_le)
    snapshot, enumerator = ctypes.c_void_p(), ctypes.c_void_p()
    count, size = 0, 0
    try:
        shell.SHCreateDataObject(None, 0, None, None, ctypes.byref(iid), ctypes.byref(snapshot))
        _method(source, 8, ctypes.HRESULT, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))(
            source, 1, ctypes.byref(enumerator))
        while True:
            fmt, medium, fetched = _Format(), _Medium(), wintypes.ULONG()
            _method(enumerator, 3, ctypes.HRESULT, wintypes.ULONG, ctypes.POINTER(_Format),
                    ctypes.POINTER(wintypes.ULONG))(enumerator, 1, ctypes.byref(fmt), ctypes.byref(fetched))
            if not fetched.value:
                break
            transferred = False
            try:
                count += 1
                if count > 256:
                    raise OSError("剪贴板格式过多，已保留原内容并停止附图")
                _method(source, 3, ctypes.HRESULT, ctypes.POINTER(_Format), ctypes.POINTER(_Medium))(
                    source, ctypes.byref(fmt), ctypes.byref(medium))
                if medium.medium == 1:
                    size += kernel.GlobalSize(medium.handle)
                    if size > 64 * 1024 * 1024:
                        raise OSError("剪贴板内容过大，已保留原内容并停止附图")
                fmt.medium = medium.medium
                _method(snapshot, 7, ctypes.HRESULT, ctypes.POINTER(_Format), ctypes.POINTER(_Medium),
                        wintypes.BOOL)(snapshot, ctypes.byref(fmt), ctypes.byref(medium), True)
                transferred = True
            finally:
                if medium.medium and not transferred:
                    ole.ReleaseStgMedium(ctypes.byref(medium))
                if fmt.target:
                    ole.CoTaskMemFree(fmt.target)
        if not count:
            return ctypes.c_void_p()
        result, snapshot = snapshot, ctypes.c_void_p()
        return result
    finally:
        _release(enumerator)
        _release(snapshot)


class PreservedClipboard:
    """Save clipboard data through OLE; never overwrite a later user copy."""

    def __enter__(self):
        self.ole = ctypes.OleDLL("ole32")
        self.user = ctypes.WinDLL("user32")
        self.ole.OleInitialize.argtypes = [ctypes.c_void_p]
        self.ole.OleGetClipboard.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        self.ole.OleSetClipboard.argtypes = [ctypes.c_void_p]
        self.user.GetClipboardSequenceNumber.restype = wintypes.DWORD
        self.ole.OleInitialize(None)
        self.original = ctypes.c_void_p()
        source = ctypes.c_void_p()
        self.sequence = self.user.GetClipboardSequenceNumber()
        try:
            self.ole.OleGetClipboard(ctypes.byref(source))
            if source:
                self.original = _snapshot_data_object(source, self.ole)
            self.check()
        except BaseException:
            _release(self.original)
            self.ole.OleUninitialize()
            raise
        finally:
            _release(source)
        return self

    def check(self):
        if self.user.GetClipboardSequenceNumber() != self.sequence:
            raise OSError("剪贴板已被其他操作修改，已停止附图")

    def copy(self, value):
        if hasattr(self, "changed_at"):
            time.sleep(max(0, 1 - (time.monotonic() - self.changed_at)))
        self.check()
        (copy_text if isinstance(value, str) else copy_files)(value)
        self.sequence = self.user.GetClipboardSequenceNumber()
        self.changed_at = time.monotonic()

    def __exit__(self, *_):
        try:
            # On an early failure, a delivered paste may still be queued by the app.
            # Leave its data available before restoring an unrelated original image.
            if hasattr(self, "changed_at"):
                time.sleep(max(0, 1 - (time.monotonic() - self.changed_at)))
            if self.user.GetClipboardSequenceNumber() == self.sequence:
                self.ole.OleSetClipboard(self.original)
                self.ole.OleFlushClipboard()
        finally:
            _release(self.original)
            self.ole.OleUninitialize()
