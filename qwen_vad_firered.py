"""CPU-only FireRedVAD speech intervals for Qwen boundary selection."""

from __future__ import annotations

from dataclasses import asdict
import math
from pathlib import Path
import time
from typing import Any
import wave


SAMPLE_RATE = 16000
FRAME_SHIFT_SAMPLES = 160
FRAME_LENGTH_SAMPLES = 400
DEFAULT_MODEL_PATH = Path(
    "D:/codex-models/classroom-agent/models/asr/fireredasr2-aed/vad/VAD"
)


def validate_intervals(
    intervals: Any, duration_seconds: float,
) -> list[list[float]]:
    """Validate the official seconds-based timestamps without changing the axis."""
    result: list[list[float]] = []
    previous_end = 0.0
    for item in intervals:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError("FireRedVAD returned an invalid speech interval")
        start, end = (float(value) for value in item)
        if (not math.isfinite(start) or not math.isfinite(end)
                or start < 0 or end <= start or start < previous_end
                or end > duration_seconds + 0.001):
            raise ValueError("FireRedVAD returned an out-of-range speech interval")
        end = min(end, duration_seconds)
        result.append([start, end])
        previous_end = end
    return result


def _frame_windows(total_frames: int, chunk_frames: int, context_frames: int):
    """Each frame is retained once; overlapping context only informs predictions."""
    for start in range(0, total_frames, chunk_frames):
        end = min(total_frames, start + chunk_frames)
        context_start = max(0, start - context_frames)
        context_end = min(total_frames, end + context_frames)
        yield start, end, context_start, context_end


def detect_firered(
    audio_path: str | Path,
    model_path: str | Path = DEFAULT_MODEL_PATH,
    *,
    min_silence_ms: int = 300,
    speech_pad_ms: int = 0,
    cpu_threads: int = 2,
    inference_chunk_seconds: float = 60,
    context_seconds: float = 2,
) -> dict[str, Any]:
    """Run only VAD, preserving every sample for the caller's subsequent ASR."""
    if min_silence_ms < 10 or speech_pad_ms < 0 or cpu_threads < 1:
        raise ValueError("Silence, padding, and CPU thread settings are invalid")
    if (not math.isfinite(inference_chunk_seconds) or inference_chunk_seconds <= 0
            or not math.isfinite(context_seconds) or context_seconds < 0):
        raise ValueError("FireRed inference chunk/context lengths are invalid")
    model_path = Path(model_path).resolve()
    for filename in ("model.pth.tar", "cmvn.ark"):
        if not (model_path / filename).is_file():
            raise FileNotFoundError(model_path / filename)

    load_start = time.perf_counter()
    import numpy as np
    import torch
    from fireredasr2s.fireredvad import FireRedVad, FireRedVadConfig

    torch.set_num_threads(cpu_threads)
    config = FireRedVadConfig(
        use_gpu=False,
        min_silence_frame=max(1, math.ceil(min_silence_ms / 10)),
        extend_speech_frame=math.ceil(speech_pad_ms / 10),
        # Default 20s splitting inserts fake one-frame silences; not real pauses.
        max_speech_frame=2**31 - 1,
    )
    model = FireRedVad.from_pretrained(str(model_path), config)
    load_seconds = time.perf_counter() - load_start

    inference_start = time.perf_counter()
    chunk_frames = max(1, round(inference_chunk_seconds * 100))
    context_frames = round(context_seconds * 100)
    probabilities = []
    with wave.open(str(audio_path), "rb") as wav, torch.inference_mode():
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (16000, 1, 2):
            raise ValueError("FireRedVAD requires a 16kHz mono PCM16 WAV")
        sample_count = wav.getnframes()
        duration_seconds = sample_count / SAMPLE_RATE
        total_frames = max(0, (sample_count - FRAME_LENGTH_SAMPLES) // FRAME_SHIFT_SAMPLES + 1)
        for start, end, context_start, context_end in _frame_windows(
            total_frames, chunk_frames, context_frames,
        ):
            first_sample = context_start * FRAME_SHIFT_SAMPLES
            final_sample = (context_end - 1) * FRAME_SHIFT_SAMPLES + FRAME_LENGTH_SAMPLES
            wav.setpos(first_sample)
            pcm = np.frombuffer(wav.readframes(final_sample - first_sample), dtype="<i2")
            _, probs = model.detect((pcm, SAMPLE_RATE), do_postprocess=False)
            probs = probs.reshape(-1)
            if len(probs) != context_end - context_start:
                raise ValueError("FireRedVAD feature frames do not match the original timeline")
            probabilities.extend(probs[start - context_start:end - context_start].tolist())
        decisions = model.vad_postprocessor.process(probabilities)
        intervals = model.vad_postprocessor.decision_to_segment(decisions, duration_seconds)
    inference_seconds = time.perf_counter() - inference_start

    return {
        "backend": "firered",
        "speech_intervals": validate_intervals(intervals, duration_seconds),
        "duration_seconds": duration_seconds,
        "load_seconds": load_seconds,
        "inference_seconds": inference_seconds,
        "device": "cpu",
        "model": str(model_path),
        "parameters": {
            **asdict(config),
            "cpu_threads": cpu_threads,
            "inference_chunk_seconds": chunk_frames / 100,
            "context_seconds": context_frames / 100,
            "frame_shift_seconds": 0.01,
            "max_speech_split_disabled": True,
            "probability_postprocessing": "global",
        },
    }
