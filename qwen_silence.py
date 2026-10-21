"""Optional silence compaction with a sample-exact map to the source timeline."""

from bisect import bisect_left, bisect_right
import math
from types import SimpleNamespace

from qwen_boundaries import speech_gaps


QWEN_AUDIO_PROFILES = ("general", "mostly-silent")


def resolve_silence_skipping(
    audio_profile: str = "general", *, skip_non_speech: bool | None = None,
) -> bool:
    """Use the caller's explicit scene choice, never infer it from noisy audio."""
    if audio_profile not in QWEN_AUDIO_PROFILES:
        raise ValueError(f"audio_profile must be one of {QWEN_AUDIO_PROFILES}")
    if skip_non_speech is not None and not isinstance(skip_non_speech, bool):
        raise ValueError("skip_non_speech must be a bool or None")
    # Keep the existing explicit switch for experiments and manual overrides.
    if skip_non_speech is not None:
        return skip_non_speech
    return audio_profile == "mostly-silent"


def build_silence_plan(
    spans, total_samples, rate, speech_intervals, *, enabled=False,
    padding_seconds=.3, min_removed_seconds=1.5,
):
    if rate <= 0 or total_samples < 0:
        raise ValueError("Invalid audio dimensions")
    if (not math.isfinite(padding_seconds) or padding_seconds < 0
            or not math.isfinite(min_removed_seconds) or min_removed_seconds <= 0):
        raise ValueError("Invalid silence padding or minimum removal duration")
    if enabled and speech_intervals is None:
        raise ValueError("Silence skipping requires VAD speech intervals")
    cursor = 0
    for span in spans:
        if span.start_sample != cursor or span.end_sample <= cursor:
            raise ValueError("External spans must cover the source contiguously")
        cursor = span.end_sample
    if cursor != total_samples:
        raise ValueError("External spans do not cover the complete audio")

    duration = total_samples / rate
    # Validate before padding, so invalid detections cannot be hidden by clipping.
    if speech_intervals is not None:
        speech_gaps(speech_intervals, total_samples, rate)
    padded = [
        [max(0, a - padding_seconds), min(duration, b + padding_seconds)]
        for a, b in (speech_intervals or [])
    ]
    deleted = [
        {"start_sample": a, "end_sample": b, "reason": "vad_non_speech"}
        for a, b in speech_gaps(padded, total_samples, rate)
        if enabled and b - a >= math.ceil(min_removed_seconds * rate)
    ]
    retained, cursor = [], 0
    for gap in deleted:
        if cursor < gap["start_sample"]:
            retained.append((cursor, gap["start_sample"]))
        cursor = gap["end_sample"]
    if cursor < total_samples:
        retained.append((cursor, total_samples))

    chunks = []
    for index, span in enumerate(spans):
        slices, position = [], 0
        for a, b in retained:
            left, right = max(a, span.start_sample), min(b, span.end_sample)
            if right > left:
                slices.append({
                    "source_start_sample": left, "source_end_sample": right,
                    "compact_start_sample": position,
                    "compact_end_sample": position + right - left,
                })
                position += right - left
        chunks.append({
            "source_chunk_index": index, "start_sample": span.start_sample,
            "end_sample": span.end_sample, "input_samples": position, "slices": slices,
        })
    removed = sum(g["end_sample"] - g["start_sample"] for g in deleted)
    if sum(c["input_samples"] for c in chunks) + removed != total_samples:
        raise ValueError("Silence plan has incomplete sample coverage")
    return {
        "schema_version": 1, "enabled": enabled, "sample_rate": rate,
        "total_samples": total_samples, "retained_samples": total_samples - removed,
        "removed_samples": removed, "deleted_regions": deleted, "chunks": chunks,
        "empty_detection_fallback": bool(enabled and not speech_intervals),
        "parameters": {
            "padding_seconds": padding_seconds, "min_removed_seconds": min_removed_seconds,
            "external_boundaries_unchanged": True, "empty_detection_policy": "retain_all",
        },
    }


def compact_audio(audio, chunk):
    import numpy as np

    pieces = [audio[s["source_start_sample"]:s["source_end_sample"]] for s in chunk["slices"]]
    if not pieces:
        return np.empty(0, dtype=audio.dtype)
    return pieces[0] if len(pieces) == 1 else np.concatenate(pieces)


def source_time(seconds, chunk, rate, *, endpoint="start"):
    """At a join, starts belong to the right slice and ends to the left slice."""
    if endpoint not in {"start", "end"}:
        raise ValueError("Invalid endpoint")
    if not math.isfinite(seconds) or not chunk["slices"]:
        raise ValueError("Invalid timestamp or empty audio map")
    position = seconds * rate
    if position < -.05 * rate or position > chunk["input_samples"] + .05 * rate:
        raise ValueError("Timestamp exceeds compacted audio")
    position = min(chunk["input_samples"], max(0, position))
    ends = [s["compact_end_sample"] for s in chunk["slices"]]
    # The application's word contract rounds times to milliseconds.
    nearest = bisect_left(ends, position)
    for index in (nearest - 1, nearest):
        if 0 <= index < len(ends) and abs(ends[index] - position) <= rate * .00051:
            position = ends[index]
            break
    index = (bisect_right if endpoint == "start" else bisect_left)(ends, position)
    item = chunk["slices"][min(index, len(ends) - 1)]
    return (item["source_start_sample"] + position - item["compact_start_sample"]) / rate


def restore_segments(segments, chunk, rate, total_samples, *, warnings=None):
    """Map every aligned word, then regroup cues across restored long pauses."""
    from qwen_transcriber import aligned_segments

    restored = []
    for segment in segments:
        if not segment.words:
            raise ValueError("Silence skipping requires word-level alignment")
        items = []
        for word in segment.words:
            start = source_time(float(word["start"]), chunk, rate, endpoint="start")
            if float(word["end"]) == float(word["start"]):
                end = start
            else:
                end = source_time(float(word["end"]), chunk, rate, endpoint="end")
            if end < start:
                raise ValueError("Invalid restored word interval")
            items.append(SimpleNamespace(text=word["word"], start_time=start, end_time=end))
            # A token spanning a splice cannot have an unambiguous single interval.
            for splice in chunk["slices"][:-1]:
                join = splice["compact_end_sample"] / rate
                if float(word["start"]) < join < float(word["end"]) and warnings is not None:
                    warnings.append({
                        "source_chunk_index": chunk["source_chunk_index"],
                        "word": word["word"], "source_start_seconds": start,
                        "source_end_seconds": end, "reason": "word_crosses_compact_join",
                    })
        restored.extend(aligned_segments(
            segment.text, items, 0, total_samples / rate, len(restored) + 1,
        ))
    return restored
