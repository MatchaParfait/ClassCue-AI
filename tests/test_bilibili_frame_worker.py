"""Supervisor IPC, hard timeout and cancellation; no external browser traffic.

The Windows tree-ownership tests run real synthetic workers that only sleep;
they never launch Edge or Playwright and never kill by bare PID.
"""

import base64
import ctypes
from ctypes import wintypes
import io
import json
import subprocess
import sys
import time

import pytest

from classcue.bilibili import BilibiliError
from classcue.bilibili_frames import BrowserFrameReader
from classcue.processing_control import ProcessingCancelled
from tests.bilibili_visual_fakes import synthetic_frame


UNIT = {"id": "synthetic", "bvid": "BV1xx411c7mD", "cid": 123, "page": 1}


class Repo:
    def __init__(self):
        self.values = {}

    def control(self, key):
        return self.values.get(key)

    def set_control(self, key, value):
        self.values[key] = value


class Process:
    def __init__(self, events):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("".join(json.dumps(event) + "\n" for event in events))

    def poll(self):
        return 0

    def wait(self, **_):
        return 0


class Job:
    """Records the assign/resume/close lifecycle without touching the OS."""

    def __init__(self, _child):
        self.lifecycle = ["assigned"]

    def resume(self, _child):
        self.lifecycle.append("resumed")

    def close(self):
        self.lifecycle.append("closed")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows suspended start and job ownership")
def test_credentials_travel_only_over_private_pipe_and_usage_requires_ack(monkeypatch):
    raw = synthetic_frame()
    process = Process([
        {"event": "ready"}, {"event": "consume", "key": "browser_requests", "amount": 1},
        {"event": "frame", "image": base64.b64encode(raw).decode(), "captured_ms": 2000, "time_source": "decoded_frame"},
    ])
    launch, costs = [], []
    job = Job(process)

    def popen(args, **kwargs):
        launch.append((args, kwargs))
        return process

    monkeypatch.setattr("classcue.bilibili_frames.subprocess.Popen", popen)
    monkeypatch.setattr("classcue.bilibili_frames.WindowsJob", lambda _child: job)
    reader = BrowserFrameReader(Repo(), "SESSDATA=synthetic-sensitive-session",
                                consume=lambda key, amount: costs.append((key, amount)))
    result = reader.capture(UNIT, 2000)
    assert result["data"] == raw and costs == [("browser_requests", 1)]
    assert "synthetic-sensitive-session" not in str(launch)
    assert launch[0][1]["creationflags"] & 4  # suspended until the job owns the tree
    assert job.lifecycle == ["assigned", "resumed"]
    sent = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
    assert sent[0]["cookie"] == "SESSDATA=synthetic-sensitive-session"
    assert sent[-1] == {"ok": True}
    reader.stop()
    assert job.lifecycle == ["assigned", "resumed", "closed"]


def test_hard_timeout_stops_worker_even_when_driver_never_responds(monkeypatch):
    reader, stopped = BrowserFrameReader(Repo()), []
    monkeypatch.setattr(reader, "start", lambda: None)
    monkeypatch.setattr(reader, "send", lambda _: None)
    monkeypatch.setattr(reader, "stop", lambda **kwargs: stopped.append(kwargs))
    monkeypatch.setattr("classcue.bilibili_frames.MAX_FRAME_SECONDS", .02)
    started = time.monotonic()
    with pytest.raises(BilibiliError) as caught:
        reader.capture(UNIT, 2000)
    assert caught.value.code == "capture_unavailable"
    assert time.monotonic() - started < .5 and stopped == [{"force": True}]


def test_timeout_reports_only_the_last_whitelisted_stage(monkeypatch):
    reader = BrowserFrameReader(Repo())
    reader.messages.put({"event": "stage", "stage": "等待定位画面"})
    reader.messages.put({"event": "stage", "stage": "synthetic-sensitive-driver-error"})
    with pytest.raises(BilibiliError) as caught:
        reader.wait("frame", .02)
    assert "等待定位画面" in str(caught.value)
    assert "synthetic-sensitive" not in str(caught.value)


def test_cancelled_supervisor_stops_worker_without_waiting_for_a_frame(monkeypatch):
    def checkpoint():
        raise ProcessingCancelled("合成取消")

    reader, stopped = BrowserFrameReader(Repo(), checkpoint=checkpoint), []
    monkeypatch.setattr(reader, "stop", lambda **kwargs: stopped.append(kwargs))
    with pytest.raises(ProcessingCancelled):
        reader.capture(UNIT, 2000)
    assert stopped == [{"force": True}]


