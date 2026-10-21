import io
import sys
from types import SimpleNamespace

import pytest

import asr_cpu
import qwen_process
from processing_control import ProcessingCancelled


@pytest.mark.skipif(sys.platform != "win32", reason="Windows worker priority and job flags")
def test_owned_worker_keeps_suspended_start_and_requests_normal_priority(monkeypatch):
    calls, lifecycle = [], []
    process = SimpleNamespace(
        stdout=io.StringIO("done\n"),
        poll=lambda: 0, wait=lambda **_kwargs: 0,
    )

    def popen(command, **options):
        calls.append(options)
        return process

    class Job:
        def __init__(self, child):
            assert child is process
            lifecycle.append("assigned")

        def resume(self, child):
            assert child is process
            lifecycle.append("resumed")

        def close(self):
            lifecycle.append("closed")

    monkeypatch.setattr(qwen_process.subprocess, "Popen", popen)
    monkeypatch.setattr(qwen_process, "WindowsJob", Job)
    assert qwen_process.run_process(["worker.exe"]) == 0
    assert calls[0]["creationflags"] == asr_cpu.worker_creationflags("gpu") | 4
    assert calls[0]["creationflags"] & qwen_process.subprocess.NORMAL_PRIORITY_CLASS
    assert calls[0]["start_new_session"] is False
    assert lifecycle == ["assigned", "resumed", "closed"]
    assert process.stdout.closed


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows GetPriorityClass")
def test_native_qwen_firered_process_launcher_reports_normal_priority():
    lines = []
    command = [
        sys.executable, "-u", "-c",
        "import ctypes;"
        "k=ctypes.WinDLL('kernel32',use_last_error=True);"
        "k.GetCurrentProcess.restype=ctypes.c_void_p;"
        "k.GetPriorityClass.argtypes=[ctypes.c_void_p];"
        "print(k.GetPriorityClass(k.GetCurrentProcess()),flush=True)",
    ]
    assert qwen_process.run_process(command, on_line=lines.append, timeout=20) == 0
    assert [line.strip() for line in lines] == ["32"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows owned-job fault injection")
@pytest.mark.parametrize("primary", [None, ValueError, ProcessingCancelled])
def test_job_cleanup_failure_still_reaps_and_closes_without_masking_primary(monkeypatch, primary):
    calls = []
    process = SimpleNamespace(
        pid=123, stdout=io.StringIO("ready\n"), poll=lambda: 0,
        wait=lambda **_kwargs: calls.append("wait") or 0,
    )
    cleanup_error = RuntimeError("job cleanup failed")
    primary_error = primary("callback failed") if primary else None

    class Job:
        def __init__(self, _child):
            pass

        def resume(self, _child):
            pass

        def close(self):
            raise cleanup_error

    def callback(_line):
        if primary:
            raise primary_error

    logs = []
    monkeypatch.setattr(qwen_process.subprocess, "Popen", lambda *_a, **_k: process)
    monkeypatch.setattr(qwen_process, "WindowsJob", Job)
    monkeypatch.setattr(qwen_process, "log_exception", lambda *_a, **kwargs: logs.append(kwargs))
    with pytest.raises(primary or RuntimeError) as error:
        qwen_process.run_process(["owned-worker"], on_line=callback)
    assert error.value is (primary_error if primary else cleanup_error)
    assert process.stdout.closed
    assert calls[-1] == "wait"
    assert logs[0]["stage"] == "process_tree"
    assert logs[0]["pid"] == 123


@pytest.mark.skipif(sys.platform != "win32", reason="Windows owned-job fault injection")
def test_wait_cleanup_failure_does_not_skip_pipe_close(monkeypatch):
    waits = []
    failure = qwen_process.subprocess.TimeoutExpired(["owned-worker"], 15)

    def wait(**_kwargs):
        waits.append(True)
        if len(waits) == 2:
            raise failure
        return 0

    process = SimpleNamespace(pid=123, stdout=io.StringIO(), poll=lambda: 0, wait=wait)
    job = SimpleNamespace(resume=lambda _p: None, close=lambda: None)
    monkeypatch.setattr(qwen_process.subprocess, "Popen", lambda *_a, **_k: process)
    monkeypatch.setattr(qwen_process, "WindowsJob", lambda _p: job)
    monkeypatch.setattr(qwen_process, "log_exception", lambda *_a, **_k: None)
    with pytest.raises(qwen_process.subprocess.TimeoutExpired) as error:
        qwen_process.run_process(["owned-worker"])
    assert error.value is failure
    assert process.stdout.closed


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows process liveness")
def test_owned_process_timeout_reaps_its_child(tmp_path):
    import time
    from scripts.launcher_process import windows_process_is_alive

    pid_file = tmp_path / "child.pid"
    command = [
        sys.executable, "-u", "-c",
        "import subprocess,sys,time,pathlib;"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        "pathlib.Path(sys.argv[1]).write_text(str(p.pid));"
        "print('ready',flush=True);time.sleep(60)",
        str(pid_file),
    ]
    with pytest.raises(qwen_process.subprocess.TimeoutExpired):
        qwen_process.run_process(command, timeout=1)
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while windows_process_is_alive(pid) and time.monotonic() < deadline:
        time.sleep(.05)
    assert not windows_process_is_alive(pid)
