"""Verify saved Windows service identities before stopping their process trees."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess


def kernel32():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel


def handle_creation(kernel, handle) -> str:
    times = [wintypes.FILETIME() for _ in range(4)]
    if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
        raise ctypes.WinError(ctypes.get_last_error())
    return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)


def creation_time(pid: int) -> str | None:
    if os.name != "nt":
        return None
    kernel = kernel32()
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        return handle_creation(kernel, handle)
    finally:
        kernel.CloseHandle(handle)


def snapshot() -> dict[int, dict]:
    if os.name != "nt":
        raise RuntimeError("Combined stop/restart currently supports Windows only")
    command = (
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new();"
        "@(Get-CimInstance Win32_Process | ForEach-Object {"
        "[pscustomobject]@{pid=[int]$_.ProcessId;parent=[int]$_.ParentProcessId;"
        "command=$_.CommandLine;created=$_.CreationDate.ToFileTimeUtc().ToString()}"
        "}) | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-Command", command],
        capture_output=True, timeout=20, check=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    records = json.loads(result.stdout.decode("utf-8-sig"))
    verified = {}
    for record in records:
        created = creation_time(record["pid"])
        # WMI rounds FILETIME to microseconds; retain the native 100ns identity.
        if created and int(created) // 10 == int(record["created"]) // 10:
            verified[record["pid"]] = {**record, "created": created}
    return verified


def command_args(command: str) -> list[str]:
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    count = ctypes.c_int()
    argv = shell.CommandLineToArgvW(command, ctypes.byref(count))
    if not argv:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return [argv[index] for index in range(count.value)]
    finally:
        kernel = ctypes.WinDLL("kernel32")
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree(argv)


def matches_service(record: dict, service: str, root: Path, port: int) -> bool:
    args = command_args(record.get("command") or "")
    if not args:
        return False
    script = root / ("scripts/api_service.py" if service == "api" else "frontend/node_modules/vite/bin/vite.js")
    expected = os.path.normcase(os.path.abspath(script))
    paths = [os.path.normcase(os.path.abspath(arg)) for arg in args[1:]]
    executable = Path(args[0]).name.lower()
    if expected not in paths or (service == "vite" and executable != "node.exe"):
        return False
    if service == "api" and executable not in {"python.exe", "pythonw.exe", "python3.exe", "python3.12.exe"}:
        return False
    try:
        return args[args.index("--port") + 1] == str(port) and args[args.index("--host") + 1] == "127.0.0.1"
    except (ValueError, IndexError):
        return False


def descendants(roots: list[int], records: dict[int, dict]) -> list[int]:
    ordered = [pid for pid in roots if pid in records]
    seen = set(ordered)
    for parent in ordered:
        for pid, record in records.items():
            if (
                pid not in seen and record["parent"] == parent
                and int(record["created"]) >= int(records[parent]["created"])
            ):
                seen.add(pid)
                ordered.append(pid)
    return ordered


def service_records(pid: int, service: str, root: Path, port: int) -> list[dict]:
    """Remember descendants too: a venv wrapper can exit before its Python child."""
    records = snapshot()
    return [
        {"pid": member, "created": records[member]["created"]}
        for member in descendants([pid], records)
        if matches_service(records[member], service, root, port)
    ]


def stop_verified(records: list[dict]) -> None:
    # Hold real process handles throughout: PID reuse cannot redirect termination.
    kernel = kernel32()
    handles = []
    try:
        for record in records:
            handle = kernel.OpenProcess(0x1000 | 0x0001 | 0x00100000, False, record["pid"])
            if not handle:
                if ctypes.get_last_error() == 87:  # Process already exited.
                    continue
                raise ctypes.WinError(ctypes.get_last_error())
            handles.append(handle)
            if handle_creation(kernel, handle) != record["created"]:
                raise RuntimeError(f"PID {record['pid']} was reused; no service was stopped")
        for handle in reversed(handles):
            exit_code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                raise ctypes.WinError(ctypes.get_last_error())
            if exit_code.value == 259 and not kernel.TerminateProcess(handle, 0):
                raise ctypes.WinError(ctypes.get_last_error())
        for handle in handles:
            if kernel.WaitForSingleObject(handle, 5000) != 0:
                raise RuntimeError("Timed out waiting for an owned service to stop")
    finally:
        for handle in handles:
            kernel.CloseHandle(handle)
