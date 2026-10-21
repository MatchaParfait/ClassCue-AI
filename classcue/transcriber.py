"""Selectable local ASR backends with lazy downloads and one resident model."""

from __future__ import annotations

import atexit
from contextlib import contextmanager
from contextvars import ContextVar
import gc
import json
import os
import signal
from pathlib import Path
from queue import Empty, Queue
import subprocess
from threading import Thread
from typing import Callable

from classcue.asr_models import (
    OPERATION_LOCK, ROOT, component_paths, delete_model, download_model, get_model,
    model_operation, selected_model_id, selected_firered_device, selected_paraformer_device,
    selected_nano_device, selected_dolphin_device, require_firered_gpu,
    require_selectable_model,
)
from classcue.core import TranscriptSegment
from classcue.asr_cpu import cpu_thread_policy, worker_creationflags
from classcue.qwen_runtime import clear_runtime, install_runtime, runtime_is_ready, runtime_python
from classcue.firered_runtime import (
    clear_runtime as clear_firered_runtime,
    install_runtime as install_firered_runtime,
    runtime_is_ready as firered_runtime_is_ready,
    runtime_python as firered_runtime_python,
)
from classcue.paraformer_runtime import (
    clear_runtime as clear_paraformer_runtime,
    install_runtime as install_paraformer_runtime,
    runtime_is_ready as paraformer_runtime_is_ready,
    runtime_python as paraformer_runtime_python,
)
from classcue.nano_runtime import (
    clear_runtime as clear_nano_runtime,
    install_runtime as install_nano_runtime,
    runtime_is_ready as nano_runtime_is_ready,
    runtime_python as nano_runtime_python,
    require_cuda as require_nano_cuda,
)
from classcue.dolphin_runtime import (
    clear_runtime as clear_dolphin_runtime,
    install_runtime as install_dolphin_runtime,
    runtime_is_ready as dolphin_runtime_is_ready,
    runtime_python as dolphin_runtime_python,
    require_cuda as require_dolphin_cuda,
)
from classcue.runtime import whisper_backend
from classcue.processing_control import ProcessingCancelled, ResourcePaused
from classcue.diagnostics import get_logger, log_exception
from classcue.qwen_process import WindowsJob


LOGGER = get_logger(__name__)


_TRANSCRIBE_LOCK = OPERATION_LOCK
_loaded_key = None
_loaded_model = None
_loaded_session = None
_SESSION = ContextVar("asr_session", default=None)


def _cleanup_resources(steps, *, primary_error=None, **context):
    errors = []
    for stage, operation in steps:
        try:
            operation()
        except Exception as error:
            errors.append(error)
            log_exception(LOGGER, "ASR resource cleanup failed", exc=error, stage=stage, **context)
    if errors and primary_error is None:
        raise errors[0]


def _start_owned_worker(worker, command, *, log, env, device, reader_name):
    worker.events = Queue()
    worker.reader = Thread(target=worker._read_events, daemon=True, name=reader_name)
    worker.job = None
    worker._owns_process_group = os.name != "nt"
    worker.process = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
        text=True, encoding="utf-8", cwd=ROOT, env=env,
        creationflags=worker_creationflags(device) | (4 if os.name == "nt" else 0),
        start_new_session=worker._owns_process_group,
    )
    try:
        if os.name == "nt":
            worker.job = WindowsJob(worker.process)
            worker.job.resume(worker.process)
        worker.reader.start()
    except BaseException as error:
        worker.close(primary_error=error)
        raise


@contextmanager
def asr_session():
    """Reuse a model within one video or batch, then release it on every exit."""
    if _SESSION.get() is not None:
        yield
        return
    session = object()
    token = _SESSION.set(session)
    primary_error = None
    try:
        yield
    except BaseException as error:
        primary_error = error
        raise
    finally:
        try:
            if _loaded_session is session:
                with OPERATION_LOCK:
                    if _loaded_session is session:
                        unload_model(primary_error=primary_error)
        finally:
            _SESSION.reset(token)


class ASRSetupError(RuntimeError):
    """Setup/inference errors must not be retried as media decoding errors."""


