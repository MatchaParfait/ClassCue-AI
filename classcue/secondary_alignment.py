"""Bounded, deterministic subtitle matching for a secondary recording."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from statistics import median
import unicodedata

from classcue.core import TranscriptSegment

FAST_SEGMENTS = 10
FAST_CALLS = 10
FAST_ASR_SECONDS = 120
MAX_SEGMENTS = 200
MAX_CALLS = 100
MAX_ASR_SECONDS = 900
MAX_DECODE_SECONDS = 1800
MAX_RUNTIME_SECONDS = 600
WINDOW_SECONDS = 12
SCAN_SECONDS = 30
ALGORITHM_VERSION = 2


def normalize(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", text).lower() if c.isalnum())


@dataclass
class TextSpan:
    text: str
    start_ms: int
    end_ms: int
    keys: tuple[tuple[int, int], ...]
    times: tuple[int | None, ...]
    windows: frozenset[int]


def spans(segments: list[TranscriptSegment], windows: dict[int, int] | None = None):
    ordered = sorted(segments, key=lambda s: (s.start_ms, s.id))
    for i, segment in enumerate(ordered):
        text, keys, times, window_ids = "", [], [], set()
        for end in range(i, min(i + 3, len(ordered))):
            item = ordered[end]
            if end > i and item.start_ms - ordered[end - 1].end_ms > 2000:
                break
            if item.end_ms - segment.start_ms > 20000:
                break
            normalized = normalize(item.text)
            word_text, word_times = "", []
            for word in item.words or []:
                value = normalize(str(word.get("word", "")))
                start = word.get("start")
                if not isinstance(start, (int, float)) or not item.start_ms <= start * 1000 <= item.end_ms:
                    word_text = ""
                    break
                word_text += value
                word_times.extend([round(start * 1000)] * len(value))
            text += normalized
            if len(text) > 120:
                break
            keys.extend((item.id, char) for char in range(len(normalized)))
            times.extend(word_times if word_text == normalized else [None] * len(normalized))
            if windows is not None:
                window_ids.add(windows[item.id])
            if len(text) >= 12 and len(set(text)) >= 6:
                yield TextSpan(text, segment.start_ms, item.end_ms, tuple(keys), tuple(times),
                               frozenset(window_ids))


def anchors(primary: list[TranscriptSegment]) -> list[TextSpan]:
    remaining = list(spans(primary))
    chosen = []
    while remaining and len(chosen) < 3:
        first = min(s.start_ms for s in remaining)
        best = max((s for s in remaining if s.start_ms <= first + 60000),
                   key=lambda s: (len(s.text), -s.start_ms))
        chosen.append(best)
        remaining = [s for s in remaining if s.start_ms >= best.start_ms + 30000]
    return chosen


def search_ranges(primary: list[TranscriptSegment], offset_ms: int, duration_ms: int):
    visited: list[tuple[float, float]] = []
    for anchor in anchors(primary):
        expected = (anchor.start_ms + offset_ms) / 1000
        for left, right in ((-15, 45), (45, 180), (-180, -15)):
            pieces = [(max(0., expected + left), min(duration_ms / 1000, expected + right))]
            for used_start, used_end in visited:
                pieces = [(a, b) for start, end in pieces
                          for a, b in ((start, min(end, used_start)), (max(start, used_end), end))
                          if b > a]
            for start, end in pieces:
                if end > start:
                    visited.append((start, end))
                    yield start, end


def search_windows(primary, offset_ms, duration_ms, *, fallback=False):
    """Visit each anchor before spending more decoding/ASR on the same location."""
    plans = []
    for anchor in anchors(primary):
        expected = (anchor.start_ms + offset_ms) / 1000
        ranges = [(-15, 15), (15, 45)]
        if fallback:
            # Expand both directions by distance, reserving opportunities for negative offsets.
            for distance in range(15, 600, SCAN_SECONDS):
                ranges.append((-min(distance + SCAN_SECONDS, 600), -distance))
                if distance + SCAN_SECONDS < 600:
                    ranges.append((distance + SCAN_SECONDS, min(distance + 2 * SCAN_SECONDS, 600)))
        windows = []
        for left, right in ranges:
            cursor, end = max(0., expected + left), min(duration_ms / 1000, expected + right)
            while cursor < end:
                stop = min(cursor + SCAN_SECONDS, end)
                windows.append((cursor, stop))
                cursor = stop
        plans.append(windows)
    for index in range(max(map(len, plans), default=0)):
        for anchor_id, windows in enumerate(plans):
            if index < len(windows):
                yield anchor_id, *windows[index]


@dataclass
class Evidence:
    primary: TextSpan
    secondary: TextSpan
    primary_keys: frozenset[tuple[int, int]]
    secondary_keys: frozenset[tuple[int, int]]
    offset_ms: int
    score: float
    weight: float
    timed: bool

    def public(self):
        return {
            "primary_text": self.primary.text[:120], "secondary_text": self.secondary.text[:120],
            "primary_ms": self.primary.start_ms, "secondary_ms": self.secondary.start_ms,
            "offset_ms": self.offset_ms, "score": round(self.score, 3),
        }


class Matcher:
    def __init__(self, primary: list[TranscriptSegment]):
        self.primary = list(spans(primary))
        self.index: dict[str, set[int]] = {}
        for index, span in enumerate(self.primary):
            for gram in self.grams(span.text):
                self.index.setdefault(gram, set()).add(index)

    @staticmethod
    def grams(text):
        return {text[i:i + 3] for i in range(len(text) - 2)}

    def match(self, secondary: TextSpan) -> Evidence | None:
        indices = set().union(*(self.index.get(g, set()) for g in self.grams(secondary.text)))
        candidates = []
        for index in sorted(indices):
            primary = self.primary[index]
            blocks = SequenceMatcher(None, secondary.text, primary.text, autojunk=False).get_matching_blocks()
            pairs = [(block.a + i, block.b + i) for block in blocks for i in range(block.size)]
            if not pairs:
                continue
            first, last = pairs[0][1], pairs[-1][1] + 1
            matched_text = primary.text[first:last]
            if primary.text.find(matched_text) != primary.text.rfind(matched_text):
                continue
            full_primary = first == 0 and last == len(primary.text)
            if not full_primary:
                primary = TextSpan(
                    primary.text[first:last], primary.times[first] or primary.start_ms,
                    primary.end_ms, primary.keys[first:last], primary.times[first:last], primary.windows,
                )
                pairs = [(a, b - first) for a, b in pairs]
            count = len(pairs)
            score = 2 * count / (len(secondary.text) + len(primary.text))
            coverage = min(count / len(secondary.text), count / len(primary.text))
            if count < 12 or score < .72 or coverage < .65:
                continue
            offsets = [secondary.times[a] - primary.times[b] for a, b in pairs
                       if secondary.times[a] is not None and primary.times[b] is not None]
            complete = (pairs[0] == (0, 0)
                        and pairs[-1] == (len(secondary.text) - 1, len(primary.text) - 1))
            timed = len(offsets) >= 12 or (complete and full_primary)
            offset = round(median(offsets)) if len(offsets) >= 12 else secondary.start_ms - primary.start_ms
            candidates.append(Evidence(
                primary, secondary, frozenset(primary.keys[b] for _, b in pairs),
                frozenset(secondary.keys[a] for a, _ in pairs), offset, score,
                min(count, 60) * score * coverage, timed,
            ))
        candidates.sort(key=lambda e: (-e.score, -e.weight, e.primary.start_ms))
        if not candidates:
            return None
        best = candidates[0]
        if best.score < .8 or min(len(best.secondary_keys) / len(best.secondary.text),
                                  len(best.primary_keys) / len(best.primary.text)) < .75:
            return None
        distinct = [e for e in candidates[1:] if not (e.primary_keys & best.primary_keys)]
        if distinct and best.score - distinct[0].score < .08:
            return None
        return best

    def evaluate(self, secondary: list[TranscriptSegment], windows: dict[int, int]):
        evidence = [e for span in spans(secondary, windows) if (e := self.match(span))]
        evidence.sort(key=lambda e: (-e.weight, e.secondary.start_ms))
        independent = []
        for item in evidence:
            if any(item.primary_keys & old.primary_keys or item.secondary_keys & old.secondary_keys
                   for old in independent):
                continue
            independent.append(item)
        base = {"version": ALGORITHM_VERSION, "outcome": "no_match", "offset_ms": None,
                "message": "未找到可靠对应句，原偏移已保留", "evidence": []}
        if not independent:
            return base
        best = independent[0]
        base.update(outcome="candidate", offset_ms=best.offset_ms,
                    message="找到一处可靠对应，请确认候选偏移",
                    evidence=[best.public()])
        if max(e.offset_ms for e in independent) - min(e.offset_ms for e in independent) > 2000:
            return {**base, "outcome": "conflict", "offset_ms": None,
                    "message": "对应句给出的时间差不一致，原偏移已保留",
                    "evidence": [e.public() for e in independent[:2]]}
        support = [best] if best.timed else []
        for item in independent[1:]:
            if not item.timed or item.secondary.windows & best.secondary.windows:
                continue
            p_delta = item.primary.start_ms - best.primary.start_ms
            s_delta = item.secondary.start_ms - best.secondary.start_ms
            if abs(p_delta) < 5000 or abs(s_delta) < 5000 or p_delta * s_delta <= 0:
                continue
            if (item.primary.start_ms < best.primary.end_ms and best.primary.start_ms < item.primary.end_ms
                    or item.secondary.start_ms < best.secondary.end_ms
                    and best.secondary.start_ms < item.secondary.end_ms):
                continue
            support.append(item)
        if len(support) < 2:
            return base
        ordered = sorted(support, key=lambda e: e.offset_ms)
        total, running = sum(e.weight for e in ordered), 0.
        offset = best.offset_ms
        for item in ordered:
            running += item.weight
            if running >= total / 2:
                offset = item.offset_ms
                break
        return {**base, "outcome": "ready", "offset_ms": offset,
                "message": "已找到两处一致对应", "evidence": [e.public() for e in support[:2]]}
