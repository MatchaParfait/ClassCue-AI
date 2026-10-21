"""Official Dolphin batch encoding/rescoring with strict native CTC timestamps."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
import hashlib
import json
import math
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from types import SimpleNamespace
import unicodedata
import wave

from paraformer_transcriber import (
    DEFAULT_CPU_THREADS, VAD_MAX_SEGMENT_MS, VAD_END_SILENCE_MS,
    _validate_component, detect_speech, is_cuda_oom, observe_encoder_batches,
    ordered_interval_indices, release_cuda_memory, runtime_environment, synchronize_device,
)
from processing_control import ResourcePaused
from qwen_transcriber import aligned_segments, stream_decode_audio_to_wav

DEFAULT_BATCH_SIZE = 8
DEFAULT_BEAM_SIZE = 10


def trim_cuda_cache(model, audit):
    if model.device != "gpu":
        return
    import torch

    reserved, allocated = torch.cuda.memory_reserved(), torch.cuda.memory_allocated()
    if reserved - allocated > 768 * 2**20:
        torch.cuda.empty_cache()
        audit["cache_trims"].append({"reserved_before_mib": reserved / 2**20,
                                     "allocated_mib": allocated / 2**20,
                                     "reserved_after_mib": torch.cuda.memory_reserved() / 2**20})


def ensure_dolphin_headroom(model, audit):
    from qwen_memory import ensure_memory_headroom

    for attempt in range(2 if model.device == "gpu" else 1):
        try:
            ensure_memory_headroom(physical_mib=0, commit_mib=2048)
            return
        except RuntimeError as exc:
            if model.device != "gpu" or attempt:
                raise ResourcePaused(str(exc), failure={"stage": "memory_headroom"}) from None
            audit["headroom_recovery"].append({
                "action": "release_unused_cuda_cache", "reason": str(exc)})
            release_cuda_memory()


@dataclass
class DolphinModels:
    recognizer: object
    vad: object
    tokenizer: object
    cpu_threads: int
    device: str = "cpu"
    vad_device: str = "cpu"
    decoder_mode: str = "official"
    profile_decode: bool = False
    decode_profiles: list | None = None


def load_dolphin_model(recognizer_path, vad_path, *, device="cpu", cpu_threads=DEFAULT_CPU_THREADS):
    os.environ.update(runtime_environment(device, cpu_threads))
    import torch
    from dolphin.tokenizer import init_tokenizer
    from funasr import AutoModel
    from dolphin_checkpoint import load_verified_dolphin_checkpoint
    from asr_checkpoint import reuse_funasr_checkpoint_tensors
    from asr_models import DOLPHIN_REQUIRED_FILES
    from scripts.prepare_dolphin import CHECKPOINT_SHA256

    path = Path(recognizer_path).resolve()
    for name in DOLPHIN_REQUIRED_FILES["recognizer"]:
        if not (path / name).is_file() or not (path / name).stat().st_size:
            raise ValueError(f"Dolphin checkpoint is incomplete: {path / name}")
    with (path / "small.cn.pt").open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != CHECKPOINT_SHA256:
            raise ValueError("Dolphin weights differ from the official pinned checkpoint")
    if device == "gpu" and not torch.cuda.is_available():
        raise RuntimeError("Dolphin GPU requires CUDA; CPU fallback is disabled")
    if device == "cpu" and torch.cuda.is_available():
        raise RuntimeError("Hide CUDA before importing torch in Dolphin CPU mode")
    torch.set_num_threads(cpu_threads)
    try:
        torch.set_num_interop_threads(1)
    except RuntimeError:
        pass
    recognizer = load_verified_dolphin_checkpoint(path, device="cuda:0" if device == "gpu" else "cpu")
    with reuse_funasr_checkpoint_tensors(AutoModel):
        vad = AutoModel(model=str(_validate_component(vad_path, "vad")), device="cpu", ngpu=0,
                        ncpu=cpu_threads, disable_update=True, check_latest=False, trust_remote_code=False,
                        disable_pbar=True, max_single_segment_time=VAD_MAX_SEGMENT_MS)
    for module, expected in ((recognizer, "cuda" if device == "gpu" else "cpu"), (vad.model, "cpu")):
        if any(parameter.device.type != expected for parameter in module.parameters()):
            raise RuntimeError(f"Dolphin component must stay on {expected}")
        module.eval()
    if vad.model.vad_opts.max_single_segment_time != VAD_MAX_SEGMENT_MS:
        raise RuntimeError("Dolphin VAD maximum segment duration was not applied")
    from dolphin_decode import default_decoder_mode

    return DolphinModels(recognizer, vad, init_tokenizer(recognizer.model_configs), cpu_threads, device,
                         decoder_mode=default_decoder_mode(device))


def _spoken(text):
    return "".join(c for c in text if c == "'" or unicodedata.category(c)[0] in {"L", "N"})


def timestamp_segments(output, offset_ms, duration_ms):
    text = output["text"]
    if not isinstance(text, str):
        raise ValueError("Invalid Dolphin output text")
    if not _spoken(text):
        return []
    stamps = output.get("word_timestamps")
    if not isinstance(stamps, list) or not stamps:
        raise ValueError("Dolphin returned text without native CTC timestamps")
    items = []
    for stamp in stamps:
        token = _spoken(stamp["word"])
        if not token:
            continue
        start, end = float(stamp["start"]), float(stamp["end"])
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            raise ValueError("Invalid Dolphin native timestamp")
        items.append(SimpleNamespace(text=token, start_time=start, end_time=end))
    return aligned_segments(text, items, offset_ms / 1000, duration_ms / 1000)


@contextmanager
def observe_dolphin_batches(recognizer):
    sizes = []
    hook = recognizer.encoder.register_forward_pre_hook(
        lambda _module, args: sizes.append(int(args[0].shape[0])))
    try:
        yield sizes
    finally:
        hook.remove()


def generate_batch(model, audios, *, beam_size=DEFAULT_BEAM_SIZE):
    from dolphin_decode import decoding_context

    profile = {} if model.profile_decode else None
    if profile is not None:
        model.decode_profiles = model.decode_profiles if model.decode_profiles is not None else []
        model.decode_profiles.append(profile)
    with decoding_context(model.recognizer, mode=model.decoder_mode, profile=profile):
        return _generate_batch(model, audios, beam_size=beam_size, profile=profile)


def _generate_batch(model, audios, *, beam_size, profile=None):
    """Keep complete batch encoder outputs for the official alignment function."""
    import torch
    from dolphin.processor import extract_feats

    recognizer, tokenizer = model.recognizer, model.tokenizer
    full_encoder = []
    hook = recognizer.encoder.register_forward_hook(
        lambda _module, _args, output: full_encoder.append(output))
    try:
        started = perf_counter()
        batch = extract_feats(audios, recognizer.model_configs)
        if profile is not None:
            profile["features"] = perf_counter() - started
        hypotheses = recognizer.decode(
            methods=["attention_rescoring"], speech=batch["feats"].to(recognizer.device),
            speech_lengths=batch["feats_lengths"].to(recognizer.device), beam_size=beam_size,
            infos={"tokenizer": tokenizer, "langs": ["<zh>"] * len(audios),
                   "regions": ["<CN>"] * len(audios), "need_timestamp": False,
                   "word_timestamp": False},
        )["attention_rescoring"]
    finally:
        hook.remove()
    if len(full_encoder) != 1 or len(hypotheses) != len(audios):
        raise ValueError("Unexpected Dolphin batch execution or output count")
    encoder_out, encoder_mask = full_encoder[0]
    if int(encoder_out.shape[0]) != len(audios):
        raise ValueError("Dolphin encoder did not process the complete batch")
    # Upstream rescoring passes its final single-utterance slice to alignment.
    # Align from the retained full batch instead, without another encoder call.
    hypotheses = recognizer.get_token_timestamp_torchaudio(
        encoder_out, encoder_mask.squeeze(1).sum(1), hypotheses, tokenizer)
    last_time = tokenizer.tokens2ids(["<30.00>"])[0]
    outputs = []
    for hypothesis in hypotheses:
        tokens = [token for token in hypothesis.tokens if token > last_time]
        text = tokenizer.detokenize(tokens)[0]
        outputs.append({"text": text.replace("\u2581", " ").strip(), "raw_text": text,
                        "tokens": hypothesis.tokens,
                        "word_timestamps": hypothesis.times})
    return outputs


def transcribe_dolphin(model, audio_path, progress=None, *, batch_size=DEFAULT_BATCH_SIZE,
                       temporary_directory=None, audit=None, batch_order="length", on_preview=None):
    import numpy as np
    import torch

    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
        raise ValueError("Dolphin batch_size must be a positive integer")
    ordered_interval_indices([], batch_order)
    audit = audit if audit is not None else {}
    audit.update({"backend": "official-dolphin-ctc", "device": model.device, "vad_device": "cpu",
                  "dtype": "fp32", "batch_size": batch_size, "effective_batch_size": batch_size,
                  "timestamp_source": "official-trained-ctc", "cpu_threads": model.cpu_threads,
                  "decoding_method": "attention_rescoring", "beam_size": DEFAULT_BEAM_SIZE,
                  "decoder_mode": model.decoder_mode,
                  "vad_max_segment_ms": VAD_MAX_SEGMENT_MS, "vad_end_silence_ms": VAD_END_SILENCE_MS,
                  "vad_batch_size": 1, "batch_order": batch_order, "batches": [], "batch_sizes": [],
                  "oom_recovery": [], "cache_trims": [], "headroom_recovery": [], "stage_seconds": {}})
    model.decode_profiles = []
    if model.profile_decode:
        audit["decode_profiles"] = model.decode_profiles
    context = TemporaryDirectory(prefix="dolphin-job-") if temporary_directory is None else nullcontext(str(temporary_directory))
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
                trim_cuda_cache(model, audit)
                ensure_dolphin_headroom(model, audit)
                indices = order[index:index + current]
                audios = []
                for item in indices:
                    start, end = intervals[item]
                    source.setpos(start * 16)
                    samples = np.frombuffer(source.readframes((end - start) * 16), dtype="<i2")
                    audio = samples.astype(np.float32)
                    audio /= 32768
                    audios.append(torch.from_numpy(audio).unsqueeze(0))
                del audio
                synchronize_device(model)
                started = perf_counter()
                try:
                    with torch.inference_mode(), observe_dolphin_batches(model.recognizer) as sizes:
                        outputs = generate_batch(model, audios)
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
                        raise ResourcePaused("Dolphin GPU memory exhausted at BS1; release GPU memory and resume.",
                                             failure=failure) from None
                    current = next_size
                    audit["effective_batch_size"] = current
                    continue
                elapsed = perf_counter() - started
                audit["stage_seconds"]["asr"] += elapsed
                if len(outputs) != len(indices) or sizes != [len(indices)]:
                    raise ValueError("Unexpected Dolphin real batch execution or output count")
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
                    cues[item] = timestamp_segments(output, start, end - start)
                audit["stage_seconds"]["validation"] += perf_counter() - started
                if on_preview is not None:
                    preview = []
                    for item in indices:
                        for cue_index, cue in enumerate(cues[item]):
                            if cue.end_ms > duration_ms + 1:
                                raise ValueError("Dolphin subtitle outside original audio timeline")
                            # Pair interval/cue indices into stable negative preview IDs.
                            pair = item + cue_index
                            preview_id = -(pair * (pair + 1) // 2 + cue_index + 1)
                            preview.append(replace(cue, id=preview_id))
                    if preview:
                        on_preview(preview)
                index += len(indices)
                if progress:
                    progress(.2 + .78 * index / max(1, len(order)), f"Dolphin {model.device.upper()}: {index}/{len(order)} segments")
                del audios, outputs
        result = [replace(cue, id=i) for i, cue in enumerate((c for group in cues for c in group), 1)]
        if any(cue.end_ms > duration_ms + 1 for cue in result):
            raise ValueError("Dolphin subtitle outside original audio timeline")
        return result, duration_ms
