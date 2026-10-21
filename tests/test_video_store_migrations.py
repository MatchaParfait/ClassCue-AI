from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Barrier

import pytest

from core import TranscriptSegment
import video_store
from video_store import VideoStore


LEGACY_MARKERS = """
    CREATE TABLE video_markers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
        start_ms INTEGER NOT NULL,
        end_ms INTEGER NOT NULL,
        title TEXT NOT NULL,
        note TEXT NOT NULL,
        source_quote TEXT NOT NULL,
        trigger_phrase TEXT NOT NULL,
        confidence REAL NOT NULL DEFAULT 1.0,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(video_id, start_ms, end_ms, trigger_phrase)
    )
"""

CURRENT_MARKERS = """
    CREATE TABLE video_markers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
        start_ms INTEGER NOT NULL,
        end_ms INTEGER NOT NULL,
        title TEXT NOT NULL,
        note TEXT NOT NULL,
        source_quote TEXT NOT NULL,
        trigger_phrase TEXT NOT NULL,
        confidence REAL NOT NULL DEFAULT 1.0,
        analysis_mode TEXT NOT NULL DEFAULT 'high_recall',
        review_status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(video_id, start_ms, end_ms, trigger_phrase, analysis_mode)
    )
"""


@pytest.fixture
def legacy_database(tmp_path):
    path = tmp_path / "legacy.db"
    store = VideoStore(path)
    video_id = store.create_video("legacy", "lesson.mp4", "lesson.mp4", "lesson.wav", 2000)
    store.mark_transcribed(video_id, [TranscriptSegment(1, 0, 1000, "old transcript")])
    with store.connect() as connection:
        connection.execute("DROP TABLE video_markers")
        connection.execute(LEGACY_MARKERS)
        connection.executemany(
            "INSERT INTO video_markers "
            "(id,video_id,start_ms,end_ms,title,note,source_quote,trigger_phrase,confidence,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(index, video_id, start, start + 500, f"marker{index}", "note",
              "quote", "trigger", .9, "2026-01-01 00:00:00")
             for index, start in ((10, 0), (20, 1000))],
        )
    return path, video_id


def table_rows(path, table):
    assert table in {"video_markers", "video_markers_legacy"}
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY id")]


def assert_valid(path):
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def assert_no_legacy_table(path):
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='video_markers_legacy'"
        ).fetchone() is None


