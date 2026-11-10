"""Read-only, passage-oriented retrieval for the video-library MCP tool."""

from __future__ import annotations

from collections import Counter, defaultdict, deque
import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from classcue.subtitle_search import validate_query
from classcue.subtitle_phonetics import (
    INDEX_VERSION, find_matches, pending_count, prepare_phonetic_index, query_key,
)


SOURCES = ("catalog", "bookmarks", "subtitles", "analysis")
CANDIDATE_LIMIT = 4000
MAX_EVIDENCE = 6
Term = Annotated[str, Field(min_length=1, max_length=120)]
Source = Literal["catalog", "bookmarks", "subtitles", "analysis"]
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


class LibrarySearch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: Term | None = None
    queries: list[Term] | None = Field(default=None, min_length=1, max_length=8)
    term_groups: list[Annotated[list[Term], Field(min_length=1, max_length=8)]] | None = Field(
        default=None, min_length=1, max_length=6)
    match_mode: Literal["any", "all"] = "any"
    phonetic_mode: Literal["auto", "off", "always"] = "auto"
    exclude_terms: list[Term] = Field(default_factory=list, max_length=8)
    sources: list[Source] = Field(default_factory=lambda: list(SOURCES), min_length=1, max_length=4)
    course_id: str | None = Field(default=None, min_length=1, max_length=100)
    video_id: int | None = Field(default=None, strict=True, gt=0, le=(1 << 53) - 1)
    start_seconds: float | None = Field(default=None, strict=True, ge=0, le=1e10, allow_inf_nan=False)
    end_seconds: float | None = Field(default=None, strict=True, gt=0, le=1e10, allow_inf_nan=False)
    window_seconds: int = Field(default=45, strict=True, ge=1, le=120)
    context_seconds: int = Field(default=15, strict=True, ge=0, le=120)
    diverse: bool = Field(default=True, strict=True)
    limit: int = Field(default=20, strict=True, ge=1, le=50)
    offset: int = Field(default=0, strict=True, ge=0, le=(1 << 53) - 1)
    snapshot_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_search(self):
        if sum(v is not None for v in (self.query, self.queries, self.term_groups)) != 1:
            raise ValueError("请只提供 query、queries 或 term_groups 中的一项")
        if self.term_groups is not None and self.match_mode != "any":
            raise ValueError("term_groups 已规定组内任一、组间全部，不同时设置 match_mode=all")
        if self.query is not None:
            self.query = validate_query(self.query)
        if self.queries is not None:
            self.queries = list(dict.fromkeys(validate_query(t) for t in self.queries))
        if self.term_groups is not None:
            if sum(map(len, self.term_groups)) > 32:
                raise ValueError("关键词组合最多包含 32 个词")
            self.term_groups = [list(dict.fromkeys(validate_query(t) for t in group))
                                for group in self.term_groups]
        self.exclude_terms = list(dict.fromkeys(validate_query(t) for t in self.exclude_terms))
        if len(set(self.sources)) != len(self.sources):
            raise ValueError("sources 不能重复")
        self.sources = [source for source in SOURCES if source in self.sources]
        if (self.start_seconds is None) != (self.end_seconds is None):
            raise ValueError("时间范围须同时提供 start_seconds 和 end_seconds")
        if self.end_seconds is not None and self.end_seconds <= self.start_seconds:
            raise ValueError("结束时间必须晚于开始时间")
        if self.offset and not self.snapshot_id:
            raise ValueError("继续翻页须提供上一页的 snapshot_id")
        return self

    def groups(self):
        if self.term_groups is not None:
            return self.term_groups
        terms = self.queries or [self.query]
        return [terms] if self.match_mode == "any" else [[term] for term in terms]


def _fold(text):
    # Keep offsets and SQL LIKE/FTS candidate matching consistent.
    return text.translate(_ASCII_LOWER)


def _excerpt(text, terms, length=500):
    folded = _fold(text)
    positions = [folded.find(_fold(term)) for term in terms]
    first = min((pos for pos in positions if pos >= 0), default=0)
    start = max(0, first - 80)
    end = min(len(text), start + length)
    return text[start:end], bool(start or end < len(text)), start