class QwenWorker:
    model_label = "Qwen"

    def __init__(self) -> None:
        if not runtime_is_ready():
            raise ASRSetupError("Qwen 运行环境未就绪，请先准备所选模型。")
        log_dir = ROOT / ".runtime" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = log_dir / "qwen-worker.log"
        env = os.environ.copy()
        env.update({"PYTHONUTF8": "1", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"})
        with self.log_path.open("a", encoding="utf-8") as log:
            _start_owned_worker(self,
                [str(runtime_python()), "-u", str(ROOT / "scripts" / "qwen_worker.py")],
                log=log, env=env, device="gpu", reader_name="qwen-output",
            )

    def _read_events(self) -> None:
        try:
            for line in self.process.stdout:
                self.events.put(line)
        finally:
            self.events.put(None)

    def close(self, *, primary_error=None) -> None:
        def close_tree():
            job = getattr(self, "job", None)
            if job is not None:
                job.close()
            elif self.process.poll() is None and os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            elif getattr(self, "_owns_process_group", False):
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

        def terminate():
            if self.process.poll() is None:
                self.process.terminate()

        def wait_soft():
            if self.process.poll() is None:
                try:
                    self.process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    pass

        def kill():
            if self.process.poll() is None:
                self.process.kill()

        def reap():
            if self.process.poll() is None:
                self.process.wait(timeout=15)

        steps = [("process_tree", close_tree), ("terminate", terminate),
                 ("wait_soft", wait_soft), ("kill", kill), ("wait", reap)]
        if self.process.stdin:
            steps.append(("stdin_close", self.process.stdin.close))
        if getattr(self.reader, "ident", True) is not None:
            steps.append(("reader_join", lambda: self.reader.join(timeout=5)))
        if self.process.stdout:
            steps.append(("stdout_close", self.process.stdout.close))
        _cleanup_resources(
            steps, primary_error=primary_error,
            model=getattr(self, "model_label", type(self).__name__),
            device=getattr(self, "device", None), pid=getattr(self.process, "pid", None),
        )

    def transcribe(self, audio_path: str | Path, paths: dict, progress, should_stop=None,
                   *, silence_options=None, audit_output=None):
        request = {"audio_path": str(Path(audio_path).resolve()),
                   "recognizer": str(paths["recognizer"]), "aligner": str(paths["aligner"])}
        if silence_options is not None:
            request["silence_options"] = silence_options
        if audit_output is not None:
            request["silence_audit_output"] = str(Path(audit_output).resolve())
        event = self._request(request, progress, should_stop)
        return [TranscriptSegment(**segment) for segment in event["segments"]], int(event["duration_ms"])

    def transcribe_batch(self, audio_paths, paths: dict, progress=None, batch_size=2, should_stop=None):
        request = {
            "audio_paths": [str(Path(path).resolve()) for path in audio_paths],
            "recognizer": str(paths["recognizer"]), "aligner": str(paths["aligner"]),
            "batch_size": batch_size,
        }
        event = self._request(request, progress, should_stop)
        return [
            ([TranscriptSegment(**s) for s in item["segments"]], int(item["duration_ms"]))
            for item in event["results"]
        ]

    def _request(self, request, progress, should_stop=None):
        if should_stop and should_stop():
            raise ProcessingCancelled("用户已请求停止处理")
        try:
            self.process.stdin.write(json.dumps(request, ensure_ascii=True) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            self._communication_error(exc, "request_write")
        while True:
            try:
                line = self.events.get(timeout=0.5)
            except Empty:
                if should_stop and should_stop():
                    failure = ProcessingCancelled("用户已请求停止处理")
                    self.close(primary_error=failure)
                    raise failure
                exitcode = self.process.poll()
                if exitcode is not None:
                    failure = ASRSetupError(
                        f"{self.model_label} 工作进程已退出（PID {self.process.pid}，"
                        f"退出码 {exitcode}），详情见 {self.log_path}"
                    )
                    self.close(primary_error=failure)
                    raise failure
                continue
            if line is None:
                raise ASRSetupError(
                    f"{self.model_label} 工作进程已退出（退出码 {self.process.poll()}），详情见 {self.log_path}"
                )
            try:
                event = json.loads(line)
                if not isinstance(event, dict) or not isinstance(event.get("event"), str):
                    raise ValueError("Worker event must be a JSON object with an event name")
                kind = event["event"]
                if kind == "progress":
                    if (isinstance(event.get("fraction"), bool)
                            or not isinstance(event.get("fraction"), (int, float))
                            or not isinstance(event.get("message"), str)):
                        raise ValueError("Worker progress event is missing valid fields")
                elif kind in ("error", "resource_paused"):
                    if not isinstance(event.get("message"), str):
                        raise ValueError("Worker failure event is missing a message")
                elif kind == "result":
                    if (not ("results" in event or "segments" in event)
                            or ("segments" in event and "duration_ms" not in event)):
                        raise ValueError("Worker result event is missing output fields")
                elif kind == "transcript_preview":
                    if not isinstance(event.get("segments"), list):
                        raise ValueError("Worker preview event is missing subtitle rows")
                    preview = [TranscriptSegment(**item) for item in event["segments"]]
                else:
                    raise ValueError("Worker event name is not supported")
            except (ValueError, TypeError, KeyError) as exc:
                self._communication_error(exc, "event_decode")
            if kind in ("progress", "transcript_preview"):
                if should_stop and should_stop():
                    failure = ProcessingCancelled("用户已请求停止处理")
                    self.close(primary_error=failure)
                    raise failure
                if kind == "transcript_preview":
                    from classcue.transcript_events import emit_transcript_preview
                    emit_transcript_preview(preview)
                elif progress:
                    progress(event["fraction"], event["message"])
            elif kind == "result":
                return event
            elif kind == "resource_paused":
                raise ResourcePaused(event["message"], failure=event.get("failure"))
            elif kind == "error":
                raise ASRSetupError(f"{self.model_label} 转写失败：{event['message']}；详情见 {self.log_path}")

    def _communication_error(self, exc, stage):
        context = {
            "model": self.model_label, "device": getattr(self, "device", "gpu"),
            "pid": getattr(self.process, "pid", None), "exitcode": self.process.poll(),
            "stage": stage, "error_type": type(exc).__name__,
            "errno": getattr(exc, "errno", None), "winerror": getattr(exc, "winerror", None),
            "worker_log": str(self.log_path),
        }
        log_exception(LOGGER, "ASR worker communication failed", exc=exc, **context)
        raise ASRSetupError(
            f"{self.model_label} 工作进程通信失败（{context['error_type']}，"
            f"PID {context['pid']}，退出码 {context['exitcode']}），详情见 {self.log_path}"
        ) from exc


class ParaformerWorker(QwenWorker):
    model_label = "Paraformer"

    def __init__(self, device: str = "cpu") -> None:
        if device not in ("cpu", "gpu"):
            raise ValueError("Paraformer device must be cpu or gpu")
        self.device = device
        if not paraformer_runtime_is_ready(device):
            raise ASRSetupError(f"Paraformer {device.upper()} runtime is not ready; prepare the model first")
        if device == "gpu":
            from classcue.paraformer_runtime import require_cuda

            require_cuda()
        from classcue.paraformer_transcriber import runtime_environment

        log_dir = ROOT / ".runtime" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = log_dir / f"paraformer-{device}-worker.log"
        policy = cpu_thread_policy(device)
        env = {**os.environ, **runtime_environment(device, policy["cpu_threads"])}
        with self.log_path.open("a", encoding="utf-8") as log:
            log.write(json.dumps({"worker": "paraformer", "device": device, **policy}) + "\n")
            log.flush()
            _start_owned_worker(self,
                [str(paraformer_runtime_python(device)), "-u",
                 str(ROOT / "scripts" / "paraformer_worker.py"), "--device", device],
                log=log, env=env, device=device, reader_name="paraformer-output",
            )

    def transcribe(self, audio_path: str | Path, paths: dict, progress, should_stop=None,
                   *, batch_size=None, cpu_threads=None):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory(prefix="paraformer-job-") as temporary:
            try:
                request = {
                    "audio_path": str(Path(audio_path).resolve()),
                    "recognizer": str(paths["recognizer"]), "vad": str(paths["vad"]),
                    "temporary_directory": temporary,
                }
                if cpu_threads is not None:
                    request["cpu_threads"] = cpu_threads
                if batch_size is not None:
                    request["batch_size"] = batch_size
                event = self._request(request, progress, should_stop)
                return ([TranscriptSegment(**segment) for segment in event["segments"]],
                        int(event["duration_ms"]))
            except BaseException as error:
                self.close(primary_error=error)
                raise


class FireRedWorker:
    def __init__(self, device: str = "gpu") -> None:
        if device not in ("gpu", "cpu"):
            raise ValueError("FireRed device must be gpu or cpu")
        self.device = device
        if not firered_runtime_is_ready():
            raise ASRSetupError("FireRedASR2 运行环境未就绪，请先准备所选模型。")
        log_dir = ROOT / ".runtime" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = log_dir / "firered-worker.log"
        env = os.environ.copy()
        env.update({"PYTHONUTF8": "1", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"})
        if device == "cpu":
            from classcue.paraformer_transcriber import runtime_environment

            policy = cpu_thread_policy(device)
            env.update(runtime_environment(device, policy["cpu_threads"]))
            env["CUDA_VISIBLE_DEVICES"] = "-1"
        with self.log_path.open("a", encoding="utf-8") as log:
            if device == "cpu":
                log.write(json.dumps({"worker": "firered", "device": device, **policy}) + "\n")
                log.flush()
            _start_owned_worker(self,
                [str(firered_runtime_python()), "-u", str(ROOT / "scripts" / "firered_direct_worker.py"),
                 "--device", device],
                log=log, env=env, device=device, reader_name="firered-output",
            )

    def _read_events(self) -> None:
        try:
            for line in self.process.stdout:
                self.events.put(line)
        finally:
            self.events.put(None)

    def close(self, *, primary_error=None) -> None:
        QwenWorker.close(self, primary_error=primary_error)

    def transcribe(self, audio_path: str | Path, paths: dict, progress, should_stop=None):
        from tempfile import TemporaryDirectory

        temporary = TemporaryDirectory(prefix="firered-job-")
        request = {"audio_path": str(Path(audio_path).resolve()),
                   "recognizer": str(paths["recognizer"]),
                   "vad": str(paths["vad"] / "VAD"),
                   "temporary_directory": temporary.name,
                   "device": getattr(self, "device", "gpu")}
        primary_error = None
        try:
            if should_stop and should_stop():
                raise ProcessingCancelled("用户已请求停止处理")
            self.process.stdin.write(json.dumps(request, ensure_ascii=True) + "\n")
            self.process.stdin.flush()
            while True:
                try:
                    line = self.events.get(timeout=0.5)
                except Empty:
                    if should_stop and should_stop():
                        raise ProcessingCancelled("用户已请求停止处理")
                    exitcode = self.process.poll()
                    if exitcode is not None:
                        raise ASRSetupError(
                            f"FireRedASR2 工作进程已退出（PID {self.process.pid}，"
                            f"退出码 {exitcode}），详情见 {self.log_path}"
                        )
                    continue
                if line is None:
                    raise ASRSetupError(
                        f"FireRedASR2 工作进程已退出（退出码 {self.process.poll()}），详情见 {self.log_path}"
                    )
                event = json.loads(line)
                if event["event"] == "progress":
                    if should_stop and should_stop():
                        raise ProcessingCancelled("用户已请求停止处理")
                    if progress:
                        progress(event["fraction"], event["message"])
                elif event["event"] == "result":
                    return [TranscriptSegment(**segment) for segment in event["segments"]], int(event["duration_ms"])
                elif event["event"] == "error":
                    raise ASRSetupError(
                        f"FireRedASR2 转写失败：{event['message']}；详情见 {self.log_path}"
                    )
        except (Empty, BrokenPipeError, OSError, ValueError) as exc:
            primary_error = exc
            self.close(primary_error=exc)
            raise ASRSetupError(f"FireRedASR2 工作进程通信失败，详情见 {self.log_path}") from exc
        except BaseException as exc:
            primary_error = exc
            self.close(primary_error=exc)
            raise
        finally:
            # The parent owns cleanup even when stopping the worker prevents its finally blocks.
            _cleanup_resources([("temporary_cleanup", temporary.cleanup)],
                               primary_error=primary_error)


class NanoWorker(QwenWorker):
    model_label = "Fun-ASR-Nano"

    def __init__(self, device: str = "gpu") -> None:
        if device not in ("cpu", "gpu"):
            raise ValueError("Nano device must be cpu or gpu")
        self.device = device
        if not nano_runtime_is_ready(device):
            raise ASRSetupError(f"Nano {device.upper()} runtime is not ready; prepare the model first")
        if device == "gpu":
            require_nano_cuda()
        from classcue.paraformer_transcriber import runtime_environment

        log_dir = ROOT / ".runtime" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = log_dir / f"nano-{device}-worker.log"
        policy = cpu_thread_policy(device)
        env = {**os.environ, **runtime_environment(device, policy["cpu_threads"])}
        with self.log_path.open("a", encoding="utf-8") as log:
            log.write(json.dumps({"worker": "nano", "device": device, **policy}) + "\n")
            log.flush()
            _start_owned_worker(self,
                [str(nano_runtime_python(device)), "-u", str(ROOT / "scripts" / "nano_worker.py"),
                 "--device", device],
                log=log, env=env, device=device, reader_name="nano-output",
            )

    def transcribe(self, audio_path: str | Path, paths: dict, progress, should_stop=None,
                   *, batch_size=None, cpu_threads=None):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory(prefix="nano-job-") as temporary:
            try:
                request = {"audio_path": str(Path(audio_path).resolve()),
                           "recognizer": str(paths["recognizer"]), "vad": str(paths["vad"]),
                           "temporary_directory": temporary}
                if cpu_threads is not None:
                    request["cpu_threads"] = cpu_threads
                if batch_size is not None:
                    request["batch_size"] = batch_size
                event = self._request(request, progress, should_stop)
                return ([TranscriptSegment(**segment) for segment in event["segments"]],
                        int(event["duration_ms"]))
            except BaseException as error:
                self.close(primary_error=error)
                raise


class DolphinWorker(QwenWorker):
    model_label = "Dolphin"

    def __init__(self, device: str = "cpu") -> None:
        if device not in ("cpu", "gpu"):
            raise ValueError("Dolphin device must be cpu or gpu")
        self.device = device
        if not dolphin_runtime_is_ready(device):
            raise ASRSetupError(f"Dolphin {device.upper()} runtime is not ready; prepare the model first")
        if device == "gpu":
            require_dolphin_cuda()
        from classcue.paraformer_transcriber import runtime_environment

        log_dir = ROOT / ".runtime" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = log_dir / f"dolphin-{device}-worker.log"
        policy = cpu_thread_policy(device)
        env = {**os.environ, **runtime_environment(device, policy["cpu_threads"])}
        with self.log_path.open("a", encoding="utf-8") as log:
            log.write(json.dumps({"worker": "dolphin", "device": device, **policy}) + "\n")
            log.flush()
            _start_owned_worker(self,
                [str(dolphin_runtime_python(device)), "-u", str(ROOT / "scripts" / "dolphin_worker.py"),
                 "--device", device],
                log=log, env=env, device=device, reader_name="dolphin-output")

    def transcribe(self, audio_path: str | Path, paths: dict, progress, should_stop=None,
                   *, batch_size=None, cpu_threads=None):
        from tempfile import TemporaryDirectory
        from classcue.transcript_events import transcript_preview_requested

        with TemporaryDirectory(prefix="dolphin-job-") as temporary:
            try:
                request = {"audio_path": str(Path(audio_path).resolve()),
                           "recognizer": str(paths["recognizer"]), "vad": str(paths["vad"]),
                           "temporary_directory": temporary,
                           "stream_preview": transcript_preview_requested()}
                if cpu_threads is not None:
                    request["cpu_threads"] = cpu_threads
                if batch_size is not None:
                    request["batch_size"] = batch_size
                event = self._request(request, progress, should_stop)
                return ([TranscriptSegment(**segment) for segment in event["segments"]],
                        int(event["duration_ms"]))
            except BaseException as error:
                self.close(primary_error=error)
                raise


def unload_model(model_id: str | None = None, *, primary_error=None) -> None:
    global _loaded_key, _loaded_model, _loaded_session
    if model_id is not None and (_loaded_key is None or _loaded_key[0] != get_model(model_id).id):
        return
    model = _loaded_model
    _loaded_model = None
    _loaded_key = None
    _loaded_session = None
    steps = []
    if isinstance(model, (QwenWorker, FireRedWorker, NanoWorker)):
        steps.append(("worker_close", model.close))
    steps.append(("gc", gc.collect))
    _cleanup_resources(steps, primary_error=primary_error, model=type(model).__name__)


atexit.register(unload_model)


def prepare_asr_model(model_id: str, progress=None, *, paraformer_device: str | None = None,
                      nano_device: str | None = None, dolphin_device: str | None = None) -> None:
    from classcue.preparation_progress import stage
    from classcue.model_downloads import download_session

    model = require_selectable_model(model_id)
    device = (paraformer_device or selected_paraformer_device()) if model.engine == "paraformer" else (
        (dolphin_device or selected_dolphin_device()) if model.engine == "dolphin" else (
            (nano_device or selected_nano_device()) if model.engine == "nano" else None))
    with model_operation(model.id), download_session(model.id, device=device):
        stage("checking", f"检查 {model.label} 已有环境与权重", model_id=model.id)
        runtime_progress = (lambda value, message: progress(.25 * value, message)) if progress else None
        weight_progress = (lambda value, message: progress(.25 + .7 * value, message)) if progress else None
        if model.engine == "qwen":
            install_runtime(runtime_progress)
        elif model.engine == "firered":
            require_firered_gpu()
            install_firered_runtime(runtime_progress)
        elif model.engine == "paraformer":
            install_paraformer_runtime(runtime_progress, device=paraformer_device or selected_paraformer_device())
        elif model.engine == "nano":
            install_nano_runtime(runtime_progress, device=nano_device or selected_nano_device())
        elif model.engine == "dolphin":
            install_dolphin_runtime(runtime_progress, device=dolphin_device or selected_dolphin_device())
        if model.engine == "paraformer":
            download_model(model.id, weight_progress, reuse_legacy=False)
        else:
            download_model(model.id, weight_progress)
        stage("finalizing", f"{model.label} 环境与权重已就绪", model_id=model.id, phase_progress=1, active=False)


def clear_asr_model(model_id: str, remove_runtime: bool = False) -> int:
    with model_operation(model_id):
        size = delete_model(model_id, unload=unload_model)
        if remove_runtime and get_model(model_id).engine == "qwen":
            unload_model(model_id)
            size += clear_runtime()
        elif remove_runtime and get_model(model_id).engine == "firered":
            unload_model(model_id)
            size += clear_firered_runtime()
        elif remove_runtime and get_model(model_id).engine == "paraformer":
            unload_model(model_id)
            size += clear_paraformer_runtime()
        elif remove_runtime and get_model(model_id).engine == "nano":
            unload_model(model_id)
            size += clear_nano_runtime()
        elif remove_runtime and get_model(model_id).engine == "dolphin":
            unload_model(model_id)
            size += clear_dolphin_runtime()
        return size


def clear_asr_runtime(model_id: str, device: str | None = None) -> int:
    """Clear the selected inference environment, keeping downloaded weights."""
    model = get_model(model_id)
    if device not in {None, "cpu", "gpu"} or (device == "cpu" and model.engine in {"qwen", "firered"}):
        raise ValueError("所选运行环境不支持该设备")
    with model_operation(model.id):
        unload_model(model.id)
        if model.engine == "qwen":
            return clear_runtime()
        if model.engine == "firered":
            return clear_firered_runtime()
        if model.engine == "paraformer":
            return clear_paraformer_runtime(device=device)
        if model.engine == "dolphin":
            return clear_dolphin_runtime(device=device)
        if model.engine == "nano":
            return clear_nano_runtime(device=device)
        raise ValueError("该模型没有可单独清理的推理环境")


def _load_whisper_model(model_id: str, device: str, compute_type: str):
    global _loaded_key, _loaded_model, _loaded_session
    key = (model_id, device, compute_type)
    if _loaded_key != key:
        unload_model()
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise ASRSetupError("未安装 faster-whisper，请先运行 pip install -r requirements.txt") from exc
        _loaded_model = WhisperModel(
            str(component_paths(model_id)["recognizer"]), device=device,
            compute_type=compute_type, local_files_only=True,
        )
        _loaded_key = key
    _loaded_session = _SESSION.get()
    return _loaded_model


@asr_session()
def transcribe_audio(
    audio_path: str | Path,
    model_size: str | None = None,
    progress: Callable[[float, str], None] | None = None,
    *,
    model_id: str | None = None,
    should_stop: Callable[[], bool] | None = None,
    qwen_options=None,
    cache_directory: str | Path | None = None,
) -> tuple[list[TranscriptSegment], int]:
    global _loaded_key, _loaded_model, _loaded_session
    try:
        selected = require_selectable_model(model_id or model_size or selected_model_id())
    except ValueError as exc:
        raise ASRSetupError(str(exc)) from exc
    with model_operation(selected.id):
        if selected.engine == "firered":
            try:
                require_firered_gpu(selected_firered_device())
            except ValueError as exc:
                raise ASRSetupError(str(exc)) from exc
        try:
            prepare_asr_model(selected.id, progress)
        except ProcessingCancelled:
            raise
        except Exception as exc:
            raise ASRSetupError(f"{selected.label} 准备失败：{exc}") from exc
        if selected.engine == "qwen":
            from classcue.qwen_resilience import QwenOptions, run_qwen
            unload_model()
            try:
                return run_qwen(
                    audio_path, cache_directory or ROOT / "data" / "qwen_jobs",
                    component_paths(selected.id),
                    options=QwenOptions(**qwen_options) if isinstance(qwen_options, dict) else qwen_options,
                    progress=progress, should_stop=should_stop,
                )
            except (ProcessingCancelled, ResourcePaused):
                raise
            except Exception as exc:
                raise ASRSetupError(f"Qwen 转写失败：{exc}") from exc
        if selected.engine == "firered":
            from classcue.firered_resilience import run_firered
            unload_model()
            paths = component_paths(selected.id)
            try:
                return run_firered(
                    audio_path, cache_directory or ROOT / "data" / "firered_jobs",
                    {"recognizer": str(paths["recognizer"]), "vad": str(paths["vad"] / "VAD")},
                    progress=progress, should_stop=should_stop,
                )
            except (ProcessingCancelled, ResourcePaused):
                raise
            except Exception as exc:
                raise ASRSetupError(f"FireRed 转写失败：{exc}") from exc
        if selected.engine == "paraformer":
            device = selected_paraformer_device()
            key = (selected.id, "worker", device)
            if _loaded_key != key:
                unload_model()
                _loaded_model = ParaformerWorker(device=device)
                _loaded_key = key
            _loaded_session = _SESSION.get()
            try:
                return _loaded_model.transcribe(
                    audio_path, component_paths(selected.id), progress, should_stop,
                )
            except BaseException as error:
                unload_model(primary_error=error)
                raise
        if selected.engine == "nano":
            device = selected_nano_device()
            key = (selected.id, "worker", device)
            if _loaded_key != key:
                unload_model()
                _loaded_model = NanoWorker(device=device)
                _loaded_key = key
            _loaded_session = _SESSION.get()
            try:
                return _loaded_model.transcribe(
                    audio_path, component_paths(selected.id), progress, should_stop,
                )
            except BaseException as error:
                unload_model(primary_error=error)
                raise
        if selected.engine == "dolphin":
            device = selected_dolphin_device()
            key = (selected.id, "worker", device)
            if _loaded_key != key:
                unload_model()
                _loaded_model = DolphinWorker(device=device)
                _loaded_key = key
            _loaded_session = _SESSION.get()
            try:
                return _loaded_model.transcribe(
                    audio_path, component_paths(selected.id), progress, should_stop)
            except BaseException as error:
                unload_model(primary_error=error)
                raise
        return _transcribe_whisper(audio_path, selected.id, progress, should_stop)


def _transcribe_whisper(audio_path, model_id, progress, should_stop=None):
    backend = whisper_backend()
    with _TRANSCRIBE_LOCK:
        if should_stop and should_stop():
            raise ProcessingCancelled("用户已请求停止处理")
        if progress:
            progress(0.03, f"准备 {get_model(model_id).label}（{backend.device} · {backend.compute_type}）...")
        try:
            model = _load_whisper_model(model_id, backend.device, backend.compute_type)
        except Exception as exc:
            raise ASRSetupError(f"Whisper 模型加载失败：{exc}") from exc
        segments_iter, info = model.transcribe(
            str(audio_path), language="zh", word_timestamps=True, vad_filter=True, beam_size=5
        )
        result: list[TranscriptSegment] = []
        estimated_duration = float(info.duration or 1.0)
        for index, segment in enumerate(segments_iter, start=1):
            if should_stop and should_stop():
                raise ProcessingCancelled("用户已请求停止处理")
            start_ms, end_ms = int(segment.start * 1000), int(segment.end * 1000)
            words = [
                {"word": word.word, "start": word.start, "end": word.end, "probability": word.probability}
                for word in (segment.words or [])
            ]
            result.append(TranscriptSegment(index, start_ms, end_ms, segment.text.strip(), words))
            if progress:
                progress(
                    min(0.98, 0.06 + segment.end / estimated_duration * 0.92),
                    f"已转写 {index} 段 · {int(segment.end)} 秒",
                )
    duration_ms = int(estimated_duration * 1000)
    if result:
        duration_ms = max(duration_ms, result[-1].end_ms)
    return result, duration_ms