def test_worker_risk_pause_is_persisted_and_not_treated_as_a_miss(monkeypatch):
    repo = Repo()
    reader = BrowserFrameReader(repo)
    reader.messages.put({"event": "pause", "reason": "browser_http_412"})
    monkeypatch.setattr(reader, "send", lambda _: None)
    with pytest.raises(BilibiliError) as caught:
        reader.wait("frame", 1)
    assert caught.value.code == "platform_paused"
    assert repo.control("platform_pause")["reason"] == "browser_http_412"


def test_worker_cannot_request_unrelated_tool_budget(monkeypatch):
    reader = BrowserFrameReader(Repo())
    reader.messages.put({"event": "consume", "key": "account_write", "amount": 1})
    monkeypatch.setattr(reader, "send", lambda _: None)
    with pytest.raises(BilibiliError, match="无效"):
        reader.wait("frame", 1)


def test_cleanup_step_failure_still_runs_later_steps_and_keeps_reference():
    reader, actions = BrowserFrameReader(Repo()), []

    class FailingJob:
        def close(self):
            actions.append("job_close")
            raise RuntimeError("job close failed")

    class Pipe:
        def __init__(self, name):
            self.name = name

        def close(self):
            actions.append(self.name + "_close")

    class StubbornProcess:
        pid = 4321

        def __init__(self):
            self.stdin, self.stdout = Pipe("stdin"), Pipe("stdout")

        def poll(self):
            return None

        def kill(self):
            actions.append("kill")

        def wait(self, **_):
            actions.append("wait")
            raise subprocess.TimeoutExpired(["worker"], 5)

    process = StubbornProcess()
    reader.process, reader.job, reader.reader_thread = process, FailingJob(), None
    with pytest.raises(RuntimeError, match="job close failed"):
        reader.stop()
    assert actions == ["job_close", "kill", "wait", "stdin_close", "stdout_close"]
    assert reader.process is process and reader.job is not None
    assert [stage for stage, _ in reader.cleanup_errors] == ["process_tree", "wait"]


def test_capture_error_is_not_masked_by_a_cleanup_failure():
    reader = BrowserFrameReader(Repo())

    class FailingJob:
        def close(self):
            raise RuntimeError("cleanup boom")

    class LiveProcess:
        pid = 99

        def __init__(self):
            self.stdin, self.stdout = io.StringIO(), io.StringIO()

        def poll(self):
            return None

        def kill(self):
            pass

        def wait(self, **_):
            return 0

    process = LiveProcess()
    reader.process, reader.job, reader.reader_thread = process, FailingJob(), None
    reader.messages.put({"event": "error", "code": "capture_failed"})
    with pytest.raises(BilibiliError) as caught:
        reader.capture(UNIT, 2000)
    assert caught.value.code == "capture_failed"
    assert reader.process is process  # retryable reference retained, not discarded
    assert reader.cleanup_errors  # the cleanup failure itself stays visible


def test_reader_join_timeout_reports_incomplete_and_retry_reaps():
    reader, actions = BrowserFrameReader(Repo()), []

    class Job:
        def close(self):
            actions.append("job_close")

    class Pipe:
        def __init__(self, name):
            self.name, self.written = name, []

        def write(self, value):
            self.written.append(value)

        def flush(self):
            pass

        def close(self):
            actions.append(self.name + "_close")

    class HangingReader:
        """A started reader thread whose join() returns while it is still alive."""

        ident = 1

        def __init__(self):
            self.alive = True

        def join(self, timeout=None):
            actions.append("reader_join")

        def is_alive(self):
            return self.alive

    class ExitedProcess:
        pid = 777

        def __init__(self):
            self.stdin, self.stdout = Pipe("stdin"), Pipe("stdout")

        def poll(self):
            return 0

        def wait(self, **_):
            actions.append("wait")
            return 0

    process, hanging = ExitedProcess(), HangingReader()
    reader.process, reader.job, reader.reader_thread = process, Job(), hanging
    with pytest.raises(RuntimeError, match="reader"):
        reader.stop(force=True)
    # join() returning does not mean the thread exited: the cleanup must be
    # reported incomplete, references kept for an explicit retry, and stdout
    # left open because readline() still owns it.
    assert actions == ["job_close", "wait", "reader_join", "stdin_close"]
    assert reader.process is process and reader.job is not None and reader.reader_thread is hanging
    assert [stage for stage, _ in reader.cleanup_errors] == ["reader_join"]

    hanging.alive = False
    reader.stop(force=True)
    assert "stdout_close" in actions and reader.cleanup_errors == []
    assert reader.process is None and reader.job is None and reader.reader_thread is None


