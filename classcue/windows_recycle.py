"""Recycle deletion bundles through the Windows Shell, retaining their Shell identity."""

from __future__ import annotations

from contextlib import contextmanager
import ctypes as c
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import NamedTemporaryFile
import time
from uuid import UUID


_UNKNOWN = "00000000-0000-0000-c000-000000000046"
_SINK = "04b0f1a7-9490-44bc-96e1-4296a31252e2"
_ITEM = "43826d1e-e718-42ee-bc55-a1e261c37bfe"
_OPERATION = "947aab5f-0a5c-4c13-b4d6-4bf7836fc9f8"
_OPERATION_CLASS = "3ad05575-8857-4850-9277-11b85bdb8e09"
_RECYCLE_IF_POSSIBLE = 0x80
_ABORT = -2147467260
# Silent, no confirmation/error UI, and fail early. Recycling also requires
# recycle-on-delete and a nuke warning; PreDeleteItem vetoes non-recycle paths.
_FLAGS = 0x0004 | 0x0010 | 0x0200 | 0x0400 | 0x00100000
_RECYCLE_FLAGS = _FLAGS | 0x00080000 | 0x20000000 | 0x4000


def _worker(action, value):
    # Keep Shell extensions and their COM/message context out of the API and
    # WebView processes. Wait for its receipt before committing the database.
    try:
        result = subprocess.run(
            [sys.executable, "-m", "classcue.windows_recycle"],
            input=json.dumps({"action": action, "value": value}), capture_output=True,
            text=True, encoding="utf-8", cwd=Path(__file__).resolve().parent.parent,
            env={**os.environ, "PYTHONUTF8": "1"}, timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired as error:
        raise OSError("等待 Windows 回收站超时，已保留恢复记录") from error
    try:
        response = json.loads(result.stdout)
    except ValueError as error:
        raise OSError("Windows 回收站操作未返回有效结果") from error
    if result.returncode or "error" in response:
        exception = ValueError if response.get("type") == "ValueError" else OSError
        raise exception(response.get("error", "Windows 回收站操作失败"))
    return response["result"]


def recycle_directory(directory: Path) -> dict | None:
    try:
        return _worker("recycle", str(directory))
    except OSError:
        # A Shell extension or worker can exit after the actual recycle.
        # Its durable receipt survives loss of stdout or the worker process.
        receipt = recycle_receipt_path(directory)
        try:
            ticket = json.loads(receipt.read_text(encoding="utf-8"))
            if (ticket.get("original") == str(directory.absolute())
                    and ticket.get("path") and Path(ticket["path"]).is_dir()
                    and not directory.exists()):
                return ticket
        except (OSError, ValueError, TypeError):
            pass
        raise


def restore_directory(ticket: dict) -> Path:
    return Path(_worker("restore", ticket))


def recycle_receipt_path(directory: Path) -> Path:
    return directory.with_name(directory.name + ".windows-recycle.json")


def write_recycle_record(path: Path, value: dict) -> None:
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            json.dump(value, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _guid(value):
    return (c.c_byte * 16).from_buffer_copy(UUID(value).bytes_le)


def _method(pointer, index, result, *arguments):
    table = c.cast(pointer, c.POINTER(c.POINTER(c.c_void_p))).contents
    return c.WINFUNCTYPE(result, c.c_void_p, *arguments)(table[index])


def _check(result):
    if result < 0:
        raise OSError(f"Windows 文件操作失败（0x{result & 0xffffffff:08X}）")


def _release(pointer):
    if pointer:
        _method(pointer, 2, w.ULONG)(pointer)


class _Shell:
    def __init__(self):
        self.ole = c.WinDLL("ole32")
        self.shell = c.WinDLL("shell32")
        self.ole.CoInitializeEx.argtypes = [c.c_void_p, w.DWORD]
        self.ole.CoInitializeEx.restype = c.c_long
        self.ole.CoCreateInstance.argtypes = [
            c.c_void_p, c.c_void_p, w.DWORD, c.c_void_p, c.POINTER(c.c_void_p),
        ]
        self.ole.CoCreateInstance.restype = c.c_long
        self.ole.CoTaskMemFree.argtypes = [c.c_void_p]
        self.shell.SHCreateItemFromParsingName.argtypes = [
            w.LPCWSTR, c.c_void_p, c.c_void_p, c.POINTER(c.c_void_p),
        ]
        self.shell.SHCreateItemFromParsingName.restype = c.c_long

    def item(self, path):
        pointer = c.c_void_p()
        _check(self.shell.SHCreateItemFromParsingName(
            str(path), None, c.byref(_guid(_ITEM)), c.byref(pointer)))
        return pointer

    def display_name(self, item, kind):
        text = c.c_void_p()
        try:
            _check(_method(item, 5, c.c_long, w.DWORD, c.POINTER(c.c_void_p))(
                item, kind, c.byref(text)))
            return c.wstring_at(text)
        finally:
            self.ole.CoTaskMemFree(text)

    def display_path(self, item):
        return self.display_name(item, 0x80058000)

    def original_path(self, item):
        item2, text = c.c_void_p(), c.c_void_p()

        class PropertyKey(c.Structure):
            _fields_ = [("format", c.c_byte * 16), ("id", w.DWORD)]

        try:
            _check(_method(item, 0, c.c_long, c.c_void_p, c.POINTER(c.c_void_p))(
                item, c.byref(_guid("7e9fb0d3-919f-4307-ab2e-9b1860310c93")), c.byref(item2)))
            key = PropertyKey(_guid("9b174b33-40ff-11d2-a27e-00c04fc30871"), 2)
            _check(_method(item2, 17, c.c_long, c.POINTER(PropertyKey), c.POINTER(c.c_void_p))(
                item2, c.byref(key), c.byref(text)))
            return Path(c.wstring_at(text)) / self.display_name(item, 0)
        finally:
            self.ole.CoTaskMemFree(text)
            _release(item2)

    def ticket(self, item):
        return {"path": self.display_path(item)}

    def recycled_item(self, ticket):
        # Bind the actual bin item so the undelete command uses the original
        # location, including recovery when the worker's reply was lost.
        root = self.item("::{645FF040-5081-101B-9F08-00AA002F954E}")
        enumerator = c.c_void_p()
        try:
            _check(_method(root, 3, c.c_long, c.c_void_p, c.c_void_p,
                           c.c_void_p, c.POINTER(c.c_void_p))(
                root, None, c.byref(_guid("94f60519-2850-4924-aa5a-d15e84868039")),
                c.byref(_guid("70629033-e363-4a28-a567-0db78006e6d7")), c.byref(enumerator)))
            while True:
                item, fetched = c.c_void_p(), w.ULONG()
                _check(_method(enumerator, 3, c.c_long, w.ULONG,
                               c.POINTER(c.c_void_p), c.POINTER(w.ULONG))(
                    enumerator, 1, c.byref(item), c.byref(fetched)))
                if not fetched.value:
                    break
                try:
                    if ticket.get("path"):
                        matched = os.path.normcase(self.display_path(item)) == os.path.normcase(ticket["path"])
                    else:
                        matched = self.original_path(item) == Path(ticket["original"])
                except OSError:
                    matched = False
                if matched:
                    return item
                _release(item)
        finally:
            _release(enumerator)
            _release(root)
        raise ValueError("Windows 回收站中的文件已被清空或移走，无法撤销删除")


class _DeleteSink:
    def __init__(self, shell, source, on_ticket=None):
        self.shell, self.source = shell, os.path.normcase(str(source))
        self.ticket = None
        self.error = None
        self.callbacks = []
        pointer, dword, text, hresult = c.c_void_p, w.DWORD, w.LPCWSTR, c.c_long

        def query(_this, iid, output):
            if c.string_at(iid, 16) not in (UUID(_UNKNOWN).bytes_le, UUID(_SINK).bytes_le):
                output[0] = None
                return -2147467262
            output[0] = c.cast(c.byref(self.instance), pointer)
            return 0

        def pre_delete(_this, flags, _item):
            if not flags & _RECYCLE_IF_POSSIBLE:
                self.error = OSError("Windows 无法回收此文件，已取消删除")
                return _ABORT
            return 0

        def post_delete(_this, _flags, item, result, recycled):
            try:
                _check(result)
                if os.path.normcase(shell.display_path(item)) != self.source:
                    return 0
                if not recycled:
                    raise OSError("Windows 未返回回收站项目")
                self.ticket = shell.ticket(recycled)
                if on_ticket is not None:
                    on_ticket(self.ticket)
                return 0
            except Exception as error:
                self.error = error
                return _ABORT

        def add(result, arguments, callback=None):
            callback = callback or (lambda *_args: 0)
            wrapped = c.WINFUNCTYPE(result, pointer, *arguments)(callback)
            self.callbacks.append(wrapped)
            return c.cast(wrapped, pointer).value

        # IUnknown followed by the complete IFileOperationProgressSink vtable.
        methods = [
            add(hresult, [pointer, c.POINTER(pointer)], query),
            add(w.ULONG, [], lambda *_: 1), add(w.ULONG, [], lambda *_: 1),
            add(hresult, []), add(hresult, [hresult]),
            add(hresult, [dword, pointer, text]),
            add(hresult, [dword, pointer, text, hresult, pointer]),
            add(hresult, [dword, pointer, pointer, text]),
            add(hresult, [dword, pointer, pointer, text, hresult, pointer]),
            add(hresult, [dword, pointer, pointer, text]),
            add(hresult, [dword, pointer, pointer, text, hresult, pointer]),
            add(hresult, [dword, pointer], pre_delete),
            add(hresult, [dword, pointer, hresult, pointer], post_delete),
            add(hresult, [dword, pointer, text]),
            add(hresult, [dword, pointer, text, text, dword, hresult, pointer]),
            add(hresult, [w.UINT, w.UINT]),
            add(hresult, []), add(hresult, []), add(hresult, []),
        ]
        self.vtable = (pointer * len(methods))(*methods)

        class Instance(c.Structure):
            _fields_ = [("vtable", c.POINTER(pointer))]

        self.instance = Instance(self.vtable)
        self.pointer = c.cast(c.byref(self.instance), pointer)


@contextmanager
def _operation(flags):
    if os.name != "nt":
        raise OSError("视频删除需要 Windows 回收站")
    shell = _Shell()
    _check(shell.ole.CoInitializeEx(None, 2))
    operation = c.c_void_p()
    try:
        _check(shell.ole.CoCreateInstance(
            c.byref(_guid(_OPERATION_CLASS)), None, 1,
            c.byref(_guid(_OPERATION)), c.byref(operation)))
        _check(_method(operation, 5, c.c_long, w.DWORD)(operation, flags))
        yield shell, operation
    finally:
        _release(operation)
        shell.ole.CoUninitialize()


def _perform(operation):
    _check(_method(operation, 21, c.c_long)(operation))
    aborted = w.BOOL()
    _check(_method(operation, 22, c.c_long, c.POINTER(w.BOOL))(operation, c.byref(aborted)))
    if aborted.value:
        raise OSError("Windows 文件操作已取消")


def _recycle_directory(directory: Path) -> dict | None:
    """Recycle one staged bundle; never fall back to permanent deletion."""
    if not directory.exists():
        return None
    directory = directory.absolute()
    if directory.resolve() != directory or directory == Path(directory.anchor):
        raise ValueError("回收目录位置无效")
    for root, directories, files in os.walk(directory, followlinks=False):
        for name in [".", *directories, *files]:
            if (Path(root) / name).lstat().st_file_attributes & 0x400:
                raise ValueError("回收目录不能包含链接或目录联接")
    receipt = recycle_receipt_path(directory)
    write_recycle_record(receipt, {"original": str(directory), "prepared": True})

    def persist(ticket):
        write_recycle_record(receipt, {**ticket, "original": str(directory)})

    with _operation(_RECYCLE_FLAGS) as (shell, operation):
        item = shell.item(directory)
        sink = _DeleteSink(shell, directory, persist)
        try:
            _check(_method(operation, 18, c.c_long, c.c_void_p, c.c_void_p)(
                operation, item, sink.pointer))
            try:
                _perform(operation)
            except OSError:
                # PostDeleteItem can already have returned the recoverable item.
                if sink.ticket is None:
                    raise sink.error or OSError("移入 Windows 回收站失败，未永久删除")
            if sink.error and sink.ticket is None:
                raise sink.error
            if sink.ticket is None:
                raise OSError("Windows 未确认文件已移入回收站")
            return {**sink.ticket, "original": str(directory)}
        finally:
            _release(item)


def _restore_directory(ticket: dict) -> Path:
    """Restore the exact Shell item, including Windows' own recycle metadata."""
    destination = Path(ticket["original"])
    if destination.exists():
        raise ValueError("回收文件的暂存位置已被占用")
    if ticket.get("path") and not Path(ticket["path"]).is_dir():
        raise ValueError("Windows 回收站中的文件已被清空或移走，无法撤销删除")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with _operation(_FLAGS) as (shell, _unused):
        item, menu = c.c_void_p(), c.c_void_p()
        try:
            item = shell.recycled_item(ticket)
            _check(_method(item, 3, c.c_long, c.c_void_p, c.c_void_p,
                           c.c_void_p, c.POINTER(c.c_void_p))(
                item, None, c.byref(_guid("3981e225-f559-11d3-8e3a-00c04f6837d5")),
                c.byref(_guid("000214e4-0000-0000-c000-000000000046")), c.byref(menu)))

            class Command(c.Structure):
                _fields_ = [
                    ("size", w.DWORD), ("mask", w.DWORD), ("window", w.HWND),
                    ("verb", w.LPCSTR), ("parameters", w.LPCSTR), ("directory", w.LPCSTR),
                    ("show", c.c_int), ("hotkey", w.DWORD), ("icon", w.HANDLE),
                ]

            command = Command(c.sizeof(Command), 0x400 | 0x100, None, b"undelete")
            _check(_method(menu, 4, c.c_long, c.POINTER(Command))(menu, c.byref(command)))
            deadline = time.monotonic() + 10
            while not destination.is_dir() and time.monotonic() < deadline:
                time.sleep(.05)
        finally:
            _release(menu)
            _release(item)
    if not destination.is_dir():
        raise OSError("Windows 未确认回收文件已恢复")
    return destination


def _main():
    try:
        request = json.load(sys.stdin)
        if request["action"] == "recycle":
            result = _recycle_directory(Path(request["value"]))
        elif request["action"] == "restore":
            result = str(_restore_directory(request["value"]))
        else:
            raise ValueError("未知的回收站操作")
        print(json.dumps({"result": result}))
    except Exception as error:
        print(json.dumps({"error": str(error), "type": type(error).__name__}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main())