@pytest.mark.parametrize("stage", ["rename", "create", "copy", "drop", "after_migration"])
def test_migration_failure_is_atomic_and_restart_can_retry(legacy_database, monkeypatch, stage):
    path, video_id = legacy_database
    original = table_rows(path, "video_markers")
    denied = []

    class FailingConnection(video_store.ClosingConnection):
        def execute(self, sql, *args, **kwargs):
            normalized = " ".join(sql.split()).upper()
            targets = {
                "rename": normalized.startswith("ALTER TABLE VIDEO_MARKERS RENAME"),
                "create": normalized.startswith("CREATE TABLE VIDEO_MARKERS ("),
                "copy": normalized.startswith("INSERT INTO VIDEO_MARKERS ("),
                "drop": normalized == "DROP TABLE VIDEO_MARKERS_LEGACY",
                "after_migration": normalized.startswith("PRAGMA TABLE_INFO(VIDEO_ANALYSIS_RUNS)"),
            }
            if targets[stage]:
                denied.append(stage)
                raise sqlite3.OperationalError(f"injected {stage}")
            return super().execute(sql, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(video_store, "ClosingConnection", FailingConnection)
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            VideoStore(path)
    assert denied == [stage]
    assert table_rows(path, "video_markers") == original
    assert_no_legacy_table(path)
    retry = VideoStore(path)
    assert [row["id"] for row in retry.list_markers(video_id)] == [10, 20]
    migrated = table_rows(path, "video_markers")
    assert all(row["analysis_mode"] == "high_recall" for row in migrated)
    assert all(row["review_status"] == "active" for row in migrated)
    for before, after in zip(original, migrated):
        assert all(after[field] == value for field, value in before.items())
    VideoStore(path)
    assert table_rows(path, "video_markers") == migrated
    assert_valid(path)


@pytest.mark.parametrize("copied", [False, True])
def test_recovers_old_non_atomic_interruption(legacy_database, copied):
    path, video_id = legacy_database
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            connection.execute("ALTER TABLE video_markers RENAME TO video_markers_legacy")
    # The old initializer could recreate an empty destination and then skip recovery.
    store = VideoStore.__new__(VideoStore)
    store.path = path
    with store.connect() as connection:
        connection.execute(CURRENT_MARKERS)
        if copied:
            connection.execute(
                "INSERT INTO video_markers "
                "(id,video_id,start_ms,end_ms,title,note,source_quote,trigger_phrase,confidence,created_at) "
                "SELECT id,video_id,start_ms,end_ms,title,note,source_quote,trigger_phrase,confidence,created_at "
                "FROM video_markers_legacy"
            )
    retry = VideoStore(path)
    assert [row["id"] for row in retry.list_markers(video_id)] == [10, 20]
    assert_no_legacy_table(path)
    assert_valid(path)


def test_recovers_rename_only_interruption(legacy_database):
    path, video_id = legacy_database
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            connection.execute("ALTER TABLE video_markers RENAME TO video_markers_legacy")
    retry = VideoStore(path)
    assert [row["id"] for row in retry.list_markers(video_id)] == [10, 20]
    assert_no_legacy_table(path)
    assert_valid(path)


def test_migration_preserves_legacy_review_status(legacy_database):
    path, video_id = legacy_database
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            connection.execute("ALTER TABLE video_markers ADD COLUMN review_status TEXT NOT NULL DEFAULT 'active'")
            connection.execute("UPDATE video_markers SET review_status='mastered' WHERE id=10")
            connection.execute("UPDATE video_markers SET review_status='excluded' WHERE id=20")
    store = VideoStore(path)
    assert store.list_markers(video_id)[0]["review_status"] == "mastered"
    assert store.list_excluded_markers(video_id)[0]["id"] == 20
    assert_valid(path)


def test_recovery_preserves_unrelated_current_markers(legacy_database):
    path, video_id = legacy_database
    VideoStore(path)
    store = VideoStore(path)
    current = store.add_marker(video_id, 500, 1000, "current", "", "new", "new", .8)
    with store.connect() as connection:
        connection.execute(
            "CREATE TABLE video_markers_legacy AS SELECT * FROM video_markers WHERE id IN (10,20)"
        )
        connection.execute("DELETE FROM video_markers WHERE id=20")
    retry = VideoStore(path)
    assert {row["id"] for row in retry.list_markers(video_id)} == {10, 20, current}
    assert_no_legacy_table(path)
    assert_valid(path)


@pytest.mark.parametrize("conflict", ["id", "unique_key", "review_status", "both_legacy"])
def test_ambiguous_recovery_keeps_both_tables(legacy_database, conflict):
    path, video_id = legacy_database
    if conflict != "both_legacy":
        store = VideoStore(path)
        with store.connect() as connection:
            connection.execute("CREATE TABLE video_markers_legacy AS SELECT * FROM video_markers")
            if conflict == "id":
                connection.execute("UPDATE video_markers SET title='current edit' WHERE id=10")
            elif conflict == "unique_key":
                connection.execute("UPDATE video_markers SET id=99 WHERE id=10")
            else:
                connection.execute("UPDATE video_markers SET review_status='mastered' WHERE id=10")
    else:
        with closing(sqlite3.connect(path)) as connection:
            with connection:
                connection.execute("CREATE TABLE video_markers_legacy AS SELECT * FROM video_markers")
    before_current = table_rows(path, "video_markers")
    before_legacy = table_rows(path, "video_markers_legacy")
    for _ in range(2):
        with pytest.raises(sqlite3.DatabaseError):
            VideoStore(path)
        assert table_rows(path, "video_markers") == before_current
        assert table_rows(path, "video_markers_legacy") == before_legacy
    assert_valid(path)


def test_direct_migration_preserves_callers_transaction_until_rollback(legacy_database):
    path, _video_id = legacy_database
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        connection.execute("UPDATE videos SET display_name='pending'")
        VideoStore._migrate_marker_modes(connection)
        assert connection.in_transaction
        connection.rollback()
        assert "analysis_mode" not in {row["name"] for row in connection.execute("PRAGMA table_info(video_markers)")}
        assert connection.execute("SELECT display_name FROM videos").fetchone()[0] != "pending"
    assert_no_legacy_table(path)


@pytest.mark.parametrize("stage", ["rename", "create", "copy", "drop"])
def test_process_exit_mid_migration_is_recoverable(legacy_database, stage):
    path, video_id = legacy_database
    before = table_rows(path, "video_markers")
    code = """
import os
import sys
import video_store

class InterruptedConnection(video_store.ClosingConnection):
    def execute(self, sql, *args, **kwargs):
        result = super().execute(sql, *args, **kwargs)
        normalized = " ".join(sql.split()).upper()
        phases = {
            "rename": normalized.startswith("ALTER TABLE VIDEO_MARKERS RENAME"),
            "create": normalized.startswith("CREATE TABLE VIDEO_MARKERS ("),
            "copy": normalized.startswith("INSERT INTO VIDEO_MARKERS ("),
            "drop": normalized == "DROP TABLE VIDEO_MARKERS_LEGACY",
        }
        if phases[sys.argv[2]]:
            os._exit(23)
        return result

video_store.ClosingConnection = InterruptedConnection
video_store.VideoStore(sys.argv[1])
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(path), stage],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert result.returncode == 23, result.stderr
    assert table_rows(path, "video_markers") == before
    assert_no_legacy_table(path)
    retry = VideoStore(path)
    assert [row["id"] for row in retry.list_markers(video_id)] == [10, 20]
    assert_valid(path)


def test_migration_preserves_autoincrement_high_water_mark(legacy_database):
    path, video_id = legacy_database
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            connection.execute("UPDATE sqlite_sequence SET seq=100 WHERE name='video_markers'")
    store = VideoStore(path)
    marker = store.add_marker(video_id, 500, 1000, "new", "", "new", "new", .9)
    assert marker == 101
    assert_valid(path)


def test_concurrent_initializers_migrate_once_without_losing_rows(legacy_database):
    path, video_id = legacy_database
    barrier = Barrier(2)

    def open_store():
        barrier.wait(timeout=5)
        store = VideoStore(path)
        return [row["id"] for row in store.list_markers(video_id)]

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(open_store) for _ in range(2)]
        assert [future.result(timeout=15) for future in futures] == [[10, 20], [10, 20]]
    assert_no_legacy_table(path)
    assert_valid(path)


def test_sqlite_backup_restore_preserves_committed_wal_data_and_review(tmp_path):
    source = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    restored = tmp_path / "restored.db"
    store = VideoStore(source)
    with store.connect() as held:
        held.execute("PRAGMA wal_autocheckpoint=0")
        held.execute("BEGIN")
        held.execute("SELECT * FROM videos").fetchall()
        video_id = store.create_video("wal", "wal.mp4", "wal.mp4", "wal.wav", 1000)
        store.mark_transcribed(video_id, [TranscriptSegment(1, 0, 1000, "snapshot")])
        marker = store.add_marker(video_id, 0, 1000, "review", "note", "quote", "test", .9)
        store.set_marker_review_status(marker, "mastered")
        assert source.with_name(source.name + "-wal").stat().st_size > 0
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as reader:
            with closing(sqlite3.connect(backup)) as destination:
                reader.backup(destination)
    store.set_marker_review_status(marker, "excluded")
    with closing(sqlite3.connect(backup.as_uri() + "?mode=ro", uri=True)) as reader:
        with closing(sqlite3.connect(restored)) as destination:
            reader.backup(destination)
    recovered = VideoStore(restored)
    assert recovered.get_segments(video_id)[0].text == "snapshot"
    assert recovered.list_markers(video_id)[0]["review_status"] == "mastered"
    assert store.list_excluded_markers(video_id)[0]["id"] == marker
    for path in (source, backup, restored):
        assert_valid(path)


def test_legacy_snapshot_can_be_restored_and_migrated(legacy_database, tmp_path):
    source, video_id = legacy_database
    backup, restored = tmp_path / "backup.db", tmp_path / "restored.db"
    original = table_rows(source, "video_markers")
    with closing(sqlite3.connect(source)) as reader:
        with closing(sqlite3.connect(backup)) as target:
            reader.backup(target)
    with closing(sqlite3.connect(source)) as connection:
        with connection:
            connection.execute("DELETE FROM video_markers")
    with closing(sqlite3.connect(backup)) as reader:
        with closing(sqlite3.connect(restored)) as target:
            reader.backup(target)
    recovered = VideoStore(restored)
    assert len(recovered.list_markers(video_id)) == len(original)
    for before, after in zip(original, table_rows(restored, "video_markers")):
        assert all(after[field] == value for field, value in before.items())
    assert table_rows(source, "video_markers") == []
    assert table_rows(backup, "video_markers") == original
    assert_valid(restored)


@pytest.mark.parametrize("status", ["failed", "transcribed"])
def test_startup_restores_successful_saved_analysis_without_rewriting_history(tmp_path, status):
    path = tmp_path / "video.db"
    store = VideoStore(path)
    video_id = store.create_video("retry", "lesson.mp4", "lesson.mp4", "lesson.wav", 1000)
    store.mark_transcribed(video_id, [TranscriptSegment(1, 0, 1000, "saved transcript")])
    store.set_status(video_id, status)
    store.set_analysis_run(video_id, "medium_recall", "completed", 0)
    store.create_job("old-failure", "video_process", video_id)
    store.update_job("old-failure", status="failed", error_message="429 WEEKLY_LIMIT_EXCEEDED")
    store.create_job("retry", "video_analysis", video_id)
    store.update_job("retry", status="completed", finished_at="2026-10-20T15:17:48+00:00")
    before = dict(store.get_analysis_run(video_id, "medium_recall"))

    recovered = VideoStore(path)

    assert recovered.get_video(video_id)["status"] == "review_ready"
    assert dict(recovered.get_analysis_run(video_id, "medium_recall")) == before
    assert recovered.get_job("old-failure")["error_message"] == "429 WEEKLY_LIMIT_EXCEEDED"
    assert recovered.get_job("old-failure")["status"] == "failed"
    assert recovered.list_markers(video_id) == []
    assert VideoStore(path).get_video(video_id)["status"] == "review_ready"
    assert_valid(path)


@pytest.mark.parametrize("condition", [
    "missing_transcript", "missing_completion", "unfinished_job", "later_failure",
    "later_interruption", "later_pending_job", "active_analysis", "active_batch",
    "processing_video", "paused_video", "later_batch_failure",
])
def test_startup_keeps_unfinished_or_later_failed_work_visible(tmp_path, condition):
    path = tmp_path / "video.db"
    store = VideoStore(path)
    video_id = store.create_video("retry", "lesson.mp4", "lesson.mp4", "lesson.wav", 1000)
    if condition != "missing_transcript":
        store.mark_transcribed(video_id, [TranscriptSegment(1, 0, 1000, "saved transcript")])
    status = {"processing_video": "processing", "paused_video": "resource_paused"}.get(condition, "failed")
    store.set_status(video_id, status)
    if condition != "missing_completion":
        store.set_analysis_run(video_id, "medium_recall", "completed", 0)
    store.create_job("retry", "video_analysis", video_id)
    store.update_job("retry", status="completed", finished_at=None if condition == "unfinished_job"
                     else "2026-10-20T15:17:48+00:00")
    if condition in {"later_failure", "later_interruption", "later_pending_job"}:
        store.create_job("later", "video_process", video_id)
        store.update_job("later", status={
            "later_failure": "failed", "later_interruption": "interrupted", "later_pending_job": "queued",
        }[condition])
    if condition == "active_analysis":
        store.set_analysis_run(video_id, "high_recall", "processing")
    if condition == "active_batch":
        store.create_job("batch", "batch_video_process")
        store.update_job("batch", status="running")
    if condition == "later_batch_failure":
        store.set_analysis_run(video_id, "high_recall", "failed")
        with store.connect() as connection:
            connection.execute(
                "UPDATE video_analysis_runs SET updated_at = '2026-10-20 15:18:00' "
                "WHERE video_id = ? AND analysis_mode = 'high_recall'", (video_id,),
            )

    assert VideoStore(path).get_video(video_id)["status"] == status
    assert_valid(path)