def test_graceful_wait_failure_still_runs_the_tree_cleanup():
    reader, actions = BrowserFrameReader(Repo()), []

    class Job:
        def close(self):
            actions.append("job_close")

    class Pipe:
        def __init__(self, name):
            self.name, self.written = name, []

        def write(self, value):
            self.written.append(value)

        def flush(self):
            pass

        def close(self):
            actions.append(self.name + "_close")

    class BrokenWaitProcess:
        pid = 888

        def __init__(self):
            self.stdin, self.stdout = Pipe("stdin"), Pipe("stdout")

        def poll(self):
            return None

        def kill(self):
            actions.append("kill")

        def wait(self, timeout=None, **_):
            if timeout == 3:
                raise OSError("synthetic wait handle failure")
            actions.append("wait")
            return 0

    process = BrokenWaitProcess()
    reader.process, reader.job, reader.reader_thread = process, Job(), None
    with pytest.raises(OSError, match="synthetic wait handle failure"):
        reader.stop()
    assert json.loads(process.stdin.written[0]) == {"action": "close"}
    assert actions == ["job_close", "kill", "wait", "stdin_close", "stdout_close"]
    assert [stage for stage, _ in reader.cleanup_errors] == ["graceful_close"]
    assert reader.process is process  # the graceful failure stays retry-visible


def test_cleanup_continues_when_logging_the_cleanup_failure_itself_breaks(monkeypatch):
    reader, actions, logged = BrowserFrameReader(Repo()), [], []

    def broken_log(*_, **kwargs):
        logged.append(kwargs.get("stage"))
        raise RuntimeError("synthetic logging failure")

    monkeypatch.setattr("classcue.bilibili_frames.log_exception", broken_log)

    class FailingJob:
        def close(self):
            actions.append("job_close")
            raise RuntimeError("job close failed")

    class Pipe:
        def __init__(self, name):
            self.name = name

        def close(self):
            actions.append(self.name + "_close")

    class StubbornProcess:
        pid = 4322

        def __init__(self):
            self.stdin, self.stdout = Pipe("stdin"), Pipe("stdout")

        def poll(self):
            return None

        def kill(self):
            actions.append("kill")

        def wait(self, **_):
            actions.append("wait")
            raise subprocess.TimeoutExpired(["worker"], 5)

    process = StubbornProcess()
    reader.process, reader.job, reader.reader_thread = process, FailingJob(), None
    with pytest.raises(RuntimeError, match="job close failed"):
        reader.stop()
    # A diagnostics failure must not skip or mask the remaining cleanup steps.
    assert actions == ["job_close", "kill", "wait", "stdin_close", "stdout_close"]
    assert logged == ["process_tree", "wait"]
    assert [stage for stage, _ in reader.cleanup_errors] == ["process_tree", "wait"]


def _win_kernel():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


def _system_filetime():
    value = wintypes.FILETIME()
    ctypes.WinDLL("kernel32").GetSystemTimeAsFileTime(ctypes.byref(value))
    return value.dwHighDateTime << 32 | value.dwLowDateTime


class HeldProcessHandle:
    """A descendant handle whose native creation time was verified at open time."""

    def __init__(self, kernel, handle):
        self.kernel, self.handle = kernel, handle

    @classmethod
    def open(cls, pid, not_before_filetime):
        kernel = _win_kernel()
        handle = kernel.OpenProcess(0x0001 | 0x1000 | 0x00100000, False, pid)
        if not handle:
            raise AssertionError("could not open the synthetic descendant handle")
        creation, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exited),
                                      ctypes.byref(kernel_time), ctypes.byref(user_time)):
            kernel.CloseHandle(handle)
            raise AssertionError("could not read the descendant creation time")
        created = creation.dwHighDateTime << 32 | creation.dwLowDateTime
        assert created >= not_before_filetime, "descendant PID does not match its native creation time"
        return cls(kernel, handle)

    def alive(self):
        return self.kernel.WaitForSingleObject(self.handle, 0) == 0x102  # WAIT_TIMEOUT

    def wait_exit(self, seconds):
        return self.kernel.WaitForSingleObject(self.handle, int(seconds * 1000)) == 0

    def reap(self):
        if self.alive():
            self.kernel.TerminateProcess(self.handle, 1)
            self.kernel.WaitForSingleObject(self.handle, 15000)
        self.kernel.CloseHandle(self.handle)


