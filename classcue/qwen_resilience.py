"""Shared staged Qwen orchestration, durable checkpoints and bounded recovery."""

from classcue.paths import PROJECT_ROOT

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
from tempfile import NamedTemporaryFile
import time
import unicodedata
import wave

from classcue.core import TranscriptSegment
from classcue.processing_control import ProcessingCancelled, ResourcePaused

ROOT = PROJECT_ROOT
VERSION = 1
_QUEUE = ContextVar("qwen_queue", default=None)


@dataclass
class QwenQueue:
    hardware: dict | None = None
    asr_batch_size: int | None = None
    align_batch_size: int | None = None
    chunk_seconds: int | None = None
    low_memory: bool = False

    def retain(self, stage, batch_size, chunk_seconds, load_profile):
        field = "asr_batch_size" if stage == "asr" else "align_batch_size"
        previous = getattr(self, field)
        setattr(self, field, min(previous or batch_size, batch_size))
        if stage == "asr":
            self.chunk_seconds = min(self.chunk_seconds or chunk_seconds, chunk_seconds)
        self.low_memory |= load_profile == "low-memory"


@contextmanager
def qwen_queue():
    """Queue-local recovery limits; a new or resumed queue starts fresh."""
    queue = QwenQueue()
    token = _QUEUE.set(queue)
    try:
        yield queue
    finally:
        _QUEUE.reset(token)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                    separators=(",", ":")).encode()).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            json.dump(value, output, ensure_ascii=False, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def file_digest(path, should_stop=None):
    result = hashlib.sha256()
    with Path(path).open("rb") as source:
        while block := source.read(2**20):
            if should_stop and should_stop():
                raise ProcessingCancelled("用户已请求停止处理")
            result.update(block)
    return result.hexdigest()


@dataclass(frozen=True)
class QwenOptions:
    mode: str = "auto"
    batch_size: int = 2
    chunk_seconds: int = 90
    align_batch_size: int = 2
    load_profile: str = "standard"
    bs2_free_mib: int = 6656  # Legacy request/cache field; not used for hardware routing.
    auto_recovery: bool = True
    cpu_threads: int = 4
    audio_profile: str = "general"
    skip_non_speech: bool | None = None
    speech_padding_seconds: float = .3
    min_removed_seconds: float = 1.5

    def __post_init__(self):
        if self.mode not in ("auto", "low-resource", "manual"):
            raise ValueError("Unknown Qwen resource mode")
        if any(not isinstance(value, int) or isinstance(value, bool) for value in (
            self.batch_size, self.align_batch_size, self.chunk_seconds, self.cpu_threads, self.bs2_free_mib,
        )):
            raise ValueError("Qwen resource sizes must be integers")
        if (not 1 <= self.batch_size <= 8 or not 1 <= self.align_batch_size <= 8
                or not 30 <= self.chunk_seconds <= 1200 or self.cpu_threads < 1
                or self.bs2_free_mib < 0 or self.load_profile not in ("standard", "low-memory")):
            raise ValueError("Invalid Qwen resource settings")
        from classcue.qwen_silence import resolve_silence_skipping
        resolve_silence_skipping(self.audio_profile, skip_non_speech=self.skip_non_speech)
        if (not math.isfinite(self.speech_padding_seconds) or self.speech_padding_seconds < 0
                or not math.isfinite(self.min_removed_seconds) or self.min_removed_seconds <= 0):
            raise ValueError("Invalid silence settings")

    def configured(self, hardware=None):
        from classcue.qwen_silence import resolve_silence_skipping
        base = replace(self, skip_non_speech=resolve_silence_skipping(
            self.audio_profile, skip_non_speech=self.skip_non_speech,
        ))
        if self.mode == "low-resource":
            return replace(base, batch_size=1, align_batch_size=1, chunk_seconds=30,
                           load_profile="low-memory")
        if self.mode == "auto":
            hardware = native_hardware() if hardware is None else hardware
            gpu, ram = hardware.get("gpu_total_mib"), hardware.get("installed_ram_mib")
            if capacity_at_least(gpu, 8, allowance_mib=64) and capacity_at_least(ram, 16):
                batch, seconds, profile = 2, 90, "standard"
            elif capacity_at_least(gpu, 6, allowance_mib=64) and capacity_at_least(ram, 12):
                batch, seconds, profile = 2, 60, "standard"
            else:
                batch, seconds, profile = 1, 30, "low-memory"
            return replace(base, batch_size=batch, align_batch_size=batch, chunk_seconds=seconds,
                           load_profile=profile, auto_recovery=True)
        return base


def capacity_at_least(value, gib, allowance_mib=0):
    return (isinstance(value, (int, float)) and math.isfinite(value)
            and value >= gib * 1024 - allowance_mib)


def _gpu_memory_mib(field):
    try:
        result = subprocess.run(
            ["nvidia-smi", f"--query-gpu=memory.{field}", "--format=csv,noheader,nounits", "--id=0"],
            capture_output=True, text=True, timeout=3,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        value = float(result.stdout.strip().splitlines()[0]) if result.returncode == 0 else None
        return value if value is not None and math.isfinite(value) and value >= 0 else None
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return None


def gpu_free_mib():
    return _gpu_memory_mib("free")


def installed_ram_mib():
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            query = kernel.GetPhysicallyInstalledSystemMemory
            query.argtypes = [ctypes.POINTER(ctypes.c_ulonglong)]
            query.restype = wintypes.BOOL
            kib = ctypes.c_ulonglong()
            return kib.value / 1024 if query(ctypes.byref(kib)) and kib.value else None
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 2**20
    except (OSError, ValueError, AttributeError):
        return None


def native_hardware():
    return {"gpu_total_mib": _gpu_memory_mib("total"), "installed_ram_mib": installed_ram_mib()}


class QwenRunError(RuntimeError):
    pass


def classify_error(error, torch=None):
    if torch is not None and isinstance(error, torch.cuda.OutOfMemoryError):
        return "cuda_oom"
    if isinstance(error, MemoryError) or (isinstance(error, OSError)
                                         and getattr(error, "winerror", None) in (8, 14, 1455)):
        return "system_memory"
    # CPU allocator uses RuntimeError rather than Python's MemoryError.
    if isinstance(error, RuntimeError) and any(
        marker in str(error) for marker in ("DefaultCPUAllocator: can't allocate memory",
                                           "DefaultCPUAllocator: not enough memory")
    ):
        return "system_memory"
    if isinstance(error, (ImportError, FileNotFoundError)):
        return "environment"
    return "inference"


def chunk_key(chunk):
    return digest(chunk)


class Checkpoints:
    def __init__(self, directory, job_id):
        self.directory, self.job_id = Path(directory), job_id

    def load(self, stage, chunk, asr=None):
        value = read_json(self.directory / stage / f"{chunk_key(chunk)}.json")
        if not isinstance(value, dict):
            return None
        if (value.get("version") != VERSION or value.get("job_id") != self.job_id
                or value.get("chunk") != chunk or value.get("asr_digest") != digest(asr)
                or value.get("checksum") != digest(value.get("value"))):
            return None
        result = value.get("value")
        if stage == "asr":
            return result if isinstance(result, str) else None
        if not isinstance(result, list):
            return None
        try:
            segments = [TranscriptSegment(**item) for item in result]
            validate_segments(segments, chunk["end_sample"] * 1000 / 16000,
                              lower_ms=chunk["start_sample"] * 1000 / 16000)
            if not isinstance(asr, str) or normalized_text(asr) != normalized_text(
                "".join(segment.text for segment in segments)
            ):
                return None
        except (ValueError, TypeError, KeyError):
            return None
        return result

    def save(self, stage, chunk, value, asr=None):
        atomic_json(self.directory / stage / f"{chunk_key(chunk)}.json", {
            "version": VERSION, "job_id": self.job_id, "chunk": chunk,
            "asr_digest": digest(asr), "value": value, "checksum": digest(value),
        })


def validate_segments(segments, duration_ms, lower_ms=0):
    previous = lower_ms - 1
    for segment in segments:
        if (not isinstance(segment.text, str) or not math.isfinite(segment.start_ms)
                or not math.isfinite(segment.end_ms) or segment.start_ms < lower_ms - 1
                or segment.end_ms < segment.start_ms or segment.end_ms > duration_ms + 1
                or segment.start_ms < previous):
            raise ValueError("Invalid subtitle timestamps")
        previous = segment.start_ms
        word_previous = lower_ms / 1000 - .001
        for word in segment.words or []:
            start, end = float(word["start"]), float(word["end"])
            if (not math.isfinite(start) or not math.isfinite(end) or start < word_previous
                    or end < start or start < segment.start_ms / 1000 - .002
                    or end > segment.end_ms / 1000 + .002):
                raise ValueError("Invalid word timestamps")
            word_previous = start


def normalized_text(text):
    return "".join(character for character in text
                   if character == "'" or unicodedata.category(character)[0] in {"L", "N"})


def model_identity(paths):
    return {
        name: [[str(file.relative_to(Path(path))), file.stat().st_size, file.stat().st_mtime_ns]
               for file in sorted(Path(path).rglob("*"))
               if file.is_file() and ".cache" not in file.parts]
        for name, path in paths.items()
    }


def make_chunks(audio, rate, options, intervals, offset=0):
    from classcue.qwen_boundaries import plan_spans
    from classcue.qwen_silence import build_silence_plan, resolve_silence_skipping

    spans = plan_spans(audio, rate, options.chunk_seconds,
                       "firered" if intervals is not None else "energy", intervals)
    plan = build_silence_plan(
        spans, len(audio), rate, intervals,
        enabled=resolve_silence_skipping(options.audio_profile, skip_non_speech=options.skip_non_speech),
        padding_seconds=options.speech_padding_seconds, min_removed_seconds=options.min_removed_seconds,
    )
    for chunk in plan["chunks"]:
        chunk["start_sample"] += offset
        chunk["end_sample"] += offset
        for part in chunk["slices"]:
            part["source_start_sample"] += offset
            part["source_end_sample"] += offset
        # A stable coordinate ID survives retries and re-batching.
        chunk["source_chunk_index"] = f'{chunk["start_sample"]}:{chunk["end_sample"]}'
    return plan


def read_audio(source, chunk):
    import numpy as np

    result = np.empty(chunk["input_samples"], dtype=np.float32)
    for part in chunk["slices"]:
        source.setpos(part["source_start_sample"])
        count = part["source_end_sample"] - part["source_start_sample"]
        raw = source.readframes(count)
        if len(raw) != count * 2:
            raise ValueError("Truncated source audio")
        target = result[part["compact_start_sample"]:part["compact_end_sample"]]
        target[:] = np.frombuffer(raw, dtype="<i2")
        target /= 32768
    return result


def valid_chunks(chunks, total):
    try:
        cursor = 0
        for chunk in chunks:
            if chunk["start_sample"] != cursor or chunk["end_sample"] <= cursor:
                return False
            cursor = chunk["end_sample"]
            compact = 0
            source_cursor = chunk["start_sample"]
            for part in chunk["slices"]:
                if (part["compact_start_sample"] != compact
                        or not source_cursor <= part["source_start_sample"]
                        < part["source_end_sample"] <= chunk["end_sample"]):
                    return False
                source_cursor = part["source_end_sample"]
                compact += part["source_end_sample"] - part["source_start_sample"]
                if part["compact_end_sample"] != compact:
                    return False
            if compact != chunk["input_samples"]:
                return False
        return cursor == total
    except (TypeError, KeyError):
        return False


def prepare_job(input_path, cache_root, paths, options, progress=None, should_stop=None):
    started = time.perf_counter()
    source_hash = file_digest(input_path, should_stop)
    identity = {"version": VERSION, "input_sha256": source_hash, "model": model_identity(paths),
                "options": asdict(options), "dtype": "float16", "language": "Chinese",
                "attention": "sdpa", "max_new_tokens": 1024}
    job_id = digest(identity)
    directory = Path(cache_root) / job_id
    directory.mkdir(parents=True, exist_ok=True)
    from filelock import FileLock
    with FileLock(str(directory / "run.lock"), timeout=0):
        return _prepare_audio(directory, identity, job_id, input_path, options, paths,
                              progress, should_stop, started)


def _prepare_audio(directory, identity, job_id, input_path, options, paths,
                   progress, should_stop, started):
    from classcue.media import extract_audio
    from classcue.qwen_vad import detect_speech
    from classcue.qwen_silence import resolve_silence_skipping
    import numpy as np

    timeline = directory / "timeline.wav"
    manifest = read_json(directory / "manifest.json")
    if (isinstance(manifest, dict) and manifest.get("identity") == identity
            and manifest.get("job_id") == job_id
            and valid_chunks(manifest.get("chunks", []), manifest.get("total_samples", -1))
            and manifest.get("checksum") == digest({k: v for k, v in manifest.items() if k != "checksum"})
            and timeline.is_file() and manifest.get("audio_sha256") == file_digest(timeline, should_stop)):
        return directory, manifest
    if progress:
        progress(.01, "提取音轨并准备 FireRed VAD")
    try:
        extract_audio(input_path, timeline, should_stop=should_stop)
    except Exception:
        timeline.unlink(missing_ok=True)
        raise
    if should_stop and should_stop():
        raise ProcessingCancelled("用户已请求停止处理")
    with wave.open(str(timeline), "rb") as source:
        if (source.getframerate(), source.getnchannels(), source.getsampwidth()) != (16000, 1, 2):
            raise QwenRunError("Expected 16kHz mono PCM16")
        rate, total = source.getframerate(), source.getnframes()
        audio = np.frombuffer(source.readframes(total), dtype="<i2").astype("float32")
        audio /= 32768
    if not total:
        raise QwenRunError("音轨为空")
    vad_started = time.perf_counter()
    try:
        vad = detect_speech(timeline, "firered", should_stop=should_stop)
    except (RuntimeError, FileNotFoundError) as error:
        if resolve_silence_skipping(options.audio_profile, skip_non_speech=options.skip_non_speech):
            raise QwenRunError("静音剔除需要可用的 FireRed VAD") from error
        vad = {"speech_intervals": None, "warning": str(error), "backend": "energy-fallback"}
        if progress:
            progress(.04, "FireRed VAD 不可用，边界暂使用低能量回退；未删除音频")
    silence = make_chunks(audio, rate, options, vad["speech_intervals"])
    manifest = {
        "identity": identity, "job_id": job_id, "audio_sha256": file_digest(timeline, should_stop),
        "timeline": str(timeline), "rate": rate, "total_samples": total,
        "vad": vad, "silence_plan": silence, "chunks": silence["chunks"],
        "paths": {name: str(path) for name, path in paths.items()},
        "preparation_seconds": time.perf_counter() - started,
        "vad_seconds": time.perf_counter() - vad_started,
    }
    manifest["checksum"] = digest(manifest)
    atomic_json(directory / "manifest.json", manifest)
    return directory, manifest


def split_pending(manifest, chunks, checkpoints, seconds):
    import numpy as np

    options = replace(QwenOptions(**manifest["identity"]["options"]), chunk_seconds=seconds)
    intervals = manifest["vad"]["speech_intervals"]
    replacement = []
    with wave.open(manifest["timeline"], "rb") as source:
        rate = manifest["rate"]
        for chunk in chunks:
            if checkpoints.load("asr", chunk) is not None or chunk["end_sample"] - chunk["start_sample"] <= seconds * rate:
                replacement.append(chunk)
                continue
            left, right = chunk["start_sample"], chunk["end_sample"]
            source.setpos(left)
            audio = np.frombuffer(source.readframes(right - left), dtype="<i2").astype("float32")
            audio /= 32768
            cropped = None if intervals is None else [
                [max(a, left / rate) - left / rate, min(b, right / rate) - left / rate]
                for a, b in intervals if b > left / rate and a < right / rate
            ]
            replacement.extend(make_chunks(audio, rate, options, cropped, offset=left)["chunks"])
    if not valid_chunks(replacement, manifest["total_samples"]):
        raise QwenRunError("Recovery boundaries have incomplete coverage")
    return replacement


def run_attempt(directory, manifest, chunks, stage, batch_size, load_profile,
                attempt_id, progress=None, should_stop=None):
    from classcue.qwen_process import run_process
    from classcue.qwen_runtime import runtime_python
    from classcue.transcript_events import emit_transcript_preview, transcript_preview_requested

    request = {
        "manifest": manifest, "chunks": chunks, "stage": stage, "batch_size": batch_size,
        "load_profile": load_profile, "attempt_id": attempt_id,
        "stream_preview": transcript_preview_requested(),
    }
    request_path = Path(directory) / "request.json"
    atomic_json(request_path, request)
    failure_path = Path(directory) / f"failure-{attempt_id}.json"
    env = os.environ.copy()
    env.update(PYTHONUTF8="1", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    started = time.perf_counter()
    started_at = time.time()
    with (Path(directory) / "inference.log").open("a", encoding="utf-8") as log:
        latest_fraction = [.06 if stage == "asr" else .8]
        def line_received(line):
            log.write(line)
            log.flush()
            event = read_event(line)
            if event and event.get("event") == "transcript_preview":
                # Display-only chunk bounds: not a settled watermark for early LLM.
                emit_transcript_preview(TranscriptSegment(**item) for item in event["segments"])
            elif event and progress and event.get("event") == "progress":
                latest_fraction[0] = float(event["fraction"])
                progress(float(event["fraction"]), str(event["message"]))
            elif event and progress and event.get("event") == "resource_warning":
                progress(latest_fraction[0], str(event["message"]))

        code = run_process(
            [str(runtime_python()), "-u", str(ROOT / "scripts" / "qwen_resilient_worker.py"),
             "--request", str(request_path), "--output", str(directory)],
            env=env, cwd=ROOT, should_stop=should_stop, on_line=line_received,
        )
    record = {"stage": stage, "batch_size": batch_size, "load_profile": load_profile,
              "wall_seconds": time.perf_counter() - started, "returncode": code,
              "attempt_id": attempt_id, "started_at": started_at, "exited_at": time.time()}
    if code:
        failure = read_json(failure_path)
        record["failure"] = failure if isinstance(failure, dict) else {
            "kind": "native_exit", "phase": "unknown", "message": f"Worker exit {code}",
        }
    return record


def read_event(line):
    try:
        value = json.loads(line)
        return value if isinstance(value, dict) else None
    except ValueError:
        return None


def run_qwen(input_path, cache_root, paths, *, options=None, progress=None,
             should_stop=None, attempt_runner=None, report_path=None):
    from filelock import FileLock

    options = options or QwenOptions()
    queue = _QUEUE.get()
    hardware = None
    if options.mode == "auto":
        hardware = queue.hardware if queue and queue.hardware is not None else native_hardware()
        if queue:
            queue.hardware = hardware
    options = options.configured(hardware)
    if progress and hardware is not None:
        gpu = hardware.get("gpu_total_mib")
        if gpu is not None and not capacity_at_least(gpu, 6, allowance_mib=64):
            progress(0, "Qwen 要求 GPU 原生显存至少 6GB；低于 6GB 请勿使用此模型。")
        progress(0, f"Qwen 原生硬件选档：BS{options.batch_size}，"
                    f"目标分块 {options.chunk_seconds} 秒；仅真实分配失败后降级")
    started = time.perf_counter()
    Path(cache_root).mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(str(Path(cache_root) / "prepare.lock"), timeout=0):
            directory, manifest = prepare_job(Path(input_path), cache_root, paths, options, progress, should_stop)
    except (MemoryError, OSError) as error:
        if classify_error(error) != "system_memory":
            raise
        raise ResourcePaused(
            "音频预处理时系统内存分配失败，任务已暂停。请释放资源后继续。",
            cache_directory=cache_root, failure={"kind": "system_memory", "phase": "preparation"},
        ) from error
    # Lock before reading mutable state; two callers cannot share live checkpoints.
    with FileLock(str(directory / "run.lock"), timeout=0):
        try:
            return run_prepared(directory, manifest, options, progress, should_stop,
                                attempt_runner or run_attempt, hardware=hardware)
        finally:
            if report_path:
                state = read_json(directory / "state.json", {})
                atomic_json(report_path, {
                    **state, "cache_directory": str(directory),
                    "wall_seconds": time.perf_counter() - started,
                    "audio_seconds": manifest["total_samples"] / manifest["rate"],
                    "preparation_seconds": manifest["preparation_seconds"],
                    "stages_never_overlap": True,
                    "metrics": [read_json(directory / f'metrics-{item["attempt_id"]}.json', {})
                                for item in state.get("attempts", [])],
                })


def run_prepared(directory, manifest, options, progress=None, should_stop=None,
                 attempt_runner=run_attempt, *, hardware=None):
    directory = Path(directory)
    checkpoints = Checkpoints(directory, manifest["job_id"])
    previous = read_json(directory / "state.json", {})
    if (not isinstance(previous, dict) or previous.get("job_id") != manifest["job_id"]
            or previous.get("version") != VERSION):
        previous = {}
    chunks = previous.get("chunks", manifest["chunks"]) if isinstance(previous, dict) else manifest["chunks"]
    if not valid_chunks(chunks, manifest["total_samples"]):
        chunks = manifest["chunks"]
    records = previous.get("attempts", []) if isinstance(previous, dict) else []
    records = records if isinstance(records, list) else []
    queue = _QUEUE.get()
    batch = min(options.batch_size, queue.asr_batch_size or options.batch_size) if queue else options.batch_size
    profile = "low-memory" if queue and queue.low_memory else options.load_profile
    recovery_events = previous.get("recoveries", [])
    recovery_events = recovery_events if isinstance(recovery_events, list) else []
    load_retries = set()
    align_retried = False
    state = {"version": VERSION, "job_id": manifest["job_id"], "chunks": chunks,
             "attempts": records, "status": "running", "native_hardware": hardware,
             "initial_configuration": {"batch_size": options.batch_size,
                                       "align_batch_size": options.align_batch_size,
                                       "chunk_seconds": options.chunk_seconds,
                                       "load_profile": options.load_profile}}
    if "recovery_chunk_seconds" in previous:
        state["recovery_chunk_seconds"] = previous["recovery_chunk_seconds"]
    started = time.perf_counter()

    def persist(status):
        state.update(status=status, chunks=chunks, current_batch_size=batch, load_profile=profile,
                     recoveries=recovery_events, elapsed_seconds=time.perf_counter() - started)
        atomic_json(directory / "state.json", state)

    try:
        if queue and queue.chunk_seconds and queue.chunk_seconds < options.chunk_seconds:
            target = min(queue.chunk_seconds, state.get("recovery_chunk_seconds", options.chunk_seconds))
            chunks = split_pending(manifest, chunks, checkpoints, target)
            state["recovery_chunk_seconds"] = target
        from classcue.qwen_preview import publish_cached_preview
        publish_cached_preview(checkpoints, chunks, manifest["rate"])
        for stage in ("asr", "align"):
            if stage == "align":
                batch = (min(options.align_batch_size, queue.align_batch_size or options.align_batch_size)
                         if queue else options.align_batch_size)
            while True:
                if should_stop and should_stop():
                    raise ProcessingCancelled("用户已请求停止处理")
                pending = [chunk for chunk in chunks if (
                    checkpoints.load("asr", chunk) is None if stage == "asr"
                    else checkpoints.load("align", chunk, checkpoints.load("asr", chunk)) is None
                )]
                if not pending:
                    break
                persist("running")
                attempt_id = f"{stage}-{len(records):04d}-{time.time_ns()}"
                record = attempt_runner(directory, manifest, chunks, stage, batch, profile,
                                        attempt_id, progress, should_stop)
                records.append(record)
                failure = record.get("failure")
                if not failure:
                    # A successful exit without all durable results is not success.
                    for chunk in chunks:
                        asr = checkpoints.load("asr", chunk)
                        result = asr if stage == "asr" else checkpoints.load("align", chunk, asr)
                        if result is None:
                            raise QwenRunError("Worker exited without complete checkpoints")
                    break
                kind, phase = failure.get("kind"), failure.get("phase")
                retry = False
                if kind not in ("cuda_oom", "system_memory"):
                    raise QwenRunError(f'{kind}: {failure.get("message", "Qwen failed")}; 日志：{directory}')
                if options.auto_recovery:
                    if kind == "system_memory" and profile != "low-memory":
                        profile = "low-memory"
                        retry = True
                    elif kind == "cuda_oom" and phase == "load":
                        if stage not in load_retries:
                            load_retries.add(stage)
                            retry = True
                    elif kind == "cuda_oom" and stage == "asr":
                        current = state.get("recovery_chunk_seconds", options.chunk_seconds)
                        if batch > 1 or current > 90:
                            batch = 1
                            if current > 90:
                                chunks = split_pending(manifest, chunks, checkpoints, 90)
                                state["recovery_chunk_seconds"] = 90
                            retry = True
                        else:
                            shorter = next((value for value in (60, 30) if value < current), None)
                            if shorter is not None:
                                chunks = split_pending(manifest, chunks, checkpoints, shorter)
                                state["recovery_chunk_seconds"] = shorter
                                retry = True
                    elif kind == "cuda_oom" and stage == "align" and not align_retried:
                        batch, align_retried, retry = 1, True, True
                recovery_events.append({"stage": stage, "failure": failure, "retry": retry,
                                        "batch_size": batch, "load_profile": profile,
                                        "chunk_seconds": state.get("recovery_chunk_seconds", options.chunk_seconds)})
                if queue:
                    queue.retain(stage, batch, state.get("recovery_chunk_seconds", options.chunk_seconds), profile)
                persist("recovering" if retry else "resource_paused")
                if not retry:
                    raise ResourcePaused(
                        f"当前可用资源不足，任务已暂停并保存进度。请关闭占用资源的软件后继续，"
                        f"或选择较小模型；未自动切换 CPU。日志：{directory}",
                        cache_directory=directory, failure=failure,
                    )
                if progress:
                    progress(.08 if stage == "asr" else .8,
                             f"{stage} 资源降级：BS{batch}，目标分块 "
                             f'{state.get("recovery_chunk_seconds", options.chunk_seconds)} 秒')
        segments = []
        for chunk in chunks:
            text = checkpoints.load("asr", chunk)
            for item in checkpoints.load("align", chunk, text):
                segments.append(TranscriptSegment(**{**item, "id": len(segments) + 1}))
        duration = round(manifest["total_samples"] * 1000 / manifest["rate"])
        validate_segments(segments, duration)
        atomic_json(directory / "result.json", {
            "segments": [asdict(segment) for segment in segments], "duration_ms": duration,
            "job_id": manifest["job_id"], "timestamp_axis": "original_source",
        })
        persist("completed")
        if progress:
            progress(1, "Qwen 字幕已完成")
        return segments, duration
    except ResourcePaused:
        persist("resource_paused")
        raise
    except ProcessingCancelled:
        persist("interrupted")
        raise
    except BaseException:
        persist("failed")
        raise
