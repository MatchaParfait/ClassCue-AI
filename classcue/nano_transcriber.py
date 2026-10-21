"""Official offline Nano inference with audited CTC timestamps and CPU VAD."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
import json
import math
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from types import SimpleNamespace
import unicodedata
import wave

from classcue.core import TranscriptSegment
from classcue.paraformer_transcriber import (
    DEFAULT_CPU_THREADS, VAD_MAX_SEGMENT_MS, VAD_END_SILENCE_MS,
    _validate_component, detect_speech, is_cuda_oom, observe_encoder_batches,
    ordered_interval_indices, release_cuda_memory, runtime_environment, synchronize_device,
)
from classcue.processing_control import ResourcePaused
from classcue.qwen_transcriber import aligned_segments, stream_decode_audio_to_wav

DEFAULT_BATCH_SIZE = 6


@dataclass
class NanoModels:
    recognizer: object
    vad: object
    cpu_threads: int
    device: str
    llm_dtype: str
    vad_device: str = "cpu"


def load_nano_model(recognizer_path, vad_path, *, device="cpu", cpu_threads=DEFAULT_CPU_THREADS):
    os.environ.update(runtime_environment(device, cpu_threads))
    from classcue.asr_models import NANO_REQUIRED_FILES
    import torch
    from funasr import AutoModel

    path = Path(recognizer_path).resolve()
    for name in NANO_REQUIRED_FILES["recognizer"]:
        file = path / name
        if not file.is_file() or file.stat().st_size <= 0:
            raise ValueError(f"Nano local checkpoint is incomplete: {file}")
    vad_path = _validate_component(vad_path, "vad")
    if device == "cpu" and torch.cuda.is_available():
        raise RuntimeError("CUDA must be hidden before importing torch in CPU mode")
    if device == "gpu" and not torch.cuda.is_available():
        raise RuntimeError("Nano GPU requires CUDA; CPU fallback is disabled")
    torch.set_num_threads(cpu_threads)
    try:
        torch.set_num_interop_threads(1)
    except RuntimeError:
        pass
    llm_dtype = "bf16" if device == "gpu" and torch.cuda.is_bf16_supported() else "fp32"
    options = {"hub": "ms", "disable_update": True, "check_latest": False,
               "trust_remote_code": False, "disable_pbar": True, "ncpu": cpu_threads,
               "vad_model": None, "punc_model": None, "spk_model": None}
    recognizer = AutoModel(
        model=str(path), device="cuda:0" if device == "gpu" else "cpu",
        ngpu=int(device == "gpu"), llm_conf={"llm_dtype": llm_dtype}, **options,
    )
    if recognizer.model.ctc_decoder is None or recognizer.model.ctc is None:
        raise ValueError("Nano requires the complete ModelScope CTC checkpoint for timestamps")
    vad = AutoModel(model=str(vad_path), device="cpu", ngpu=0,
                    max_single_segment_time=VAD_MAX_SEGMENT_MS, **options)
    for component, expected in ((recognizer, "cuda" if device == "gpu" else "cpu"), (vad, "cpu")):
        if any(parameter.device.type != expected for parameter in component.model.parameters()):
            raise RuntimeError(f"Nano component parameters must stay on {expected}")
        component.model.eval()
    if vad.model.vad_opts.max_single_segment_time != VAD_MAX_SEGMENT_MS:
        raise RuntimeError("Nano VAD maximum segment duration was not applied")
    return NanoModels(recognizer, vad, cpu_threads, device, llm_dtype)


def _spoken(text):
    return "".join(c for c in text if c == "'" or unicodedata.category(c)[0] in {"L", "N"})


def timestamp_segments(output, offset_ms, duration_ms, tokenizer):
    text = output.get("text", "")
    if not isinstance(text, str):
        raise ValueError("Invalid Nano output text")
    stamps = output.get("timestamps", [])
    if not _spoken(text):
        return []
    if not isinstance(stamps, list) or not stamps:
        raise ValueError("Nano returned text without native CTC timestamps")
    ids = tokenizer.encode(text)
    if len(ids) != len(stamps):
        raise ValueError("Nano native timestamps do not cover every text token")
    items, pending, decoded = [], [], []
    previous_start = 0.0
    for token_id, stamp in zip(ids, stamps):
        if stamp.get("token") != tokenizer.decode([token_id]):
            raise ValueError("Nano timestamp token does not match the recognized text")
        start, end = float(stamp["start_time"]), float(stamp["end_time"])
        if (not math.isfinite(start) or not math.isfinite(end) or start < 0
                or end <= start or start < previous_start or end > duration_ms / 1000 + .25):
            raise ValueError("Nano native timestamp is invalid or outside the audio segment")
        previous_start = start
        if not pending:
            token_start = start
        pending.append(token_id)
        token = tokenizer.decode(pending)
        # Whisper BPE may split a Chinese UTF-8 character across multiple tokens.
        if "\ufffd" in token:
            continue
        decoded.append(token)
        spoken = _spoken(token)
        if spoken:
            items.append(SimpleNamespace(text=spoken, start_time=token_start, end_time=end))
        pending = []
    if pending or "".join(decoded) != text:
        raise ValueError("Nano native timestamp tokens could not reconstruct the original text")
    return aligned_segments(text, items, offset_ms / 1000, duration_ms / 1000)


@contextmanager
def observe_nano_batches(recognizer):
    sizes = []
    hook = recognizer.model.audio_encoder.register_forward_pre_hook(
        lambda _module, args: sizes.append(int(args[0].shape[0]))
    )
    try:
        yield sizes
    finally:
        hook.remove()


def transcribe_nano(model, audio_path, progress=None, *, batch_size=DEFAULT_BATCH_SIZE,
                    temporary_directory=None, audit=None, batch_order="length"):
    import numpy as np
    import torch

    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
        raise ValueError("Nano batch_size must be a positive integer")
    ordered_interval_indices([], batch_order)
    audit = audit if audit is not None else {}
    audit.update({"backend": "official-funasr-nano-ctc", "device": model.device,
                  "vad_device": "cpu", "llm_dtype": model.llm_dtype, "encoder_dtype": "fp32",
                  "batch_size": batch_size, "effective_batch_size": batch_size,
                  "batch_semantics": "input-group; official CTC path runs sequentially",
                  "timestamp_source": "official-trained-ctc", "cpu_threads": model.cpu_threads,
                  "vad_max_segment_ms": VAD_MAX_SEGMENT_MS, "vad_end_silence_ms": VAD_END_SILENCE_MS,
                  "vad_batch_size": 1, "batch_order": batch_order, "batches": [],
                  "batch_sizes": [], "oom_recovery": [], "stage_seconds": {}})
    context = TemporaryDirectory(prefix="nano-job-") if temporary_directory is None else nullcontext(str(temporary_directory))
    with context as temporary:
        directory = Path(temporary)
        directory.mkdir(parents=True, exist_ok=True)
        wav = directory / "timeline.wav"
        started = perf_counter()
        duration_ms = stream_decode_audio_to_wav(audio_path, wav, progress)
        audit["stage_seconds"]["audio"] = perf_counter() - started
        started = perf_counter()
        with observe_encoder_batches(model.vad) as vad_sizes:
            intervals = detect_speech(model.vad, wav, progress, batch_size=1)
        audit["stage_seconds"]["vad"] = perf_counter() - started
        audit["vad_encoder_batch_sizes"] = vad_sizes
        audit["speech_intervals_ms"] = intervals
        audit["speech_seconds"] = sum(end - start for start, end in intervals) / 1000
        order = ordered_interval_indices(intervals, batch_order)
        audit["input_order"] = order
        audit["stage_seconds"].update(asr=0.0, validation=0.0)
        cues = [[] for _ in intervals]
        raw_path = audit.get("raw_output_path")
        raw_context = nullcontext(None)
        if raw_path:
            Path(raw_path).parent.mkdir(parents=True, exist_ok=True)
            raw_context = Path(raw_path).open("w", encoding="utf-8")
        with wave.open(str(wav), "rb") as source, raw_context as raw:
            index, current = 0, batch_size
            while index < len(order):
                indices = order[index:index + current]
                audios = []
                for item in indices:
                    start, end = intervals[item]
                    source.setpos(start * 16)
                    samples = np.frombuffer(source.readframes((end - start) * 16), dtype="<i2")
                    audios.append(torch.from_numpy(samples.astype(np.float32) / 32768))
                synchronize_device(model)
                started = perf_counter()
                try:
                    with torch.inference_mode(), observe_nano_batches(model.recognizer) as sizes:
                        outputs = model.recognizer.generate(
                            input=audios, batch_size=current,
                            language="\u4e2d\u6587", itn=True, llm_dtype=model.llm_dtype,
                            llm_kwargs={"do_sample": False, "num_beams": 1}, max_length=512,
                        )
                    synchronize_device(model)
                except Exception as exc:
                    if model.device != "gpu" or not is_cuda_oom(exc):
                        raise
                    audit["stage_seconds"]["asr"] += perf_counter() - started
                    next_size = max(1, len(indices) // 2) if len(indices) > 1 else None
                    failure = {"stage": "asr", "failed_interval_indices": indices,
                               "batch_size": current, "next_batch_size": next_size}
                    audit["oom_recovery"].append(failure)
                    exc.__traceback__ = None
                    release_cuda_memory()
                    if next_size is None:
                        raise ResourcePaused("Nano GPU memory exhausted at BS1; release GPU memory and resume.",
                                             failure=failure) from None
                    current = next_size
                    audit["effective_batch_size"] = current
                    continue
                elapsed = perf_counter() - started
                audit["stage_seconds"]["asr"] += elapsed
                if len(outputs) != len(indices) or sizes != [1] * len(indices):
                    raise ValueError("Unexpected official Nano CTC batch execution or output count")
                audit["batch_sizes"].append(len(indices))
                audit["batches"].append({"index": len(audit["batches"]), "batch_size": len(indices),
                                         "target_batch_size": current, "original_indices": indices,
                                         "encoder_batch_sizes": sizes, "duration": elapsed,
                                         "output_count": len(outputs)})
                started = perf_counter()
                for item, output in zip(indices, outputs):
                    start, end = intervals[item]
                    if raw:
                        raw.write(json.dumps({"interval_index": item, "span_ms": [start, end],
                                              "output": output}, ensure_ascii=True) + "\n")
                        raw.flush()
                    cues[item] = timestamp_segments(output, start, end - start, model.recognizer.model.ctc_tokenizer)
                audit["stage_seconds"]["validation"] += perf_counter() - started
                index += len(indices)
                if progress:
                    progress(.2 + .78 * index / max(1, len(order)), f"Nano {model.device.upper()}: {index}/{len(order)} segments")
                del audios, outputs
        result = [replace(cue, id=i) for i, cue in enumerate((c for group in cues for c in group), 1)]
        if any(cue.end_ms > duration_ms + 1 for cue in result):
            raise ValueError("Nano subtitle outside the original video timeline")
        return result, duration_ms
