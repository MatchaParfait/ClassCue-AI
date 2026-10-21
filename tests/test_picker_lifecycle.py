import ctypes
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

import classcue.path_picker as path_picker
from classcue.picker_manager import PickerManager


def make_manager(*, ttl=900):
    now = [0.0]
    discarded = []
    manager = PickerManager(
        start=lambda kind: {"kind": kind, "finished": False, "path": ""},
        read=lambda state: (state["finished"], state["path"]),
        discard=discarded.append, clock=lambda: now[0], ttl_seconds=ttl,
    )
    return manager, now, discarded


def test_manager_preserves_payload_and_releases_completed_picker():
    manager, _, discarded = make_manager()
    created = manager.create("directory")
    picker_id = created["picker_id"]
    assert created == {"picker_id": picker_id, "status": "pending"}
    assert manager.get(picker_id) == {"picker_id": picker_id, "status": "pending", "path": ""}
    manager._pickers[picker_id].state.update(finished=True, path="E:/videos")
    assert manager.get(picker_id) == {"picker_id": picker_id, "status": "completed", "path": "E:/videos"}
    assert len(discarded) == 1
    with pytest.raises(KeyError):
        manager.get(picker_id)


def test_managers_are_isolated_and_cancel_is_not_repeatable():
    first, _, discarded = make_manager()
    second, _, _ = make_manager()
    picker_id = first.create("file")["picker_id"]
    with pytest.raises(KeyError):
        second.get(picker_id)
    assert first.cancel(picker_id)["status"] == "cancelled"
    assert len(discarded) == 1
    with pytest.raises(KeyError):
        first.cancel(picker_id)


def test_expiration_and_shutdown_remove_all_states_despite_cleanup_error():
    manager, now, discarded = make_manager(ttl=900)
    first = manager.create("file")["picker_id"]
    now[0] = 899
    second = manager.create("directory")["picker_id"]
    assert manager.reap_expired() == 0
    now[0] = 900
    assert manager.reap_expired() == 1
    assert len(discarded) == 1
    with pytest.raises(KeyError):
        manager.get(first)

    def discard_failure(state):
        discarded.append(state)
        raise OSError("cleanup failed")

    manager._discard = discard_failure
    third = manager.create("directory")["picker_id"]
    manager.shutdown()
    manager.shutdown()
    assert len(discarded) == 3
    assert not manager._pickers
    with pytest.raises(KeyError):
        manager.get(second)
    with pytest.raises(KeyError):
        manager.get(third)
    with pytest.raises(RuntimeError, match="shutting down"):
        manager.create("file")


def test_get_expiration_and_read_failure_return_cancelled_without_raw_error():
    manager, now, discarded = make_manager(ttl=10)
    expired = manager.create("file")["picker_id"]
    now[0] = 10
    assert manager.get(expired)["status"] == "cancelled"
    failed = manager.create("file")["picker_id"]

    def fail_read(_state):
        raise ValueError("sensitive internal detail")

    manager._read = fail_read
    assert manager.get(failed) == {"picker_id": failed, "status": "cancelled", "path": ""}
    assert len(discarded) == 2


def test_create_failure_leaves_no_registry_entry():
    manager, _, _ = make_manager()

    def fail_start(_kind):
        raise OSError("cannot launch picker")

    manager._start = fail_start
    with pytest.raises(OSError):
        manager.create("file")
    assert not manager._pickers


def fake_process(*, returncode=0, calls=None):
    calls = calls if calls is not None else []
    return SimpleNamespace(
        pid=1234, returncode=returncode, poll=lambda: returncode,
        wait=lambda **_options: calls.append("wait"),
        kill=lambda: calls.append("kill"),
        terminate=lambda: calls.append("terminate"),
    )


@pytest.mark.parametrize("payload,returncode,expected", [
    ('{"path": " E:/video.mp4 "}', 0, "E:/video.mp4"),
    ('{"path": ""}', 0, ""),
    ('{not-json', 0, ""),
    ('{"path": "E:/ignored.mp4"}', 2, ""),
])
def test_read_result_cleans_file_and_waits(tmp_path, payload, returncode, expected):
    result_path = tmp_path / "result.json"
    result_path.write_text(payload, encoding="utf-8")
    calls = []
    state = {"process": fake_process(returncode=returncode, calls=calls),
             "result_path": result_path, "kind": "file"}
    assert path_picker.read_path_picker_result(state) == (True, expected)
    assert not result_path.exists()
    assert "wait" in calls


def test_cleanup_failure_does_not_skip_kill_wait_or_result_file(tmp_path):
    result_path = tmp_path / "result.json"
    result_path.write_text("{}", encoding="utf-8")
    calls = []

    def close_failure():
        calls.append("close")
        raise OSError("job close failed")

    state = {"process": fake_process(returncode=None, calls=calls),
             "result_path": result_path, "job": SimpleNamespace(close=close_failure)}
    path_picker.discard_path_picker(state)
    assert calls == ["close", "kill", "wait"]
    assert not result_path.exists()
    assert "job" not in state


@pytest.mark.skipif(sys.platform != "win32", reason="Windows owned process-tree validation")
def test_hidden_picker_launcher_reaps_child_and_result(tmp_path, monkeypatch):
    script = tmp_path / "hidden_picker.py"
    script.write_text(
        "import argparse,json,subprocess,sys,time\n"
        "from pathlib import Path\n"
        "p=argparse.ArgumentParser();p.add_argument('--kind');p.add_argument('--result');a=p.parse_args()\n"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
        "Path(a.result+'.child').write_text(str(child.pid))\n"
        "Path(a.result).write_text(json.dumps({'path':''}))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(path_picker, "PICKER_SCRIPT", script)
    monkeypatch.setattr(path_picker, "PICKER_DIR", tmp_path)
    state = path_picker.start_path_picker("file")
    process = state["process"]
    child_path = Path(str(state["result_path"]) + ".child")
    child_handle = None
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.WaitForSingleObject.restype = ctypes.c_ulong
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    try:
        deadline = time.monotonic() + 15
        while not child_path.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        assert child_path.exists(), "hidden parent failed to launch its child"
        child_pid = int(child_path.read_text(encoding="utf-8"))
        child_handle = kernel.OpenProcess(0x100000, False, child_pid)
        assert child_handle
        path_picker.discard_path_picker(state)
        assert process.wait(timeout=5) is not None
        assert kernel.WaitForSingleObject(child_handle, 5000) == 0
        assert not Path(state["result_path"]).exists()
    finally:
        path_picker.discard_path_picker(state)
        if child_handle:
            if kernel.WaitForSingleObject(child_handle, 0) == 258:
                subprocess.run(
                    ["taskkill", "/PID", str(child_pid), "/T", "/F"],
                    capture_output=True, timeout=15,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            kernel.CloseHandle(child_handle)
