"""Local, marker-owned screenshots with bounded uploads and deferred file cleanup."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from classcue.bilibili_store import BilibiliStore, encode
from classcue.asr_storage import library_scope
from classcue.paths import PROJECT_ROOT
from classcue.question_analysis import QuestionImage


def initialize_captures(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS question_captures (
        id TEXT PRIMARY KEY,
        marker_id INTEGER NOT NULL REFERENCES video_markers(id) ON DELETE CASCADE,
        video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
        captured_at_ms INTEGER NOT NULL,
        image_hash TEXT NOT NULL, mime_type TEXT NOT NULL,
        width INTEGER NOT NULL, height INTEGER NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    connection.execute("CREATE INDEX IF NOT EXISTS captures_by_marker ON question_captures(marker_id)")
    connection.execute("CREATE TABLE IF NOT EXISTS question_capture_garbage (id TEXT PRIMARY KEY)")
    connection.execute("""CREATE TRIGGER IF NOT EXISTS queue_capture_cleanup AFTER DELETE ON question_captures
        BEGIN INSERT OR IGNORE INTO question_capture_garbage(id) VALUES (OLD.id); END""")


class QuestionCaptures:
    def __init__(self, store):
        self.store = store
        self.base = Path(library_scope(PROJECT_ROOT, store.path))
        self.root = self.base / "question-captures"

    def path(self, capture_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", capture_id):
            raise ValueError("截图编号无效")
        if not self.root.resolve().is_relative_to(self.base.resolve()):
            raise ValueError("截图目录无效")
        path = (self.root / f"{capture_id}.image").resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError("截图路径无效")
        return path

    def get(self, marker_id: int, capture_id: str) -> dict:
        with self.store.connect() as connection:
            row = connection.execute("SELECT * FROM question_captures WHERE id = ? AND marker_id = ?",
                                     (capture_id, marker_id)).fetchone()
        if row is None:
            raise KeyError("截图不存在或不属于当前考点")
        return dict(row)

    def list(self, marker_id: int) -> list[dict]:
        with self.store.connect() as connection:
            rows = connection.execute("SELECT * FROM question_captures WHERE marker_id = ? ORDER BY rowid DESC",
                                      (marker_id,)).fetchall()
        return [dict(row) for row in rows]

    def add(self, marker_id: int, captured_at_ms: int, data: bytes) -> dict:
        marker = BilibiliStore(self.store).marker(marker_id)
        video = self.store.get_video(marker["video_id"])
        if captured_at_ms < 0 or captured_at_ms > int(video["duration_ms"] or 0):
            raise ValueError("截图时间超出当前视频范围")
        image = QuestionImage.from_bytes("capture", data)
        capture_id = uuid.uuid4().hex
        path = self.path(capture_id)
        self.root.mkdir(parents=True, exist_ok=True)
        self.collect()
        try:
            with self.store.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute("SELECT * FROM question_captures WHERE marker_id = ? AND captured_at_ms = ? AND image_hash = ?",
                                              (marker_id, captured_at_ms, image.sha256)).fetchone()
                if existing:
                    return dict(existing)
                if connection.execute("SELECT COUNT(*) FROM question_captures WHERE marker_id = ?", (marker_id,)).fetchone()[0] >= 20:
                    raise ValueError("本考点已保存 20 张截图，请删除不需要的截图后再试")
                with path.open("xb") as output:
                    output.write(image.data)
                connection.execute("""INSERT INTO question_captures
                    (id, marker_id, video_id, captured_at_ms, image_hash, mime_type, width, height)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", (capture_id, marker_id, marker["video_id"], captured_at_ms,
                        image.sha256, image.mime_type, image.width, image.height))
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return self.get(marker_id, capture_id)

    def delete(self, marker_id: int, capture_id: str):
        self.get(marker_id, capture_id)
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT payload_json FROM question_materials WHERE marker_id = ?", (marker_id,)).fetchone()
            if row:
                material = json.loads(row[0])
                material["capture_ids"] = [value for value in material.get("capture_ids", []) if value != capture_id]
                connection.execute("UPDATE question_materials SET payload_json = ? WHERE marker_id = ?", (encode(material), marker_id))
            connection.execute("DELETE FROM question_captures WHERE id = ? AND marker_id = ?", (capture_id, marker_id))
        self.collect()

    def collect(self):
        # Cascading deletes queue file cleanup in the same database transaction.
        # Retain failed entries for a later request or application restart.
        with self.store.connect() as connection:
            rows = connection.execute("SELECT id FROM question_capture_garbage").fetchall()
            for row in rows:
                try:
                    self.path(row["id"]).unlink(missing_ok=True)
                except (OSError, ValueError):
                    continue
                connection.execute("DELETE FROM question_capture_garbage WHERE id = ?", (row["id"],))
