"""SQLite persistence isolated from the original audio-only demo."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import os
import shutil
import uuid
from pathlib import Path

from classcue.core import TranscriptSegment, TriggerMatch
from classcue.sqlite_connection import ClosingConnection
from classcue.subtitle_search import initialize_subtitle_search, search_subtitles


def source_directory_key(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).expanduser().resolve()))


class VideoStore:
    def __init__(self, path: str | Path = "data/video_demo.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, factory=ClosingConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _init_schema(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS videos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content_hash TEXT NOT NULL UNIQUE,
                    filename TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    video_path TEXT NOT NULL,
                    source_folder TEXT NOT NULL DEFAULT '',
                    audio_path TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    source_size INTEGER NOT NULL DEFAULT 0,
                    source_mtime_ns INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'uploaded',
                    library_folder_id INTEGER REFERENCES video_folders(id) ON DELETE SET NULL,
                    library_order INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS video_folders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    parent_id INTEGER REFERENCES video_folders(id) ON DELETE SET NULL,
                    library_order INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS video_transcript_segments (
                    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
                    segment_id INTEGER NOT NULL,
                    start_ms INTEGER NOT NULL,
                    end_ms INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    words_json TEXT,
                    UNIQUE(video_id, segment_id)
                );
                CREATE TABLE IF NOT EXISTS video_markers (
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
                );
                CREATE TABLE IF NOT EXISTS video_keyword_hits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
                    segment_id INTEGER NOT NULL,
                    start_ms INTEGER NOT NULL,
                    end_ms INTEGER NOT NULL,
                    phrase TEXT NOT NULL,
                    strength TEXT NOT NULL,
                    category TEXT NOT NULL,
                    source_quote TEXT NOT NULL,
                    UNIQUE(video_id, segment_id, phrase)
                );
                CREATE TABLE IF NOT EXISTS video_analysis_runs (
                    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
                    analysis_mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    candidate_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(video_id, analysis_mode)
                );
                CREATE TABLE IF NOT EXISTS video_share_imports (
                    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
                    package_hash TEXT NOT NULL,
                    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(video_id, package_hash)
                );
                CREATE TABLE IF NOT EXISTS recall_schemes (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    archived INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS video_experiments (
                    id TEXT PRIMARY KEY,
                    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    analysis_mode TEXT NOT NULL,
                    options_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'queued',
                    candidate_count INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS llm_transcript_snapshots (
                    hash TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS llm_experiment_calls (
                    id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL REFERENCES video_experiments(id) ON DELETE CASCADE,
                    chunk_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    parent_call_id TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS llm_calls_by_experiment
                    ON llm_experiment_calls(experiment_id);
                CREATE TABLE IF NOT EXISTS llm_experiment_steps (
                    experiment_id TEXT NOT NULL REFERENCES video_experiments(id) ON DELETE CASCADE,
                    step_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(experiment_id, step_id)
                );
                CREATE TABLE IF NOT EXISTS llm_experiment_results (
                    experiment_id TEXT PRIMARY KEY REFERENCES video_experiments(id) ON DELETE CASCADE,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS video_ai_chunks (
                    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
                    analysis_mode TEXT NOT NULL,
                    chunk_key TEXT NOT NULL,
                    selection_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(video_id, analysis_mode, chunk_key)
                );
                CREATE TABLE IF NOT EXISTS video_processing_metrics (
                    video_id INTEGER PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
                    transcription_seconds REAL NOT NULL DEFAULT 0,
                    ai_seconds REAL NOT NULL DEFAULT 0,
                    ai_chunks INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS processing_jobs (
                    id TEXT PRIMARY KEY,
                    video_id INTEGER REFERENCES videos(id) ON DELETE SET NULL,
                    job_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL DEFAULT '',
                    progress REAL NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    started_at TEXT,
                    finished_at TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS recycle_bin (
                    operation_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    undone_at TEXT
                );
                """
            )
            # Keep existing-schema upgrades in one transaction after table bootstrap.
            connection.execute("BEGIN IMMEDIATE")
            self._migrate_video_metadata(connection)
            job_columns = {row["name"] for row in connection.execute("PRAGMA table_info(processing_jobs)")}
            if "request_json" not in job_columns:
                connection.execute("ALTER TABLE processing_jobs ADD COLUMN request_json TEXT NOT NULL DEFAULT ''")
            if "resource_json" not in job_columns:
                connection.execute("ALTER TABLE processing_jobs ADD COLUMN resource_json TEXT")
            if "preparation_json" not in job_columns:
                connection.execute("ALTER TABLE processing_jobs ADD COLUMN preparation_json TEXT")
            if "cleanup_json" not in job_columns:
                connection.execute("ALTER TABLE processing_jobs ADD COLUMN cleanup_json TEXT")
            self._migrate_library_metadata(connection)
            folder_columns = {row["name"] for row in connection.execute("PRAGMA table_info(video_folders)")}
            if "import_source" not in folder_columns:
                connection.execute("ALTER TABLE video_folders ADD COLUMN import_source TEXT")
                sources: dict[int, set[str]] = {}
                owners: dict[str, set[int]] = {}
                for row in connection.execute(
                    "SELECT id, library_folder_id, source_folder FROM videos WHERE library_folder_id IS NOT NULL"
                ):
                    raw = row["source_folder"]
                    folder_id = int(row["library_folder_id"])
                    if not raw or not Path(raw).is_absolute():
                        sources.setdefault(folder_id, set()).add(f"unknown:{row['id']}")
                        continue
                    try:
                        key = source_directory_key(raw)
                    except OSError:
                        sources.setdefault(folder_id, set()).add(f"unknown:{row['id']}")
                        continue
                    sources.setdefault(folder_id, set()).add(key)
                    owners.setdefault(key, set()).add(folder_id)
                for folder_id, keys in sources.items():
                    if len(keys) == 1 and len(owners.get(next(iter(keys)), set())) == 1:
                        connection.execute(
                            "UPDATE video_folders SET import_source = ? WHERE id = ?",
                            (next(iter(keys)), folder_id),
                        )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS folders_by_import_source ON video_folders(import_source)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS videos_by_library_folder ON videos(library_folder_id)"
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(videos)")}
            if "transcription_model" not in columns:
                connection.execute("ALTER TABLE videos ADD COLUMN transcription_model TEXT NOT NULL DEFAULT ''")
            self._migrate_marker_modes(connection)
            self._migrate_marker_review_status(connection)
            self._migrate_marker_jump_time(connection)
            self._migrate_marker_importance(connection)
            run_columns = {row["name"] for row in connection.execute("PRAGMA table_info(video_analysis_runs)")}
            for column in ("analysis_signature", "experiment_id"):
                if column not in run_columns:
                    connection.execute(f"ALTER TABLE video_analysis_runs ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
            if "scheme_revision" not in run_columns:
                connection.execute("ALTER TABLE video_analysis_runs ADD COLUMN scheme_revision INTEGER NOT NULL DEFAULT 0")
            connection.execute(
                """
                INSERT OR IGNORE INTO video_analysis_runs(video_id, analysis_mode, status, candidate_count)
                SELECT video_id, 'high_recall', 'completed', COUNT(*) FROM video_markers
                WHERE analysis_mode = 'high_recall' GROUP BY video_id
                """
            )
            initialize_subtitle_search(connection)
            self._restore_completed_analysis_status(connection)

    @staticmethod
    def _restore_completed_analysis_status(connection: sqlite3.Connection) -> int:
        """Repair old saved-analysis successes without concealing later failures."""
        return connection.execute(
            """
            UPDATE videos SET status = 'review_ready'
            WHERE status IN ('failed', 'transcribed')
                AND EXISTS (
                    SELECT 1 FROM video_transcript_segments s WHERE s.video_id = videos.id
                )
                AND EXISTS (
                    SELECT 1 FROM video_analysis_runs r
                    WHERE r.video_id = videos.id AND r.status = 'completed'
                )
                AND NOT EXISTS (
                    SELECT 1 FROM video_analysis_runs r
                    WHERE r.video_id = videos.id AND r.status = 'processing'
                )
                AND NOT EXISTS (
                    SELECT 1 FROM processing_jobs
                    WHERE status IN ('queued', 'running', 'resource_paused')
                )
                AND EXISTS (
                    SELECT 1 FROM processing_jobs j
                    WHERE j.video_id = videos.id AND j.job_type = 'video_analysis'
                        AND j.status = 'completed' AND j.finished_at IS NOT NULL
                        AND NOT EXISTS (
                            SELECT 1 FROM video_analysis_runs r
                            WHERE r.video_id = videos.id AND r.status = 'failed'
                                AND julianday(r.updated_at) > julianday(j.finished_at)
                        )
                        AND j.rowid = (
                            SELECT MAX(latest.rowid) FROM processing_jobs latest
                            WHERE latest.video_id = videos.id
                        )
                )
            """
        ).rowcount

    @staticmethod
    def _migrate_library_metadata(connection: sqlite3.Connection) -> None:
        """Add user-facing folders and ordering without invalidating old DBs."""
        # ``videos`` may predate the folder feature.  SQLite allows adding
        # nullable foreign keys here; the FK itself is enforced for new writes.
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(videos)")}
        if "library_folder_id" not in columns:
            connection.execute(
                "ALTER TABLE videos ADD COLUMN library_folder_id INTEGER REFERENCES video_folders(id) ON DELETE SET NULL"
            )
        if "library_order" not in columns:
            connection.execute(
                "ALTER TABLE videos ADD COLUMN library_order INTEGER NOT NULL DEFAULT 0"
            )
            # Initialize only a newly added column; zero is a valid saved position.
            connection.execute(
                """
                UPDATE videos
                SET library_order = id
                WHERE library_order = 0
                """
            )

    @staticmethod
    def _migrate_video_metadata(connection: sqlite3.Connection) -> None:
        """Add human-facing metadata without invalidating existing databases."""
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(videos)")}
        if "display_name" not in columns:
            connection.execute("ALTER TABLE videos ADD COLUMN display_name TEXT NOT NULL DEFAULT ''")
        if "source_folder" not in columns:
            connection.execute("ALTER TABLE videos ADD COLUMN source_folder TEXT NOT NULL DEFAULT ''")
        if "source_size" not in columns:
            connection.execute("ALTER TABLE videos ADD COLUMN source_size INTEGER NOT NULL DEFAULT 0")
        if "source_mtime_ns" not in columns:
            connection.execute("ALTER TABLE videos ADD COLUMN source_mtime_ns INTEGER NOT NULL DEFAULT 0")
        rows = connection.execute(
            "SELECT id, filename, display_name, video_path, source_folder, source_size, source_mtime_ns FROM videos"
        ).fetchall()
        for row in rows:
            display_name = (row["display_name"] or Path(row["filename"]).stem or Path(row["video_path"]).stem).strip()
            source_folder = (row["source_folder"] or str(Path(row["video_path"]).expanduser().resolve().parent)).strip()
            source_size = int(row["source_size"] or 0)
            source_mtime_ns = int(row["source_mtime_ns"] or 0)
            try:
                stat = Path(row["video_path"]).stat()
                if not source_size:
                    source_size = int(stat.st_size)
                if not source_mtime_ns:
                    source_mtime_ns = int(stat.st_mtime_ns)
            except OSError:
                pass
            connection.execute(
                "UPDATE videos SET display_name = ?, source_folder = ?, source_size = ?, source_mtime_ns = ? WHERE id = ?",
                (display_name, source_folder, source_size, source_mtime_ns, int(row["id"])),
            )

    @staticmethod
    def _migrate_marker_modes(connection: sqlite3.Connection) -> None:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(video_markers)")}
        legacy_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'video_markers_legacy'"
        ).fetchone() is not None
        if "analysis_mode" in columns and not legacy_exists:
            return
        connection.execute("SAVEPOINT marker_modes")
        try:
            if "analysis_mode" not in columns:
                if legacy_exists:
                    raise sqlite3.DatabaseError("Conflicting legacy marker tables; both tables have been preserved.")
                connection.execute("ALTER TABLE video_markers RENAME TO video_markers_legacy")
                connection.execute(
                    """
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
                )
            VideoStore._migrate_marker_review_status(connection)
            VideoStore._migrate_marker_jump_time(connection)
            VideoStore._migrate_marker_importance(connection)
            legacy_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(video_markers_legacy)")
            }
            fields = (
                "id", "video_id", "start_ms", "end_ms", "title", "note",
                "source_quote", "trigger_phrase", "confidence",
                "analysis_mode", "review_status", "created_at", "user_start_ms", "user_start_updated_at", "importance",
            )
            source = {field: f"legacy.{field}" for field in fields}
            for field, default in (("analysis_mode", "'high_recall'"), ("review_status", "'active'"), ("importance", "'standard'")):
                if field not in legacy_columns:
                    source[field] = default
            for field in ("user_start_ms", "user_start_updated_at"):
                if field not in legacy_columns:
                    source[field] = "NULL"
            # Skip only identical copied rows. Any conflicting ID or key aborts
            # recovery, leaving both the legacy data and current edits intact.
            identical = " AND ".join(f"current.{field} IS {source[field]}" for field in fields)
            connection.execute(
                f"INSERT INTO video_markers ({', '.join(fields)}) "
                f"SELECT {', '.join(source[field] for field in fields)} "
                "FROM video_markers_legacy legacy "
                f"WHERE NOT EXISTS (SELECT 1 FROM video_markers current WHERE {identical})"
            )
            sequence = connection.execute(
                "SELECT seq FROM sqlite_sequence WHERE name = 'video_markers_legacy'"
            ).fetchone()
            if sequence is not None:
                updated = connection.execute(
                    "UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = 'video_markers'",
                    (sequence["seq"],),
                )
                if updated.rowcount == 0:
                    connection.execute(
                        "INSERT INTO sqlite_sequence(name, seq) VALUES ('video_markers', ?)",
                        (sequence["seq"],),
                    )
            connection.execute("DROP TABLE video_markers_legacy")
            connection.execute("RELEASE SAVEPOINT marker_modes")
        except BaseException:
            connection.execute("ROLLBACK TO SAVEPOINT marker_modes")
            connection.execute("RELEASE SAVEPOINT marker_modes")
            raise

    @staticmethod
    def _migrate_marker_review_status(connection: sqlite3.Connection) -> None:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(video_markers)")}
        if "review_status" not in columns:
            connection.execute(
                "ALTER TABLE video_markers ADD COLUMN review_status TEXT NOT NULL DEFAULT 'active'"
            )

    @staticmethod
    def _migrate_marker_jump_time(connection: sqlite3.Connection) -> None:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(video_markers)")}
        if "user_start_ms" not in columns:
            connection.execute(
                "ALTER TABLE video_markers ADD COLUMN user_start_ms INTEGER CHECK(user_start_ms >= 0)"
            )
        if "user_start_updated_at" not in columns:
            connection.execute("ALTER TABLE video_markers ADD COLUMN user_start_updated_at TEXT")

    @staticmethod
    def _migrate_marker_importance(connection: sqlite3.Connection) -> None:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(video_markers)")}
        if "importance" not in columns:
            connection.execute(
                "ALTER TABLE video_markers ADD COLUMN importance TEXT NOT NULL DEFAULT 'standard' "
                "CHECK(importance IN ('standard', 'important', 'critical'))"
            )

    @staticmethod
    def content_hash(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def file_hash(path: str | Path, chunk_size: int = 4 * 1024 * 1024) -> str:
        digest = hashlib.sha256()
        with Path(path).open("rb") as source:
            while chunk := source.read(chunk_size):
                digest.update(chunk)
        return digest.hexdigest()

    def get_video_by_hash(self, content_hash: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute("SELECT * FROM videos WHERE content_hash = ?", (content_hash,)).fetchone()

    def create_video(
        self,
        content_hash: str,
        filename: str,
        video_path: str,
        audio_path: str,
        duration_ms: int,
        source_size: int = 0,
        source_mtime_ns: int = 0,
    ) -> int:
        display_name = Path(filename).stem or filename
        source_folder = str(Path(video_path).expanduser().resolve().parent)
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO videos(content_hash, filename, display_name, video_path, source_folder, audio_path, duration_ms, source_size, source_mtime_ns) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (content_hash, filename, display_name, video_path, source_folder, audio_path, duration_ms, source_size, source_mtime_ns),
            )
            return int(cursor.lastrowid)

    def get_video_by_path(self, video_path: str | Path) -> sqlite3.Row | None:
        resolved = str(Path(video_path).expanduser().resolve())
        with self.connect() as connection:
            return connection.execute("SELECT * FROM videos WHERE video_path = ?", (resolved,)).fetchone()

    def update_video_path(
        self, video_id: int, video_path: str, source_size: int | None = None, source_mtime_ns: int | None = None
    ) -> None:
        resolved = str(Path(video_path).expanduser().resolve())
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE videos SET video_path = ?, source_folder = ?,
                    source_size = COALESCE(?, source_size),
                    source_mtime_ns = COALESCE(?, source_mtime_ns)
                WHERE id = ?
                """,
                (resolved, str(Path(resolved).parent), source_size, source_mtime_ns, video_id),
            )

    def update_video_file(self, video_id: int, path: Path, *, source_folder: str | None = None) -> None:
        stat = path.stat()
        with self.connect() as connection:
            connection.execute(
                """UPDATE videos SET video_path = ?, filename = ?, display_name = ?,
                   source_size = ?, source_mtime_ns = ?,
                   source_folder = COALESCE(?, source_folder) WHERE id = ?""",
                (str(path.resolve()), path.name, path.stem, stat.st_size, stat.st_mtime_ns,
                 source_folder, video_id),
            )

    def reset_video_content(
        self,
        video_id: int,
        content_hash: str,
        filename: str,
        video_path: str,
        audio_path: str,
        duration_ms: int,
        source_size: int,
        source_mtime_ns: int,
    ) -> None:
        """Replace derived data when a known source path now contains new content."""
        resolved = str(Path(video_path).expanduser().resolve())
        with self.connect() as connection:
            for table in (
                "video_transcript_segments",
                "video_keyword_hits",
                "video_markers",
                "video_analysis_runs",
                "video_ai_chunks",
                "video_processing_metrics",
                "video_share_imports",
            ):
                connection.execute(f"DELETE FROM {table} WHERE video_id = ?", (video_id,))
            connection.execute(
                """
                UPDATE videos SET content_hash = ?, filename = ?, video_path = ?, source_folder = ?,
                    audio_path = ?, duration_ms = ?, source_size = ?, source_mtime_ns = ?,
                    status = 'uploaded', transcription_model = ''
                WHERE id = ?
                """,
                (
                    content_hash,
                    filename,
                    resolved,
                    str(Path(resolved).parent),
                    audio_path,
                    duration_ms,
                    source_size,
                    source_mtime_ns,
                    video_id,
                ),
            )

    def update_video_display_name(self, video_id: int, display_name: str) -> None:
        name = display_name.strip()
        if not name:
            raise ValueError("视频显示名称不能为空")
        with self.connect() as connection:
            connection.execute("UPDATE videos SET display_name = ? WHERE id = ?", (name, video_id))

    def get_video(self, video_id: int) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT v.*, EXISTS(SELECT 1 FROM video_transcript_segments s "
                "WHERE s.video_id = v.id) AS has_transcript FROM videos v WHERE v.id = ?",
                (video_id,),
            ).fetchone()

    def list_videos(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                "SELECT v.*, EXISTS(SELECT 1 FROM video_transcript_segments s "
                "WHERE s.video_id = v.id) AS has_transcript FROM videos v "
                "ORDER BY v.created_at DESC, v.id DESC"
            ).fetchall()

    # ---- User-facing library folders -------------------------------------------------
    # ``source_folder`` remains the actual filesystem parent.  These helpers
    # manage the logical library tree shown by the UI.

    def create_folder(self, name: str, parent_id: int | None = None) -> int:
        normalized = name.strip()
        if not normalized:
            raise ValueError("文件夹名称不能为空")
        with self.connect() as connection:
            if parent_id is not None:
                parent = connection.execute(
                    "SELECT id FROM video_folders WHERE id = ?", (int(parent_id),)
                ).fetchone()
                if parent is None:
                    raise ValueError("父文件夹不存在")
            next_order = connection.execute(
                "SELECT COALESCE(MAX(library_order), -1) + 1 AS value "
                "FROM video_folders WHERE parent_id IS ?",
                (parent_id,),
            ).fetchone()["value"]
            cursor = connection.execute(
                "INSERT INTO video_folders(name, parent_id, library_order) VALUES (?, ?, ?)",
                (normalized, parent_id, int(next_order)),
            )
            return int(cursor.lastrowid)

    def get_folder(self, folder_id: int) -> sqlite3.Row | None:
        rows = self._folder_summaries("id = ?", (int(folder_id),))
        return rows[0] if rows else None

    def list_folders(self, parent_id: int | None = None) -> list[sqlite3.Row]:
        return self._folder_summaries("parent_id IS ?", (parent_id,))

    def _folder_summaries(self, selection: str, parameters: tuple) -> list[sqlite3.Row]:
        """Aggregate each requested subtree once, without multiplying videos by markers."""
        with self.connect() as connection:
            return connection.execute(
                f"""WITH RECURSIVE roots AS (
                    SELECT * FROM video_folders WHERE {selection}
                ), descendants(root_id, id) AS (
                    SELECT id, id FROM roots
                    UNION
                    SELECT d.root_id, f.id FROM video_folders f
                    JOIN descendants d ON f.parent_id = d.id
                ), video_counts AS (
                    SELECT d.root_id, COUNT(*) AS video_count
                    FROM descendants d JOIN videos v ON v.library_folder_id = d.id
                    GROUP BY d.root_id
                ), marker_counts AS (
                    SELECT d.root_id,
                        SUM(m.importance = 'standard') AS standard_count,
                        SUM(m.importance = 'important') AS important_count,
                        SUM(m.importance = 'critical') AS critical_count
                    FROM descendants d JOIN videos v ON v.library_folder_id = d.id
                    JOIN video_markers m ON m.video_id = v.id
                    WHERE m.review_status != 'excluded'
                    GROUP BY d.root_id
                )
                SELECT f.*, COALESCE(v.video_count, 0) AS video_count,
                    COALESCE(m.standard_count, 0) AS standard_count,
                    COALESCE(m.important_count, 0) AS important_count,
                    COALESCE(m.critical_count, 0) AS critical_count
                FROM roots f LEFT JOIN video_counts v ON v.root_id = f.id
                LEFT JOIN marker_counts m ON m.root_id = f.id
                ORDER BY f.library_order, f.name COLLATE NOCASE, f.id""",
                parameters,
            ).fetchall()

    def find_folder_by_name(self, name: str, parent_id: int | None = None) -> sqlite3.Row | None:
        """Return the first logical folder with this name in the requested parent."""
        normalized = name.strip()
        if not normalized:
            return None
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM video_folders WHERE name = ? AND parent_id IS ? "
                "ORDER BY id LIMIT 1",
                (normalized, parent_id),
            ).fetchone()

    def import_folder(self, source: str | Path, parent_id: int | None = None) -> int:
        key = source_directory_key(source)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if parent_id is not None and connection.execute(
                "SELECT id FROM video_folders WHERE id = ?", (parent_id,)
            ).fetchone() is None:
                raise ValueError("父文件夹不存在")
            existing = connection.execute(
                "SELECT id, parent_id FROM video_folders WHERE import_source = ?", (key,)
            ).fetchone()
            if existing:
                if parent_id is not None and existing["parent_id"] != parent_id:
                    ancestor = parent_id
                    while ancestor is not None:
                        if ancestor == existing["id"]:
                            raise ValueError("文件夹不能移入自身或其子文件夹")
                        ancestor = connection.execute(
                            "SELECT parent_id FROM video_folders WHERE id = ?", (ancestor,)
                        ).fetchone()["parent_id"]
                    connection.execute(
                        "UPDATE video_folders SET parent_id = ?, library_order = "
                        "(SELECT COALESCE(MAX(library_order), -1) + 1 FROM video_folders WHERE parent_id IS ?), "
                        "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (parent_id, parent_id, existing["id"]),
                    )
                return int(existing["id"])
            base = Path(source).name or "视频文件夹"
            name, suffix = base, 1
            while connection.execute(
                "SELECT 1 FROM video_folders WHERE parent_id IS ? AND name = ?", (parent_id, name)
            ).fetchone():
                name = f"{base} ({suffix})"
                suffix += 1
            order = connection.execute(
                "SELECT COALESCE(MAX(library_order), -1) + 1 FROM video_folders WHERE parent_id IS ?",
                (parent_id,),
            ).fetchone()[0]
            return int(connection.execute(
                "INSERT INTO video_folders(name, parent_id, library_order, import_source) VALUES (?, ?, ?, ?)",
                (name, parent_id, order, key),
            ).lastrowid)

    def update_folder(self, folder_id: int, name: str) -> None:
        normalized = name.strip()
        if not normalized:
            raise ValueError("文件夹名称不能为空")
        with self.connect() as connection:
            updated = connection.execute(
                "UPDATE video_folders SET name = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (normalized, int(folder_id)),
            )
            if updated.rowcount == 0:
                raise ValueError("文件夹不存在")

    def set_folder_parent(self, folder_id: int, parent_id: int | None) -> None:
        with self.connect() as connection:
            # Serialize ancestry validation with the move to prevent concurrent cycles.
            connection.execute("BEGIN IMMEDIATE")
            folder = connection.execute(
                "SELECT parent_id FROM video_folders WHERE id = ?", (folder_id,)
            ).fetchone()
            if folder is None:
                raise ValueError("文件夹不存在")
            ancestor = parent_id
            visited = {folder_id}
            while ancestor is not None:
                if ancestor in visited:
                    raise ValueError("不能将文件夹移入自身或其子文件夹")
                visited.add(ancestor)
                row = connection.execute(
                    "SELECT parent_id FROM video_folders WHERE id = ?", (ancestor,)
                ).fetchone()
                if row is None:
                    raise ValueError("目标文件夹不存在")
                ancestor = row["parent_id"]
            if folder["parent_id"] == parent_id:
                return
            next_order = connection.execute(
                "SELECT COALESCE(MAX(library_order), -1) + 1 FROM ("
                "SELECT library_order FROM video_folders WHERE parent_id IS ? "
                "UNION ALL SELECT library_order FROM videos WHERE library_folder_id IS ?)",
                (parent_id, parent_id),
            ).fetchone()[0]
            connection.execute(
                "UPDATE video_folders SET parent_id = ?, library_order = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (parent_id, next_order, folder_id),
            )

    def delete_folder(self, folder_id: int) -> None:
        with self.connect() as connection:
            deleted = connection.execute(
                "DELETE FROM video_folders WHERE id = ?", (int(folder_id),)
            )
            if deleted.rowcount == 0:
                raise ValueError("文件夹不存在")

    # ---- Recycle bin --------------------------------------------------------------
    _RECYCLE_CHILD_TABLES = (
        "video_transcript_segments",
        "video_keyword_hits",
        "video_markers",
        "video_analysis_runs",
        "video_ai_chunks",
        "video_processing_metrics",
        "video_experiments",
        "video_share_imports",
    )

    @staticmethod
    def _json_rows(connection: sqlite3.Connection, query: str, parameters: tuple = ()) -> list[dict[str, object]]:
        return [{key: row[key] for key in row.keys()} for row in connection.execute(query, parameters).fetchall()]

    @staticmethod
    def _insert_rows(connection: sqlite3.Connection, table: str, rows: list[dict[str, object]]) -> None:
        if not rows:
            return
        columns = list(rows[0])
        placeholders = ", ".join("?" for _ in columns)
        quoted = ", ".join(f'"{column}"' for column in columns)
        connection.executemany(
            f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders})',
            [tuple(row.get(column) for column in columns) for row in rows],
        )

    def _recycle_snapshot(
        self, connection: sqlite3.Connection, video_ids: list[int], folder_ids: list[int],
    ) -> dict[str, object]:
        videos = self._json_rows(
            connection,
            f"SELECT * FROM videos WHERE id IN ({','.join('?' for _ in video_ids)}) ORDER BY id",
            tuple(video_ids),
        ) if video_ids else []
        folders = self._json_rows(
            connection,
            f"SELECT * FROM video_folders WHERE id IN ({','.join('?' for _ in folder_ids)}) ORDER BY id",
            tuple(folder_ids),
        ) if folder_ids else []
        related: dict[str, list[dict[str, object]]] = {}
        for table in self._RECYCLE_CHILD_TABLES:
            if table == "video_experiments":
                query = (
                    f"SELECT * FROM {table} WHERE video_id IN "
                    f"({','.join('?' for _ in video_ids)}) ORDER BY id"
                )
                rows = self._json_rows(connection, query, tuple(video_ids)) if video_ids else []
            else:
                query = (
                    f"SELECT * FROM {table} WHERE video_id IN "
                    f"({','.join('?' for _ in video_ids)})"
                )
                rows = self._json_rows(connection, query, tuple(video_ids)) if video_ids else []
            related[table] = rows
        experiment_ids = [str(row["id"]) for row in related["video_experiments"]]
        for table, column in (("llm_experiment_calls", "experiment_id"),
                              ("llm_experiment_steps", "experiment_id"),
                              ("llm_experiment_results", "experiment_id")):
            related[table] = self._json_rows(
                connection,
                f"SELECT * FROM {table} WHERE {column} IN ({','.join('?' for _ in experiment_ids)})",
                tuple(experiment_ids),
            ) if experiment_ids else []
        return {"videos": videos, "folders": folders, "related": related}

    @staticmethod
    def _folder_tree(connection: sqlite3.Connection, folder_id: int) -> list[dict[str, object]]:
        return [{key: row[key] for key in row.keys()} for row in connection.execute(
            """
            WITH RECURSIVE tree(id, depth) AS (
                SELECT id, 0 FROM video_folders WHERE id = ?
                UNION ALL
                SELECT f.id, tree.depth + 1
                FROM video_folders f JOIN tree ON f.parent_id = tree.id
            )
            SELECT f.*, tree.depth FROM video_folders f JOIN tree ON tree.id = f.id
            ORDER BY tree.depth DESC, f.id
            """, (int(folder_id),),
        ).fetchall()]

    def _move_to_recycle_bin(
        self, kind: str, video_ids: list[int], folder_ids: list[int], trash_root: str | Path,
    ) -> dict[str, object]:
        operation_id = uuid.uuid4().hex
        trash_directory = Path(trash_root).expanduser().resolve() / operation_id
        moved: list[dict[str, object]] = []
        try:
            with self.connect() as connection:
                if folder_ids:
                    snapshot = self._recycle_snapshot(connection, video_ids, folder_ids)
                else:
                    snapshot = self._recycle_snapshot(connection, video_ids, [])
                if not snapshot["videos"] and not snapshot["folders"]:
                    raise ValueError("要删除的内容不存在")

                seen_paths: set[str] = set()
                for video in snapshot["videos"]:
                    for role, field in (("video", "video_path"), ("audio", "audio_path")):
                        raw_path = str(video.get(field) or "")
                        if not raw_path:
                            continue
                        source = Path(raw_path).expanduser()
                        key = str(source.resolve()).casefold()
                        if key in seen_paths:
                            continue
                        seen_paths.add(key)
                        record: dict[str, object] = {"role": role, "original": str(source), "moved": False}
                        managed_root = Path(trash_root).expanduser().resolve().parent
                        is_external_source = role == "video" and not source.resolve().is_relative_to(managed_root)
                        if source.is_file() and not is_external_source:
                            destination = trash_directory / role / f'{video["id"]}-{source.name}'
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(source), str(destination))
                            record.update(trash=str(destination), moved=True)
                        moved.append(record)

                payload = {**snapshot, "files": moved}
                connection.execute(
                    "INSERT INTO recycle_bin(operation_id, kind, payload_json) VALUES (?, ?, ?)",
                    (operation_id, kind, json.dumps(payload, ensure_ascii=False)),
                )
                if video_ids:
                    connection.execute(
                        f"DELETE FROM videos WHERE id IN ({','.join('?' for _ in video_ids)})",
                        tuple(video_ids),
                    )
                for folder in snapshot["folders"]:
                    connection.execute("DELETE FROM video_folders WHERE id = ?", (int(folder["id"]),))
        except Exception:
            for record in reversed(moved):
                if record.get("moved"):
                    source = Path(str(record["trash"]))
                    destination = Path(str(record["original"]))
                    if source.exists() and not destination.exists():
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(source), str(destination))
            shutil.rmtree(trash_directory, ignore_errors=True)
            raise
        return {
            "operation_id": operation_id,
            "kind": kind,
            "videos": len(snapshot["videos"]),
            "folders": len(snapshot["folders"]),
        }

    def trash_video(self, video_id: int, trash_root: str | Path) -> dict[str, object]:
        return self._move_to_recycle_bin("video", [int(video_id)], [], trash_root)

    def trash_folder(self, folder_id: int, trash_root: str | Path) -> dict[str, object]:
        with self.connect() as connection:
            folders = self._folder_tree(connection, int(folder_id))
            if not folders:
                raise ValueError("文件夹不存在")
            ids = [int(folder["id"]) for folder in folders]
            placeholders = ",".join("?" for _ in ids)
            videos = connection.execute(
                f"SELECT id FROM videos WHERE library_folder_id IN ({placeholders})", tuple(ids),
            ).fetchall()
            video_ids = [int(row["id"]) for row in videos]
        return self._move_to_recycle_bin("folder", video_ids, ids, trash_root)

    def undo_latest_recycle(self, trash_root: str | Path) -> dict[str, object]:
        moved_back: list[dict[str, object]] = []
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM recycle_bin WHERE undone_at IS NULL ORDER BY created_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
            if row is None:
                raise ValueError("没有可撤销的删除操作")
            payload = json.loads(row["payload_json"])
            files = payload.get("files", [])
            try:
                for record in files:
                    if not record.get("moved"):
                        continue
                    source = Path(str(record["trash"]))
                    destination = Path(str(record["original"]))
                    if not source.is_file():
                        raise ValueError(f"回收站文件不存在：{source.name}")
                    if destination.exists():
                        raise ValueError(f"原位置已有同名文件：{destination.name}")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(source), str(destination))
                    moved_back.append({"source": str(destination), "destination": str(source)})

                folder_rows = payload.get("folders", [])
                folder_map = {int(item["id"]): item for item in folder_rows}
                def folder_depth(item: dict[str, object]) -> int:
                    depth = 0
                    parent = item.get("parent_id")
                    seen: set[int] = set()
                    while parent is not None and int(parent) in folder_map and int(parent) not in seen:
                        seen.add(int(parent))
                        depth += 1
                        parent = folder_map[int(parent)].get("parent_id")
                    return depth
                folders = sorted(folder_rows, key=lambda item: (folder_depth(item), int(item["id"])))
                self._insert_rows(connection, "video_folders", folders)
                self._insert_rows(connection, "videos", payload.get("videos", []))
                related = payload.get("related", {})
                for table in (
                    "video_transcript_segments", "video_keyword_hits", "video_markers",
                    "video_analysis_runs", "video_ai_chunks", "video_processing_metrics",
                    "video_experiments", "llm_experiment_calls", "llm_experiment_steps",
                    "llm_experiment_results",
                    "video_share_imports",
                ):
                    self._insert_rows(connection, table, related.get(table, []))
                connection.execute(
                    "UPDATE recycle_bin SET undone_at = CURRENT_TIMESTAMP WHERE operation_id = ?",
                    (row["operation_id"],),
                )
            except Exception:
                for record in reversed(moved_back):
                    source = Path(record["source"])
                    destination = Path(record["destination"])
                    if source.exists() and not destination.exists():
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(source), str(destination))
                raise
        return {
            "operation_id": row["operation_id"],
            "kind": row["kind"],
            "videos": len(payload.get("videos", [])),
            "folders": len(payload.get("folders", [])),
        }

    def latest_recycle(self) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT operation_id, kind, payload_json, created_at FROM recycle_bin "
                "WHERE undone_at IS NULL ORDER BY created_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload_json"])
        return {
            "operation_id": row["operation_id"],
            "kind": row["kind"],
            "videos": len(payload.get("videos", [])),
            "folders": len(payload.get("folders", [])),
            "created_at": row["created_at"],
        }

    def set_video_folder(self, video_id: int, folder_id: int | None) -> None:
        with self.connect() as connection:
            if folder_id is not None:
                exists = connection.execute(
                    "SELECT id FROM video_folders WHERE id = ?", (int(folder_id),)
                ).fetchone()
                if exists is None:
                    raise ValueError("文件夹不存在")
                next_order = connection.execute(
                    "SELECT COALESCE(MAX(library_order), -1) + 1 AS value "
                    "FROM videos WHERE library_folder_id = ?",
                    (int(folder_id),),
                ).fetchone()["value"]
            else:
                next_order = connection.execute(
                    "SELECT COALESCE(MAX(library_order), -1) + 1 AS value "
                    "FROM videos WHERE library_folder_id IS NULL",
                ).fetchone()["value"]
            updated = connection.execute(
                "UPDATE videos SET library_folder_id = ?, library_order = ? WHERE id = ?",
                (folder_id, int(next_order), int(video_id)),
            )
            if updated.rowcount == 0:
                raise ValueError("视频不存在")

    def list_folder_videos(self, folder_id: int | None, *, recursive: bool = False) -> list[sqlite3.Row]:
        with self.connect() as connection:
            if recursive and folder_id is not None:
                return connection.execute(
                    "WITH RECURSIVE tree(id) AS (SELECT id FROM video_folders WHERE id = ? "
                    "UNION SELECT f.id FROM video_folders f JOIN tree t ON f.parent_id = t.id) "
                    "SELECT v.*, EXISTS(SELECT 1 FROM video_transcript_segments s "
                    "WHERE s.video_id = v.id) AS has_transcript FROM videos v "
                    "WHERE v.library_folder_id IN (SELECT id FROM tree) "
                    "ORDER BY v.library_folder_id, v.library_order, v.display_name COLLATE NOCASE, v.id",
                    (int(folder_id),),
                ).fetchall()
            return connection.execute(
                "SELECT v.*, EXISTS(SELECT 1 FROM video_transcript_segments s "
                "WHERE s.video_id = v.id) AS has_transcript FROM videos v "
                "WHERE v.library_folder_id IS ? "
                "ORDER BY v.library_order, v.display_name COLLATE NOCASE, v.id",
                (None if folder_id is None else int(folder_id),),
            ).fetchall()

    def reorder_library_items(
        self,
        parent_id: int | None,
        ordered_folders: list[int],
        ordered_videos: list[int],
    ) -> None:
        """Persist drag-and-drop positions for one directory.

        Folder and video positions share one integer sequence.  Callers may
        submit either list (or both) in the desired visual order; the method
        validates ownership so an item cannot be moved across directories by
        accident.
        """
        with self.connect() as connection:
            expected_folders = {
                int(row["id"])
                for row in connection.execute(
                    "SELECT id FROM video_folders WHERE parent_id IS ?", (parent_id,)
                )
            }
            expected_videos = {
                int(row["id"])
                for row in connection.execute(
                    "SELECT id FROM videos WHERE library_folder_id IS ?", (parent_id,)
                )
            }
            if set(ordered_folders) - expected_folders:
                raise ValueError("排序列表包含不属于当前目录的文件夹")
            if set(ordered_videos) - expected_videos:
                raise ValueError("排序列表包含不属于当前目录的视频")
            if len(set(ordered_folders)) != len(ordered_folders) or len(set(ordered_videos)) != len(ordered_videos):
                raise ValueError("排序列表不能包含重复项目")
            position = 0
            for folder_id in ordered_folders:
                connection.execute(
                    "UPDATE video_folders SET library_order = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (position, int(folder_id)),
                )
                position += 1
            for video_id in ordered_videos:
                connection.execute(
                    "UPDATE videos SET library_order = ? WHERE id = ?",
                    (position, int(video_id)),
                )
                position += 1

    def reorder_library(
        self,
        parent_id: int | None,
        ordered_items: list[tuple[str, int]],
    ) -> None:
        """Persist a mixed folder/video order from a drag-and-drop payload."""
        with self.connect() as connection:
            expected_folders = {
                int(row["id"])
                for row in connection.execute(
                    "SELECT id FROM video_folders WHERE parent_id IS ?", (parent_id,)
                )
            }
            expected_videos = {
                int(row["id"])
                for row in connection.execute(
                    "SELECT id FROM videos WHERE library_folder_id IS ?", (parent_id,)
                )
            }
            seen: set[tuple[str, int]] = set()
            for kind, item_id in ordered_items:
                normalized_kind = "folder" if kind in {"folder", "folders"} else "video" if kind in {"video", "videos"} else ""
                key = (normalized_kind, int(item_id))
                if not normalized_kind or key in seen:
                    raise ValueError("排序列表包含无效或重复项目")
                seen.add(key)
                if normalized_kind == "folder" and int(item_id) not in expected_folders:
                    raise ValueError("排序列表包含不属于当前目录的文件夹")
                if normalized_kind == "video" and int(item_id) not in expected_videos:
                    raise ValueError("排序列表包含不属于当前目录的视频")
            # A client may send only the dragged item(s).  Keep all omitted
            # siblings in their previous order and append them after the
            # explicitly ordered items so positions remain deterministic.
            existing = [
                (str(row["kind"]), int(row["id"]))
                for row in connection.execute(
                    """
                    SELECT 'folder' AS kind, id, library_order
                    FROM video_folders WHERE parent_id IS ?
                    UNION ALL
                    SELECT 'video' AS kind, id, library_order
                    FROM videos WHERE library_folder_id IS ?
                    ORDER BY library_order, id
                    """,
                    (parent_id, parent_id),
                )
            ]
            provided = {(("folder" if kind in {"folder", "folders"} else "video"), int(item_id)) for kind, item_id in ordered_items}
            sequence = list(ordered_items) + [item for item in existing if item not in provided]
            for position, (kind, item_id) in enumerate(sequence):
                if kind in {"folder", "folders"}:
                    connection.execute(
                        "UPDATE video_folders SET library_order = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (position, int(item_id)),
                    )
                else:
                    connection.execute(
                        "UPDATE videos SET library_order = ? WHERE id = ?",
                        (position, int(item_id)),
                    )

    def set_status(self, video_id: int, status: str) -> None:
        with self.connect() as connection:
            connection.execute("UPDATE videos SET status = ? WHERE id = ?", (status, video_id))

    def mark_transcribed(
        self, video_id: int, segments: list[TranscriptSegment], model_id: str = ""
    ) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM video_transcript_segments WHERE video_id = ?", (video_id,))
            connection.executemany(
                "INSERT INTO video_transcript_segments(video_id, segment_id, start_ms, end_ms, text, words_json) VALUES (?, ?, ?, ?, ?, ?)",
                [(video_id, segment.id, segment.start_ms, segment.end_ms, segment.text, json.dumps(segment.words or [], ensure_ascii=False)) for segment in segments],
            )
            connection.execute(
                "UPDATE videos SET status = 'transcribed', transcription_model = ? WHERE id = ?",
                (model_id, video_id),
            )

    def claim_retranscription(self, video_id: int) -> bool:
        with self.connect() as connection:
            updated = connection.execute(
                "UPDATE videos SET status = 'processing' WHERE id = ? AND status != 'processing'",
                (video_id,),
            )
            return updated.rowcount == 1

    def replace_transcript_for_reanalysis(
        self, video_id: int, segments: list[TranscriptSegment], model_id: str
    ) -> None:
        with self.connect() as connection:
            for table in (
                "video_transcript_segments", "video_keyword_hits", "video_markers",
                "video_analysis_runs", "video_ai_chunks", "video_processing_metrics",
                "video_share_imports",
            ):
                connection.execute(f"DELETE FROM {table} WHERE video_id = ?", (video_id,))
            connection.executemany(
                "INSERT INTO video_transcript_segments(video_id, segment_id, start_ms, end_ms, text, words_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(video_id, segment.id, segment.start_ms, segment.end_ms, segment.text,
                  json.dumps(segment.words or [], ensure_ascii=False)) for segment in segments],
            )
            connection.execute(
                "UPDATE videos SET status = 'transcribed', transcription_model = ? WHERE id = ?",
                (model_id, video_id),
            )

    def get_segments(self, video_id: int) -> list[TranscriptSegment]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM video_transcript_segments WHERE video_id = ? ORDER BY start_ms", (video_id,)).fetchall()
        return [TranscriptSegment(int(row["segment_id"]), int(row["start_ms"]), int(row["end_ms"]), row["text"], json.loads(row["words_json"] or "[]")) for row in rows]

    def has_transcript(self, video_id: int) -> bool:
        with self.connect() as connection:
            return connection.execute(
                "SELECT 1 FROM video_transcript_segments WHERE video_id = ? LIMIT 1", (video_id,)
            ).fetchone() is not None

    def search_subtitles(
        self, query: str, *, limit: int = 20, offset: int = 0, video_id: int | None = None,
        preferred_video_id: int | None = None,
    ) -> dict[str, object]:
        with self.connect() as connection:
            return search_subtitles(connection, query, limit=limit, offset=offset, video_id=video_id,
                                    preferred_video_id=preferred_video_id)

    def replace_keyword_hits(self, video_id: int, matches: list[TriggerMatch], segments: list[TranscriptSegment]) -> None:
        source_by_id = {segment.id: segment.text for segment in segments}
        with self.connect() as connection:
            connection.execute("DELETE FROM video_keyword_hits WHERE video_id = ?", (video_id,))
            connection.executemany(
                "INSERT INTO video_keyword_hits(video_id, segment_id, start_ms, end_ms, phrase, strength, category, source_quote) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        video_id,
                        match.segment_id,
                        match.start_ms,
                        match.end_ms,
                        match.phrase,
                        match.strength,
                        match.category,
                        source_by_id.get(match.segment_id, ""),
                    )
                    for match in matches
                ],
            )

    def list_keyword_hits(self, video_id: int) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM video_keyword_hits WHERE video_id = ? ORDER BY start_ms, phrase", (video_id,)
            ).fetchall()

    def add_marker(
        self,
        video_id: int,
        start_ms: int,
        end_ms: int,
        title: str,
        note: str,
        source_quote: str,
        trigger_phrase: str,
        confidence: float,
        analysis_mode: str = "high_recall",
    ) -> int:
        if end_ms <= start_ms:
            raise ValueError("结束时间必须晚于开始时间")
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT id FROM video_markers WHERE video_id = ? AND start_ms = ? AND end_ms = ? AND trigger_phrase = ? AND analysis_mode = ?",
                (video_id, start_ms, end_ms, trigger_phrase, analysis_mode),
            ).fetchone()
            if existing:
                return int(existing["id"])
            cursor = connection.execute(
                "INSERT INTO video_markers(video_id, start_ms, end_ms, title, note, source_quote, trigger_phrase, confidence, analysis_mode) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (video_id, start_ms, end_ms, title.strip(), note.strip(), source_quote, trigger_phrase, confidence, analysis_mode),
            )
            return int(cursor.lastrowid)

    def list_markers(self, video_id: int, analysis_mode: str | None = None) -> list[sqlite3.Row]:
        with self.connect() as connection:
            if analysis_mode:
                return connection.execute(
                    "SELECT * FROM video_markers WHERE video_id = ? AND analysis_mode = ? AND review_status != 'excluded' ORDER BY start_ms",
                    (video_id, analysis_mode),
                ).fetchall()
            return connection.execute(
                "SELECT * FROM video_markers WHERE video_id = ? AND review_status != 'excluded' ORDER BY start_ms",
                (video_id,),
            ).fetchall()

    def marker_importance_counts(self, video_id: int | None = None) -> dict[int, dict[str, int]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT video_id, importance, COUNT(*) AS count FROM video_markers "
                "WHERE review_status != 'excluded' "
                + ("AND video_id = ? " if video_id is not None else "")
                + "GROUP BY video_id, importance",
                (video_id,) if video_id is not None else (),
            ).fetchall()
        counts: dict[int, dict[str, int]] = {}
        for row in rows:
            values = counts.setdefault(int(row["video_id"]), {"standard": 0, "important": 0, "critical": 0})
            values[row["importance"]] = int(row["count"])
        return counts

    def list_excluded_markers(self, video_id: int) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM video_markers WHERE video_id = ? AND review_status = 'excluded' ORDER BY start_ms",
                (video_id,),
            ).fetchall()

    def set_marker_review_status(self, marker_id: int, review_status: str) -> None:
        if review_status not in {"active", "mastered", "excluded"}:
            raise ValueError("未知的重点复习状态")
        with self.connect() as connection:
            connection.execute(
                "UPDATE video_markers SET review_status = ? WHERE id = ?",
                (review_status, marker_id),
            )

    def set_marker_jump_time(self, marker_id: int, user_start_ms: int | None) -> sqlite3.Row:
        if user_start_ms is not None and (type(user_start_ms) is not int or user_start_ms < 0):
            raise ValueError("跳转时间必须为非负整数毫秒")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            marker = connection.execute(
                "SELECT m.*, v.duration_ms FROM video_markers m "
                "JOIN videos v ON v.id = m.video_id WHERE m.id = ?",
                (marker_id,),
            ).fetchone()
            if marker is None:
                raise KeyError(marker_id)
            if user_start_ms is not None and marker["duration_ms"] > 0 and user_start_ms > marker["duration_ms"]:
                raise ValueError("跳转时间不能超过视频时长")
            connection.execute(
                "UPDATE video_markers SET user_start_ms = ?, user_start_updated_at = "
                "CASE WHEN ? IS NULL THEN NULL ELSE CURRENT_TIMESTAMP END WHERE id = ?",
                (user_start_ms, user_start_ms, marker_id),
            )
            return connection.execute("SELECT * FROM video_markers WHERE id = ?", (marker_id,)).fetchone()

    def set_marker_importance(self, marker_id: int, importance: str) -> sqlite3.Row:
        if importance not in {"standard", "important", "critical"}:
            raise ValueError("未知的考点重要程度")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                "UPDATE video_markers SET importance = ? WHERE id = ?", (importance, marker_id),
            )
            if updated.rowcount == 0:
                raise KeyError(marker_id)
            return connection.execute("SELECT * FROM video_markers WHERE id = ?", (marker_id,)).fetchone()

    def delete_markers_for_mode(self, video_id: int, analysis_mode: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM video_markers WHERE video_id = ? AND analysis_mode = ? "
                "AND review_status != 'excluded' AND user_start_ms IS NULL AND importance = 'standard'",
                (video_id, analysis_mode),
            )

    def get_ai_chunk_selection(self, video_id: int, analysis_mode: str, chunk_key: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT selection_json FROM video_ai_chunks WHERE video_id = ? AND analysis_mode = ? AND chunk_key = ?",
                (video_id, analysis_mode, chunk_key),
            ).fetchone()
        return str(row["selection_json"]) if row else None

    def save_ai_chunk_selection(
        self, video_id: int, analysis_mode: str, chunk_key: str, selection_json: str
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO video_ai_chunks(video_id, analysis_mode, chunk_key, selection_json, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(video_id, analysis_mode, chunk_key) DO UPDATE SET
                    selection_json = excluded.selection_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (video_id, analysis_mode, chunk_key, selection_json),
            )

    def get_analysis_run(self, video_id: int, analysis_mode: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM video_analysis_runs WHERE video_id = ? AND analysis_mode = ?", (video_id, analysis_mode)
            ).fetchone()

    def list_recall_schemes(self) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM recall_schemes ORDER BY created_at, id").fetchall()
        return [self._recall_scheme_payload(row) for row in rows]

    @staticmethod
    def _recall_scheme_payload(row) -> dict:
        return {**json.loads(row["config_json"]), "id": row["id"], "name": row["name"],
                "revision": row["revision"], "archived": bool(row["archived"])}

    def get_recall_scheme(self, scheme_id: str) -> dict | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM recall_schemes WHERE id = ?", (scheme_id,)).fetchone()
        return self._recall_scheme_payload(row) if row else None

    def save_recall_scheme(self, config: dict, scheme_id: str | None = None) -> dict:
        from classcue.recall_schemes import semantic_hash
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute("SELECT * FROM recall_schemes WHERE id = ?", (scheme_id,)).fetchone() if scheme_id else None
            if scheme_id and previous is None:
                raise ValueError("自定义召回方案不存在")
            rows = connection.execute("SELECT * FROM recall_schemes WHERE archived = 0").fetchall()
            if any(row["id"] != scheme_id and row["name"].casefold() == config["name"].casefold() for row in rows):
                raise ValueError("已有同名方案，请使用其他名称")
            if previous is None and len(rows) >= 20:
                raise ValueError("最多保留 20 个启用的自定义方案")
            if previous and previous["archived"]:
                raise ValueError("该方案已停用，请新建方案")
            revision = int(previous["revision"]) if previous else 1
            if previous and semantic_hash(json.loads(previous["config_json"])) != semantic_hash(config):
                revision += 1
            scheme_id = scheme_id or f"custom_{uuid.uuid4().hex}"
            connection.execute(
                """INSERT INTO recall_schemes(id, name, config_json, revision) VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET name = excluded.name, config_json = excluded.config_json,
                revision = excluded.revision, updated_at = CURRENT_TIMESTAMP""",
                (scheme_id, config["name"], json.dumps(config, ensure_ascii=False), revision),
            )
        return self.get_recall_scheme(scheme_id)

    def archive_recall_scheme(self, scheme_id: str) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE recall_schemes SET archived = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (scheme_id,),
            )
            if not cursor.rowcount:
                raise ValueError("自定义召回方案不存在")

    def create_experiment(
        self,
        experiment_id: str,
        video_id: int,
        name: str,
        strategy: str,
        analysis_mode: str,
        options_json: str = "{}",
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO video_experiments(
                    id, video_id, name, strategy, analysis_mode, options_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (experiment_id, video_id, name.strip() or experiment_id, strategy, analysis_mode, options_json),
            )

    def get_experiment(self, experiment_id: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM video_experiments WHERE id = ?", (experiment_id,)
            ).fetchone()

    def finalize_early_experiment(self, experiment_id: str, snapshot: dict) -> None:
        """Bind a production prefetch run to the completed transcript before validation."""
        metadata = {key: value for key, value in snapshot.items() if key != "transcript"}
        with self.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO llm_transcript_snapshots(hash, payload_json) VALUES (?, ?)",
                (snapshot["transcript_hash"], json.dumps(snapshot["transcript"], ensure_ascii=False)),
            )
            cursor = connection.execute(
                """
                UPDATE video_experiments SET options_json = ?, status = 'queued'
                WHERE id = ? AND video_id = ? AND status = 'prefetching'
                """,
                (json.dumps(metadata, ensure_ascii=False), experiment_id, snapshot["video_id"]),
            )
            if cursor.rowcount != 1:
                raise ValueError("Early extraction run is no longer available")

    def save_transcript_snapshot(self, digest: str, payload: dict) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO llm_transcript_snapshots(hash, payload_json) VALUES (?, ?)",
                (digest, json.dumps(payload, ensure_ascii=False)),
            )

    def get_transcript_snapshot(self, digest: str) -> dict | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM llm_transcript_snapshots WHERE hash = ?", (digest,),
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def save_experiment_call(self, experiment_id: str, payload: dict) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO llm_experiment_calls(
                    id, experiment_id, chunk_id, stage, parent_call_id, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    chunk_id = excluded.chunk_id, stage = excluded.stage,
                    parent_call_id = excluded.parent_call_id, payload_json = excluded.payload_json
                """,
                (
                    payload["call_id"], experiment_id, payload["chunk_id"], payload["stage"],
                    payload.get("parent_call_id"), json.dumps(payload, ensure_ascii=False),
                ),
            )

    def list_experiment_calls(self, experiment_id: str) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM llm_experiment_calls WHERE experiment_id = ? ORDER BY rowid",
                (experiment_id,),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def save_experiment_step(self, experiment_id: str, step_id: str, payload: dict) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO llm_experiment_steps(experiment_id, step_id, payload_json)
                VALUES (?, ?, ?)
                ON CONFLICT(experiment_id, step_id) DO UPDATE SET payload_json = excluded.payload_json
                """,
                (experiment_id, step_id, json.dumps(payload, ensure_ascii=False)),
            )

    def list_experiment_steps(self, experiment_id: str) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM llm_experiment_steps WHERE experiment_id = ? ORDER BY rowid",
                (experiment_id,),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def save_experiment_results(self, experiment_id: str, payload: dict) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO llm_experiment_results(experiment_id, payload_json) VALUES (?, ?)
                """,
                (experiment_id, json.dumps(payload, ensure_ascii=False)),
            )

    def get_experiment_results(self, experiment_id: str) -> dict | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM llm_experiment_results WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def list_experiments(self, video_id: int) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM video_experiments WHERE video_id = ? ORDER BY created_at, id",
                (video_id,),
            ).fetchall()

    def update_experiment(
        self,
        experiment_id: str,
        *,
        status: str | None = None,
        candidate_count: int | None = None,
        error_message: str | None = None,
    ) -> None:
        fields: list[str] = []
        values: list[object] = []
        if status is not None:
            fields.append("status = ?")
            values.append(status)
        if candidate_count is not None:
            fields.append("candidate_count = ?")
            values.append(int(candidate_count))
        if error_message is not None:
            fields.append("error_message = ?")
            values.append(error_message)
        if not fields:
            return
        fields.append("updated_at = CURRENT_TIMESTAMP")
        values.append(experiment_id)
        with self.connect() as connection:
            connection.execute(
                f"UPDATE video_experiments SET {', '.join(fields)} WHERE id = ?",
                values,
            )

    def set_analysis_run(self, video_id: int, analysis_mode: str, status: str, candidate_count: int = 0) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO video_analysis_runs(video_id, analysis_mode, status, candidate_count, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(video_id, analysis_mode) DO UPDATE SET
                    status = excluded.status,
                    candidate_count = excluded.candidate_count,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (video_id, analysis_mode, status, candidate_count),
            )

    def confirm_analysis_revision(self, video_id: int, mode: str, signature: str, revision: int) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE video_analysis_runs SET scheme_revision = ?, updated_at = CURRENT_TIMESTAMP
                WHERE video_id = ? AND analysis_mode = ? AND status = 'completed' AND analysis_signature = ?""",
                (revision, video_id, mode, signature),
            )

    def publish_exam_results(
        self, video_id: int, mode: str, results: list[dict], signature: str, run_id: str, *, scheme_revision: int = 0,
    ) -> None:
        """Promote a complete run atomically, retaining matching user review decisions."""
        with self.connect() as connection:
            old = connection.execute(
                "SELECT * FROM video_markers WHERE video_id = ? AND analysis_mode = ?",
                (video_id, mode),
            ).fetchall()
            by_key = {(row["start_ms"], row["end_ms"], row["title"]): row for row in old}
            retained = set()
            for item in results:
                start, end, title = item["start_ms"], item["end_ms"], item["title"].strip()
                if end <= start or not title:
                    raise ValueError("Invalid production marker")
                # Different knowledge objects can share the same timestamp and signal.
                trigger = f"AI-{item['signal_type']}:{hashlib.sha256(title.encode('utf-8')).hexdigest()[:16]}"
                existing = by_key.get((start, end, title))
                values = (
                    title, item["reason"], item["source_quote"], trigger,
                )
                if existing:
                    retained.add(existing["id"])
                    connection.execute(
                        "UPDATE video_markers SET title = ?, note = ?, source_quote = ?, trigger_phrase = ? WHERE id = ?",
                        (*values, existing["id"]),
                    )
                else:
                    cursor = connection.execute(
                        """INSERT INTO video_markers(
                            video_id, start_ms, end_ms, title, note, source_quote,
                            trigger_phrase, confidence, analysis_mode
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1.0, ?)""",
                        (video_id, start, end, *values, mode),
                    )
                    retained.add(cursor.lastrowid)
            for row in old:
                if (
                    row["id"] not in retained and row["review_status"] != "excluded"
                    and row["user_start_ms"] is None
                    and row["importance"] == "standard"
                    and row["trigger_phrase"].startswith("AI-")
                ):
                    # Recheck user edits made after the analysis snapshot.
                    connection.execute(
                        "DELETE FROM video_markers WHERE id = ? AND user_start_ms IS NULL AND importance = 'standard'",
                        (row["id"],),
                    )
            connection.execute(
                """INSERT INTO video_analysis_runs(
                    video_id, analysis_mode, status, candidate_count, analysis_signature, experiment_id, scheme_revision
                ) VALUES (?, ?, 'completed', ?, ?, ?, ?)
                ON CONFLICT(video_id, analysis_mode) DO UPDATE SET
                    status = 'completed', candidate_count = excluded.candidate_count,
                    analysis_signature = excluded.analysis_signature,
                    experiment_id = excluded.experiment_id, scheme_revision = excluded.scheme_revision,
                    updated_at = CURRENT_TIMESTAMP""",
                (video_id, mode, len(results), signature, run_id, scheme_revision),
            )

    def record_transcription_metrics(self, video_id: int, elapsed_seconds: float) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO video_processing_metrics(video_id, transcription_seconds, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(video_id) DO UPDATE SET
                    transcription_seconds = excluded.transcription_seconds,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (video_id, max(0.0, float(elapsed_seconds))),
            )

    def record_ai_metrics(self, video_id: int, elapsed_seconds: float, chunk_count: int) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO video_processing_metrics(video_id, ai_seconds, ai_chunks, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(video_id) DO UPDATE SET
                    ai_seconds = excluded.ai_seconds,
                    ai_chunks = excluded.ai_chunks,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (video_id, max(0.0, float(elapsed_seconds)), max(0, int(chunk_count))),
            )

    def list_processing_metrics(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT v.id, v.duration_ms, v.status,
                    COALESCE(m.transcription_seconds, 0) AS transcription_seconds,
                    COALESCE(m.ai_seconds, 0) AS ai_seconds,
                    COALESCE(m.ai_chunks, 0) AS ai_chunks
                FROM videos v LEFT JOIN video_processing_metrics m ON m.video_id = v.id
                ORDER BY v.id
                """
            ).fetchall()

    def mark_interrupted_video_failed(self, video_id: int) -> bool:
        """Release a video only after the user confirms its processing task was interrupted."""
        with self.connect() as connection:
            video = connection.execute("SELECT status FROM videos WHERE id = ?", (video_id,)).fetchone()
            if not video or video["status"] != "processing":
                return False
            connection.execute("UPDATE videos SET status = 'failed' WHERE id = ?", (video_id,))
            connection.execute(
                "UPDATE video_analysis_runs SET status = 'failed', updated_at = CURRENT_TIMESTAMP "
                "WHERE video_id = ? AND status = 'processing'",
                (video_id,),
            )
            return True

    def delete_marker(self, marker_id: int) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM video_markers WHERE id = ?", (marker_id,))

    def create_job(self, job_id: str, job_type: str, video_id: int | None = None) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO processing_jobs(id, video_id, job_type, status, stage, progress, message)
                VALUES (?, ?, ?, 'queued', 'queued', 0, '')
                """,
                (job_id, video_id, job_type),
            )

    def get_job(self, job_id: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM processing_jobs WHERE id = ?", (job_id,)
            ).fetchone()

    def latest_batch_job(self) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM processing_jobs WHERE job_type = 'batch_video_process' "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1"
            ).fetchone()

    def update_job(
        self,
        job_id: str,
        *,
        status: str | None = None,
        stage: str | None = None,
        progress: float | None = None,
        message: str | None = None,
        error_message: str | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
        request_json: str | None = None,
        resource_json: str | None = None,
        preparation_json: str | None = None,
        cleanup_json: str | None = None,
    ) -> None:
        fields: list[str] = []
        values: list[object] = []
        for name, value in (
            ("status", status),
            ("stage", stage),
            ("progress", progress),
            ("message", message),
            ("error_message", error_message),
            ("started_at", started_at),
            ("finished_at", finished_at),
            ("request_json", request_json),
            ("resource_json", resource_json),
            ("preparation_json", preparation_json),
            ("cleanup_json", cleanup_json),
        ):
            if value is not None:
                fields.append(f"{name} = ?")
                values.append(value)
        if not fields:
            return
        values.append(job_id)
        with self.connect() as connection:
            connection.execute(
                f"UPDATE processing_jobs SET {', '.join(fields)} WHERE id = ?", values
            )

    def list_active_jobs(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM processing_jobs WHERE status IN ('queued', 'running') ORDER BY created_at, id"
            ).fetchall()

    def list_paused_jobs(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM processing_jobs WHERE status = 'resource_paused' ORDER BY created_at DESC, id DESC"
            ).fetchall()

    def mark_jobs_interrupted(self) -> int:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT video_id FROM processing_jobs WHERE status IN ('queued', 'running') "
                "AND video_id IS NOT NULL"
            ).fetchall()
            batch_rows = connection.execute(
                "SELECT request_json FROM processing_jobs "
                "WHERE status IN ('queued', 'running') AND video_id IS NULL"
            ).fetchall()
            batch_ids = []
            for row in batch_rows:
                try:
                    request = json.loads(row["request_json"] or "{}")
                    batch_ids.extend(int(item[0]) for item in request.get("items", []))
                    batch_ids.extend(
                        int(entry["video_id"]) for entry in request.get("entries", []) if entry.get("video_id")
                    )
                except (ValueError, TypeError, KeyError, IndexError):
                    continue
            cursor = connection.execute(
                """
                UPDATE processing_jobs
                SET status = 'interrupted', stage = 'interrupted',
                    message = '应用重启，任务已中断',
                    finished_at = CURRENT_TIMESTAMP
                WHERE status IN ('queued', 'running')
                """
            )
            for row in rows:
                connection.execute(
                    "UPDATE videos SET status = 'failed' WHERE id = ? AND status = 'processing'",
                    (int(row["video_id"]),),
                )
            for video_id in batch_ids:
                connection.execute(
                    "UPDATE videos SET status = 'failed' WHERE id = ? AND status = 'processing'",
                    (video_id,),
                )
            return cursor.rowcount
