"""Official BiCIF Paraformer timestamps and bounded CPU/CUDA batches."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
import gc
import json
import math
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from types import SimpleNamespace
import wave

from core import TranscriptSegment
from qwen_transcriber import aligned_segments, stream_decode_audio_to_wav
from paraformer_routing import lower_batch_size
from processing_control import ResourcePaused


DEFAULT_CPU_THREADS = 16
VAD_MAX_SEGMENT_MS = 30000
VAD_END_SILENCE_MS = 800


@dataclass
class ParaformerModels:
    recognizer: object
    vad: object
    cpu_threads: int
    device: str = "cpu"
    vad_device: str = "cpu"


def cpu_environment(cpu_threads: int = DEFAULT_CPU_THREADS) -> dict[str, str]:
    return runtime_environment("cpu", cpu_threads)


def runtime_environment(device: str = "cpu", cpu_threads: int = DEFAULT_CPU_THREADS) -> dict[str, str]:
    if device not in ("cpu", "gpu"):
        raise ValueError("Paraformer device must be cpu or gpu")
    if isinstance(cpu_threads, bool) or not isinstance(cpu_threads, int) or cpu_threads < 1:
        raise ValueError("cpu_threads must be a positive integer")
    environment = {
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "MODELSCOPE_OFFLINE": "1",
        "PYTHONUTF8": "1",
        "OMP_NUM_THREADS": str(cpu_threads),
        "MKL_NUM_THREADS": str(cpu_threads),
        "OPENBLAS_NUM_THREADS": str(cpu_threads),
        "OMP_WAIT_POLICY": "PASSIVE",
    }
    if device == "cpu":
        environment["CUDA_VISIBLE_DEVICES"] = ""
    return environment


def _validate_component(directory: str | Path, kind: str) -> Path:
    import yaml

    path = Path(directory).resolve()
    required = ["model.pt", "config.yaml", "configuration.json", "am.mvn"]
    if kind == "recognizer":
        required += ["tokens.json", "seg_dict"]
    for name in required:
        if not (path / name).is_file() or not (path / name).stat().st_size:
            raise ValueError(f"Paraformer local {kind} is incomplete: {path / name}")
    config = yaml.safe_load((path / "config.yaml").read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError(f"Invalid Paraformer local {kind} configuration")
    if kind == "recognizer" and (
        config.get("model") != "BiCifParaformer" or config.get("predictor") != "CifPredictorV3"
    ):
        raise ValueError("Reliable timestamps require official BiCifParaformer/CifPredictorV3 weights")
    if kind == "vad" and config.get("model") != "FsmnVADStreaming":
        raise ValueError("Paraformer requires the official FSMN-VAD component")
    return path


def load_paraformer_model(
    recognizer_path: str | Path, vad_path: str | Path, *, cpu_threads: int = DEFAULT_CPU_THREADS,
    device: str = "cpu", vad_device: str = "cpu",
) -> ParaformerModels:
    if vad_device not in ("cpu", "gpu"):
        raise ValueError("Paraformer VAD device must be cpu or gpu")
    if device == "cpu" and vad_device != "cpu":
        raise ValueError("CPU Paraformer requires CPU VAD")
    os.environ.update(runtime_environment(device, cpu_threads))
    recognizer_path = _validate_component(recognizer_path, "recognizer")
    vad_path = _validate_component(vad_path, "vad")
    import torch
    from funasr import AutoModel
    from asr_checkpoint import reuse_funasr_checkpoint_tensors

    if device == "cpu" and torch.cuda.is_available():
        raise RuntimeError("CUDA must be hidden before importing torch")
    if device == "gpu" and not torch.cuda.is_available():
        raise RuntimeError("Paraformer GPU requires available CUDA; CPU fallback is disabled")
    torch.set_num_threads(cpu_threads)
    try:
        torch.set_num_interop_threads(1)
    except RuntimeError:
        pass
    options = {
        "device": "cuda:0" if device == "gpu" else "cpu",
        "ngpu": 1 if device == "gpu" else 0, "ncpu": cpu_threads, "hub": "ms",
        "disable_update": True, "check_latest": False, "disable_pbar": True,
        "trust_remote_code": False, "vad_model": None, "punc_model": None,
        "spk_model": None, "lm_model": None,
    }
    # CPU VAD avoids per-frame CUDA scalar reads without changing detection logic.
    vad_options = {
        **options, "device": "cuda:0" if vad_device == "gpu" else "cpu",
        "ngpu": 1 if vad_device == "gpu" else 0,
    }
    with reuse_funasr_checkpoint_tensors(AutoModel):
        recognizer = AutoModel(model=str(recognizer_path), **options)
        vad = AutoModel(
            model=str(vad_path), max_single_segment_time=VAD_MAX_SEGMENT_MS, **vad_options,
        )
    if vad.model.vad_opts.max_single_segment_time != VAD_MAX_SEGMENT_MS:
        raise RuntimeError("FSMN-VAD maximum segment length was not applied at construction")
    for model, component_device in ((recognizer, device), (vad, vad_device)):
        expected_device = "cuda" if component_device == "gpu" else "cpu"
        if any(parameter.device.type != expected_device for parameter in model.model.parameters()):
            raise RuntimeError(f"Paraformer parameters must stay on {expected_device}")
        model.model.eval()
    return ParaformerModels(recognizer, vad, cpu_threads, device, vad_device)


def synchronize_device(model: ParaformerModels) -> None:
    if model.device == "gpu":
        import torch

        torch.cuda.synchronize(0)


def is_cuda_oom(error: Exception) -> bool:
    import torch

    return isinstance(error, torch.cuda.OutOfMemoryError)


def release_cuda_memory() -> None:
    import torch

    gc.collect()
    torch.cuda.empty_cache()


def timestamp_segments(
    output: dict, offset_ms: int, duration_ms: int, first_id: int = 1,
) -> list[TranscriptSegment]:
    raw_text = output.get("text", "")
    if not isinstance(raw_text, str):
        raise ValueError("Invalid Paraformer output text")
    timestamps = output.get("timestamp", [])
    if not raw_text.strip():
        if timestamps:
            raise ValueError("Paraformer returned timestamps without text")
        return []
    # BiCIF sentence_postprocess returns space-separated timestamp units,
    # including merged English subwords, not arbitrary character durations.
    tokens = raw_text.split()
    if not isinstance(timestamps, list) or len(tokens) != len(timestamps):
        raise ValueError("Paraformer timestamp units do not cover the full text")
    words = []
    for token, stamp in zip(tokens, timestamps):
        if not isinstance(stamp, (list, tuple)) or len(stamp) != 2:
            raise ValueError("Invalid Paraformer timestamp pair")
        start, end = map(float, stamp)
        if not math.isfinite(start) or not math.isfinite(end):
            raise ValueError("Nonfinite Paraformer timestamp")
        words.append(SimpleNamespace(text=token, start_time=start / 1000, end_time=end / 1000))
    text = ""
    previous = ""
    for token in tokens:
        if previous and previous[-1].isascii() and token[0].isascii():
            text += " "
        text += token
        previous = token
    return aligned_segments(text, words, offset_ms / 1000, duration_ms / 1000, first_id)


def detect_speech(
    vad, wav_path: Path, progress=None, *, batch_size: int = 1,
) -> list[tuple[int, int]]:
    """Return complete official offline VAD intervals on the original timeline."""
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
        raise ValueError("batch_size must be a positive integer")
    with wave.open(str(wav_path), "rb") as source:
        if (source.getframerate() != 16000 or source.getnchannels() != 1
                or source.getsampwidth() != 2 or not source.getnframes()):
            raise ValueError("Offline FSMN-VAD requires nonempty mono 16 kHz PCM16 audio")
        duration_ms = round(source.getnframes() * 1000 / 16000)
    if progress:
        progress(.04, f"FSMN-VAD detecting full offline file; requested BS={batch_size}")
    outputs = vad.generate(
        input=str(wav_path), is_final=True, is_streaming_input=False,
        chunk_size=60000, batch_size=batch_size, fs=16000, dynamic_silence=False,
        max_end_silence_time=VAD_END_SILENCE_MS,
    )
    if len(outputs) != 1:
        raise ValueError("Unexpected FSMN-VAD output count")
    intervals = outputs[0].get("value", [])
    if not isinstance(intervals, list):
        raise ValueError("Invalid offline FSMN-VAD intervals")
    for pair in intervals:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError("Invalid FSMN-VAD interval")
        if any(isinstance(value, bool) or not isinstance(value, int) for value in pair):
            raise ValueError("Offline FSMN-VAD must return complete integer intervals")
    result = validate_intervals(intervals, duration_ms)
    if progress:
        progress(.20, f"FSMN-VAD offline: {len(result)} complete speech segments")
    return result


def validate_intervals(intervals, duration_ms: int) -> list[tuple[int, int]]:
    previous_end = 0
    result = []
    for start, end in intervals:
        if start < previous_end or end <= start or start < 0 or end > duration_ms + 30:
            raise ValueError("FSMN-VAD interval outside the original audio timeline")
        end = min(duration_ms, end)
        if end - start > 31000:
            raise ValueError("FSMN-VAD interval exceeds the bounded ASR segment duration")
        result.append((start, end))
        previous_end = end
    return result


def ordered_interval_indices(intervals, batch_order: str = "length") -> list[int]:
    if batch_order not in ("length", "time"):
        raise ValueError("batch_order must be length or time")
    indices = list(range(len(intervals)))
    if batch_order == "length":
        indices.sort(key=lambda index: intervals[index][1] - intervals[index][0])
    return indices


@contextmanager
def observe_encoder_batches(recognizer):
    observed = []
    encoder = getattr(getattr(recognizer, "model", None), "encoder", None)
    handle = None
    if encoder is not None:
        handle = encoder.register_forward_pre_hook(
            lambda _module, inputs: observed.append(int(inputs[0].shape[0]))
        )
    try:
        yield observed
    finally:
        if handle is not None:
            handle.remove()


def transcribe_paraformer(
    model: ParaformerModels, audio_path: str | Path, progress=None, *,
    batch_size: int = 4, temporary_directory: str | Path | None = None,
    audit: dict | None = None, batch_order: str = "length",
) -> tuple[list[TranscriptSegment], int]:
    import numpy as np

    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
        raise ValueError("batch_size must be a positive integer")
    ordered_interval_indices([], batch_order)
    audit = audit if audit is not None else {}
    audit.update({
        "backend": f"funasr-pytorch-{'cuda' if model.device == 'gpu' else 'cpu'}",
        "device": "cuda:0" if model.device == "gpu" else "cpu", "batch_size": batch_size,
        "vad_device": "cuda:0" if model.vad_device == "gpu" else "cpu",
        "cpu_threads": model.cpu_threads, "timestamp_source": "official-bicif",
        "vad_mode": "offline-file", "vad_end_silence_ms": VAD_END_SILENCE_MS,
        "vad_batch_size": batch_size, "vad_input_count": 1,
        "vad_max_segment_ms": VAD_MAX_SEGMENT_MS,
        "batch_order": batch_order, "estimated_padded_seconds": 0.0,
        "batch_sizes": [], "batches": [], "stage_seconds": {},
        "effective_batch_size": batch_size, "oom_recovery": [],
    })
    context = TemporaryDirectory(prefix="paraformer-job-") if temporary_directory is None else nullcontext(
        str(temporary_directory)
    )
    with context as temporary:
        directory = Path(temporary)
        directory.mkdir(parents=True, exist_ok=True)
        wav_path = directory / "timeline.wav"
        started = perf_counter()
        duration_ms = stream_decode_audio_to_wav(audio_path, wav_path, progress)
        audit["stage_seconds"]["audio"] = perf_counter() - started
        synchronize_device(model)
        started = perf_counter()
        # FSMN-VAD accepts one file; its internal chunks are not a file batch.
        with observe_encoder_batches(model.vad) as vad_encoder_batches:
            intervals = detect_speech(model.vad, wav_path, progress, batch_size=batch_size)
        synchronize_device(model)
        audit["stage_seconds"]["vad"] = perf_counter() - started
        audit["vad_encoder_batch_sizes"] = vad_encoder_batches
        audit["speech_intervals_ms"] = intervals
        audit["speech_seconds"] = sum(end - start for start, end in intervals) / 1000
        input_order = ordered_interval_indices(intervals, batch_order)
        audit["input_order"] = input_order
        audit["stage_seconds"].update({"asr": 0.0, "validation": 0.0})
        raw_path = audit.get("raw_output_path")
        raw_context = nullcontext(None)
        if raw_path:
            Path(raw_path).parent.mkdir(parents=True, exist_ok=True)
            raw_context = Path(raw_path).open("w", encoding="utf-8")
        segments_by_interval = [[] for _ in intervals]
        with wave.open(str(wav_path), "rb") as source, raw_context as raw_output:
            index, current_batch_size = 0, batch_size
            while index < len(intervals):
                original_indices = input_order[index:index + current_batch_size]
                group = [intervals[original_index] for original_index in original_indices]
                audios = []
                for start, end in group:
                    source.setpos(round(start * 16))
                    samples = source.readframes(round((end - start) * 16))
                    audio = np.frombuffer(samples, dtype="<i2").astype(np.float32)
                    audio /= 32768
                    audios.append(audio)
                del audio
                synchronize_device(model)
                started = perf_counter()
                try:
                    with observe_encoder_batches(model.recognizer) as encoder_batches:
                        outputs = model.recognizer.generate(
                            input=audios, batch_size=current_batch_size, fs=16000,
                            output_timestamp=True, pred_timestamp=True,
                        )
                    synchronize_device(model)
                except Exception as exc:
                    if model.device != "gpu" or not is_cuda_oom(exc):
                        raise
                    elapsed = perf_counter() - started
                    next_size = lower_batch_size(min(current_batch_size, len(group)))
                    failure = {
                        "stage": "asr", "completed_intervals": index,
                        "failed_interval_indices": original_indices,
                        "batch_size": current_batch_size, "actual_batch_size": len(group),
                        "next_batch_size": next_size, "duration": elapsed,
                    }
                    audit["oom_recovery"].append(failure)
                    audit["stage_seconds"]["asr"] += elapsed
                    exc.__traceback__ = None
                    del audios
                    release_cuda_memory()
                    if next_size is None:
                        raise ResourcePaused(
                            "Paraformer GPU 在 BS1 仍显存不足，任务已暂停；请释放显存后继续。",
                            failure=failure,
                        ) from None
                    current_batch_size = next_size
                    audit["effective_batch_size"] = next_size
                    if progress:
                        progress(
                            .2 + .78 * index / len(intervals),
                            f"Paraformer CUDA OOM: retrying failed batch with BS{next_size}",
                        )
                    continue
                elapsed = perf_counter() - started
                audit["stage_seconds"]["asr"] += elapsed
                if len(outputs) != len(group):
                    raise ValueError("Paraformer batch output count does not match the input")
                if encoder_batches and encoder_batches != [len(group)]:
                    raise ValueError("Paraformer did not execute the requested true utterance batch")
                record = {
                    "index": len(audit["batches"]), "batch_size": len(group),
                    "target_batch_size": current_batch_size,
                    "original_indices": original_indices,
                    "input_seconds": [len(audio) / 16000 for audio in audios],
                    "output_count": len(outputs), "duration": elapsed,
                    "encoder_batch_sizes": encoder_batches,
                }
                record["estimated_padded_seconds"] = max(record["input_seconds"]) * len(group)
                audit["estimated_padded_seconds"] += record["estimated_padded_seconds"]
                audit["batch_sizes"].append(len(group))
                audit["batches"].append(record)
                started = perf_counter()
                for original_index, span, output in zip(original_indices, group, outputs):
                    if raw_output:
                        raw_output.write(json.dumps(
                            {"interval_index": original_index, "span_ms": span, "output": output},
                            ensure_ascii=True,
                        ) + "\n")
                        raw_output.flush()
                    segments_by_interval[original_index] = timestamp_segments(
                        output, span[0], span[1] - span[0],
                    )
                audit["stage_seconds"]["validation"] += perf_counter() - started
                if progress:
                    progress(.2 + .78 * (index + len(group)) / max(1, len(intervals)),
                             f"Paraformer {model.device.upper()}: "
                             f"{index + len(group)}/{len(intervals)} speech segments")
                del audios, outputs
                index += len(group)
        # Recognition order is independent of the original subtitle timeline.
        started = perf_counter()
        segments = [
            replace(segment, id=index)
            for index, segment in enumerate(
                (segment for group in segments_by_interval for segment in group), 1,
            )
        ]
        audit["stage_seconds"]["validation"] += perf_counter() - started
        if any(segment.start_ms < 0 or segment.end_ms > duration_ms + 1 for segment in segments):
            raise ValueError("Subtitle timestamp outside the original video timeline")
        return segments, duration_ms
