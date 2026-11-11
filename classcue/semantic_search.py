"""Scoped cosine retrieval and rank fusion, retaining original source evidence."""

import heapq
import json

import numpy as np

from classcue.semantic_embeddings import DIMENSIONS
from classcue.semantic_index import coverage


CANDIDATE_LIMIT = 100


def _evidence(search, chunk):
    originals = {row["segment_id"]: row for row in search.db.execute(
        "SELECT segment_id,start_ms,end_ms,text FROM video_transcript_segments "
        "WHERE video_id=? AND end_ms>? AND start_ms<?",
        (chunk["video_id"], chunk["start_ms"], chunk["end_ms"]))}
    evidence = []
    for ref in json.loads(chunk["references_json"]):
        row = originals.get(ref["segment_id"])
        if row is None:
            raise ValueError("语义索引来源已变化，请重新建立索引")
        if search.args.start_seconds is not None and (
                row["end_ms"] <= round(search.args.start_seconds * 1000)
                or row["start_ms"] >= round(search.args.end_seconds * 1000)):
            continue
        evidence.append({"record_id": row["segment_id"], "start_ms": row["start_ms"],
                         "end_ms": row["end_ms"],
                         "text": row["text"][ref["start_char"]:ref["end_char"]]})
    return evidence


def retrieve(search, vector, version):
    vector = np.asarray(vector, dtype="<f4")
    if vector.shape != (DIMENSIONS,) or not np.isfinite(vector).all() or np.linalg.norm(vector) <= 0:
        raise ValueError("查询向量无效")
    vector = vector / np.linalg.norm(vector)
    cursor = search.db.execute(
        f"WITH scope AS ({search.scope_sql}) SELECT c.* FROM semantic_chunks c "
        "JOIN scope v ON v.id=c.video_id JOIN semantic_videos i ON i.video_id=c.video_id "
        f"WHERE i.model_version=? {search._time_filter('c.start_ms', 'c.end_ms')} ORDER BY c.id",
        [*search.scope_parameters, version])
    best, checked = [], 0
    while rows := cursor.fetchmany(256):
        matrix = np.stack([np.frombuffer(row["vector"], dtype="<f4") for row in rows])
        if matrix.shape[1] != DIMENSIONS or not np.isfinite(matrix).all():
            raise ValueError("语义索引向量无效，请重新建立索引")
        for row, score in zip(rows, matrix @ vector):
            candidate = dict(row)
            evidence = _evidence(search, row) if search.args.start_seconds is not None else None
            if evidence is not None and not evidence:
                continue
            text = "\n".join(item["text"] for item in evidence) if evidence else row["text"]
            start = min(item["start_ms"] for item in evidence) if evidence else row["start_ms"]
            end = max(item["end_ms"] for item in evidence) if evidence else row["end_ms"]
            if not search._allowed(text) or not search._passage_allowed(row["video_id"], start, end):
                continue
            checked += 1
            candidate["evidence"] = evidence
            candidate = (float(score), -row["id"], candidate)
            if len(best) < CANDIDATE_LIMIT:
                heapq.heappush(best, candidate)
            elif candidate[:2] > best[0][:2]:
                heapq.heapreplace(best, candidate)
    hits = []
    for score, _, chunk in sorted(best, reverse=True):
        evidence = chunk["evidence"] if chunk["evidence"] is not None else _evidence(search, chunk)
        if not evidence:
            continue
        start, end = min(row["start_ms"] for row in evidence), max(row["end_ms"] for row in evidence)
        position = max(start, round(search.args.start_seconds * 1000)) if search.args.start_seconds is not None else start
        hit = search._hit("subtitles", chunk["video_id"], evidence, "\n".join(row["text"] for row in evidence),
                          start, end, position, "asr_transcript")
        for item in hit["evidence"]:
            item.update(match_type="semantic", semantic_similarity=round(score, 6),
                        matched_terms=item["literal_matched_terms"], phonetic_matches=[],
                        phonetic_matches_truncated=False)
        hits.append(hit)
    result = search.finish(hits)
    result.sort(key=lambda item: (-item["semantic_similarity"], item["video_id"],
                                 item["position_seconds"], item["result_id"]))
    return result, coverage(search.db, search.videos, version), checked > CANDIDATE_LIMIT


def fuse(literal, semantic, *, diverse=True):
    merged = [{**item, "score": 1 / (60 + rank), "retrieval_methods": ["literal"]}
              for rank, item in enumerate(literal, 1)]
    for rank, item in enumerate(semantic, 1):
        target = next((candidate for candidate in merged if candidate["video_id"] == item["video_id"]
                       and candidate["start_seconds"] is not None
                       and candidate["start_seconds"] < item["end_seconds"]
                       and item["start_seconds"] < candidate["end_seconds"]), None)
        if target is None:
            merged.append({**item, "score": 1 / (60 + rank), "retrieval_methods": ["semantic"]})
            continue
        if "semantic" in target["retrieval_methods"]:
            continue
        target["score"] += 1 / (60 + rank)
        target["retrieval_methods"].append("semantic")
        target["semantic_similarity"] = item["semantic_similarity"]
        target["match_type"] = "mixed"
        evidence = {(e["source"], e["record_id"], e["text"]): e
                    for e in [*target["evidence"], *item["evidence"]]}
        target["evidence"] = list(evidence.values())[:6]
        target["evidence_count"] = len(evidence)
        target["evidence_truncated"] = (
            len(evidence) > 6 or target["evidence_truncated"] or item["evidence_truncated"])
    merged.sort(key=lambda item: (-item["score"], item["result_id"]))
    if diverse:
        occurrences = {}
        for item in merged:
            seen = occurrences.get(item["video_id"], 0)
            item["_rank"] = item["score"] / (1 + .25 * seen)
            occurrences[item["video_id"]] = seen + 1
        merged.sort(key=lambda item: (-item["_rank"], -item["score"], item["result_id"]))
        for item in merged:
            del item["_rank"]
    return merged