def _contains(column, terms, *, indexed=False):
    clauses, parameters = [], []
    for term in terms:
        literal = f"instr(lower({column}), ?) > 0"
        values = [_fold(term)]
        if indexed and len(term) >= 3:
            literal = ("s.row_id IN (SELECT rowid FROM video_subtitle_fts "
                       "WHERE video_subtitle_fts MATCH ?) AND " + literal)
            values.insert(0, '"' + term.replace('"', '""') + '"')
        clauses.append(f"({literal})")
        parameters.extend(values)
    return "(" + " OR ".join(clauses) + ")", parameters


class _Search:
    def __init__(self, db, args, scope_sql, scope_parameters, url):
        self.db, self.args, self.url = db, args, url
        self.scope_sql, self.scope_parameters = scope_sql, scope_parameters
        self.groups = [[_fold(t) for t in group] for group in args.groups()]
        self.terms = list(dict.fromkeys(t for group in args.groups() for t in group))
        self.excludes = [_fold(t) for t in args.exclude_terms]
        self.truncated = []
        self.counts = {source: 0 for source in args.sources}
        self.window = args.window_seconds * 1000
        self.videos = {row["id"]: dict(row) for row in db.execute(scope_sql, scope_parameters)}
        self.paths, self.course_names = self._paths()
        self.phonetic_keys = {}

    def _paths(self):
        folders = {row["id"]: dict(row) for row in self.db.execute(
            "SELECT id,name,parent_id FROM video_folders")}
        courses = {row["folder_id"]: row["name"] for row in self.db.execute(
            "SELECT folder_id,name FROM study_courses WHERE folder_id IS NOT NULL")}
        paths, course_names = {}, {}
        for video in self.videos.values():
            parts, names, seen = [], [], set()
            current = video["library_folder_id"]
            while current in folders and current not in seen:
                seen.add(current)
                parts.append(folders[current]["name"])
                if current in courses:
                    names.append(courses[current])
                current = folders[current]["parent_id"]
            paths[video["id"]] = " / ".join(reversed(parts))
            course_names[video["id"]] = " / ".join(reversed(names))
        return paths, course_names

    def _matches(self, text, *, phonetic=False):
        terms = {_fold(term) for term in self._terms(text, phonetic=phonetic)}
        return {i for i, group in enumerate(self.groups) if any(t in terms for t in group)}

    def _allowed(self, text):
        folded = _fold(text)
        return not any(t in folded for t in self.excludes)

    def _passage_allowed(self, video_id, start, end):
        if not self.excludes:
            return True
        condition, parameters = _contains("s.text", self.args.exclude_terms)
        return not self.db.execute(
            f"SELECT 1 FROM video_transcript_segments s WHERE s.video_id=? "
            f"AND s.end_ms>? AND s.start_ms<? AND {condition} LIMIT 1",
            [video_id, start, end, *parameters],
        ).fetchone()

    def _terms(self, text, *, phonetic=False):
        folded = _fold(text)
        sounds = {m["query_term"] for m in find_matches(text, self.phonetic_keys)} if phonetic else set()
        return [term for term in self.terms if _fold(term) in folded or term in sounds]

    def _rows(self, source, select, column, extra="", *, indexed=False):
        condition, parameters = _contains(column, self.terms, indexed=indexed)
        order, order_parameters = "video_id,start_ms,record_id", []
        if source == "subtitles" and self.phonetic_keys:
            alternatives = [condition]
            for key in dict.fromkeys(self.phonetic_keys.values()):
                alternatives.append(
                    "(s.row_id IN (SELECT row_id FROM video_subtitle_phonetics "
                    "WHERE row_id IN (SELECT rowid FROM video_subtitle_phonetic_fts "
                    "WHERE video_subtitle_phonetic_fts MATCH ?) "
                    "AND version=? AND instr(pronunciation,?)>0))")
                parameters.extend(['"' + key + '"', INDEX_VERSION, key])
            condition = "(" + " OR ".join(alternatives) + ")"
            priority, order_parameters = _contains(column, self.terms)
            order = f"{priority} DESC," + order
        rows = self.db.execute(
            f"WITH scope AS ({self.scope_sql}) {select} WHERE {condition} {extra} "
            f"ORDER BY {order} LIMIT ?",
            [*self.scope_parameters, *parameters, *order_parameters, CANDIDATE_LIMIT + 1],
        ).fetchall()
        if len(rows) > CANDIDATE_LIMIT:
            self.truncated.append(source)
        selected = [dict(row) for row in rows[:CANDIDATE_LIMIT]]
        return sorted(selected, key=lambda row: (row["video_id"], row["start_ms"] or 0, row["record_id"]))

    def _time_filter(self, start, end):
        if self.args.start_seconds is None:
            return ""
        # Values are validated finite numbers, converted to integer milliseconds.
        lower = round(self.args.start_seconds * 1000)
        upper = round(self.args.end_seconds * 1000)
        return f" AND {end}>{lower} AND {start}<{upper}"

    def _hit(self, source, video_id, rows, text, start, end, position, evidence_type):
        phonetic = source == "subtitles" and bool(self.phonetic_keys)
        terms = list(dict.fromkeys(term for row in rows
                                   for term in self._terms(row["text"], phonetic=phonetic)))
        evidence = []
        for row in rows:
            raw = row["text"]
            literal_terms = self._terms(raw)
            sounds = [m for m in find_matches(raw, self.phonetic_keys)
                      if m["query_term"] not in literal_terms] if phonetic else []
            excerpt, truncated, excerpt_start = _excerpt(
                raw, [*self.terms, *(m["source_text"] for m in sounds)])
            visible = [{**m, "start_char": m["start_char"] - excerpt_start,
                        "end_char": m["end_char"] - excerpt_start} for m in sounds
                       if excerpt_start <= m["start_char"] and m["end_char"] <= excerpt_start + len(excerpt)]
            item = {"source": source, "evidence_type": evidence_type,
                    "record_id": row["record_id"], "text": excerpt,
                    "text_truncated": truncated, "matched_terms": self._terms(raw, phonetic=phonetic),
                    "literal_matched_terms": literal_terms,
                    "match_type": "mixed" if sounds and literal_terms else "phonetic" if sounds else "literal",
                    "phonetic_matches": visible[:8],
                    "phonetic_matches_truncated": len(visible[:8]) != len(sounds),
                    "start_seconds": row["start_ms"] / 1000 if row["start_ms"] is not None else None,
                    "end_seconds": row["end_ms"] / 1000 if row["end_ms"] is not None else None}
            for name in ("analysis_mode", "review_status", "jump_seconds"):
                if name in row:
                    item[name] = row[name]
            evidence.append(item)
        span = end - start if start is not None else self.window
        score = (50 + 8 * min(len(terms), 8) + max(0, 6 * (1 - span / self.window))
                 + {"catalog": 0, "bookmarks": 6, "subtitles": 5, "analysis": 3}[source])
        if any(e["match_type"] != "literal" for e in evidence):
            score -= 16
        self.counts[source] += 1
        return {"video_id": video_id, "_start": start, "_end": end, "_position": position,
                "score": score, "evidence": evidence, "matched_terms": terms}

    def catalog(self):
        hits = []
        if self.args.start_seconds is not None:
            return hits
        for video in self.videos.values():
            text = "\n".join([video["video_name"], video["filename"],
                              self.paths[video["id"]], self.course_names[video["id"]]])
            if len(self._matches(text)) != len(self.groups) or not self._allowed(text):
                continue
            row = {"record_id": video["id"], "start_ms": None, "end_ms": None, "text": text}
            hits.append(self._hit("catalog", video["id"], [row], text, None, None, None, "video_metadata"))
            if len(hits) > CANDIDATE_LIMIT:
                self.truncated.append("catalog")
                self.counts["catalog"] -= 1
                return hits[:CANDIDATE_LIMIT]
        return hits

    def bookmarks(self):
        point = "b.position_ms"
        time_filter = self._time_filter(point, f"({point}+1)")
        rows = self._rows("bookmarks",
                          "SELECT b.id record_id,b.video_id,b.position_ms start_ms,"
                          "b.position_ms end_ms,b.note text FROM video_bookmarks b JOIN scope v ON v.id=b.video_id",
                          "b.note", time_filter)
        return [self._hit("bookmarks", row["video_id"], [row], row["text"], row["start_ms"],
                          row["end_ms"], row["start_ms"], "user_bookmark") for row in rows
                if len(self._matches(row["text"])) == len(self.groups) and self._allowed(row["text"])]

    def analysis(self):
        column = "(m.title || char(10) || m.note || char(10) || m.source_quote)"
        rows = self._rows(
            "analysis", "SELECT m.id record_id,m.video_id,m.start_ms,m.end_ms,"
            f"{column} text,m.trigger_phrase,m.analysis_mode,m.review_status,"
            "COALESCE(m.user_start_ms,m.start_ms)/1000.0 jump_seconds "
            "FROM video_markers m JOIN scope v ON v.id=m.video_id",
            column, " AND m.review_status!='excluded'" + self._time_filter("m.start_ms", "m.end_ms"))
        return [self._hit("analysis", row["video_id"], [row], row["text"], row["start_ms"],
                          row["end_ms"], round(row["jump_seconds"] * 1000),
                          "manual_marker" if row["trigger_phrase"].startswith("MANUAL-") else "saved_analysis")
                for row in rows if len(self._matches(row["text"])) == len(self.groups)
                and self._allowed(row["text"])]

    def subtitles(self):
        rows = self._rows(
            "subtitles", "SELECT s.segment_id record_id,s.video_id,s.start_ms,s.end_ms,s.text "
            "FROM video_transcript_segments s JOIN scope v ON v.id=s.video_id",
            "s.text", self._time_filter("s.start_ms", "s.end_ms"), indexed=True)
        by_video = defaultdict(list)
        for row in rows:
            row["_groups"] = self._matches(row["text"], phonetic=bool(self.phonetic_keys))
            by_video[row["video_id"]].append(row)
        hits = []
        for video_id, segments in by_video.items():
            windows, counts, ends, left = [], Counter(), deque(), 0
            for right, row in enumerate(segments):
                counts.update(row["_groups"])
                while ends and segments[ends[-1]]["end_ms"] <= row["end_ms"]:
                    ends.pop()
                ends.append(right)
                while left < right and segments[ends[0]]["end_ms"] - segments[left]["start_ms"] > self.window:
                    counts.subtract(segments[left]["_groups"])
                    left += 1
                    while ends[0] < left:
                        ends.popleft()
                if not all(counts[i] > 0 for i in range(len(self.groups))):
                    continue
                while left < right and all(counts[i] - (i in segments[left]["_groups"]) > 0
                                           for i in range(len(self.groups))):
                    counts.subtract(segments[left]["_groups"])
                    left += 1
                    while ends[0] < left:
                        ends.popleft()
                start, end = segments[left]["start_ms"], segments[ends[0]]["end_ms"]
                if (windows and start <= windows[-1][1] + 5000
                        and end - windows[-1][0] <= self.window
                        and self._passage_allowed(video_id, windows[-1][0], max(end, windows[-1][1]))):
                    previous = windows[-1]
                    windows[-1] = (previous[0], max(end, previous[1]), previous[2], right)
                else:
                    windows.append((start, end, left, right))
            for start, end, first, last in windows:
                evidence = segments[first:last + 1]
                if not self._passage_allowed(video_id, start, end):
                    continue
                # Separate records with a newline: a phrase must occur in an original
                # sentence, while AND groups may be satisfied by different sentences.
                text = "\n".join(row["text"] for row in evidence)
                hits.append(self._hit("subtitles", video_id, evidence, text, start, end, start, "asr_transcript"))
        return hits

    def finish(self, hits):
        merged = []
        by_video = defaultdict(list)
        for hit in hits:
            by_video[hit["video_id"]].append(hit)
        for video_id, values in by_video.items():
            timed = sorted((h for h in values if h["_start"] is not None),
                           key=lambda h: (h["_start"], h["_end"], -h["score"]))
            groups = []
            for hit in timed:
                if (groups and hit["_start"] <= groups[-1]["_end"] + 5000
                        and max(hit["_end"], groups[-1]["_end"]) - groups[-1]["_start"] <= self.window
                        and (not self.excludes or not any(e["source"] == "subtitles" for e in
                                     [*groups[-1]["evidence"], *hit["evidence"]])
                             or self._passage_allowed(video_id, groups[-1]["_start"],
                                                      max(hit["_end"], groups[-1]["_end"])))):
                    group = groups[-1]
                    if hit["score"] > group["score"]:
                        group["_position"] = hit["_position"]
                        group["score"] = hit["score"]
                    group["_end"] = max(group["_end"], hit["_end"])
                    group["evidence"].extend(hit["evidence"])
                    group["matched_terms"] = list(dict.fromkeys([*group["matched_terms"], *hit["matched_terms"]]))
                else:
                    groups.append(hit)
            groups.extend(h for h in values if h["_start"] is None)
            video = self.videos[video_id]
            for hit in groups:
                sources = [source for source in SOURCES if any(e["source"] == source for e in hit["evidence"])]
                hit["score"] = round(hit["score"] + 3 * (len(sources) - 1), 4)
                # Select evidence covering the most query groups before filling the
                # remaining slots; long passages must not hide their decisive words.
                evidence, covered = [], set()
                remaining = list(hit["evidence"])
                while remaining and len(evidence) < MAX_EVIDENCE:
                    best = max(range(len(remaining)), key=lambda i: (
                        len(self._evidence_groups(remaining[i]) - covered),
                        len(remaining[i]["matched_terms"])))
                    item = remaining.pop(best)
                    evidence.append(item)
                    covered.update(self._evidence_groups(item))
                evidence.sort(key=lambda e: (e["start_seconds"] or 0, e["source"], e["record_id"]))
                position = hit["_position"]
                context = None
                if position is not None and video["duration_ms"] > 0:
                    start = max(0, min(hit["_start"], position) - self.args.context_seconds * 1000)
                    end = min(video["duration_ms"], max(hit["_end"], position + 1)
                              + self.args.context_seconds * 1000)
                    if start < end:
                        context = {"start_seconds": start / 1000, "end_seconds": end / 1000}
                identity = [[e["source"], e["record_id"]] for e in hit["evidence"]]
                result_id = hashlib.sha256(json.dumps([video_id, sorted(identity)]).encode()).hexdigest()[:24]
                merged.append({
                    "result_id": result_id, "video_id": video_id, "video_name": video["video_name"],
                    "folder_id": video["library_folder_id"], "folder_path": self.paths[video_id],
                    "sources": sources, "score": hit["score"], "matched_terms": hit["matched_terms"],
                    "match_type": self._match_type(hit["evidence"]),
                    "position_seconds": position / 1000 if position is not None else None,
                    "start_seconds": hit["_start"] / 1000 if hit["_start"] is not None else None,
                    "end_seconds": hit["_end"] / 1000 if hit["_end"] is not None else None,
                    "context_range": context, "has_subtitles": bool(video["has_subtitles"]),
                    "evidence": evidence, "evidence_count": len(hit["evidence"]),
                    "evidence_truncated": bool(remaining),
                    "play_url": self.url(video_id, position / 1000 if position is not None else 0),
                })
        merged.sort(key=lambda item: (item["match_type"] != "literal", -item["score"], item["video_id"],
                                     item["position_seconds"] if item["position_seconds"] is not None else -1,
                                     item["result_id"]))
        if self.args.diverse:
            occurrences = Counter()
            for item in merged:
                item["_rank_score"] = item["score"] / (1 + .25 * occurrences[item["video_id"]])
                occurrences[item["video_id"]] += 1
            merged.sort(key=lambda item: (item["match_type"] != "literal",
                                         -item["_rank_score"], -item["score"], item["result_id"]))
            for item in merged:
                del item["_rank_score"]
        return merged

    def _evidence_groups(self, evidence):
        terms = {_fold(t) for t in evidence["matched_terms"]}
        return {i for i, group in enumerate(self.groups) if any(t in terms for t in group)}

    @staticmethod
    def _match_type(evidence):
        kinds = {e["match_type"] for e in evidence}
        return "literal" if kinds == {"literal"} else "phonetic" if kinds == {"phonetic"} else "mixed"


