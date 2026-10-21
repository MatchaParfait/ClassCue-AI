"""Deterministic ASR quote localization; no LLM calls or semantic substitutions."""

from __future__ import annotations

from functools import lru_cache
import re

from core import TranscriptSegment


MATCHING_VERSION = "citation-matching-v1"
FILLERS = ("好吧", "呃", "唉", "嗯")
NEGATION = re.compile(r"不|没|无需|未")


def normalize(text: str, *, oral: bool = False) -> tuple[str, list[int]]:
    characters, offsets = [], []
    index = 0
    while index < len(text):
        token = next((word for word in FILLERS if oral and text.startswith(word, index)), None)
        if token:
            index += len(token)
        elif text[index].isspace() or (oral and text[index] in "，。！？、；："):
            index += 1
        else:
            characters.append(text[index])
            offsets.append(index)
            index += 1
    return "".join(characters), offsets


def _contiguous(quote: str, lines: list[tuple[str, list[int]]]) -> list[tuple]:
    text = "".join(line[0] for line in lines)
    starts = []
    position = text.find(quote)
    while position >= 0 and len(starts) < 2:
        starts.append(position)
        position = text.find(quote, position + 1)
    paths = []
    for start in starts:
        path, offset = [], 0
        for index, (line, _) in enumerate(lines):
            left, right = max(start - offset, 0), min(start + len(quote) - offset, len(line))
            if left < right:
                path.append((index, left, right))
            offset += len(line)
        paths.append(tuple(path))
    return paths


def _fragments(quote: str, lines: list[tuple[str, list[int]]]) -> list[tuple]:
    if len(quote) < 20 or len(quote) > 4000 or len(lines) > 200:
        return []

    @lru_cache(maxsize=None)
    def search(index: int, consumed: int) -> tuple:
        if consumed == len(quote):
            return ((),)
        if index == len(lines):
            return ()
        found = []
        line = lines[index][0]
        remaining = quote[consumed:]
        start = line.find(remaining[0])
        while start >= 0:
            length = 0
            while length < min(len(line) - start, len(remaining)) and line[start + length] == remaining[length]:
                length += 1
            whole_line = start == 0 and length == len(line) and length >= 4
            final = length == len(remaining) and consumed > 0 and length >= 2
            if length >= 12 or whole_line or final:
                for tail in search(index + 1, consumed + length):
                    path = ((index, start, start + length),) + tail
                    if path not in found:
                        found.append(path)
                    if len(found) == 2:
                        return tuple(found)
            start = line.find(remaining[0], start + 1)
        for path in search(index + 1, consumed):
            if path not in found:
                found.append(path)
            if len(found) == 2:
                break
        return tuple(found)

    return list(search(0, 0))


def locate_quote(quote: str, provided: list[TranscriptSegment]) -> dict:
    supplied = sorted(provided, key=lambda segment: (segment.start_ms, segment.id))
    for method, oral in (("exact", False), ("oral_normalized", True), ("ordered_fragments", True)):
        target = normalize(quote, oral=oral)[0]
        if not target:
            return {"status": "unmatched", "reason": "Empty normalized quote"}
        lines = [normalize(segment.text, oral=oral) for segment in supplied]
        paths = _fragments(target, lines) if method == "ordered_fragments" else _contiguous(target, lines)
        if len(paths) > 1:
            return {"status": "ambiguous", "reason": "Quote has multiple possible source locations", "method": method}
        if not paths:
            continue
        path = paths[0]
        fragments = []
        for index, start, end in path:
            offsets = lines[index][1]
            left, right = offsets[start], offsets[end - 1] + 1
            fragments.append({
                "sentence_id": supplied[index].id,
                "start_offset": left, "end_offset": right,
                "source_fragment": supplied[index].text[left:right],
            })
        for (a, _, end), (b, start, _) in zip(path, path[1:]):
            gap = supplied[a].text[lines[a][1][end - 1] + 1:]
            gap += "".join(segment.text for segment in supplied[a + 1:b])
            gap += supplied[b].text[:lines[b][1][start]]
            if b - a > 24 or NEGATION.search(gap):
                return {
                    "status": "needs_review", "method": method,
                    "reason": "Omitted source span is too long or contains negation",
                    "fragments": fragments,
                }
        ids = [fragment["sentence_id"] for fragment in fragments]
        positions = {segment.id: index for index, segment in enumerate(provided)}
        selected = provided[positions[ids[0]]:positions[ids[-1]] + 1]
        if any(b.id != a.id + 1 for a, b in zip(selected, selected[1:])):
            return {"status": "needs_review", "reason": "Match crosses an unsupplied transcript gap"}
        return {
            "status": "matched", "method": method, "sentence_ids": ids,
            "fragments": fragments, "matching_version": MATCHING_VERSION,
        }
    return {"status": "unmatched", "reason": "No unique literal or anchored source match"}


def remap_candidate(candidate: dict, provided: list[TranscriptSegment]) -> tuple[dict | None, dict]:
    match = locate_quote(candidate["evidence_quote"], provided)
    audit = {**match, "original_candidate": candidate}
    if match["status"] != "matched":
        return None, audit
    available = {segment.id: segment for segment in provided}
    ids = match["sentence_ids"]
    first, last = ids[0], ids[-1]
    old_first, old_last = candidate["start_sentence_id"], candidate["end_sentence_id"]
    if old_first in available and old_last in available and old_first <= old_last:
        first, last = min(first, old_first), max(last, old_last)
    quote = candidate["evidence_quote"]
    if match["method"] != "exact":
        # Keep the model wording in the audit; the accepted quote is copied from ASR.
        quote = "".join(available[i].text for i in ids)
    corrected = {
        **candidate, "start_sentence_id": first, "end_sentence_id": last,
        "evidence_sentence_ids": ids, "evidence_quote": quote,
    }
    audit["corrected_candidate"] = corrected
    audit["source_quote_reconstructed"] = match["method"] != "exact"
    return corrected, audit
