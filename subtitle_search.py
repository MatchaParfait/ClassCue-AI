"""Local, literal subtitle lookup backed by SQLite's trigram index."""

from __future__ import annotations

import re
import sqlite3


MAX_QUERY_LENGTH = 120
MAX_PAGE_SIZE = 100
MAX_SQLITE_INTEGER = (1 << 63) - 1
SNIPPET_LENGTH = 200


def initialize_subtitle_search(connection: sqlite3.Connection) -> None:
    """Run inside the store's migration transaction; backfill only once."""
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'video_subtitle_fts'"
    ).fetchone()
    connection.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS video_subtitle_fts USING fts5("
        "text, content='video_transcript_segments', content_rowid='row_id', tokenize='trigram')"
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS video_subtitle_search_insert
        AFTER INSERT ON video_transcript_segments BEGIN
            INSERT INTO video_subtitle_fts(rowid, text) VALUES (new.row_id, new.text);
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS video_subtitle_search_delete
        AFTER DELETE ON video_transcript_segments BEGIN
            INSERT INTO video_subtitle_fts(video_subtitle_fts, rowid, text)
            VALUES ('delete', old.row_id, old.text);
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS video_subtitle_search_update
        AFTER UPDATE ON video_transcript_segments BEGIN
            INSERT INTO video_subtitle_fts(video_subtitle_fts, rowid, text)
            VALUES ('delete', old.row_id, old.text);
            INSERT INTO video_subtitle_fts(rowid, text) VALUES (new.row_id, new.text);
        END
        """
    )
    if not exists:
        connection.execute("INSERT INTO video_subtitle_fts(video_subtitle_fts) VALUES ('rebuild')")


def validate_query(query: str) -> str:
    query = query.strip()
    if not query or len(query) > MAX_QUERY_LENGTH or "\x00" in query:
        raise ValueError(f"搜索内容须为 1 至 {MAX_QUERY_LENGTH} 个字符，且不能包含空字符")
    return query


def _snippet(text: str, query: str) -> tuple[str, list[dict[str, object]]]:
    # ASCII case folding matches SQLite LIKE; all other characters stay literal.
    pattern = re.compile(re.escape(query), re.IGNORECASE | re.ASCII)
    first = pattern.search(text)
    start = max(0, first.start() - 50) if first else 0
    end = min(len(text), start + SNIPPET_LENGTH)
    start = max(0, end - SNIPPET_LENGTH)
    excerpt = text[start:end]
    parts: list[dict[str, object]] = []
    cursor = 0
    if start:
        parts.append({"text": "…", "match": False})
    for match in pattern.finditer(excerpt):
        if cursor < match.start():
            parts.append({"text": excerpt[cursor:match.start()], "match": False})
        parts.append({"text": match.group(), "match": True})
        cursor = match.end()
    if cursor < len(excerpt):
        parts.append({"text": excerpt[cursor:], "match": False})
    if end < len(text):
        parts.append({"text": "…", "match": False})
    return "".join(str(part["text"]) for part in parts), parts


def search_subtitles(
    connection: sqlite3.Connection,
    query: str,
    *,
    limit: int = 20,
    offset: int = 0,
    video_id: int | None = None,
    preferred_video_id: int | None = None,
) -> dict[str, object]:
    query = validate_query(query)
    if (not 1 <= limit <= MAX_PAGE_SIZE or not 0 <= offset <= MAX_SQLITE_INTEGER
            or any(value is not None and not 1 <= value <= MAX_SQLITE_INTEGER
                   for value in (video_id, preferred_video_id))):
        raise ValueError("搜索分页或视频参数无效")
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    parameters: list[object] = []
    index_join = ""
    predicates = []
    # FTS5 trigrams require three code points; shorter queries still work via LIKE.
    if len(query) >= 3:
        index_join = "JOIN video_subtitle_fts ON video_subtitle_fts.rowid = s.row_id"
        predicates.append("video_subtitle_fts MATCH ?")
        parameters.append('"' + query.replace('"', '""') + '"')
    predicates.append("s.text LIKE ? ESCAPE '\\'")
    parameters.append(f"%{escaped}%")
    if video_id is not None:
        predicates.append("s.video_id = ?")
        parameters.append(video_id)
    matching = f"""
        SELECT s.row_id, s.video_id, s.segment_id, s.start_ms, s.end_ms, s.text
        FROM video_transcript_segments s
        {index_join}
        JOIN videos v ON v.id = s.video_id
        WHERE {' AND '.join(predicates)}
    """
    # Counts and the page share a snapshot while ASR may commit new subtitles.
    connection.execute("BEGIN")
    totals = connection.execute(
        f"WITH matching AS ({matching}) "
        "SELECT COUNT(*) AS total, COUNT(DISTINCT video_id) AS total_videos FROM matching",
        parameters,
    ).fetchone()
    rows = connection.execute(
        f"""
        WITH matching AS ({matching}),
        counts AS (SELECT video_id, COUNT(*) AS video_match_count FROM matching GROUP BY video_id)
        SELECT m.*, c.video_match_count, v.filename,
               COALESCE(NULLIF(v.display_name, ''), v.filename) AS video_name,
               v.status AS video_status, v.duration_ms,
               v.library_folder_id AS folder_id, f.name AS folder_name
        FROM matching m
        JOIN counts c ON c.video_id = m.video_id
        JOIN videos v ON v.id = m.video_id
        LEFT JOIN video_folders f ON f.id = v.library_folder_id
        ORDER BY CASE WHEN m.video_id = ? THEN 0 ELSE 1 END,
                 c.video_match_count DESC, m.video_id DESC, m.start_ms, m.segment_id
        LIMIT ? OFFSET ?
        """,
        [*parameters, preferred_video_id, limit, offset],
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item.pop("row_id")
        item["snippet"], item["snippet_parts"] = _snippet(item["text"], query)
        items.append(item)
    total = int(totals["total"])
    return {
        "query": query,
        "total": total,
        "total_videos": int(totals["total_videos"]),
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(items) < total,
        "items": items,
    }
