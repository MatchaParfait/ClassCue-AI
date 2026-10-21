"""Choose contiguous audio boundaries without filtering the original timeline."""

from dataclasses import dataclass
import math

import numpy as np


@dataclass(frozen=True)
class AudioSpan:
    start_sample: int
    end_sample: int
    reason: str


def quietest_sample(audio, left: int, right: int, rate: int) -> int:
    """Qwen-style 100 ms low-amplitude window, with linear-time window sums."""
    left, right = max(0, left), min(len(audio), right)
    if right <= left:
        raise ValueError("Empty boundary search range")
    amplitudes = np.abs(audio[left:right])
    window = min(len(amplitudes), max(4, round(rate * .1)))
    cumulative = np.concatenate(([0.0], np.cumsum(amplitudes, dtype=np.float64)))
    sums = cumulative[window:] - cumulative[:-window]
    start = int(np.argmin(sums))
    return left + start + int(np.argmin(amplitudes[start:start + window]))


def speech_gaps(intervals, total_samples: int, rate: int):
    """Validate VAD spans and return their complement in sample coordinates."""
    spans = []
    for start, end in intervals:
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
            raise ValueError("Invalid VAD speech interval")
        if end > total_samples / rate + .1:
            raise ValueError("VAD interval exceeds audio duration")
        spans.append((min(total_samples, round(start * rate)),
                      min(total_samples, round(end * rate))))
    if not spans:
        # No detected speech is not proof that the entire file is safe silence.
        return []
    gaps, cursor = [], 0
    for start, end in sorted(spans):
        if start > cursor:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < total_samples:
        gaps.append((cursor, total_samples))
    return gaps


def plan_spans(
    audio, rate: int = 16000, target_seconds: float = 90,
    mode: str = "energy", speech_intervals=None, search_seconds: float = 5,
    min_gap_seconds: float = .3, guard_seconds: float = .1,
) -> list[AudioSpan]:
    if rate <= 0 or target_seconds < 30 or search_seconds < 0:
        raise ValueError("Invalid chunk duration, search range or sample rate")
    if mode not in {"fixed", "energy", "firered"}:
        raise ValueError("Unknown boundary mode")
    if mode == "firered" and speech_intervals is None:
        raise ValueError("VAD mode requires detection results; no silent fallback")
    total = len(audio)
    target = round(target_seconds * rate)
    search = round(search_seconds * rate)
    guard = round(guard_seconds * rate)
    minimum = round(min_gap_seconds * rate)
    gaps = speech_gaps(speech_intervals or [], total, rate)
    spans, start = [], 0
    while total - start > target:
        desired = start + target
        left, right = max(start + 1, desired - search), min(total, desired + search)
        reason = mode
        if mode == "fixed":
            cut = desired
        else:
            candidates = []
            if mode == "firered":
                for a, b in gaps:
                    lo, hi = max(left, a + guard), min(right, b - guard)
                    if b - a >= minimum and hi > lo:
                        distance = abs((lo + hi) / 2 - desired)
                        candidates.append((distance, -(hi - lo), lo, hi))
            if candidates:
                _, _, lo, hi = min(candidates)
                cut = quietest_sample(audio, lo, hi, rate)
            else:
                cut = quietest_sample(audio, left, right, rate)
                if mode == "firered":
                    reason = "energy-fallback"
        cut = max(start + 1, min(total, cut))
        spans.append(AudioSpan(start, cut, reason))
        start = cut
    if start < total:
        spans.append(AudioSpan(start, total, "tail"))
    return spans
