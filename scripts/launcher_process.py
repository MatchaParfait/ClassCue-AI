"""Owned child-process cleanup and bounded launcher diagnostics."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import subprocess
import sys
from threading import RLock


MAX_LOG_BYTES = 2 * 1024 * 1024
LOG_BACKUPS = 3


class RedactingRotatingFileHandler(RotatingFileHandler):
    def _open(self):
        return open(
            self.baseFilename, self.mode, encoding=self.encoding,
            errors=self.errors, newline="",
        )

    def format(self, record):
        from classcue.diagnostics import redact_text

        text = redact_text(super().format(record))
        data = text.encode(self.encoding or "utf-8", errors="replace")
        limit = min(65536, self.maxBytes - 1) if self.maxBytes > 0 else 65536
        if len(data) <= limit:
            return text
        marker = b"\n[oversized log entry truncated]\n"
        if limit < len(marker):
            return marker[:max(0, limit)].decode()
        remaining = max(0, limit - len(marker))
        head = data[:remaining // 2].decode("utf-8", errors="ignore")
        tail = data[-(remaining - remaining // 2):].decode("utf-8", errors="ignore") if remaining else ""
        return head + marker.decode() + tail

    def handleError(self, record):
        # stderr is itself captured while serving; never send logging errors there.
        try:
            from classcue.diagnostics import redact_text

            stream = sys.__stderr__
            if stream is not None and not isinstance(stream, BoundedLogStream):
                error = sys.exc_info()[1]
                stream.write(f"Service log write failed: {redact_text(error)[:1024]}\n")
                stream.flush()
        except Exception:
            pass

    def shouldRollover(self, record):
        if self.stream is None:
            self.stream = self._open()
        if self.maxBytes > 0:
            self.stream.seek(0, 2)
            size = len((self.format(record) + self.terminator).encode(self.encoding or "utf-8"))
            return self.stream.tell() + size > self.maxBytes
        return False


class BoundedLogStream:
    """Capture Python output without keeping an unbounded unterminated line."""

    encoding = "utf-8"

    def __init__(self, logger: logging.Logger, level: int):
        self.logger = logger
        self.level = level
        self.pending = ""
        self.lock = RLock()

    def write(self, text: str) -> int:
        from classcue.diagnostics import redact_text

        with self.lock:
            self.pending += text
            while "\n" in self.pending:
                line, self.pending = self.pending.split("\n", 1)
                self._line(line)
            # Retain a tail so normal credentials split across writes are masked
            # before an output boundary is committed to disk.
            while len(self.pending) >= 16384:
                self.pending = redact_text(self.pending)
                if len(self.pending) < 16384:
                    break
                self._line(self.pending[:8192])
                self.pending = self.pending[8192:]
        return len(text)

    def _line(self, text: str) -> None:
        from classcue.diagnostics import redact_text

        text = redact_text(text)
        for offset in range(0, len(text), 8192):
            self.logger.log(self.level, "%s", text[offset:offset + 8192])

    def flush(self) -> None:
        with self.lock:
            if self.pending:
                self._line(self.pending)
                self.pending = ""

    def isatty(self) -> bool:
        return False


def service_logger(log_dir: Path, service: str) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"classroom.service.{service}.{log_dir.resolve()}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if logger.handlers:
        return logger
    handler = RedactingRotatingFileHandler(
        log_dir / f"{service}.log", maxBytes=MAX_LOG_BYTES,
        backupCount=LOG_BACKUPS, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


def launcher_logger(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"classroom.launcher.{log_dir.resolve()}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = RedactingRotatingFileHandler(
            log_dir / "launcher.log", maxBytes=MAX_LOG_BYTES,
            backupCount=LOG_BACKUPS, encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


def _kernel32():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel.CreateJobObjectW.restype = ctypes.c_void_p
    kernel.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel.AssignProcessToJobObject.restype = ctypes.c_int
    kernel.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    kernel.TerminateJobObject.restype = ctypes.c_int
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    return kernel


def attach_cleanup_job(process: subprocess.Popen) -> None:
    """Track Windows descendants without killing healthy servers on launcher exit."""
    if os.name != "nt":
        return
    kernel = _kernel32()
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    if not kernel.AssignProcessToJobObject(job, int(process._handle)):
        error = ctypes.get_last_error()
        kernel.CloseHandle(job)
        raise ctypes.WinError(error)
    # No KILL_ON_JOB_CLOSE: successful background servers outlive this launcher.
    process._launcher_job = job


def release_cleanup_job(process: subprocess.Popen) -> None:
    job = getattr(process, "_launcher_job", None)
    if job:
        _kernel32().CloseHandle(job)
        process._launcher_job = None


def resume_owned_process(process: subprocess.Popen) -> None:
    """Resume the primary thread only after its suspended process is in the job."""
    if os.name != "nt":
        return
    kernel = _kernel32()

    class ThreadEntry(ctypes.Structure):
        _fields_ = [
            ("size", wintypes.DWORD), ("usage", wintypes.DWORD),
            ("thread_id", wintypes.DWORD), ("process_id", wintypes.DWORD),
            ("priority", wintypes.LONG), ("delta", wintypes.LONG), ("flags", wintypes.DWORD),
        ]

    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
    kernel.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
    kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenThread.restype = wintypes.HANDLE
    kernel.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel.ResumeThread.restype = wintypes.DWORD
    snapshot = kernel.CreateToolhelp32Snapshot(4, 0)  # TH32CS_SNAPTHREAD
    if snapshot == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = ThreadEntry()
        entry.size = ctypes.sizeof(entry)
        found = kernel.Thread32First(snapshot, ctypes.byref(entry))
        while found:
            if entry.process_id == process.pid:
                thread = kernel.OpenThread(2, False, entry.thread_id)  # THREAD_SUSPEND_RESUME
                if not thread:
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    if kernel.ResumeThread(thread) == 0xFFFFFFFF:
                        raise ctypes.WinError(ctypes.get_last_error())
                finally:
                    kernel.CloseHandle(thread)
                return
            found = kernel.Thread32Next(snapshot, ctypes.byref(entry))
        raise RuntimeError("Owned suspended process has no primary thread")
    finally:
        kernel.CloseHandle(snapshot)


def terminate_owned_tree(process: subprocess.Popen, *, timeout: float = 5.0) -> None:
    """Stop only a process created by this launcher and its descendants."""
    try:
        if os.name == "nt":
            job = getattr(process, "_launcher_job", None)
            if job and _kernel32().TerminateJobObject(job, 1):
                process.wait(timeout=timeout)
                return
            if process.poll() is None:
                result = subprocess.run(
                    ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=timeout,
                    creationflags=subprocess.CREATE_NO_WINDOW, check=False,
                )
                if result.returncode and process.poll() is None:
                    process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            process.wait(timeout=timeout)
    finally:
        release_cleanup_job(process)


def windows_process_is_alive(pid: int) -> bool:
    """Query a process without signals or requesting terminate rights."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
    kernel.GetExitCodeProcess.restype = ctypes.c_int
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        exit_code = ctypes.c_uint()
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and exit_code.value == 259
    finally:
        kernel.CloseHandle(handle)