def _read_pid_file(path, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists():
            text = path.read_text(encoding="utf-8")
            if text.strip():
                return int(text)
        time.sleep(.05)
    raise AssertionError("synthetic worker never reported its child pid")


def _wait_for_parent_exit(reader, seconds=10):
    process = reader.process
    assert process is not None
    deadline = time.monotonic() + seconds
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(.05)
    assert process.poll() is not None, "synthetic worker should exit abruptly by itself"


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows worker tree ownership")
def test_worker_tree_is_reaped_after_abnormal_parent_exit(tmp_path, monkeypatch):
    child_pid_file = tmp_path / "child.pid"
    worker_script = tmp_path / "synthetic_worker.py"
    worker_script.write_text(
        "import json, os, subprocess, sys\n"
        "print(json.dumps({'event': 'ready'}), flush=True)\n"
        "sys.stdin.readline()\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        f"with open(r'{child_pid_file}', 'w') as fh:\n"
        "    fh.write(str(child.pid))\n"
        "os._exit(0)\n",
        encoding="utf-8",
    )
    real_popen = subprocess.Popen

    def popen(args, **kwargs):
        assert args[1:] == ["-m", "classcue.bilibili_frame_worker"]
        return real_popen([sys.executable, str(worker_script)], **kwargs)

    monkeypatch.setattr("classcue.bilibili_frames.subprocess.Popen", popen)
    started_before = _system_filetime()
    reader = BrowserFrameReader(Repo())
    child = None
    try:
        reader.start()
        child = HeldProcessHandle.open(_read_pid_file(child_pid_file), started_before)
        _wait_for_parent_exit(reader)
        assert child.alive(), "the sleeping descendant must outlive the crashed worker"
        reader.stop(force=True)
        assert child.wait_exit(15), "stop must reap the whole owned tree after the parent exited first"
        assert reader.process is None and reader.job is None
    finally:
        if child is not None:
            child.reap()
        try:
            reader.stop(force=True)
        except Exception:
            pass


@pytest.mark.skipif(sys.platform != "win32", reason="Windows suspended worker reclamation")
def test_job_assignment_failure_reclaims_the_suspended_worker(monkeypatch):
    real_popen, spawned, assigned = subprocess.Popen, [], []

    def popen(args, **kwargs):
        spawned.append(real_popen(args, **kwargs))
        return spawned[-1]

    class UnassignedJob:
        def __init__(self, process):
            assigned.append(process)
            raise OSError("job assignment failed")

        def resume(self, _process):
            raise AssertionError("resume must not run after a failed assignment")

        def close(self):
            raise AssertionError("an unassigned job owns nothing to close")

    monkeypatch.setattr("classcue.bilibili_frames.subprocess.Popen", popen)
    monkeypatch.setattr("classcue.bilibili_frames.WindowsJob", UnassignedJob)
    reader = BrowserFrameReader(Repo())
    try:
        with pytest.raises(OSError, match="job assignment failed"):
            reader.start()
    finally:
        try:
            reader.stop(force=True)
        except Exception:
            pass
    assert assigned == [spawned[0]]
    deadline = time.monotonic() + 10
    while spawned[0].poll() is None and time.monotonic() < deadline:
        time.sleep(.05)
    assert spawned[0].poll() is not None, "the suspended direct worker must be reclaimed by handle"
    assert reader.process is None and reader.job is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows owned-job fault injection")
def test_resume_failure_terminates_the_suspended_worker_through_the_job(monkeypatch):
    from classcue.qwen_process import WindowsJob

    real_popen, spawned, closed = subprocess.Popen, [], []
    real_close = WindowsJob.close

    def popen(args, **kwargs):
        spawned.append(real_popen(args, **kwargs))
        return spawned[-1]

    def close(self):
        closed.append(True)
        real_close(self)

    def broken_resume(self, _process):
        raise OSError("resume failed")

    monkeypatch.setattr(WindowsJob, "close", close)
    monkeypatch.setattr(WindowsJob, "resume", broken_resume)
    monkeypatch.setattr("classcue.bilibili_frames.subprocess.Popen", popen)
    reader = BrowserFrameReader(Repo())
    try:
        with pytest.raises(OSError, match="resume failed"):
            reader.start()
    finally:
        try:
            reader.stop(force=True)
        except Exception:
            pass
    assert closed == [True], "the job itself must be the reaper of the suspended worker"
    deadline = time.monotonic() + 10
    while spawned[0].poll() is None and time.monotonic() < deadline:
        time.sleep(.05)
    assert spawned[0].poll() is not None
    assert reader.process is None and reader.job is None
