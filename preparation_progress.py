"""Structured, bounded preparation telemetry shared by workers and the API."""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time


PREFIX = "@@ASR_PREPARATION@@"
_SINK = ContextVar("preparation_sink", default=None)
_DETAIL = ContextVar("preparation_detail", default=None)
_CHECKPOINT = ContextVar("preparation_checkpoint", default=None)
LABELS = {
    "checking": "检查已有环境与模型", "installing": "安装运行环境",
    "metadata": "获取权重文件清单", "downloading": "下载模型权重",
    "verifying": "校验模型文件", "finalizing": "完成模型准备",
}


@contextmanager
def preparation_events(callback, checkpoint=None):
    token = _SINK.set(callback)
    state = _DETAIL.set(None)
    control = _CHECKPOINT.set(checkpoint or _CHECKPOINT.get())
    try:
        yield
    finally:
        _DETAIL.reset(state)
        _SINK.reset(token)
        _CHECKPOINT.reset(control)


def checkpoint():
    if callback := _CHECKPOINT.get():
        callback()


def publish(detail: dict) -> None:
    checkpoint()
    value = {"active": True, **detail, "updated_at": time.time()}
    _DETAIL.set(value)
    if callback := _SINK.get():
        callback(value)
    elif os.environ.get("ASR_PREPARATION_EVENTS") == "1":
        print(PREFIX + json.dumps(value, ensure_ascii=True), flush=True)


def stage(phase: str, message: str, **fields) -> None:
    publish({"phase": phase, "label": LABELS[phase], "message": message,
             "phase_progress": None, "eta_seconds": None,
             "speed_bytes_per_second": None, **fields})


class DownloadProgress:
    """Estimate remaining *transfer* time from recent received bytes, never file length."""

    def __init__(self, total: int, progress=None, *, source="ModelScope"):
        self.total = total
        self.progress = progress
        self.source = source
        self.completed = 0
        self.samples = deque()
        self.current = {}
        self.last_bytes = 0
        self.last_change = time.monotonic()
        self.has_sample = False

    def file(self, component, repository, name, size):
        self.current = dict(component=component, repository=repository,
                            current_file=name, file_bytes_total=size)
        self.samples.clear()
        self.last_bytes = 0
        self.last_change = time.monotonic()
        self.has_sample = False
        self.report(0)

    def report(self, size: int):
        size = max(0, min(size, self.current["file_bytes_total"]))
        now = time.monotonic()
        if size > 0 and not self.has_sample:
            # Existing resumed bytes are the baseline, not newly received traffic.
            self.samples.clear()
            self.has_sample = True
        if size < self.last_bytes:  # A server ignored Range; restart the estimator too.
            self.samples.clear()
        if size != self.last_bytes:
            self.last_change = now
        self.last_bytes = size
        self.samples.append((now, size))
        while len(self.samples) > 2 and self.samples[1][0] < now - 12:
            self.samples.popleft()
        elapsed = now - self.samples[0][0]
        speed = (size - self.samples[0][1]) / elapsed if elapsed >= 2 else 0
        stalled = now - self.last_change >= 10
        if stalled:
            speed = 0
        completed = self.completed + size
        fraction = completed / max(1, self.total)
        message = f"{self.source} · {self.current['component']}/{self.current['current_file']}"
        publish({
            "phase": "downloading", "label": LABELS["downloading"], "message": message,
            "source": self.source, **self.current, "file_bytes_completed": size,
            "bytes_completed": completed, "bytes_total": self.total,
            "phase_progress": min(1, fraction), "speed_bytes_per_second": speed or None,
            "eta_seconds": (self.total - completed) / speed if speed and completed < self.total else None,
            "waiting_for_network": stalled,
        })
        if self.progress:
            self.progress(min(.99, fraction), message)

    def verifying(self, name, *, checked=None, total=None):
        stage("verifying", f"正在校验 {name}", **self.current,
              bytes_completed=self.completed + self.last_bytes, bytes_total=self.total,
              phase_progress=checked / total if total else None,
              checked_bytes=checked, check_total_bytes=total)


def prepare_component_files(model_id, progress=None):
    """Network dependencies stay in the host, outside the isolated inference runtime."""
    from scripts.prepare_asr_modelscope import download_components
    from asr_models import model_directory

    manifest = download_components(model_id, progress)
    directory = model_directory(model_id)
    temporary = directory / "download.pending.tmp"
    temporary.write_text(json.dumps(manifest), encoding="utf-8")
    temporary.replace(directory / "download.pending.json")


