"""Fixed native-VRAM routing and bounded recovery for the official FireRed system."""

from classcue.paths import PROJECT_ROOT

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
import copy
import json
import logging
import math
import os
from pathlib import Path
from threading import RLock
import time
import wave

from classcue.core import TranscriptSegment
from classcue.processing_control import ProcessingCancelled, ResourcePaused
from classcue.qwen_resilience import (
    atomic_json, capacity_at_least, classify_error, digest, file_digest,
    installed_ram_mib, model_identity, read_event, read_json, validate_segments,
)

ROOT = PROJECT_ROOT
VERSION = 1
BATCH_LADDER = (8, 6, 5, 3, 2, 1)
_QUEUE = ContextVar("firered_queue", default=None)
_RESOURCE = ContextVar("firered_resource_reporter", default=None)
_PROFILE_LOCK = RLock()
_PROFILE_CACHE = None


class FireRedRunError(RuntimeError):
    pass


@dataclass
class FireRedQueue:
    hardware: dict | None = None
    batch_size: int | None = None


@contextmanager
def firered_queue():
    token = _QUEUE.set(FireRedQueue())
    try:
        yield
    finally:
        _QUEUE.reset(token)


@contextmanager
def resource_events(callback):
    """An independent optional metadata callback; progress remains (fraction, message)."""
    token = _RESOURCE.set(callback)
    try:
        yield
    finally:
        _RESOURCE.reset(token)


def select_batch_size(total_mib):
    return next((batch for gib, batch in ((12, 8), (8, 6), (6, 5), (4, 3))
                 if capacity_at_least(total_mib, gib, allowance_mib=64)), 1)


def resource_profile(*, refresh=False, should_stop=None):
    """Probe the isolated runtime's actual CUDA device, never import/load models."""
    global _PROFILE_CACHE
    from classcue.firered_runtime import runtime_is_ready, runtime_python
    from classcue.qwen_process import run_process

    with _PROFILE_LOCK:
        if not refresh and _PROFILE_CACHE and time.monotonic() - _PROFILE_CACHE[0] < 30:
            return copy.deepcopy(_PROFILE_CACHE[1])
        value = {"engine": "firered", "device": "cuda:0", "cuda_available": None,
                 "gpu_total_mib": None, "installed_ram_mib": installed_ram_mib()}
        if runtime_is_ready():
            events = []
            code = run_process(
                [str(runtime_python()), "-u", str(ROOT / "scripts/firered_worker.py"), "--probe"],
                cwd=ROOT, timeout=30, should_stop=should_stop,
                on_line=lambda line: events.append(read_event(line)),
            )
            probe = next((event for event in events
                          if event and event.get("event") == "hardware"), None)
            if code == 0 and probe:
                value.update(probe["hardware"])
            else:
                failure = next((event for event in events
                                if event and event.get("event") == "error"), {})
                value["warning"] = failure.get("message", "FireRed CUDA capacity probe failed")
                value["failure_kind"] = failure.get("kind", "environment")
        else:
            value["warning"] = "FireRed 独立运行环境尚未准备；开始处理前将检测 CUDA。"
        value["initial_batch_size"] = select_batch_size(value["gpu_total_mib"])
        if value["cuda_available"] is False:
            value["warning"] = "FireRed 需要可用 CUDA；不会自动切换 CPU，请检查运行环境或手动选择其他模型。"
        elif value["initial_batch_size"] == 1:
            value.setdefault("warning", "低于 4GB 或显存容量未知，使用 BS1 保守尝试；此档位尚未经硬件验证。")
        value["policy"] = "native-vram-v1"
        _PROFILE_CACHE = (time.monotonic(), value)
        return copy.deepcopy(value)


def checked_document(path, job_id):
    value = read_json(path)
    if (not isinstance(value, dict) or value.get("version") != VERSION
            or value.get("job_id") != job_id
            or value.get("checksum") != digest({k: v for k, v in value.items() if k != "checksum"})):
        return None
    return value


def save_document(path, job_id, **value):
    document = {"version": VERSION, "job_id": job_id, **value}
    atomic_json(path, {**document, "checksum": digest(document)})


def validate_output(output, duration_seconds, uttid=None):
    from classcue.firered_transcriber import _segments_from_tokens, _text, _tokens
    import re

    if not isinstance(output, dict) or not isinstance(output.get("text"), str):
        raise ValueError("Invalid FireRed output")
    if uttid is not None and output.get("uttid") != uttid:
        raise ValueError("FireRed output identity mismatch")
    text = _text(output)
    if text and not re.search(r"(<blank>)|(<sil>)", text):
        _segments_from_tokens(_tokens(output), text, duration_seconds=duration_seconds)
    return output


