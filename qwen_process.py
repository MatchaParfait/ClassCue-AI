"""Cancellable subprocesses whose entire owned process tree is reaped."""

import ctypes
from ctypes import wintypes
import os
from queue import Empty, Queue
import signal
import subprocess
from threading import Thread
import time

from processing_control import ProcessingCancelled
from asr_cpu import worker_creationflags
from diagnostics import get_logger, log_exception


LOGGER = get_logger(__name__)


class WindowsJob:
    """A private kill-on-close job includes the Windows venv launcher child."""

    def __init__(self, process):
        kernel = self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        size = ctypes.c_size_t

        class Limits(ctypes.Structure):
            _fields_ = [("user", ctypes.c_int64), ("job_user", ctypes.c_int64),
                        ("flags", wintypes.DWORD), ("min_working", size), ("max_working", size),
                        ("active", wintypes.DWORD), ("affinity", size),
                        ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]

        class Extended(ctypes.Structure):
            _fields_ = [("basic", Limits), ("io", ctypes.c_uint64 * 6),
                        ("process_memory", size), ("job_memory", size),
                        ("peak_process", size), ("peak_job", size)]

        class Accounting(ctypes.Structure):
            _fields_ = [("times", ctypes.c_int64 * 4), ("faults", wintypes.DWORD),
                        ("total", wintypes.DWORD), ("active", wintypes.DWORD),
                        ("terminated", wintypes.DWORD)]

        self.Accounting = Accounting
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                      wintypes.DWORD, ctypes.c_void_p]
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if (not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits))
                or not kernel.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(process._handle)))):
            error = ctypes.WinError(ctypes.get_last_error())
            kernel.CloseHandle(self.handle)
            self.handle = None
            raise error

    def active(self):
        info = self.Accounting()
        if not self.kernel.QueryInformationJobObject(
            self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return info.active

    def resume(self, process):
        # Assign the suspended launcher before it can create an untracked child.
        kernel = self.kernel

        class ThreadEntry(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD),
                        ("thread_id", wintypes.DWORD), ("process_id", wintypes.DWORD),
                        ("priority", wintypes.LONG), ("delta", wintypes.LONG), ("flags", wintypes.DWORD)]

        kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        kernel.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenThread.restype = wintypes.HANDLE
        kernel.ResumeThread.argtypes = [wintypes.HANDLE]
        kernel.ResumeThread.restype = wintypes.DWORD
        snapshot = kernel.CreateToolhelp32Snapshot(4, 0)
        if snapshot == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = ThreadEntry()
            entry.size = ctypes.sizeof(entry)
            found = kernel.Thread32First(snapshot, ctypes.byref(entry))
            while found:
                if entry.process_id == process.pid:
                    thread = kernel.OpenThread(2, False, entry.thread_id)
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

    def close(self):
        if self.handle:
            try:
                if self.active():
                    self.kernel.TerminateJobObject(self.handle, 1)
                    deadline = time.monotonic() + 15
                    while self.active():
                        if time.monotonic() > deadline:
                            raise RuntimeError("Owned model process tree did not exit")
                        time.sleep(.05)
            finally:
                self.kernel.CloseHandle(self.handle)
                self.handle = None


def run_process(command, *, env=None, cwd=None, should_stop=None, on_line=None, timeout=None):
    if should_stop and should_stop():
        raise ProcessingCancelled("用户已请求停止处理")
    process = subprocess.Popen(
        command, env=env, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        creationflags=(worker_creationflags("gpu") | 4) if os.name == "nt" else 0,
        start_new_session=os.name != "nt",
    )
    job = None
    events = Queue()

    def read():
        try:
            for line in process.stdout:
                events.put(line)
        finally:
            events.put(None)

    reader = Thread(target=read, daemon=True)
    started = time.monotonic()
    primary_error = None
    try:
        if os.name == "nt":
            job = WindowsJob(process)
            job.resume(process)
        reader.start()
        while True:
            if should_stop and should_stop():
                raise ProcessingCancelled("用户已请求停止处理")
            if timeout is not None and time.monotonic() - started > timeout:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                line = events.get(timeout=.2)
            except Empty:
                # An orphan may still hold stdout after its parent has exited.
                if process.poll() is not None:
                    break
                continue
            if line is None:
                break
            if on_line:
                on_line(line)
        return process.wait(timeout=15)
    except BaseException as error:
        primary_error = error
        raise
    finally:
        cleanup_errors = []

        def cleanup(stage, operation):
            try:
                operation()
            except Exception as error:
                cleanup_errors.append(error)
                log_exception(LOGGER, "Owned worker cleanup failed", exc=error,
                              stage=stage, pid=getattr(process, "pid", None))

        def close_tree():
            if job:
                job.close()
            elif os.name == "nt":
                if process.poll() is None:
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        capture_output=True, timeout=15,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

        cleanup("process_tree", close_tree)

        def kill_direct():
            if process.poll() is None:
                process.kill()

        cleanup("kill", kill_direct)
        cleanup("wait", lambda: process.wait(timeout=15))
        if reader.ident is not None:
            cleanup("reader_join", lambda: reader.join(timeout=5))
        cleanup("stdout_close", process.stdout.close)
        if cleanup_errors and primary_error is None:
            raise cleanup_errors[0]
