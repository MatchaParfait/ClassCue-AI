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
    folder_id: int | None = None,
    root_only: bool = False,
    queries: list[str] | None = None,
    match_mode: str = "any",
    merge_gap_ms: int | None = None,
    diverse: bool = False,
) -> dict[str, object]:
    query = validate_query(query)
    terms = list(dict.fromkeys(validate_query(term) for term in (queries or [query])))
    if len(terms) > 8 or match_mode not in {"any", "all"} or (
        merge_gap_ms is not None and not 0 <= merge_gap_ms <= 60_000
    ):
        raise ValueError("搜索候选词、匹配方式或合并间隔无效")
    if (not 1 <= limit <= MAX_PAGE_SIZE or not 0 <= offset <= MAX_SQLITE_INTEGER
            or any(value is not None and not 1 <= value <= MAX_SQLITE_INTEGER
                   for value in (video_id, preferred_video_id, folder_id))):
        raise ValueError("搜索分页、视频或文件夹参数无效")
    parameters: list[object] = []
    predicates = ["v.is_secondary = 0"]
    if root_only:
        predicates.append("v.library_folder_id IS NULL")
    alternatives = []
    for term in terms:
        clauses = []
        # FTS5 trigrams require three code points; shorter queries still use LIKE.
        if len(term) >= 3:
            clauses.append("s.row_id IN (SELECT rowid FROM video_subtitle_fts WHERE video_subtitle_fts MATCH ?)")
            parameters.append('"' + term.replace('"', '""') + '"')
        clauses.append("s.text LIKE ? ESCAPE '\\'")
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        parameters.append(f"%{escaped}%")
        alternatives.append("(" + " AND ".join(clauses) + ")")
    predicates.append("(" + (" OR " if match_mode == "any" else " AND ").join(alternatives) + ")")
    if video_id is not None:
        predicates.append("s.video_id = ?")
        parameters.append(video_id)
    if folder_id is not None:
        predicates.append("""
            v.library_folder_id IN (
                WITH RECURSIVE tree(id) AS (
                    SELECT id FROM video_folders WHERE id = ?
                    UNION
                    SELECT f.id FROM video_folders f JOIN tree t ON f.parent_id = t.id
                )
                SELECT id FROM tree
            )
        """)
        parameters.append(folder_id)
    matching = f"""
        SELECT s.row_id, s.video_id, s.segment_id, s.start_ms, s.end_ms, s.text
        FROM video_transcript_segments s
        JOIN videos v ON v.id = s.video_id
        WHERE {' AND '.join(predicates)}
    """
    # Counts and the page share a snapshot while ASR may commit new subtitles.
    connection.execute("BEGIN")
    grouped_ctes = "grouped AS (SELECT *, 1 AS match_count FROM matching)"
    if merge_gap_ms is not None:
        grouped_ctes = f"""
            boundaries AS (
                SELECT *, CASE WHEN start_ms > MAX(end_ms) OVER (
                    PARTITION BY video_id ORDER BY start_ms, segment_id
                    ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                ) + {merge_gap_ms} THEN 1 ELSE 0 END AS boundary FROM matching
            ),
            clustered AS (
                SELECT *, SUM(boundary) OVER (
                    PARTITION BY video_id ORDER BY start_ms, segment_id) AS cluster FROM boundaries
            ),
            representatives AS (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY video_id, cluster ORDER BY start_ms, segment_id) AS position,
                    MAX(end_ms) OVER (PARTITION BY video_id, cluster) AS group_end_ms,
                    COUNT(*) OVER (PARTITION BY video_id, cluster) AS match_count FROM clustered
            ),
            grouped AS (
                SELECT row_id, video_id, segment_id, start_ms, group_end_ms AS end_ms, text, match_count
                FROM representatives WHERE position=1
            )
        """
    ctes = f"WITH matching AS ({matching}), {grouped_ctes}"
    totals = connection.execute(
        ctes + " SELECT COUNT(*) AS total, COUNT(DISTINCT video_id) AS total_videos,"
        "COALESCE(SUM(match_count),0) AS total_matches FROM grouped",
        parameters,
    ).fetchone()
    rows = connection.execute(
        f"""
        {ctes},
        counts AS (SELECT video_id, SUM(match_count) AS video_match_count FROM grouped GROUP BY video_id),
        ranked AS (SELECT *, ROW_NUMBER() OVER (
            PARTITION BY video_id ORDER BY start_ms, segment_id) AS video_rank FROM grouped)
        SELECT m.row_id, m.video_id, s.segment_id, m.start_ms, m.end_ms, m.match_count,
               s.text, c.video_match_count, v.filename,
               COALESCE(NULLIF(v.display_name, ''), v.filename) AS video_name,
               v.status AS video_status, v.duration_ms,
               v.library_folder_id AS folder_id, f.name AS folder_name
        FROM ranked m
        JOIN video_transcript_segments s ON s.row_id = m.row_id
        JOIN counts c ON c.video_id = m.video_id
        JOIN videos v ON v.id = m.video_id
        LEFT JOIN video_folders f ON f.id = v.library_folder_id
        ORDER BY CASE WHEN m.video_id = ? THEN 0 ELSE 1 END,
                 {"m.video_rank," if diverse else ""}
                 c.video_match_count DESC, m.video_id DESC, m.start_ms, m.segment_id
        LIMIT ? OFFSET ?
        """,
        [*parameters, preferred_video_id, limit, offset],
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item.pop("row_id")
        matched = next((term for term in terms if re.search(re.escape(term), item["text"],
                        re.IGNORECASE | re.ASCII)), query)
        item["snippet"], item["snippet_parts"] = _snippet(item["text"], matched)
        items.append(item)
    total = int(totals["total"])
    return {
        "query": query,
        "total": total,
        "total_videos": int(totals["total_videos"]),
        "total_matches": int(totals["total_matches"]),
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(items) < total,
        "items": items,
    }
