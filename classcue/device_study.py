"""Explicit transfer-only replacement. Legacy share import remains non-overwriting."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from classcue import study_share
from classcue.device_protocol import semantic_digest
from classcue.device_transfer_store import TransferStore
from classcue.video_store import VideoStore

TABLES = ("video_transcript_segments", "video_markers", "video_analysis_runs",
          "video_keyword_hits", "video_ai_chunks", "video_share_imports")


def current_digest(store: VideoStore, video_id: int) -> str | None:
    row = store.get_video(video_id)
    if row is None:
        raise ValueError("视频不存在")
    if row["status"] != "review_ready":
        with store.connect() as connection:
            occupied = any(connection.execute(f"SELECT 1 FROM {table} WHERE video_id=? LIMIT 1", (video_id,)).fetchone()
                           for table in TABLES)
        return "occupied" if occupied else None
    return semantic_digest(study_share.export_package(store, video_id))


def _snapshot(connection, video_id):
    video = connection.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
    return {
        "video": {key: video[key] for key in ("content_hash", "status", "transcription_model", "duration_ms")},
        "tables": {table: [dict(row) for row in connection.execute(f"SELECT * FROM {table} WHERE video_id=?", (video_id,))]
                   for table in TABLES},
    }


def _insert(connection, table, rows):
    if not rows:
        return
    columns = list(rows[0])
    connection.executemany(
        f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
        [tuple(row[column] for column in columns) for row in rows])


def save_transfer_commit(connection, task_id, item_id, result):
    connection.execute("""CREATE TABLE IF NOT EXISTS device_transfer_commits(
        task_id TEXT NOT NULL,item_id TEXT NOT NULL,result_json TEXT NOT NULL,
        PRIMARY KEY(task_id,item_id))""")
    connection.execute("INSERT OR REPLACE INTO device_transfer_commits VALUES(?,?,?)",
                       (task_id, item_id, json.dumps(result, ensure_ascii=False)))


def transfer_commit(store: VideoStore, task_id, item_id):
    with store.connect() as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='device_transfer_commits'").fetchone():
            return None
        row = connection.execute("SELECT result_json FROM device_transfer_commits WHERE task_id=? AND item_id=?",
                                 (task_id, item_id)).fetchone()
        return json.loads(row[0]) if row else None


def import_study(store: VideoStore, journal: TransferStore, video_id: int, content: bytes,
                 *, overwrite=False, expected_digest: str | None = None,
                 commit_task=None, commit_result=None) -> str | None:
    package = study_share.decode_package(content)
    video = store.get_video(video_id)
    if video is None:
        raise ValueError("视频不存在")
    path = Path(video["video_path"])
    before = path.stat()
    if (video["content_hash"] != package.video.sha256 or before.st_size != package.video.size
            or store.file_hash(path) != package.video.sha256
            or abs(video["duration_ms"] - package.video.duration_ms) > 100):
        raise ValueError("学习包与接收视频不匹配")
    if path.stat().st_mtime_ns != before.st_mtime_ns:
        raise ValueError("视频在校验期间发生变化")
    digest = current_digest(store, video_id)
    if digest == semantic_digest(content):
        if commit_task and commit_result is not None:
            with store.connect() as connection:
                save_transfer_commit(connection, commit_task, package.video.sha256, commit_result)
        return None
    if digest is not None and not overwrite:
        raise ValueError("此视频已有学习资料，请选择覆盖或跳过")
    if digest != expected_digest:
        raise ValueError("接收确认后资料已改变，请重新预览")
    snapshot_id = None
    with store.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if current_digest(store, video_id) != digest:
            raise ValueError("接收确认后资料已改变，请重新预览")
        if digest is not None:
            snapshot = _snapshot(connection, video_id)
            snapshot["library_path"] = str(Path(store.path).resolve())
            snapshot_id = journal.snapshot(video_id, snapshot)
        for table in TABLES:
            connection.execute(f"DELETE FROM {table} WHERE video_id=?", (video_id,))
        mapping = {}
        for run in package.analysis_runs:
            mode = run.analysis_mode
            if mode.startswith("custom_"):
                mode = f"custom_{uuid4().hex}"
                config = {"name": run.name, "base_mode": "medium_recall", "rules": [], "notes": ""}
                connection.execute("INSERT INTO recall_schemes(id,name,config_json,revision,archived) VALUES(?,?,?,?,1)",
                                   (mode, run.name, json.dumps(config, ensure_ascii=False), max(1, run.scheme_revision)))
            mapping[run.analysis_mode] = mode
            connection.execute(
                "INSERT INTO video_analysis_runs(video_id,analysis_mode,status,candidate_count,scheme_revision) VALUES(?,?,'completed',?,?)",
                (video_id, mode, sum(marker.analysis_mode == run.analysis_mode for marker in package.markers),
                 max(1, run.scheme_revision) if mode.startswith("custom_") else 0))
        connection.executemany(
            "INSERT INTO video_transcript_segments(video_id,segment_id,start_ms,end_ms,text,words_json) VALUES(?,?,?,?,?,?)",
            [(video_id, s.id, s.start_ms, s.end_ms, s.text, json.dumps([word.model_dump() for word in s.words], ensure_ascii=False))
             for s in package.subtitles])
        connection.executemany(
            """INSERT INTO video_markers(video_id,start_ms,end_ms,title,note,source_quote,trigger_phrase,
                confidence,analysis_mode,user_start_ms,importance,review_status) VALUES(?,?,?,?,?,?,?,?,?,?,?,'active')""",
            [(video_id, m.start_ms, m.end_ms, m.title, m.note, m.source_quote, m.trigger_phrase, m.confidence,
              mapping[m.analysis_mode], m.user_start_ms, m.importance) for m in package.markers])
        connection.execute("UPDATE videos SET status='review_ready',transcription_model=?,duration_ms=? WHERE id=?",
                           (package.transcription_model, package.video.duration_ms, video_id))
        connection.execute("INSERT INTO video_share_imports(video_id,package_hash) VALUES(?,?)",
                           (video_id, study_share.package_digest(package)))
        if commit_task and commit_result is not None:
            commit_result["snapshot_id"] = snapshot_id
            save_transfer_commit(connection, commit_task, package.video.sha256, commit_result)
    return snapshot_id


def restore_study(store: VideoStore, journal: TransferStore, snapshot_id: str):
    snapshot = journal.snapshot_value(snapshot_id)
    video_id = snapshot["video_id"]
    value = json.loads(snapshot["body"])
    if value.get("library_path") and value["library_path"] != str(Path(store.path).resolve()):
        raise ValueError("覆盖备份属于另一个视频库，请切回原视频库")
    with store.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        video = connection.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
        if video is None or video["content_hash"] != value["video"]["content_hash"]:
            raise ValueError("原视频已不存在，无法恢复此备份")
        for table in TABLES:
            connection.execute(f"DELETE FROM {table} WHERE video_id=?", (video_id,))
            _insert(connection, table, value["tables"][table])
        metadata = value["video"]
        connection.execute("UPDATE videos SET status=?,transcription_model=?,duration_ms=? WHERE id=?",
                           (metadata["status"], metadata["transcription_model"], metadata["duration_ms"], video_id))
    journal.restored(snapshot_id)
    return video_id
