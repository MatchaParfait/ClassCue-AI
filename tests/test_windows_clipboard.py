import ctypes
import os
import struct
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace

import pytest

from classcue.windows_clipboard import PreservedClipboard, copy_files, copy_text, file_drop_data


def test_file_drop_keeps_unicode_order_and_double_terminator(tmp_path):
    paths = [tmp_path / "题图 2.png", tmp_path / "题图 1.png"]
    for path in paths:
        path.write_bytes(b"synthetic")
    data = file_drop_data(paths)
    assert struct.unpack("<IiiII", data[:20]) == (20, 0, 0, 0, 1)
    assert data[20:].decode("utf-16-le") == "\0".join(str(path.resolve()) for path in paths) + "\0\0"
    with pytest.raises(ValueError):
        file_drop_data([])
    with pytest.raises((OSError, ValueError)):
        file_drop_data([tmp_path])


@pytest.mark.skipif(os.name != "nt", reason="Windows native DROPFILES decoder")
def test_windows_decodes_real_file_drop_without_touching_clipboard(tmp_path):
    paths = [tmp_path / "题图 2.png", tmp_path / "题图 1.png"]
    for path in paths:
        path.write_bytes(b"synthetic")
    data = file_drop_data(paths)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    kernel.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel.GlobalLock.restype = ctypes.c_void_p
    kernel.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel.GlobalFree.argtypes = [wintypes.HGLOBAL]
    shell.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
    shell.DragQueryFileW.restype = wintypes.UINT
    memory = kernel.GlobalAlloc(0x0042, len(data))
    assert memory
    try:
        pointer = kernel.GlobalLock(memory)
        assert pointer
        ctypes.memmove(pointer, data, len(data))
        kernel.GlobalUnlock(memory)
        assert shell.DragQueryFileW(memory, 0xFFFFFFFF, None, 0) == 2
        actual = []
        for index in range(2):
            length = shell.DragQueryFileW(memory, index, None, 0)
            output = ctypes.create_unicode_buffer(length + 1)
            shell.DragQueryFileW(memory, index, output, length + 1)
            actual.append(output.value)
        assert actual == [str(path.resolve()) for path in paths]
    finally:
        kernel.GlobalFree(memory)


@pytest.mark.skipif(os.name != "nt", reason="Windows clipboard allocation contract")
@pytest.mark.parametrize("failure", [None, "busy", "set"])
@pytest.mark.parametrize("kind", ["files", "text"])
def test_clipboard_ownership_retry_and_cleanup_are_bounded(tmp_path, monkeypatch, failure, kind):
    path = tmp_path / "synthetic.png"
    path.write_bytes(b"synthetic")
    payload = file_drop_data([path]) if kind == "files" else "合成题干\0".encode("utf-16-le")
    write = lambda: copy_files([path]) if kind == "files" else copy_text("合成题干")
    memory = ctypes.create_string_buffer(len(payload))
    calls = []

    def function(name, result):
        def invoke(*args):
            calls.append((name, args))
            return result
        return invoke

    user = SimpleNamespace(
        CreateWindowExW=function("create", 31), DestroyWindow=function("destroy", 1),
        OpenClipboard=function("open", failure != "busy"), CloseClipboard=function("close", 1),
        EmptyClipboard=function("empty", 1), SetClipboardData=function("set", 0 if failure == "set" else 42),
    )
    kernel = SimpleNamespace(
        GlobalAlloc=function("alloc", 42), GlobalLock=function("lock", ctypes.addressof(memory)),
        GlobalUnlock=function("unlock", 1), GlobalFree=function("free", None),
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **_: user if name == "user32" else kernel)
    monkeypatch.setattr("classcue.windows_clipboard.time.sleep", lambda _: None)
    if failure:
        with pytest.raises(OSError):
            write()
    else:
        write()
    assert memory.raw == payload
    assert [args[0] for name, args in calls if name == "open"] == [31] * (6 if failure == "busy" else 1)
    assert calls[-1][0] == "destroy"
    assert any(name == "free" for name, _ in calls) == bool(failure)
    assert any(name == "close" for name, _ in calls) == (failure != "busy")
    if failure == "busy":
        assert not any(name == "empty" for name, _ in calls)
    if not failure:
        assert next(args[0] for name, args in calls if name == "set") == (15 if kind == "files" else 13)


