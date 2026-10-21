"""Non-blocking Windows path picker helpers for the Streamlit UI."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from typing import Literal

from diagnostics import get_logger, log_exception
from qwen_process import WindowsJob


ROOT = Path(__file__).resolve().parent
PICKER_SCRIPT = ROOT / "scripts" / "path_picker.py"
PICKER_DIR = ROOT / ".runtime" / "pickers"
PickerKind = Literal["file", "directory"]
LOGGER = get_logger(__name__)


def start_path_picker(kind: PickerKind) -> dict[str, object]:
    """Launch the native dialog without blocking the Streamlit process."""
    if kind not in {"file", "directory"}:
        raise ValueError(f"Unknown picker kind: {kind}")
    PICKER_DIR.mkdir(parents=True, exist_ok=True)
    handle, result_name = tempfile.mkstemp(prefix="picker-", suffix=".json", dir=PICKER_DIR)
    os.close(handle)
    result_path = Path(result_name)
    result_path.unlink(missing_ok=True)
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP | 4
    process = subprocess.Popen(
        [sys.executable, str(PICKER_SCRIPT), "--kind", kind, "--result", str(result_path)],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        start_new_session=os.name != "nt",
        close_fds=True,
    )
    state = {"process": process, "result_path": result_path, "kind": kind,
             "owns_process_group": os.name != "nt"}
    try:
        if os.name == "nt":
            state["job"] = WindowsJob(process)
            state["job"].resume(process)
        return state
    except BaseException:
        discard_path_picker(state)
        raise


def read_path_picker_result(state: dict[str, object]) -> tuple[bool, str]:
    """Return (finished, selected_path); an empty path means the dialog was cancelled."""
    process = state["process"]
    if process.poll() is None:
        return False, ""
    result_path = Path(state["result_path"])
    try:
        if process.returncode:
            LOGGER.warning("Path picker exited unsuccessfully pid=%s exit_code=%s kind=%s",
                           process.pid, process.returncode, state.get("kind"))
            return True, ""
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        selected = payload.get("path", "") if isinstance(payload, dict) else ""
        return True, str(selected).strip() if isinstance(selected, str) else ""
    except (OSError, ValueError, TypeError) as exc:
        log_exception(LOGGER, "Unable to read path picker result", exc=exc,
                      phase="picker_result", pid=process.pid, exit_code=process.returncode)
        return True, ""
    finally:
        discard_path_picker(state)


def discard_path_picker(state: dict[str, object]) -> None:
    process = state.get("process")
    context = {"phase": "picker_cleanup", "pid": getattr(process, "pid", None)}

    def cleanup(label, action):
        try:
            action()
        except Exception as exc:
            log_exception(LOGGER, label, exc=exc, **context)

    # Each resource is independent: one failed termination must not skip file cleanup.
    job = state.pop("job", None)
    if job is not None:
        cleanup("Unable to close path picker process tree", job.close)
    elif process is not None:
        if os.name == "nt":
            def terminate_tree():
                if process.poll() is None:
                    result = subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        capture_output=True, timeout=15,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                    if result.returncode and process.poll() is None:
                        raise RuntimeError(f"taskkill failed with exit code {result.returncode}")
            cleanup("Unable to terminate path picker process tree", terminate_tree)
        elif state.get("owns_process_group"):
            def terminate_group():
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            cleanup("Unable to terminate path picker process group", terminate_group)
        else:
            cleanup("Unable to terminate path picker", lambda: process.terminate() if process.poll() is None else None)
    if process is not None:
        cleanup("Unable to kill path picker", lambda: process.kill() if process.poll() is None else None)
        cleanup("Unable to wait for path picker", lambda: process.wait(timeout=15))
    result_path = state.get("result_path")
    if result_path:
        cleanup("Unable to remove path picker result", lambda: Path(result_path).unlink(missing_ok=True))
