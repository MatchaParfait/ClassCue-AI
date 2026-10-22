import ctypes
import sys

import pytest

from scripts import webui_process


pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows process handles")


class ProcessKernel:
    def __init__(self):
        self.created = {10: "100", 11: "101"}
        self.running = {10: True, 11: True}
        self.open_errors = {}
        self.query_allowed = set()
        self.terminate_errors = {}
        self.exit_during_termination = set()
        self.opened = []
        self.terminated = []
        self.waited = []
        self.closed = []

    def OpenProcess(self, access, inherit, pid):
        self.opened.append(pid)
        if pid in self.open_errors and (access & 1 or pid not in self.query_allowed):
            ctypes.set_last_error(self.open_errors[pid])
            return 0
        return pid

    def TerminateProcess(self, handle, exit_code):
        self.terminated.append(handle)
        if handle in self.exit_during_termination:
            self.running[handle] = False
        if handle in self.terminate_errors:
            ctypes.set_last_error(self.terminate_errors[handle])
            return False
        self.running[handle] = False
        return True

    def WaitForSingleObject(self, handle, timeout):
        self.waited.append((handle, timeout))
        return 258 if self.running[handle] else 0

    def CloseHandle(self, handle):
        self.closed.append(handle)


@pytest.fixture
def kernel(monkeypatch):
    fixture = ProcessKernel()
    monkeypatch.setattr(webui_process, "kernel32", lambda: fixture)
    monkeypatch.setattr(webui_process, "handle_creation", lambda current, handle: current.created[handle])
    return fixture


def identities(*pids):
    return [{"pid": pid, "created": str(90 + pid)} for pid in pids]


def test_stop_verifies_all_identities_before_stopping_children_first(kernel):
    webui_process.stop_verified(identities(10, 11))
    assert kernel.opened == [10, 11]
    assert kernel.terminated == [11, 10]
    assert kernel.closed == [10, 11]


def test_stop_skips_already_signaled_process_without_terminating_it(kernel):
    kernel.running[10] = False
    webui_process.stop_verified(identities(10))
    assert kernel.terminated == []
    assert kernel.closed == [10]


def test_stop_accepts_access_denied_only_when_same_handle_has_exited(kernel):
    kernel.terminate_errors[10] = 5
    kernel.exit_during_termination.add(10)
    webui_process.stop_verified(identities(10, 11))
    assert kernel.terminated == [11, 10]
    assert (10, 5000) in kernel.waited
    assert kernel.closed == [10, 11]


def test_stop_does_not_ignore_access_denied_for_a_running_process(kernel):
    kernel.terminate_errors[10] = 5
    with pytest.raises(OSError) as error:
        webui_process.stop_verified(identities(10))
    assert error.value.winerror == 5
    assert kernel.running[10]
    assert kernel.closed == [10]


def test_stop_does_not_ignore_other_termination_errors_even_after_exit(kernel):
    kernel.terminate_errors[10] = 6
    kernel.exit_during_termination.add(10)
    with pytest.raises(OSError) as error:
        webui_process.stop_verified(identities(10))
    assert error.value.winerror == 6
    assert kernel.closed == [10]


def test_stop_rejects_reused_pid_before_terminating_any_process(kernel):
    kernel.created[11] = "new-process"
    with pytest.raises(RuntimeError, match="reused"):
        webui_process.stop_verified(identities(10, 11))
    assert kernel.terminated == []
    assert kernel.closed == [10, 11]


def test_stop_preserves_open_access_denied_and_closes_previous_handles(kernel):
    kernel.open_errors[11] = 5
    with pytest.raises(OSError) as error:
        webui_process.stop_verified(identities(10, 11))
    assert error.value.winerror == 5
    assert kernel.terminated == []
    assert kernel.closed == [10]


def test_stop_ignores_missing_process_but_still_stops_remaining_owned_process(kernel):
    kernel.open_errors[11] = 87
    webui_process.stop_verified(identities(10, 11))
    assert kernel.terminated == [10]
    assert kernel.closed == [10]


def test_stop_accepts_open_denied_only_after_query_handle_confirms_exit(kernel):
    kernel.open_errors[11] = 5
    kernel.query_allowed.add(11)
    kernel.running[11] = False
    webui_process.stop_verified(identities(10, 11))
    assert kernel.terminated == [10]
    assert kernel.closed == [11, 10]


def test_stop_rejects_open_denied_when_query_handle_is_still_running(kernel):
    kernel.open_errors[11] = 5
    kernel.query_allowed.add(11)
    with pytest.raises(OSError) as error:
        webui_process.stop_verified(identities(10, 11))
    assert error.value.winerror == 5
    assert kernel.terminated == []
    assert kernel.closed == [11, 10]


def test_stop_rejects_reused_pid_even_when_query_handle_has_exited(kernel):
    kernel.open_errors[11] = 5
    kernel.query_allowed.add(11)
    kernel.running[11] = False
    kernel.created[11] = "reused-process"
    with pytest.raises(RuntimeError, match="reused"):
        webui_process.stop_verified(identities(10, 11))
    assert kernel.terminated == []
    assert kernel.closed == [11, 10]


def test_stop_reports_timeout_if_termination_does_not_signal_handle(kernel, monkeypatch):
    monkeypatch.setattr(kernel, "TerminateProcess", lambda *_args: True)
    with pytest.raises(RuntimeError, match="PID 10"):
        webui_process.stop_verified(identities(10))
    assert kernel.closed == [10]


def test_stop_preserves_failed_wait_error_without_terminating_process(kernel, monkeypatch):
    def failed_wait(*_args):
        ctypes.set_last_error(6)
        return 0xFFFFFFFF

    monkeypatch.setattr(kernel, "WaitForSingleObject", failed_wait)
    with pytest.raises(OSError) as error:
        webui_process.stop_verified(identities(10))
    assert error.value.winerror == 6
    assert kernel.terminated == []
    assert kernel.closed == [10]
