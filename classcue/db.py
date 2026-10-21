"""Small SQLite persistence layer."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from classcue.core import CandidateDecision, TranscriptSegment
from classcue.sqlite_connection import ClosingConnection


class Database:
    def __init__(self, path: str | Path = "data/classroom.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, factory=ClosingConnection)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS recordings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename TEXT NOT NULL,
                    audio_path TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS transcript_segments (
                    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recording_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
                    segment_id INTEGER NOT NULL,
                    start_ms INTEGER NOT NULL,
                    end_ms INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    words_json TEXT,
                    UNIQUE(recording_id, segment_id)
                );
                CREATE TABLE IF NOT EXISTS knowledge_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recording_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
                    start_ms INTEGER NOT NULL,
                    end_ms INTEGER NOT NULL,
                    trigger_phrase TEXT NOT NULL,
                    trigger_sentence_ids TEXT NOT NULL,
                    start_sentence_id INTEGER,
                    end_sentence_id INTEGER,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    source_quote TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS knowledge_points (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    candidate_id INTEGER REFERENCES knowledge_candidates(id),
                    recording_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
                    start_ms INTEGER NOT NULL,
                    end_ms INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    note TEXT NOT NULL,
                    source_quote TEXT NOT NULL,
                    trigger_phrase TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

    def create_recording(self, filename: str, audio_path: str, duration_ms: int = 0) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO recordings(filename, audio_path, duration_ms) VALUES (?, ?, ?)",
                (filename, audio_path, duration_ms),
            )
            return int(cursor.lastrowid)

    def replace_transcript(self, recording_id: int, segments: list[TranscriptSegment]) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM transcript_segments WHERE recording_id = ?", (recording_id,))
            connection.executemany(
                "INSERT INTO transcript_segments(recording_id, segment_id, start_ms, end_ms, text, words_json) VALUES (?, ?, ?, ?, ?, ?)",
                [(recording_id, segment.id, segment.start_ms, segment.end_ms, segment.text, json.dumps(segment.words or [], ensure_ascii=False)) for segment in segments],
            )

    def get_segments(self, recording_id: int) -> list[TranscriptSegment]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM transcript_segments WHERE recording_id = ? ORDER BY start_ms", (recording_id,)).fetchall()
        return [TranscriptSegment(int(row["segment_id"]), int(row["start_ms"]), int(row["end_ms"]), row["text"], json.loads(row["words_json"] or "[]")) for row in rows]

    def create_candidate(self, recording_id: int, trigger_phrase: str, decision: CandidateDecision, segments: list[TranscriptSegment]) -> int:
        if not decision.start_sentence_id or not decision.end_sentence_id:
            raise ValueError("考点候选缺少有效句子区间")
        by_id = {segment.id: segment for segment in segments}
        start_segment, end_segment = by_id[decision.start_sentence_id], by_id[decision.end_sentence_id]
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO knowledge_candidates(recording_id, start_ms, end_ms, trigger_phrase, trigger_sentence_ids,
                start_sentence_id, end_sentence_id, title, summary, source_quote, confidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (recording_id, start_segment.start_ms, end_segment.end_ms, trigger_phrase, json.dumps(decision.trigger_sentence_ids), decision.start_sentence_id, decision.end_sentence_id, decision.knowledge_point, decision.summary, decision.exam_signal_quote, decision.confidence),
            )
            return int(cursor.lastrowid)

    def list_candidates(self, recording_id: int) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM knowledge_candidates WHERE recording_id = ? AND status = 'pending' ORDER BY start_ms DESC",
                (recording_id,),
            ).fetchall()

    def confirm_candidate(self, candidate_id: int, title: str, note: str, start_ms: int, end_ms: int) -> int:
        with self.connect() as connection:
            candidate = connection.execute("SELECT * FROM knowledge_candidates WHERE id = ?", (candidate_id,)).fetchone()
            if not candidate:
                raise ValueError("候选考点不存在")
            cursor = connection.execute(
                """INSERT INTO knowledge_points(candidate_id, recording_id, start_ms, end_ms, title, note, source_quote, trigger_phrase)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (candidate_id, candidate["recording_id"], start_ms, end_ms, title.strip(), note.strip(), candidate["source_quote"], candidate["trigger_phrase"]),
            )
            connection.execute("UPDATE knowledge_candidates SET status = 'confirmed' WHERE id = ?", (candidate_id,))
            return int(cursor.lastrowid)

    def list_points(self, recording_id: int) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute("SELECT * FROM knowledge_points WHERE recording_id = ? ORDER BY start_ms", (recording_id,)).fetchall()

    def update_point(self, point_id: int, title: str, note: str, start_ms: int, end_ms: int) -> None:
        if end_ms <= start_ms:
            raise ValueError("结束时间必须晚于开始时间")
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE knowledge_points SET title = ?, note = ?, start_ms = ?, end_ms = ? WHERE id = ?",
                (title.strip(), note.strip(), start_ms, end_ms, point_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("已保存考点不存在")
