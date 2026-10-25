"""Durable, marker-scoped Bilibili study evidence in the existing library."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from classcue.video_store import VideoStore


VERSION = "bilibili-study-v1"


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


class StudyContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    course: str = Field(default="", max_length=200)
    notes: str = Field(default="", max_length=4000)


def initialize_bilibili(connection) -> None:
    statements = (
        """CREATE TABLE IF NOT EXISTS bili_marker_context (
            marker_id INTEGER PRIMARY KEY REFERENCES video_markers(id) ON DELETE CASCADE,
            payload_json TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS bili_study_runs (
            id TEXT PRIMARY KEY,
            video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            marker_id INTEGER REFERENCES video_markers(id) ON DELETE SET NULL,
            input_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        "CREATE INDEX IF NOT EXISTS bili_runs_by_marker ON bili_study_runs(marker_id, created_at)",
        """CREATE TABLE IF NOT EXISTS bili_run_trace (
            run_id TEXT NOT NULL REFERENCES bili_study_runs(id) ON DELETE CASCADE,
            seq INTEGER NOT NULL, payload_json TEXT NOT NULL,
            PRIMARY KEY(run_id, seq)
        )""",
        """CREATE TABLE IF NOT EXISTS bili_read_cache (
            key TEXT PRIMARY KEY, payload_json TEXT NOT NULL, saved_at REAL NOT NULL,
            video_id INTEGER REFERENCES videos(id) ON DELETE CASCADE
        )""",
        """CREATE TABLE IF NOT EXISTS bili_controls (
            key TEXT PRIMARY KEY, payload_json TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS bili_frames (
            id TEXT PRIMARY KEY,
            marker_id INTEGER NOT NULL REFERENCES video_markers(id) ON DELETE CASCADE,
            video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            run_id TEXT NOT NULL REFERENCES bili_study_runs(id) ON DELETE CASCADE,
            unit_id TEXT NOT NULL, bvid TEXT NOT NULL, cid INTEGER NOT NULL, page INTEGER NOT NULL,
            requested_ms INTEGER NOT NULL, captured_ms INTEGER NOT NULL, time_source TEXT NOT NULL,
            image_hash TEXT NOT NULL, mime_type TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
            UNIQUE(run_id, unit_id, requested_ms)
        )""",
        "CREATE TABLE IF NOT EXISTS bili_frame_garbage (id TEXT PRIMARY KEY)",
        """CREATE TRIGGER IF NOT EXISTS queue_bili_frame_cleanup AFTER DELETE ON bili_frames
            BEGIN INSERT OR IGNORE INTO bili_frame_garbage(id) VALUES (OLD.id); END""",
    )
    for statement in statements:
        connection.execute(statement)


class BilibiliStore:
    def __init__(self, store: VideoStore):
        self.store = store

    def marker(self, marker_id: int) -> dict:
        with self.store.connect() as connection:
            row = connection.execute("SELECT * FROM video_markers WHERE id = ?", (marker_id,)).fetchone()
        if row is None:
            raise KeyError("考点不存在或已被重新分析替换")
        return dict(row)

    def context(self, marker_id: int) -> dict:
        self.marker(marker_id)
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM bili_marker_context WHERE marker_id = ?", (marker_id,),
            ).fetchone()
        saved = json.loads(row[0]) if row else {}
        return StudyContext.model_validate({
            key: saved[key] for key in StudyContext.model_fields if key in saved
        }).model_dump()

    def save_context(self, marker_id: int, context: StudyContext) -> dict:
        self.marker(marker_id)
        value = {key: text.strip() for key, text in context.model_dump().items()}
        with self.store.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO bili_marker_context(marker_id, payload_json) VALUES (?, ?)",
                (marker_id, encode(value)),
            )
        return value

    def snapshot(self, marker_id: int, *, include_question: bool = True) -> dict:
        marker = self.marker(marker_id)
        segments = self.store.get_segments(marker["video_id"])
        transcript = [
            {"id": item.id, "start_ms": item.start_ms, "end_ms": item.end_ms, "text": item.text}
            for item in segments
        ]
        snapshot = {
            "version": VERSION,
            "marker": {key: marker[key] for key in (
                "id", "video_id", "start_ms", "end_ms", "title", "note", "source_quote", "analysis_mode",
            )},
            "context": self.context(marker_id),
            "classroom_segments": [
                item for item in transcript if item["end_ms"] > marker["start_ms"] - 20000
                and item["start_ms"] < marker["end_ms"] + 20000
            ],
            "transcript_hash": digest(transcript),
        }
        if include_question:
            from classcue.question_store import QuestionStore
            question = QuestionStore(self.store).search_projection(marker_id, snapshot)
            if question is not None:
                snapshot["question"] = question
        return snapshot

    def create_run(self, snapshot: dict, profile: dict) -> dict:
        run = {
            "id": uuid.uuid4().hex, "status": "planning", "job_id": None,
            "input": snapshot, "input_hash": digest(snapshot), "profile": profile,
            "version": VERSION, "queries": [], "candidates": [], "error": "",
            "visual_verification": True,
        }
        marker = snapshot["marker"]
        with self.store.connect() as connection:
            connection.execute(
                """INSERT INTO bili_study_runs(id, video_id, marker_id, input_hash, payload_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (run["id"], marker["video_id"], marker["id"], run["input_hash"], encode(run)),
            )
        return run

    def save_run(self, run: dict) -> None:
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE bili_study_runs SET payload_json = ? WHERE id = ?", (encode(run), run["id"]),
            )
            for event in run.get("trace", []):
                connection.execute("INSERT OR IGNORE INTO bili_run_trace(run_id, seq, payload_json) VALUES (?, ?, ?)",
                                   (run["id"], event["seq"], encode(event)))

    def get_run(self, marker_id: int, run_id: str) -> dict:
        with self.store.connect() as connection:
            row = connection.execute(
                """SELECT r.payload_json, j.status AS job_status FROM bili_study_runs r
                   LEFT JOIN processing_jobs j ON j.id = json_extract(r.payload_json, '$.job_id')
                   WHERE r.id = ? AND r.marker_id = ?""",
                (run_id, marker_id),
            ).fetchone()
        if row is None:
            raise KeyError("搜索记录不存在或不属于此考点")
        run = json.loads(row["payload_json"])
        if run["status"] in {"planning", "searching", "screening", "reading", "checking",
                             "locating", "capturing", "visual_checking"} and row["job_status"] not in {"queued", "running"}:
            run["status"] = "interrupted"
        return run

    def runs(self, marker_id: int) -> list[dict]:
        from classcue.bilibili_harness import current_protocol

        fingerprint = digest(self.snapshot(marker_id))
        with self.store.connect() as connection:
            rows = connection.execute(
                """SELECT r.payload_json, j.status AS job_status FROM bili_study_runs r
                   LEFT JOIN processing_jobs j ON j.id = json_extract(r.payload_json, '$.job_id')
                   WHERE r.marker_id = ? ORDER BY r.created_at DESC, r.rowid DESC LIMIT 20""",
                (marker_id,),
            ).fetchall()
        result = []
        for row in rows:
            run = json.loads(row["payload_json"])
            run["stale"] = run["input_hash"] != fingerprint
            run["legacy"] = run.get("version") != current_protocol(run)
            if run["status"] in {"planning", "searching", "screening", "reading", "checking",
                                 "locating", "capturing", "visual_checking"}:
                if row["job_status"] not in {"queued", "running"}:
                    run["status"] = "interrupted"
            for candidate in run.get("candidates", []):
                candidate.pop("metadata", None)
                candidate.pop("unit_queue", None)
                for unit in candidate.get("units", []):
                    unit.pop("subtitle", None)
                    unit.pop("localization", None)
                    if unit.get("visual"):
                        unit["visual"].pop("batches", None)
                    if unit.get("frames"):
                        from classcue.bilibili_visual import frame_metadata
                        unit["frames"] = [frame_metadata(frame) for frame in unit["frames"]]
                    verification = unit.pop("verification", {})
                    if not unit.get("coverage") and verification.get("coverage"):
                        unit["coverage"] = verification["coverage"]
            result.append(run)
        return result

    def control(self, key: str, default=None):
        with self.store.connect() as connection:
            row = connection.execute("SELECT payload_json FROM bili_controls WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_control(self, key: str, value) -> None:
        with self.store.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO bili_controls(key, payload_json) VALUES (?, ?)", (key, encode(value)),
            )

    def cache_get(self, key: str, ttl: int = 21600):
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM bili_read_cache WHERE key = ? AND saved_at > ?",
                (key, time.time() - ttl),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def cache_put(self, key: str, value, video_id: int | None = None) -> None:
        with self.store.connect() as connection:
            connection.execute("DELETE FROM bili_read_cache WHERE saved_at < ?", (time.time() - 21600,))
            connection.execute(
                "INSERT OR REPLACE INTO bili_read_cache(key, payload_json, saved_at, video_id) VALUES (?, ?, ?, ?)",
                (key, encode(value), time.time(), video_id),
            )