@pytest.mark.skipif(os.name != "nt", reason="Windows OLE clipboard preservation")
@pytest.mark.parametrize("external_copy", [False, True])
def test_preserve_empty_clipboard_restores_only_if_no_user_copy(monkeypatch, external_copy):
    sequence = [10]
    calls = []

    def fn(name):
        def call(*args):
            calls.append(name)
        return call

    ole = SimpleNamespace(OleInitialize=fn("init"), OleGetClipboard=fn("get"),
                          OleSetClipboard=fn("restore"), OleFlushClipboard=fn("flush"),
                          OleUninitialize=fn("uninit"))

    def get_sequence():
        return sequence[0]

    user = SimpleNamespace(GetClipboardSequenceNumber=get_sequence)
    monkeypatch.setattr(ctypes, "OleDLL", lambda _: ole)
    monkeypatch.setattr(ctypes, "WinDLL", lambda _: user)
    with PreservedClipboard():
        if external_copy:
            sequence[0] += 1
    assert ("restore" in calls) == (not external_copy)
    assert calls[-1] == "uninit"


def isolated_clipboard_roundtrip():
    """Exercise the real APIs without reading or changing the user's clipboard."""
    from tempfile import TemporaryDirectory

    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    user.CreateWindowStationW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    user.CreateWindowStationW.restype = wintypes.HANDLE
    user.GetProcessWindowStation.restype = wintypes.HANDLE
    user.SetProcessWindowStation.argtypes = [wintypes.HANDLE]
    user.CloseWindowStation.argtypes = [wintypes.HANDLE]
    user.CreateDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p,
                                    wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    user.CreateDesktopW.restype = wintypes.HANDLE
    user.SetThreadDesktop.argtypes = [wintypes.HANDLE]
    user.GetThreadDesktop.argtypes = [wintypes.DWORD]
    user.GetThreadDesktop.restype = wintypes.HANDLE
    user.CloseDesktop.argtypes = [wintypes.HANDLE]
    user.GetClipboardData.argtypes = [wintypes.UINT]
    user.GetClipboardData.restype = wintypes.HANDLE
    kernel.GlobalLock.argtypes = [wintypes.HANDLE]
    kernel.GlobalLock.restype = ctypes.c_void_p
    kernel.GlobalUnlock.argtypes = [wintypes.HANDLE]
    station_before = user.GetProcessWindowStation()
    desktop_before = user.GetThreadDesktop(kernel.GetCurrentThreadId())
    station = user.CreateWindowStationW(None, 0, 0xF037F, None)
    assert station
    desktop = None

    def read_text():
        assert user.OpenClipboard(None)
        pointer = None
        handle = None
        try:
            handle = user.GetClipboardData(13)
            pointer = kernel.GlobalLock(handle)
            assert pointer
            return ctypes.wstring_at(pointer)
        finally:
            if pointer:
                kernel.GlobalUnlock(handle)
            user.CloseClipboard()

    try:
        assert user.SetProcessWindowStation(station)
        desktop = user.CreateDesktopW("ClassCueSynthetic", None, None, 0, 0xF01FF, None)
        assert desktop and user.SetThreadDesktop(desktop)
        assert user.GetProcessWindowStation() == station
        assert user.OpenClipboard(None)
        try:
            assert user.EmptyClipboard()
        finally:
            user.CloseClipboard()
        with TemporaryDirectory() as directory:
            image = Path(directory) / "合成题图.png"
            image.write_bytes(b"synthetic")
            with PreservedClipboard() as clipboard:
                clipboard.copy("原本为空")
            assert not user.IsClipboardFormatAvailable(13)
            assert not user.IsClipboardFormatAvailable(15)
            copy_text("用户原有的合成剪贴板")
            with PreservedClipboard() as clipboard:
                clipboard.copy("临时题干")
                clipboard.copy([image])
            assert read_text() == "用户原有的合成剪贴板"
            with PreservedClipboard() as clipboard:
                clipboard.copy("临时题干")
                copy_text("用户后来复制的内容")
            assert read_text() == "用户后来复制的内容"
            copy_files([image])
            with PreservedClipboard() as clipboard:
                clipboard.copy("临时题干")
            shell = ctypes.WinDLL("shell32")
            shell.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
            shell.DragQueryFileW.restype = wintypes.UINT
            assert user.OpenClipboard(None)
            try:
                handle = user.GetClipboardData(15)
                assert handle and shell.DragQueryFileW(handle, 0xFFFFFFFF, None, 0) == 1
                length = shell.DragQueryFileW(handle, 0, None, 0)
                value = ctypes.create_unicode_buffer(length + 1)
                shell.DragQueryFileW(handle, 0, value, length + 1)
                assert value.value == str(image.resolve())
            finally:
                user.CloseClipboard()
    finally:
        user.SetThreadDesktop(desktop_before)
        user.SetProcessWindowStation(station_before)
        if desktop:
            user.CloseDesktop(desktop)
        user.CloseWindowStation(station)


@pytest.mark.skipif(os.name != "nt", reason="Windows isolated clipboard")
def test_real_clipboard_snapshot_survives_replacement_and_preserves_later_copy():
    result = subprocess.run(
        [sys.executable, "-c",
         "import runpy,sys; runpy.run_path(sys.argv[1])['isolated_clipboard_roundtrip']()", __file__],
        capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