def component_manifest(model_id):
    from asr_models import get_model, model_directory

    if "--validate-only" not in sys.argv:
        from scripts.prepare_asr_modelscope import download_components
        return download_components(model_id)
    value = json.loads((model_directory(model_id) / "download.pending.json").read_text(encoding="utf-8"))
    if value["repositories"] != dict(get_model(model_id).repositories):
        raise ValueError("Prepared repositories do not match the model")
    return value


def command_label(command):
    if "clone" in command:
        return "下载 Dolphin 官方推理代码"
    if "checkout" in command:
        return "检查 Dolphin 推理代码版本"
    if "-c" in command:
        return "验证依赖导入与处理设备"
    if "venv" in command:
        return "创建独立 Python 环境"
    if any(str(part).startswith(("torch==", "torch-")) for part in command):
        return "下载并安装 PyTorch 与计算依赖"
    return "下载并安装推理依赖"


def run_logged(command, *, progress=None, step=None, steps=None, relay=False, **kwargs):
    """Poll in the caller's context; never block the task behind a silent child."""
    phase = "verifying" if relay else "installing"
    label = "检查权重与模型结构" if relay else command_label(command)
    detail = dict(step=step, steps=steps,
                  phase_progress=(step - 1) / steps if step and steps else None)
    stage(phase, label, **detail)
    if relay:
        kwargs["env"] = {**kwargs.get("env", os.environ), "ASR_PREPARATION_EVENTS": "1"}
    timeout = kwargs.pop("timeout", 3600)
    kwargs.pop("check", None)
    output = kwargs.pop("stdout", None)
    kwargs.pop("stderr", None)
    if os.name != "nt":
        kwargs["start_new_session"] = True
    # A file prevents pipe-buffer deadlocks and allows children to use ordinary logs.
    with tempfile.TemporaryDirectory(prefix="asr-progress-") as temporary:
        path = Path(temporary) / "output.log"
        with path.open("wb") as writer:
            process = subprocess.Popen(command, stdout=writer, stderr=subprocess.STDOUT, **kwargs)
        capture = path.open("rb")
        started = last_report = time.monotonic()
        position = 0
        pending = b""
        try:
            while True:
                capture.seek(position)
                data = capture.read()
                position += len(data)
                if data:
                    if output:
                        output.write(data.decode("utf-8", errors="replace"))
                        output.flush()
                    pending += data
                    lines = re.split(rb"[\r\n]", pending)
                    pending = lines.pop()
                    for raw in lines:
                        line = raw.decode("utf-8", errors="replace")
                        if relay and line.startswith(PREFIX):
                            try:
                                event = json.loads(line[len(PREFIX):])
                            except ValueError:
                                continue
                            publish(event)
                            if progress:
                                fraction = event.get("phase_progress")
                                progress(min(.99, fraction) if fraction is not None else 0,
                                         event.get("message", "准备模型"))
                        elif not relay:
                            # Only package names are exposed; URLs, credentials and tracebacks stay in logs.
                            match = re.search(r"(?:Downloading|Installing|Preparing|Building|Unpacking)\s+([A-Za-z0-9_.+-]+)", line)
                            if match and match[1].lower() not in {"collected", "packages", "metadata", "build"}:
                                detail["current_package"] = match[1][:100]
                    pending = pending[-65536:]
                code = process.poll()
                if code is not None and not data:
                    return subprocess.CompletedProcess(command, code)
                now = time.monotonic()
                if now - started > timeout:
                    raise subprocess.TimeoutExpired(command, timeout)
                if now - last_report >= 1:
                    if not relay:
                        stage(phase, label, elapsed_seconds=now - started, **detail)
                        if progress:
                            progress((step - 1) / steps if step and steps else 0, label)
                    else:
                        # Continue liveness reporting while a child hashes/loads a large checkpoint.
                        current = _DETAIL.get()
                        if current:
                            event = {**current}
                            if time.time() - event.get("updated_at", 0) > 10:
                                event.update(eta_seconds=None, speed_bytes_per_second=None)
                            publish(event)
                    last_report = now
                time.sleep(.2)
        finally:
            if process.poll() is None:
                from scripts.launcher_process import terminate_owned_tree
                terminate_owned_tree(process)
            capture.close()
