# Cross-video subtitle search

`GET /api/library/search` searches saved subtitles in the active workspace,
including all logical folders and videos whose AI processing is pending or has
failed. It does not load videos, run ASR, or make AI/network requests.

## Request

```http
GET /api/library/search?q=%E5%88%86%E6%B2%BB%E6%B3%95&limit=20&offset=0
```

| Parameter | Meaning |
| --- | --- |
| `q` | Required literal substring, 1-120 characters; surrounding whitespace is trimmed. |
| `limit` | Number of matching subtitle segments per page, default 20, maximum 100. |
| `offset` | Zero-based result offset, default 0; at most the signed 64-bit maximum. |
| `video_id` | Optional positive signed 64-bit video ID to fetch more hits within one video. |

Chinese substrings and single-character/two-character queries are supported.
ASCII English matching is case-insensitive. Quotes, `%`, `_`, and operators
such as `OR` are ordinary text, not wildcards or search-language syntax.
Internal spaces and punctuation are significant. This first version searches
within individual subtitle segments; it does not join adjacent segments or
perform semantic, synonym, phonetic, or typo correction. A segment containing
several occurrences is one result, not one result per occurrence.

Blank queries, null characters, overlong queries, and invalid pagination receive
HTTP 422. No matches (including an unknown `video_id` filter) return HTTP 200
with empty `items` and zero totals.

## Response

```json
{
  "query": "分治法",
  "library_id": "aabbccddeeff",
  "total": 3,
  "total_videos": 2,
  "limit": 20,
  "offset": 0,
  "has_more": false,
  "items": [
    {
      "video_id": 12,
      "video_name": "算法第一课",
      "filename": "算法第一课.mp4",
      "video_status": "transcribed",
      "duration_ms": 3600000,
      "folder_id": 4,
      "folder_name": "算法课程",
      "video_match_count": 2,
      "segment_id": 85,
      "start_ms": 450000,
      "end_ms": 455000,
      "text": "我们先讲分治法",
      "snippet": "我们先讲分治法",
      "snippet_parts": [
        {"text": "我们先讲", "match": false},
        {"text": "分治法", "match": true}
      ]
    }
  ]
}
```

The example abbreviates `items`; a real first page above contains all three
matching segments. `total` counts matching segments, `total_videos` counts
distinct matching videos, and `video_match_count` counts all matching segments
in that result's video, independent of pagination. Root-level videos have null
`folder_id` and `folder_name`. Names, folder membership, and statuses come from
current database records, so moves and renames do not invalidate results.

Results are grouped by video: more matching segments first, then descending
video ID; segments within each video are ordered by start time and segment ID.
This ordering is deterministic for an unchanged dataset. Each request reads its
counts and page in one SQLite snapshot. Subsequent pages may shift if ASR saves
or replaces subtitles between requests; reset pagination when refreshing.

`text` is the full source segment. `snippet` is up to 200 source characters plus
leading/trailing ellipses, centered near the first occurrence. `snippet_parts`
contains plain text with explicit highlight flags. Render these as text nodes;
never pass subtitle text to `innerHTML` / `dangerouslySetInnerHTML`.

## Frontend integration

1. Add the search field to the library; debounce requests by about 250 ms.
   Clear results without a request when input becomes blank. Cancel or discard
   stale requests when the query/workspace changes.
2. Key caches by workspace, query, optional video ID, and pagination. Check the
   returned `library_id` before displaying results after a workspace switch.
3. Render names, folder, `snippet_parts`, and a time label derived from
   `start_ms`. Group by `video_id` and use `video_match_count` for more-results
   controls if desired.
4. When clicked, open `/review/{video_id}` and pass `start_ms` as navigation
   state or a chosen URL query parameter. The review player must consume that
   value after loading metadata and seek to `start_ms / 1000` seconds. Handle
   a different result clicked within the same video as well.
5. The timestamp is the source subtitle's beginning, not an AI marker's time.
   Search does not create, select, or modify markers and does not overwrite
   saved manual jump times. Missing media remains searchable as saved text;
   playback should use the existing missing-file error handling.

The frontend now exposes a global subtitle-search capsule on the library,
processing, settings, and review headers. Ctrl+K / Cmd+K opens the same dialog;
on narrow screens the capsule becomes a search icon. Results are grouped by
video, highlight literal matches, and load 20 more segments at a time. The
dialog preserves its query and loaded pages across navigation. Workspace
changes clear that state; returned workspace IDs guard against mixing pages.

Search navigation uses `/review/:videoId?start_ms=…&library_id=…`.
The player waits for media metadata, gives the explicit timestamp priority
over locally saved playback progress, and consumes each navigation once.
Selecting another hit (including the same timestamp) in the current video
seeks again without remounting its player. Invalid or cross-workspace timestamp
parameters are ignored. Existing missing-media errors remain visible.

The dialog debounces input by 250 ms, waits for IME composition to finish,
aborts superseded requests and discards late responses, supports arrow-key
selection / Enter / Escape, traps focus, and restores it after closing.
Opening search preserves the underlying form drafts, playback and scrolling.
It uses the existing yellow/green theme roles and reduced-motion support.
See [frontend search notes](frontend-search.md) for implementation and QA.

## Storage and migration

The index is an external-content SQLite FTS5 table using the built-in trigram
tokenizer, keyed to `video_transcript_segments.row_id`. Queries of three or
more characters use the index and a final escaped literal LIKE check. Shorter
queries use LIKE directly. No tokenizer package or external search service is
required. SQLite must include FTS5 with trigram support (SQLite 3.34+).

Existing subtitles are indexed once during the existing transactional database
migration. Normal restarts do not rebuild the index. Insert/update/delete
triggers keep it synchronized atomically with transcription replacement,
rollback, and cascaded video deletion, including direct SQL writes. Video and
review data remain authoritative and are not rewritten by the search migration.

Verification uses temporary databases only:

```powershell
python -m pytest tests/test_subtitle_search.py
```

## Validation snapshot (2026-10-15)

- All 42 search tests pass, including literal Chinese/English searches,
  parameter boundaries, pagination, highlighting, active-workspace isolation,
  index synchronization, concurrent reads/writes, and transactional migration.
- Related store, migration, library, API, manual-marker-time, importance,
  backend-ownership, and batch regression coverage passed 155 existing tests.
  Five assertions in `tests/test_library_queries.py` fail identically on the
  unmodified base commit `785c6f5`: they expect counts before the existing
  marker-importance aggregation query was added. Those unrelated assertions
  were not changed here.
- A disposable 60-video, 60,000-segment dataset took about 413 ms to backfill.
  Across seven runs each, median lookup times were 20 ms for an indexed phrase
  with 300 hits, 42 ms for a two-character query with 300 hits, and 91 ms for a
  common indexed term matching every segment. Each response returned 20 items.
  These are local synthetic measurements, not a production latency guarantee.
- No real user workspace was migrated, no media files were moved, and no AI
  requests were needed. The running frontend/backend were left on their current
  checkout; the new API becomes available after integrating this branch and
  restarting the backend.