class FireRedCheckpoints:
    def __init__(self, directory, job_id, total_samples):
        self.directory, self.job_id = Path(directory), job_id
        self.total_samples = total_samples

    def load_vad(self):
        value = checked_document(self.directory / "vad.json", self.job_id)
        if value is None:
            return None
        try:
            previous = 0
            spans = value["result"]["timestamps"]
            if not isinstance(spans, list):
                return None
            for start, end in spans:
                if (not math.isfinite(start) or not math.isfinite(end)
                        or not previous <= start < end <= self.total_samples / 16000 + .001):
                    return None
                previous = end
            return value["result"]
        except (ValueError, TypeError, KeyError):
            return None

    def save_vad(self, result):
        save_document(self.directory / "vad.json", self.job_id, result=result)
        if self.load_vad() is None:
            raise ValueError("Invalid native FireRed VAD boundaries")

    def chunk(self, index, span):
        start, end = span
        return {"index": index, "start_sample": int(start * 16000), "end_sample": int(end * 16000),
                "uttid": f"video_s{int(start * 1000)}_e{int(end * 1000)}"}

    def load(self, chunk):
        value = checked_document(self.directory / "asr" / f"{digest(chunk)}.json", self.job_id)
        if value is None or value.get("chunk") != chunk or value.get("completed") is not True:
            return None
        try:
            return validate_output(value["output"],
                                   (chunk["end_sample"] - chunk["start_sample"]) / 16000,
                                   chunk["uttid"])
        except (ValueError, TypeError, KeyError):
            return None

    def save(self, chunk, output):
        validate_output(output, (chunk["end_sample"] - chunk["start_sample"]) / 16000, chunk["uttid"])
        save_document(self.directory / "asr" / f"{digest(chunk)}.json", self.job_id,
                      chunk=chunk, completed=True, output=output)

    def counts(self):
        vad = self.load_vad()
        if vad is None:
            return 0, 0
        chunks = [self.chunk(index, span) for index, span in enumerate(vad["timestamps"])]
        return sum(self.load(chunk) is not None for chunk in chunks), len(chunks)

    def result(self):
        value = checked_document(self.directory / "result.json", self.job_id)
        if value is None:
            return None
        try:
            completed, total = self.counts()
            if self.load_vad() is None or completed != total:
                return None
            duration = round(self.total_samples * 1000 / 16000)
            if value["duration_ms"] != duration:
                return None
            segments = [TranscriptSegment(**item) for item in value["segments"]]
            validate_segments(segments, duration)
            return segments, duration
        except (TypeError, ValueError, KeyError):
            return None


def _prepare_job(input_path, cache_root, paths, hardware, progress, should_stop):
    from classcue.firered_runtime import RUNTIME_VERSION
    from classcue.qwen_transcriber import stream_decode_audio_to_wav

    started = time.perf_counter()
    identity = {"version": VERSION, "source_sha256": file_digest(input_path, should_stop),
                "models": model_identity(paths), "runtime": RUNTIME_VERSION,
                "decode": "stream-pcm16-16khz-mono-pts-v1", "vad": "official-defaults",
                "aed": {"beam": 3, "nbest": 1, "timestamp": "strict", "lid": False, "punc": False,
                        "dtype": hardware.get("dtype", "native-half")}}
    job_id = digest(identity)
    directory = Path(cache_root) / job_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory, identity, job_id, started, stream_decode_audio_to_wav