def search_library(db, args, *, folder_id, root_only, url, _prepare_index=True):
    """Read one final snapshot; only a requested fallback warms a derived cache."""
    predicates, parameters = ["v.is_secondary=0"], []
    if root_only:
        predicates.append("v.library_folder_id IS NULL")
    if folder_id is not None:
        predicates.append("v.library_folder_id IN (WITH RECURSIVE tree(id) AS "
                          "(SELECT id FROM video_folders WHERE id=? UNION "
                          "SELECT f.id FROM video_folders f JOIN tree t ON f.parent_id=t.id) SELECT id FROM tree)")
        parameters.append(folder_id)
    if args.video_id is not None:
        if not db.execute("SELECT 1 FROM videos WHERE id=? AND is_secondary=0", (args.video_id,)).fetchone():
            raise KeyError("视频不存在或不是独立课程视频")
        predicates.append("v.id=?")
        parameters.append(args.video_id)
    scope = ("SELECT v.id,v.filename,COALESCE(NULLIF(v.display_name,''),v.filename) video_name,"
             "v.library_folder_id,v.duration_ms,EXISTS(SELECT 1 FROM video_transcript_segments s "
             "WHERE s.video_id=v.id) has_subtitles FROM videos v WHERE " + " AND ".join(predicates))
    search = _Search(db, args, scope, parameters, url)
    hits = []
    for source in args.sources:
        hits.extend(getattr(search, source)())
    phonetic_used, index_pending = False, 0
    if (args.phonetic_mode != "off" and "subtitles" in args.sources and search.videos
            and (args.phonetic_mode == "always" or (not hits and not search.truncated))):
        search.phonetic_keys = {term: key for term in search.terms if (key := query_key(term))}
        phonetic_used = bool(search.phonetic_keys)
        if phonetic_used:
            time_filter = search._time_filter("s.start_ms", "s.end_ms")
            index_pending = pending_count(db, scope, parameters, time_filter)
            if index_pending and _prepare_index:
                # Release the old read snapshot before cache writes. Compute outside
                # write transactions, then repeat all sources in one final snapshot.
                db.rollback()
                prepare_phonetic_index(db, scope, parameters, time_filter)
                db.execute("BEGIN")
                return search_library(db, args, folder_id=folder_id, root_only=root_only,
                                      url=url, _prepare_index=False)
            hits = [hit for hit in hits if not any(e["source"] == "subtitles" for e in hit["evidence"])]
            search.counts["subtitles"] = 0
            search.truncated = [s for s in search.truncated if s != "subtitles"]
            hits.extend(search.subtitles())
    items = search.finish(hits)
    coverage = {
        "video_count": len(search.videos),
        "with_subtitles": sum(bool(video["has_subtitles"]) for video in search.videos.values()),
        "missing_subtitles": sum(not video["has_subtitles"] for video in search.videos.values()),
    }
    query = args.model_dump(exclude={"limit", "offset", "snapshot_id"})
    snapshot = hashlib.sha256(json.dumps(
        [query, items, coverage, search.truncated, search.counts, phonetic_used, index_pending],
        ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if args.snapshot_id and args.snapshot_id != snapshot:
        raise ValueError("搜索资料或参数已变化，请从 offset=0 重新搜索，不沿用旧 snapshot_id")
    page = items[args.offset:args.offset + args.limit]
    has_more = args.offset + len(page) < len(items)
    return {
        "items": page, "total": len(items), "total_videos": len({item["video_id"] for item in items}),
        "has_more": has_more, "next_offset": args.offset + len(page) if has_more else None,
        "snapshot_id": snapshot, "searched_sources": args.sources, "source_counts": search.counts,
        "coverage": coverage, "match_type": "phonetic_passage" if phonetic_used else "literal_passage",
        "phonetic_mode": args.phonetic_mode, "phonetic_search_used": phonetic_used,
        "phonetic_index_pending": index_pending,
        "phonetic_terms": list(search.phonetic_keys),
        "term_groups": args.groups(),
        "exclude_terms": args.exclude_terms, "window_seconds": args.window_seconds,
        "search_complete": not search.truncated and not index_pending,
        "total_is_exact": not search.truncated and not index_pending,
        "candidate_truncated_sources": search.truncated, "candidate_limit_per_source": CANDIDATE_LIMIT,
        "empty_reason": (None if items else "no_videos" if not search.videos else
                         "incomplete_search" if search.truncated or index_pending else "no_matching_content"),
    }
