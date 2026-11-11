"""Regenerable subtitle vectors with exact source references and atomic publication."""

import hashlib
import json
import sqlite3
import time

import numpy as np

from classcue.processing_control import ProcessingCancelled
from classcue import semantic_embeddings as embeddings


CHUNK_VERSION = "subtitle-chunks-v1"
CHUNK_CHARACTERS = 500
CHUNK_SECONDS = 90
BATCH_SIZE = 8


def initialize_semantic_index(db):
    db.execute(
        "CREATE TABLE IF NOT EXISTS semantic_videos ("
        "video_id INTEGER PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,"
        "source_version TEXT NOT NULL,model_version TEXT NOT NULL,chunk_count INTEGER NOT NULL,"
        "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
    db.execute(
        "CREATE TABLE IF NOT EXISTS semantic_chunks ("
        "id INTEGER PRIMARY KEY,video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,"
        "ordinal INTEGER NOT NULL,start_ms INTEGER NOT NULL,end_ms INTEGER NOT NULL,"
        "text TEXT NOT NULL,references_json TEXT NOT NULL,vector BLOB NOT NULL,"
        "UNIQUE(video_id,ordinal))")
    for suffix, event, identity in (
        ("insert", "INSERT", "new.video_id"), ("delete", "DELETE", "old.video_id"),
        ("update", "UPDATE OF text,start_ms,end_ms,segment_id", "old.video_id"),
    ):
        db.execute(
            f"CREATE TRIGGER IF NOT EXISTS semantic_subtitle_{suffix} AFTER {event} "
            f"ON video_transcript_segments BEGIN DELETE FROM semantic_chunks WHERE video_id={identity};"
            f"DELETE FROM semantic_videos WHERE video_id={identity}; END")
    db.execute(
        "CREATE TRIGGER IF NOT EXISTS semantic_subtitle_reassign AFTER UPDATE OF video_id "
        "ON video_transcript_segments BEGIN "
        "DELETE FROM semantic_chunks WHERE video_id IN (old.video_id,new.video_id);"
        "DELETE FROM semantic_videos WHERE video_id IN (old.video_id,new.video_id); END")


def model_version():
    return CHUNK_VERSION + "/" + embeddings.model_status()["model_version"]


def source_version(rows):
    return hashlib.sha256(json.dumps(
        [(r["segment_id"], r["start_ms"], r["end_ms"], r["text"]) for r in rows],
        ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def read_segments(db, video_id):
    return [dict(row) for row in db.execute(
        "SELECT segment_id,start_ms,end_ms,text FROM video_transcript_segments "
        "WHERE video_id=? ORDER BY start_ms,segment_id", (video_id,))]


def subtitle_chunks(rows):
    pieces = []
    for row in rows:
        for start in range(0, len(row["text"]), CHUNK_CHARACTERS):
            pieces.append({**row, "text": row["text"][start:start + CHUNK_CHARACTERS],
                           "start_char": start, "end_char": min(len(row["text"]), start + CHUNK_CHARACTERS)})
    chunks, current, size = [], [], 0

    def emit():
        return {"text": "\n".join(r["text"] for r in current),
                "start_ms": min(r["start_ms"] for r in current),
                "end_ms": max(r["end_ms"] for r in current),
                "references": [{key: row[key] for key in ("segment_id", "start_char", "end_char")}
                               for row in current]}

    for piece in pieces:
        if current and (size + len(piece["text"]) + 1 > CHUNK_CHARACTERS
                        or piece["end_ms"] - current[0]["start_ms"] > CHUNK_SECONDS * 1000
                        or piece["start_ms"] - current[-1]["end_ms"] > 15_000):
            chunks.append(emit())
            overlap = current[-1:] if len(current) > 1 and len(current[-1]["text"]) <= 100 else []
            if overlap and (len(overlap[0]["text"]) + len(piece["text"]) + 1 > CHUNK_CHARACTERS
                            or piece["end_ms"] - overlap[0]["start_ms"] > CHUNK_SECONDS * 1000
                            or piece["start_ms"] - overlap[0]["end_ms"] > 15_000):
                overlap = []
            current, size = overlap, sum(len(r["text"]) for r in overlap)
        current.append(piece)
        size += len(piece["text"]) + 1
    if current:
        chunks.append(emit())
    return chunks


def coverage(db, video_ids, version=None):
    ids = set(video_ids)
    available = {r[0] for r in db.execute(
        "SELECT DISTINCT s.video_id FROM video_transcript_segments s "
        "JOIN videos v ON v.id=s.video_id WHERE v.is_secondary=0") if r[0] in ids}
    records = [dict(r) for r in db.execute("SELECT * FROM semantic_videos") if r["video_id"] in available]
    indexed = [r for r in records if r["model_version"] == (version or model_version())]
    return {"video_count": len(ids), "with_subtitles": len(available),
            "missing_subtitles": len(ids - available), "indexed_videos": len(indexed),
            "pending_videos": len(available) - len(indexed),
            "chunk_count": sum(r["chunk_count"] for r in indexed)}


def build_index(store, video_ids, progress=None, should_stop=None):
    if not embeddings.model_status()["ready"]:
        raise RuntimeError("请先准备 Qwen 检索模型")
    version = model_version()
    summary = {"completed": 0, "skipped": 0, "missing_subtitles": 0, "chunks": 0}
    started = time.monotonic()
    ids = list(dict.fromkeys(video_ids))
    for index, video_id in enumerate(ids):
        if should_stop and should_stop():
            raise ProcessingCancelled("已停止语义索引，已完成的视频结果保留")
        with store.connect() as db:
            db.execute("BEGIN")
            video = db.execute("SELECT is_secondary FROM videos WHERE id=?", (video_id,)).fetchone()
            if video is None or video["is_secondary"]:
                raise ValueError("索引目标已变化，请重新选择课程")
            rows = read_segments(db, video_id)
            stamp = source_version(rows)
            saved = db.execute("SELECT * FROM semantic_videos WHERE video_id=?", (video_id,)).fetchone()
        if not rows:
            summary["missing_subtitles"] += 1
            continue
        if saved and saved["source_version"] == stamp and saved["model_version"] == version:
            summary["skipped"] += 1
            summary["chunks"] += saved["chunk_count"]
            continue
        chunks = subtitle_chunks(rows)
        vectors = []
        for offset in range(0, len(chunks), BATCH_SIZE):
            batch = chunks[offset:offset + BATCH_SIZE]
            if progress:
                fraction = (index + offset / max(1, len(chunks))) / max(1, len(ids))
                progress(fraction, f"建立课程索引 · 视频 {index + 1}/{len(ids)} · "
                         f"片段 {min(offset + len(batch), len(chunks))}/{len(chunks)}", "indexing")
            result = embeddings.encoder.encode([chunk["text"] for chunk in batch], should_stop=should_stop)
            if result.shape != (len(batch), embeddings.DIMENSIONS) or not np.isfinite(result).all():
                raise ValueError("检索模型返回无效向量")
            norms = np.linalg.norm(result, axis=1, keepdims=True)
            if np.any(norms <= 0):
                raise ValueError("检索模型返回零向量")
            vectors.extend((result / norms).astype("<f4"))
        if should_stop and should_stop():
            raise ProcessingCancelled("已停止语义索引，已完成的视频结果保留")
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if source_version(read_segments(db, video_id)) != stamp or model_version() != version:
                raise ValueError("字幕或检索模型已变化，请重新建立课程索引")
            db.execute("DELETE FROM semantic_chunks WHERE video_id=?", (video_id,))
            db.executemany(
                "INSERT INTO semantic_chunks(video_id,ordinal,start_ms,end_ms,text,references_json,vector) "
                "VALUES(?,?,?,?,?,?,?)",
                [(video_id, i, c["start_ms"], c["end_ms"], c["text"],
                  json.dumps(c["references"]), v.tobytes()) for i, (c, v) in enumerate(zip(chunks, vectors))])
            db.execute(
                "INSERT INTO semantic_videos(video_id,source_version,model_version,chunk_count) VALUES(?,?,?,?) "
                "ON CONFLICT(video_id) DO UPDATE SET source_version=excluded.source_version,"
                "model_version=excluded.model_version,chunk_count=excluded.chunk_count,updated_at=CURRENT_TIMESTAMP",
                (video_id, stamp, version, len(chunks)))
        summary["completed"] += 1
        summary["chunks"] += len(chunks)
    summary["elapsed_seconds"] = round(time.monotonic() - started, 3)
    if progress:
        progress(1, f"课程索引已完成 · {summary['chunks']} 个字幕片段", "completed")
    return summary
