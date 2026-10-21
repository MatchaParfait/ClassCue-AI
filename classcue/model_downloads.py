"""Durable model-download recovery and narrowly scoped temporary-file cleanup."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import time

from classcue.processing_control import ProcessingCancelled


class PreparationPaused(ProcessingCancelled):
    pass


STATE_NAME = ".preparation-state.json"


def _size(path):
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0  # A status reader may overlap the end of a cleanup task.


def _directory(model_id):
    from classcue.asr_models import model_directory
    return model_directory(model_id)


def read_state(model_id):
    try:
        value = json.loads((_directory(model_id) / STATE_NAME).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(model_id, value):
    from classcue.asr_models import _managed_path
    directory = _managed_path(_directory(model_id))
    directory.mkdir(parents=True, exist_ok=True)
    path = _managed_path(directory / STATE_NAME)
    temporary = _managed_path(directory / (STATE_NAME + ".tmp"))
    with temporary.open("w", encoding="utf-8") as target:
        json.dump({**value, "updated_at": time.time()}, target, ensure_ascii=False)
        target.flush()
        os.fsync(target.fileno())
    temporary.replace(path)


@contextmanager
def download_session(model_id, *, device=None):
    """The state survives process death; no startup action resumes work implicitly."""
    from classcue.preparation_progress import _SINK, PREFIX, preparation_events
    parent = _SINK.get()
    value = {**read_state(model_id), "status": "preparing", "device": device}
    _write(model_id, value)
    last_write = 0

    def report(detail):
        nonlocal last_write
        # Honor pause before writing a new phase or publishing progress.
        if parent:
            parent(detail)
        elif os.environ.get("ASR_PREPARATION_EVENTS") == "1":
            print(PREFIX + json.dumps(detail, ensure_ascii=True), flush=True)
        changed = value.get("phase") != detail.get("phase")
        value.update({key: detail[key] for key in (
            "phase", "component", "current_file", "file_bytes_completed",
            "bytes_completed", "bytes_total", "source",
        ) if key in detail})
        if changed or time.monotonic() - last_write >= .75:
            _write(model_id, value)
            last_write = time.monotonic()

    try:
        with preparation_events(report):
            yield
    except BaseException as exc:
        value["status"] = "paused" if isinstance(exc, PreparationPaused) else "interrupted"
        # Do not replace the original cancellation/network error with a disk error.
        try:
            _write(model_id, value)
        except OSError:
            pass
        raise
    else:
        (_directory(model_id) / STATE_NAME).unlink(missing_ok=True)


def _files(model_id):
    """Only app-owned model components/caches; never inference environments."""
    from classcue.asr_models import _managed_path, component_paths
    directory = _managed_path(_directory(model_id))
    if not directory.exists():
        return [], []
    components = tuple(component_paths(model_id).values())
    temporary, completed = [], []
    for path in directory.rglob("*"):
        # Reject junction/symlink escapes even when cleanup is called directly.
        resolved = _managed_path(path)
        if not resolved.is_relative_to(directory):
            raise ValueError("下载文件越出模型目录")
        if path.is_symlink():
            raise ValueError("下载目录包含符号链接，不能清理")
        if not path.is_file():
            continue
        relative = path.relative_to(directory)
        cache = ".hub-cache" in relative.parts or ".cache" in relative.parts
        if path.name in {STATE_NAME, "ready.json"}:
            continue
        if path.name == STATE_NAME + ".tmp":
            temporary.append(path)
            continue
        if ((not cache and path.name.endswith((".part", ".part.aria2", ".part.json")))
                or (cache and path.name.endswith(".incomplete"))):
            temporary.append(path)
        elif len(relative.parts) == 1 and path.name in {
            "download.pending.json", "download.pending.tmp", "ready.tmp",
        }:
            temporary.append(path)
        elif not cache and any(path.is_relative_to(component) for component in components):
            completed.append(path)
    return temporary, completed


def download_snapshot(model_id, *, active=False):
    state = read_state(model_id)
    temporary, completed = _files(model_id)
    # Completed files will be hash-checked again before reuse, not downloaded again.
    kept = sum(_size(path) for path in completed)
    partial = 0
    estimated = False
    from classcue.asr_models import component_paths
    component = state.get("component")
    base = component_paths(model_id).get(component)
    for path in temporary:
        if path.name.endswith(".part"):
            try:
                record = json.loads(path.with_name(path.name + ".json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                record = {}
            if path.with_name(path.name + ".aria2").exists() or record.get("segmented"):
                # aria2 may preallocate/sparsely write: length is not received bytes.
                estimated = True
                current = str((base / state["current_file"]).with_name(
                    Path(state["current_file"]).name + ".part")) if base and state.get("current_file") else None
                if str(path) == current:
                    partial += min(_size(path), max(0, int(state.get("file_bytes_completed", 0))))
            else:
                partial += _size(path)
        elif path.name.endswith(".incomplete"):
            # Xet also preallocates. Use the last confirmed transfer count for
            # this file; unknown older pieces are deliberately not overcounted.
            estimated = True
            if base and path.is_relative_to(base) and state.get("current_file") and path.name.startswith(
                    Path(state["current_file"]).name + "."):
                partial += min(_size(path), max(0, int(state.get("file_bytes_completed", 0))))
    from classcue.asr_models import model_is_downloaded
    ready = model_is_downloaded(model_id)
    if not state and not temporary and (not kept or ready):
        return None
    status = state.get("status", "interrupted")
    if active:
        status = "preparing"
    elif status == "preparing":
        status = "interrupted"
    return {
        "status": status, "retained_bytes": kept + partial, "completed_bytes": kept,
        "resumable": bool(state) or not ready,
        "retained_bytes_estimated": estimated,
        "partial_bytes": partial, "temporary_files": len(temporary),
        "can_cleanup": bool(temporary) or bool(state and (not kept or ready)),
        "bytes_total": state.get("bytes_total"),
        "current_file": state.get("current_file"), "device": state.get("device"),
    }


def cleanup_incomplete(model_id):
    from classcue.asr_models import model_operation, model_is_downloaded
    from classcue.cleanup_progress import CleanupProgress
    with model_operation(model_id):
        progress = CleanupProgress("未完成下载文件")
        progress.report(force=True, phase="scanning", message="检查下载临时文件，保留完整权重与运行环境")
        temporary, completed = _files(model_id)
        released = sum(path.stat().st_size for path in temporary)
        progress.report(force=True, phase="deleting", message="清理下载临时文件",
                        files_total=len(temporary), directories_total=0, bytes_total=released)
        # All targets were validated before the first mutation. Completed weights,
        # ready markers, HF metadata and inference environments remain untouched.
        deleted_bytes = 0
        for index, path in enumerate(temporary):
            size = _size(path)
            path.unlink(missing_ok=True)
            deleted_bytes += size
            progress.report(files_deleted=index + 1, bytes_deleted=deleted_bytes, current_path=path.name)
        state = _directory(model_id) / STATE_NAME
        if completed and not model_is_downloaded(model_id):
            _write(model_id, {"status": "interrupted", "device": read_state(model_id).get("device")})
        else:
            state.unlink(missing_ok=True)
        progress.report(force=True, phase="completed", message="未完成下载已清理，完整权重与运行环境已保留",
                        phase_progress=1, files_deleted=len(temporary), bytes_deleted=deleted_bytes,
                        eta_seconds=None, current_path=None)
        return released