def run_attempt(directory, manifest, batch_size, attempt_id, progress=None, should_stop=None,
                on_resource=None):
    from classcue.firered_runtime import runtime_python
    from classcue.qwen_process import run_process

    request = {"manifest": manifest, "batch_size": batch_size, "attempt_id": attempt_id}
    from classcue.transcript_events import receive_transcript, transcript_requested
    if transcript_requested():
        request["stream_transcript"] = True
    request_path = Path(directory) / "request.json"
    atomic_json(request_path, request)
    started = time.perf_counter()
    latest_fraction = .05
    with (Path(directory) / "inference.log").open("a", encoding="utf-8") as log:
        def line_received(line):
            nonlocal latest_fraction
            log.write(line)
            log.flush()
            event = read_event(line)
            if not event:
                return
            if event.get("event") == "progress":
                latest_fraction = float(event["fraction"])
                if progress:
                    progress(latest_fraction, event["message"])
            elif event.get("event") == "resource" and on_resource:
                on_resource(event["resource"])
            elif event.get("event") == "resource_warning" and progress:
                progress(latest_fraction, event["message"])
            elif event.get("event") == "transcript":
                receive_transcript(event)

        env = {**os.environ, "PYTHONUTF8": "1", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
        try:
            code = run_process(
                [str(runtime_python()), "-u", str(ROOT / "scripts/firered_worker.py"),
                 "--request", str(request_path), "--output", str(directory)],
                env=env, cwd=ROOT, should_stop=should_stop, on_line=line_received,
            )
        except ProcessingCancelled as error:
            error.firered_attempt = {
                "attempt_id": attempt_id, "batch_size": batch_size,
                "wall_seconds": time.perf_counter() - started, "status": "interrupted",
                "metrics": read_json(Path(directory) / f"metrics-{attempt_id}.json", {}),
            }
            raise
    record = {"attempt_id": attempt_id, "batch_size": batch_size,
              "wall_seconds": time.perf_counter() - started, "returncode": code,
              "metrics": read_json(Path(directory) / f"metrics-{attempt_id}.json", {})}
    if code:
        record["failure"] = read_json(Path(directory) / f"failure-{attempt_id}.json") or {
            "kind": "native_exit", "phase": "unknown", "message": f"Worker exit {code}",
        }
    return record


def run_prepared(directory, manifest, hardware, *, progress=None, should_stop=None,
                 on_resource=None, attempt_runner=run_attempt):
    directory = Path(directory)
    checkpoints = FireRedCheckpoints(directory, manifest["job_id"], manifest["total_samples"])
    previous = checked_document(directory / "state.json", manifest["job_id"]) or {}
    records = previous.get("attempts", [])
    recoveries = previous.get("recoveries", [])
    queue = _QUEUE.get()
    initial = hardware["initial_batch_size"]
    batch = min(initial, queue.batch_size or initial) if queue else initial
    state = {"hardware": hardware, "initial_batch_size": initial, "current_batch_size": batch,
             "attempts": records, "recoveries": recoveries, "cache_hit": False}
    retries = set()
    started = time.perf_counter()
    reporter = on_resource or _RESOURCE.get()
    latest_progress = 0

    def report_progress(fraction, message):
        nonlocal latest_progress
        latest_progress = max(latest_progress, min(1, max(0, fraction)))
        if progress:
            progress(latest_progress, message)

    def publish(status, updates=None, *, refresh_counts=True):
        if updates:
            state.update(updates)
        completed, total = (checkpoints.counts() if refresh_counts else
                            (state.get("completed_segments", 0), state.get("total_segments", 0)))
        state.update(status=status, current_batch_size=batch,
                     completed_segments=completed, total_segments=total,
                     elapsed_seconds=time.perf_counter() - started)
        state["total_attempt_seconds"] = sum(record.get("wall_seconds", 0) for record in records)
        save_document(directory / "state.json", manifest["job_id"], **state)
        if reporter:
            reporter({"engine": "firered", "device": hardware.get("device", "cuda:0"),
                      "gpu_total_mib": hardware.get("gpu_total_mib"),
                      "installed_ram_mib": hardware.get("installed_ram_mib"),
                      "initial_batch_size": initial, "current_batch_size": batch,
                      "completed_segments": completed, "total_segments": total,
                      "recovery_count": len(recoveries), "recovery_reason": state.get("recovery_reason"),
                      "status": status, "cache_hit": state["cache_hit"]})

    def worker_resource(value):
        nonlocal batch
        batch = min(batch, value.get("current_batch_size", batch))
        publish("running", value, refresh_counts=False)

    try:
        if should_stop and should_stop():
            raise ProcessingCancelled("用户已请求停止处理")
        cached = checkpoints.result()
        if cached is not None:
            state["cache_hit"] = True
            publish("completed")
            if progress:
                report_progress(1, "FireRed 字幕缓存已验证；无需加载模型")
            return cached
        if progress:
            report_progress(.04, f"FireRed 原生显存选档 BS{initial}，当前 BS{batch}；仅真实资源错误后降档")
        while True:
            if should_stop and should_stop():
                raise ProcessingCancelled("用户已请求停止处理")
            publish("running")
            attempt_id = f"{len(records):04d}-{time.time_ns()}"
            record = attempt_runner(directory, manifest, batch, attempt_id, report_progress,
                                    should_stop, worker_resource)
            records.append(record)
            failure = record.get("failure")
            if not failure:
                result = checkpoints.result()
                if result is None:
                    raise FireRedRunError("Worker exited without complete validated FireRed checkpoints")
                publish("completed")
                return result
            kind, phase = failure.get("kind"), failure.get("phase")
            if kind not in ("cuda_oom", "system_memory"):
                raise FireRedRunError(f"{kind}: {failure.get('message', 'FireRed failed')}")
            retry = False
            if kind == "cuda_oom":
                if phase in ("load", "vad") and phase not in retries:
                    retries.add(phase)
                    retry = True
                elif phase in ("inference", "alignment") and batch > 1:
                    batch = next(value for value in BATCH_LADDER if value < batch)
                    retry = True
            recoveries.append({"failure": failure, "retry": retry, "batch_size": batch})
            state["recovery_reason"] = f"{phase}: {failure.get('message', kind)}"
            if queue:
                queue.batch_size = min(queue.batch_size or batch, batch)
            publish("recovering" if retry else "resource_paused")
            if not retry:
                raise ResourcePaused(
                    f"FireRed 资源不足，已保存进度并暂停（{kind}/{phase}）。"
                    "释放资源后可继续；未自动切换 CPU 或模型。",
                    cache_directory=directory, failure=failure,
                )
            if progress:
                completed, total = checkpoints.counts()
                report_progress(.15 + .8 * completed / max(1, total),
                         f"FireRed 资源恢复：BS{batch}，已保存 {completed}/{total}；{phase}")
    except ResourcePaused:
        publish("resource_paused")
        raise
    except ProcessingCancelled as error:
        if getattr(error, "firered_attempt", None):
            records.append(error.firered_attempt)
        publish("interrupted")
        raise
    except BaseException:
        publish("failed")
        raise


def run_firered(input_path, cache_root, paths, *, progress=None, should_stop=None,
                on_resource=None, attempt_runner=None, hardware=None):
    from filelock import FileLock

    queue = _QUEUE.get()
    if hardware is None:
        hardware = (queue.hardware if queue and queue.hardware is not None else
                    resource_profile(refresh=True, should_stop=should_stop))
    if queue:
        queue.hardware = hardware
    if hardware.get("cuda_available") is not True:
        if hardware.get("failure_kind") == "system_memory":
            raise ResourcePaused("FireRed CUDA 检测时内存分配失败，请释放资源后继续",
                                 cache_directory=cache_root,
                                 failure={"kind": "system_memory", "phase": "probe"})
        raise FireRedRunError(hardware.get("warning") or "FireRed 需要可用 CUDA；不会自动切换 CPU")
    Path(cache_root).mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    with FileLock(str(Path(cache_root) / "prepare.lock"), timeout=0):
        directory, identity, job_id, _, decoder = _prepare_job(
            Path(input_path), cache_root, paths, hardware, progress, should_stop)
    with FileLock(str(directory / "run.lock"), timeout=0):
        manifest = checked_document(directory / "manifest.json", job_id)
        checkpoints = FireRedCheckpoints(directory, job_id, (manifest or {}).get("total_samples", 0))
        timeline = directory / "timeline.wav"
        try:
            if checkpoints.result() is None:
                audio_valid = (manifest is not None and timeline.is_file()
                               and file_digest(timeline, should_stop) == manifest.get("audio_sha256"))
                if not audio_valid:
                    partial = directory / "timeline.partial.wav"
                    def report_decode(fraction, message):
                        if should_stop and should_stop():
                            raise ProcessingCancelled("用户已请求停止处理")
                        if progress:
                            progress(fraction, message)
                    try:
                        decoder(input_path, partial, report_decode, should_stop=should_stop)
                        with wave.open(str(partial), "rb") as source:
                            if (source.getframerate(), source.getnchannels(), source.getsampwidth()) != (16000, 1, 2):
                                raise ValueError("Expected 16kHz mono PCM16 WAV")
                            total = source.getnframes()
                        if not total:
                            raise ValueError("Empty audio")
                        audio_hash = file_digest(partial, should_stop)
                        os.replace(partial, timeline)
                        if manifest and manifest.get("audio_sha256") != audio_hash:
                            raise FireRedRunError("Decoded audio changed; cached timestamps cannot be reused")
                        save_document(directory / "manifest.json", job_id,
                                      identity=identity, timeline=str(timeline), paths=paths,
                                      total_samples=total, rate=16000, audio_sha256=audio_hash,
                                      preparation_seconds=time.perf_counter() - started)
                        manifest = checked_document(directory / "manifest.json", job_id)
                    finally:
                        partial.unlink(missing_ok=True)
            result = run_prepared(
                directory, manifest, hardware, progress=progress, should_stop=should_stop,
                on_resource=on_resource, attempt_runner=attempt_runner or run_attempt,
            )
            try:
                timeline.unlink(missing_ok=True)
            except OSError as error:
                logging.getLogger(__name__).warning("FireRed WAV cleanup deferred: %s", error)
            return result
        except (MemoryError, OSError) as error:
            if classify_error(error) == "system_memory":
                raise ResourcePaused("FireRed 音频准备时内存分配失败，任务已暂停",
                                     cache_directory=directory,
                                     failure={"kind": "system_memory", "phase": "preparation"}) from error
            raise
