"""Bounded course operations shared by the authenticated MCP endpoint."""

from __future__ import annotations

import base64
import hashlib
from io import BytesIO
import json
from pathlib import Path
import time
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from classcue.asr_models import frozen_selection, selection_snapshot, get_model, require_firered_gpu
from classcue.diagnostics import redact_text
from classcue.library_search import LibrarySearch, search_library
from classcue.processing_control import ProcessingCancelled, ResourcePaused
from classcue.playback_links import playback_url
from classcue.recall_schemes import ModeId
from classcue.transcriber import require_prepared_asr_model


JOB_TYPE = "course_transcribe"
SEQUENCE_TYPE = "course_sequence"
ACTIVE = {"queued", "running", "resource_paused"}
MAX_INTEGER = (1 << 53) - 1


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Page(Arguments):
    query: str = Field(default="", max_length=120)
    limit: int = Field(default=20, ge=1, le=50, strict=True)
    offset: int = Field(default=0, ge=0, le=MAX_INTEGER, strict=True)


class ScopedPage(Page):
    course_id: str | None = Field(default=None, max_length=100)
    subtitle_status: str = Field(default="all", pattern="^(all|available|missing)$")


class Search(Page):
    query: str | None = Field(default=None, min_length=1, max_length=120)
    queries: list[Annotated[str, Field(min_length=1, max_length=120)]] | None = Field(
        default=None, min_length=1, max_length=8)
    course_id: str | None = Field(default=None, max_length=100)
    video_id: int | None = Field(default=None, gt=0, le=MAX_INTEGER, strict=True)
    match_mode: Literal["any", "all"] = "any"
    merge_gap_seconds: int = Field(default=5, ge=0, le=60, strict=True)
    diverse: bool = Field(default=True, strict=True)
    context_seconds: int = Field(default=20, ge=0, le=120, strict=True)

    @model_validator(mode="after")
    def terms(self):
        if (self.query is None) == (self.queries is None):
            raise ValueError("请只提供 query 或 queries 中的一项")
        return self


class BookmarkSearch(Page):
    query: str | None = Field(default=None, min_length=1, max_length=120)
    queries: list[Annotated[str, Field(min_length=1, max_length=120)]] | None = Field(
        default=None, min_length=1, max_length=8)
    course_id: str | None = Field(default=None, max_length=100)
    video_id: int | None = Field(default=None, gt=0, le=MAX_INTEGER, strict=True)
    match_mode: Literal["any", "all"] = "any"
    context_seconds: int = Field(default=30, ge=1, le=120, strict=True)

    @model_validator(mode="after")
    def terms(self):
        if self.query is not None and self.queries is not None:
            raise ValueError("请只提供 query 或 queries 中的一项；省略两者可浏览书签")
        return self


class Video(Arguments):
    video_id: int = Field(gt=0, le=MAX_INTEGER, strict=True)


class Read(Video):
    start_seconds: float = Field(ge=0, le=1e10, allow_inf_nan=False, strict=True)
    end_seconds: float = Field(gt=0, le=1e10, allow_inf_nan=False, strict=True)
    offset: int = Field(default=0, ge=0, le=MAX_INTEGER, strict=True)
    transcript_version: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def interval(self):
        if self.end_seconds <= self.start_seconds:
            raise ValueError("结束时间必须晚于开始时间")
        if self.offset and not self.transcript_version:
            raise ValueError("继续读取需提供上次返回的 transcript_version")
        return self


class Open(Video):
    start_seconds: float = Field(default=0, ge=0, le=1e10, allow_inf_nan=False, strict=True)


class SlidePage(Video):
    start_seconds: float = Field(ge=0, le=1e10, allow_inf_nan=False, strict=True)
    end_seconds: float = Field(gt=0, le=1e10, allow_inf_nan=False, strict=True)
    limit: int = Field(default=20, ge=1, le=50, strict=True)
    offset: int = Field(default=0, ge=0, le=MAX_INTEGER, strict=True)

    @model_validator(mode="after")
    def interval(self):
        if self.end_seconds <= self.start_seconds:
            raise ValueError("结束时间必须晚于开始时间")
        return self


class SlideImage(Video):
    slide_id: int = Field(gt=0, le=MAX_INTEGER, strict=True)


class AnalysisPage(Video):
    analysis_mode: ModeId | None = None
    limit: int = Field(default=20, ge=1, le=50, strict=True)
    offset: int = Field(default=0, ge=0, le=MAX_INTEGER, strict=True)


class JobsPage(Arguments):
    status: Literal["all", "active", "finished"] = "all"
    video_id: int | None = Field(default=None, gt=0, le=MAX_INTEGER, strict=True)
    limit: int = Field(default=20, ge=1, le=50, strict=True)
    offset: int = Field(default=0, ge=0, le=MAX_INTEGER, strict=True)


class SemanticScope(Arguments):
    course_id: str | None = Field(default=None, min_length=1, max_length=100)
    video_id: int | None = Field(default=None, gt=0, le=MAX_INTEGER, strict=True)


class SemanticTarget(SemanticScope):
    @model_validator(mode="after")
    def explicit_target(self):
        if self.course_id is None and self.video_id is None:
            raise ValueError("请明确指定需要建立索引的课程或视频")
        return self


class Start(Arguments):
    course_id: str | None = Field(default=None, max_length=100)
    video_ids: list[Annotated[int, Field(strict=True, gt=0, le=MAX_INTEGER)]] | None = Field(
        default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def target(self):
        if (self.course_id is None) == (self.video_ids is None):
            raise ValueError("请只提供 course_id 或 video_ids 中的一项")
        if self.video_ids and any(type(v) is not int or not 0 < v <= MAX_INTEGER for v in self.video_ids):
            raise ValueError("视频 ID 必须为正整数")
        return self


class Job(Arguments):
    job_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")


class BatchOptions(Arguments):
    analysis_modes: list[ModeId] = Field(default_factory=lambda: ["medium_recall"], min_length=1, max_length=8)
    asr_model_id: str | None = Field(default=None, min_length=1, max_length=100)
    keywords: list[Annotated[str, Field(min_length=1, max_length=120)]] = Field(default_factory=list, max_length=50)
    use_keyword_prefilter: bool = Field(default=False, strict=True)
    filter_formula_noise: bool = Field(default=False, strict=True)
    filter_formula: str | None = Field(default=None, max_length=2000)
    force_retranscribe: bool = Field(default=False, strict=True)


class BatchTarget(Arguments):
    course_id: str | None = Field(default=None, max_length=100)
    video_ids: list[Annotated[int, Field(strict=True, gt=0, le=MAX_INTEGER)]] | None = Field(
        default=None, min_length=1, max_length=500)
    folders: list[Annotated[str, Field(min_length=1, max_length=1000)]] | None = Field(
        default=None, min_length=1, max_length=20)
    options: BatchOptions = Field(default_factory=BatchOptions)
    import_only: bool = Field(default=False, strict=True)
    expected_recall_revision: int | None = Field(default=None, ge=0, strict=True)

    @model_validator(mode="after")
    def target(self):
        if sum(value is not None for value in (self.course_id, self.video_ids, self.folders)) != 1:
            raise ValueError("请只提供 course_id、video_ids 或 folders 中的一项")
        if self.import_only and not self.folders:
            raise ValueError("仅导入只适用于本机 folders")
        if self.import_only and self.options.force_retranscribe:
            raise ValueError("仅导入不能同时要求重新转写")
        if self.folders and any(not Path(path).is_absolute() for path in self.folders):
            raise ValueError("folders 必须是用户指定的本机绝对目录")
        return self


RequestId = Annotated[str, Field(min_length=8, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")]


class SemanticPrepare(Arguments):
    request_id: RequestId
    device: Literal["cpu", "cuda"] | None = None


class SemanticSetup(SemanticTarget):
    request_id: RequestId
    device: Literal["cpu", "cuda"] | None = None


class BatchStart(BatchTarget):
    request_id: RequestId


class BatchCommand(Job):
    request_id: RequestId


class SequenceStart(Start):
    request_id: RequestId
    steps: list[Literal["transcribe", "extract_slides"]] = Field(
        default_factory=lambda: ["transcribe", "extract_slides"], min_length=1, max_length=2)

    @model_validator(mode="after")
    def unique_steps(self):
        if len(set(self.steps)) != len(self.steps):
            raise ValueError("处理步骤不能重复")
        return self


class JobQuery(Job):
    limit: int = Field(default=50, ge=1, le=50, strict=True)
    offset: int = Field(default=0, ge=0, le=MAX_INTEGER, strict=True)


SCHEMAS = {"list_courses": Page, "list_videos": ScopedPage, "search_library": LibrarySearch, "search_subtitles": Search,
           "search_bookmarks": BookmarkSearch,
           "read_transcript": Read, "open_video": Open, "start_transcription": Start,
           "get_job": JobQuery, "cancel_job": Job, "preview_batch": BatchTarget,
           "start_batch_processing": BatchStart, "retry_batch": BatchCommand,
           "resume_batch": BatchCommand, "start_processing_sequence": SequenceStart,
           "list_slides": SlidePage, "read_slide": SlideImage, "read_video_frame": Open,
           "read_analysis": AnalysisPage, "list_jobs": JobsPage, "get_processing_status": Arguments,
           "get_semantic_status": SemanticScope, "start_semantic_index": SemanticTarget,
           "start_semantic_prepare": SemanticPrepare, "start_semantic_setup": SemanticSetup}


class CourseTools:
    def __init__(self, provider, jobs, *, ensure_changes, origin, batch=None):
        self.provider, self.jobs = provider, jobs
        self.ensure_changes, self.origin = ensure_changes, origin.rstrip("/")
        self.batch = batch

    @property
    def store(self):
        return self.provider.get()

    def call(self, name, arguments, *, expected_library_id=None):
        if name not in SCHEMAS:
            raise ValueError("未知课程工具")
        args = SCHEMAS[name].model_validate(arguments)
        if name in {"read_slide", "read_video_frame"}:
            return self._read_image(args, frame=name == "read_video_frame",
                                    expected_library_id=expected_library_id)
        if name == "search_library":
            return self.search_library(args, expected_library_id=expected_library_id)
        return {"library_id": self.provider.library_id(), **getattr(self, name)(args)}

    def _folder(self, course_id):
        if course_id is None:
            return None
        if course_id == "root":
            return "root"
        if course_id.startswith("folder:"):
            value = course_id[7:]
            if not value.isdecimal() or len(value) > 16:
                raise ValueError("无效课程 ID")
            folder_id = int(value)
        else:
            with self.store.connect() as db:
                row = db.execute("SELECT folder_id FROM study_courses WHERE id=?", (course_id,)).fetchone()
            if row is None or row["folder_id"] is None:
                raise KeyError("课程不存在或尚未绑定视频文件夹")
            folder_id = row["folder_id"]
        if not self.store.get_folder(folder_id):
            raise KeyError("课程文件夹不存在")
        return folder_id

    def _videos(self, course_id=None):
        folder = self._folder(course_id)
        if folder is None:
            return self.store.list_videos()
        return self.store.list_folder_videos(None if folder == "root" else folder, recursive=folder != "root")

    def _video(self, video_id):
        row = self.store.get_video(video_id)
        if row is None or row["is_secondary"]:
            raise KeyError("视频不存在或不是独立课程视频")
        return row

    def _url(self, video_id, seconds=0):
        return playback_url(self.origin, video_id, round(seconds * 1000), self.provider.library_id())

    def _coverage(self, rows):
        available = sum(bool(row["has_transcript"]) if "has_transcript" in row.keys()
                        else bool(self.store.has_transcript(row["id"])) for row in rows)
        return {"video_count": len(rows), "with_subtitles": available, "missing_subtitles": len(rows) - available}

    @staticmethod
    def _page(items, args):
        page = items[args.offset:args.offset + args.limit]
        return {"total": len(items), "items": page, "has_more": args.offset + len(page) < len(items),
                "next_offset": args.offset + len(page) if args.offset + len(page) < len(items) else None}

    def list_courses(self, args):
        with self.store.connect() as db:
            folders = db.execute(
                "SELECT f.id,f.name,f.parent_id,c.id course_id FROM video_folders f "
                "LEFT JOIN study_courses c ON c.folder_id=f.id ORDER BY f.library_order,f.id").fetchall()
        by_id = {row["id"]: row for row in folders}
        items = []
        for row in folders:
            parts, current, seen = [], row, set()
            while current and current["id"] not in seen:
                seen.add(current["id"])
                parts.append(current["name"])
                current = by_id.get(current["parent_id"])
            path = " / ".join(reversed(parts))
            if args.query.casefold() not in path.casefold():
                continue
            identity = row["course_id"] or f"folder:{row['id']}"
            items.append({"course_id": identity, "name": row["name"], "path": path,
                          **self._coverage(self._videos(identity))})
        root = self._videos("root")
        if root and (not args.query or args.query in "未归类视频"):
            items.append({"course_id": "root", "name": "未归类视频", "path": "", **self._coverage(root)})
        return self._page(items, args)

    def list_videos(self, args):
        rows = self._videos(args.course_id)
        coverage = self._coverage(rows)
        active = {}
        for job in [*self.store.list_active_jobs(), *self.store.list_paused_jobs()]:
            if job["job_type"] not in {JOB_TYPE, SEQUENCE_TYPE}:
                continue
            state = json.loads(job["request_json"] or "{}")
            for entry in state.get("entries", []):
                if entry["status"] in {"pending", "running"}:
                    if job["job_type"] == JOB_TYPE or any(
                        step["name"] == "transcribe" and step["status"] in {"pending", "running"}
                        for step in entry.get("steps", [])
                    ):
                        active[entry["video_id"]] = job["id"]
        items = []
        for row in rows:
            has = self.store.has_transcript(row["id"])
            name = row["display_name"] or row["filename"]
            if args.query.casefold() not in name.casefold():
                continue
            if (args.subtitle_status == "available" and not has) or (args.subtitle_status == "missing" and has):
                continue
            items.append({"video_id": row["id"], "name": name, "duration_seconds": row["duration_ms"] / 1000,
                          "has_subtitles": has, "transcription_job_id": active.get(row["id"]),
                          "slide_count": row["slide_count"] if "slide_count" in row.keys()
                          else len(self.store.list_slides(row["id"], include_auxiliary=True)),
                          "play_url": self._url(row["id"])})
        return {**self._page(items, args), "coverage": coverage}

    def search_library(self, args, *, expected_library_id=None):
        with self.jobs._lock:
            library_id, store = self.provider.library_id(), self.store
            if expected_library_id is not None and library_id != expected_library_id:
                raise ValueError("视频库身份已变化，请重新搜索")
            folder = self._folder(args.course_id)
        semantic = {}
        if args.semantic_query is not None:
            from classcue.semantic_embeddings import encoder
            from classcue.semantic_index import model_version
            with encoder.lock:
                if self.jobs.is_running():
                    raise RuntimeError("后台任务正在使用处理资源，请完成或停止任务后再做语义检索；原文搜索仍可使用")
                version = model_version()
                vector = encoder.encode([args.semantic_query], query=True)[0]
            if model_version() != version:
                raise RuntimeError("检索模型已变化，请重新搜索")
            semantic = {"semantic_vector": vector, "semantic_version": version}
        with store.connect() as db:
            db.execute("BEGIN")
            result = search_library(db, args, folder_id=None if folder == "root" else folder,
                                    root_only=folder == "root", url=self._url, **semantic)
        with self.jobs._lock:
            if library_id != self.provider.library_id() or store.path != self.store.path:
                raise ValueError("视频库身份已变化，请重新搜索")
        return {"library_id": library_id, **result}

    def _semantic_videos(self, args):
        videos = self._videos(args.course_id)
        if args.video_id is not None:
            self._video(args.video_id)
            videos = [row for row in videos if row["id"] == args.video_id]
        return [row["id"] for row in videos]

    def get_semantic_status(self, args):
        from classcue.semantic_embeddings import model_status
        from classcue.semantic_index import coverage
        ids = self._semantic_videos(args)
        with self.store.connect() as db:
            indexed = coverage(db, ids)
        active = next((str(row["id"]) for row in self.store.list_active_jobs()
                       if row["job_type"] in {
                           "semantic_prepare", "semantic_index", "semantic_setup", "semantic_cleanup"}), None)
        return {**model_status(), "coverage": indexed,
                "busy": self.jobs.is_running() or bool(self.store.list_paused_jobs()),
                "active_job_id": active}

    def _start_semantic(self, args, *, index):
        from classcue.semantic_embeddings import model_status, prepare_model
        from classcue.semantic_index import build_index
        kind = "semantic_setup" if index else "semantic_prepare"

        def operation(marker):
            self.ensure_changes()
            device = args.device or model_status()["device"]
            ids, store = (self._semantic_videos(args) if index else []), self.store
            if index and not ids:
                raise ValueError("指定范围内没有视频")
            state = {"kind": kind, "model_id": "Qwen3-Embedding-0.6B", "device": device,
                     "mcp_request": marker, "entries": [{"video_id": value} for value in ids]}
            if index:
                state["course_id"] = args.course_id
            job_id = uuid4().hex

            def run(update, stop):
                if stop():
                    raise ProcessingCancelled("已停止课程检索准备")
                prepare_model(device, lambda value, message, stage: update(
                    value * .5 if index else value, message, stage), stop)
                if stop():
                    raise ProcessingCancelled("已停止课程检索准备，已有索引保留")
                if index:
                    summary = build_index(store, ids, lambda value, message, stage: update(
                        .5 + value * .5, message, stage), stop)
                    state["summary"] = summary
                    store.update_job(job_id, request_json=json.dumps(state, ensure_ascii=False))
                    return {"message": f"课程检索已就绪 · {summary['chunks']} 个字幕片段"}
                return {"message": f"{'CPU' if device == 'cpu' else 'GPU'} 检索模型已就绪"}

            self.jobs.submit(kind, None, run, request=state, job_id=job_id)
            return {"job_id": job_id}

        return self._batch_request(f"start_{kind}", args, operation)

    def start_semantic_prepare(self, args):
        return self._start_semantic(args, index=False)

    def start_semantic_setup(self, args):
        return self._start_semantic(args, index=True)

    def start_semantic_index(self, args):
        from classcue.semantic_embeddings import model_status
        from classcue.semantic_index import build_index
        self.ensure_changes()
        if not model_status()["ready"]:
            raise RuntimeError("请先用 start_semantic_setup 准备该范围，或在设置 → 课程检索中准备模型")
        ids, store = self._semantic_videos(args), self.store
        if not ids:
            raise ValueError("指定范围内没有视频")
        job_id = self.jobs.submit(
            "semantic_index", None, lambda update, stop: build_index(store, ids, update, stop),
            request={"kind": "semantic_index", "course_id": args.course_id,
                     "entries": [{"video_id": value} for value in ids]})
        return {"job_id": job_id, "job_type": "semantic_index", "status": "queued",
                "video_count": len(ids)}

    def search_subtitles(self, args):
        from classcue.subtitle_search import validate_query
        terms = list(dict.fromkeys(validate_query(term) for term in (args.queries or [args.query])))
        query = terms[0]
        rows = self._videos(args.course_id)
        if args.video_id is not None:
            self._video(args.video_id)
            rows = [row for row in rows if row["id"] == args.video_id]
        folder = self._folder(args.course_id)
        result = self.store.search_subtitles(query, folder_id=None if folder == "root" else folder,
                                             root_only=folder == "root", limit=args.limit, offset=args.offset,
                                             video_id=args.video_id, queries=terms, match_mode=args.match_mode,
                                             merge_gap_ms=args.merge_gap_seconds * 1000, diverse=args.diverse)
        page = {"total": result["total"], "has_more": result["has_more"],
                "next_offset": args.offset + len(result["items"]) if result["has_more"] else None,
                "items": result["items"]}
        items = []
        for row in page["items"]:
            start = max(0, row["start_ms"] - args.context_seconds * 1000)
            end = min(row["duration_ms"], row["end_ms"] + args.context_seconds * 1000)
            with self.store.connect() as db:
                context = db.execute(
                    "SELECT segment_id,start_ms,end_ms,text FROM video_transcript_segments "
                    "WHERE video_id=? AND end_ms>? AND start_ms<? "
                    "ORDER BY ABS(start_ms-?),start_ms,segment_id LIMIT 6",
                    (row["video_id"], start, end, row["start_ms"])).fetchall()
            context = sorted(context, key=lambda s: (s["start_ms"], s["segment_id"]))
            items.append({"video_id": row["video_id"], "video_name": row["video_name"] or row["filename"],
                          "segment_id": row["segment_id"], "start_seconds": row["start_ms"] / 1000,
                          "end_seconds": row["end_ms"] / 1000, "text": row.get("snippet", row["text"])[:1200],
                          "match_count": row["match_count"],
                          "context_range": {"start_seconds": start / 1000, "end_seconds": end / 1000},
                          "context": [{"segment_id": s["segment_id"], "start_seconds": s["start_ms"] / 1000,
                                       "end_seconds": s["end_ms"] / 1000, "text": s["text"][:300]}
                                      for s in context[:5]],
                          "context_truncated": len(context) > 5 or any(len(s["text"]) > 300 for s in context),
                          "play_url": self._url(row["video_id"], row["start_ms"] / 1000)})
        coverage = self._coverage(rows)
        return {**page, "items": items, "coverage": coverage, "total_videos": result["total_videos"],
                "total_matches": result["total_matches"], "match_type": "literal", "query": query,
                "queries": terms, "match_mode": args.match_mode, "merge_gap_seconds": args.merge_gap_seconds,
                "diverse": args.diverse,
                "empty_reason": ("no_videos" if not rows else "no_subtitles" if not coverage["with_subtitles"]
                                 else "no_literal_match") if not result["total"] else None}

    def search_bookmarks(self, args):
        from classcue.study_bookmarks import StudyBookmarkStore
        from classcue.subtitle_search import validate_query

        candidates = args.queries or ([args.query] if args.query is not None else [])
        terms = list(dict.fromkeys(validate_query(term) for term in candidates))
        folder = self._folder(args.course_id)
        if args.video_id is not None:
            self._video(args.video_id)
        rows = StudyBookmarkStore(self.store).bookmarks(
            folder_id=None if folder == "root" else folder, video_id=args.video_id)
        if folder == "root":
            rows = [row for row in rows if row["folder_id"] is None]
        items = []
        for row in rows:
            note = row["note"].casefold()
            matches = [term.casefold() in note for term in terms]
            if terms and not (all(matches) if args.match_mode == "all" else any(matches)):
                continue
            position = row["position_ms"]
            context = None
            if position is not None and row["duration_ms"] > 0:
                start = max(0, position - args.context_seconds * 1000)
                end = min(position + args.context_seconds * 1000, row["duration_ms"])
                if start < end:
                    context = {"start_seconds": start / 1000, "end_seconds": end / 1000}
            items.append({
                "bookmark_id": row["id"], "video_id": row["video_id"],
                "video_name": row["display_name"] or row["filename"],
                "note": row["note"], "position_seconds": position / 1000 if position is not None else None,
                "folder_id": row["folder_id"],
                "folder_path": " / ".join(folder["name"] for folder in row["path"]),
                "has_subtitles": bool(row["has_transcript"]), "context_range": context,
                "play_url": self._url(row["video_id"], position / 1000 if position is not None else 0),
                "created_at": row["created_at"], "updated_at": row["updated_at"],
            })
        return {**self._page(items, args), "total_videos": len({item["video_id"] for item in items}),
                "searched_bookmarks": len(rows), "queries": terms, "match_type": "literal",
                "match_mode": args.match_mode, "evidence_type": "user_bookmark",
                "empty_reason": ("no_bookmarks" if not rows else "no_literal_match") if not items else None}

    def _capability(self, name):
        if self.batch is None or name not in self.batch:
            raise RuntimeError("此能力需要更新 ClassCue 后端")
        return self.batch[name]

    def list_slides(self, args):
        video = self._video(args.video_id)
        if args.start_seconds * 1000 >= video["duration_ms"]:
            raise ValueError("开始时间超出视频时长")
        slides = self._capability("slides_read")(args.video_id, round(args.start_seconds * 1000),
                                                round(args.end_seconds * 1000))
        items = []
        for slide in slides:
            items.append({"slide_id": slide["id"], "start_seconds": slide["start_ms"] / 1000,
                          "end_seconds": slide["end_ms"] / 1000,
                          "visible_from_seconds": slide["visible_from_ms"] / 1000,
                          "visible_until_seconds": slide["visible_until_ms"] / 1000,
                          "representative_seconds": slide["representative_ms"] / 1000,
                          "is_preferred": slide["is_llm_preferred"], "image_hash": slide["image_hash"]})
        return {"video_id": args.video_id, **self._page(items, args),
                "extracted_slide_count": len(self.store.list_slides(args.video_id, include_auxiliary=True))}

    @staticmethod
    def _image(path):
        from PIL import Image, ImageOps
        try:
            with Image.open(path) as source:
                image = ImageOps.exif_transpose(source).convert("RGB")
                for bound in (1600, 1024):
                    image.thumbnail((bound, bound))
                    output = BytesIO()
                    image.save(output, format="JPEG", quality=82)
                    if output.tell() <= 1024 * 1024:
                        return {"mime_type": "image/jpeg", "data": base64.b64encode(output.getvalue()).decode("ascii"),
                                "width": image.width, "height": image.height}
        except OSError:
            raise ValueError("图片缺失或无法解码，请检查 PPT 提取结果或视频文件") from None
        raise ValueError("图片超过工具返回上限")

    def read_slide(self, args):
        return self._read_image(args, frame=False)

    def read_video_frame(self, args):
        return self._read_image(args, frame=True)

    @staticmethod
    def _file_identity(path):
        try:
            path = Path(path).resolve(strict=True)
            stat = path.stat()
            if not path.is_file():
                raise OSError()
            return (str(path), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        except OSError:
            raise ValueError("图像来源文件不可用或已变化，请重新读取") from None

    def _read_image(self, args, *, frame, expected_library_id=None):
        with self.jobs._lock:
            library_id, store = self.provider.library_id(), self.store
            if expected_library_id is not None and library_id != expected_library_id:
                raise ValueError("视频库身份已变化，请重新读取图像")
            video = self._video(args.video_id)
            video_identity = tuple(video[key] for key in ("video_path", "duration_ms", "is_secondary"))
            slide = None
            if frame:
                if args.start_seconds * 1000 >= video["duration_ms"]:
                    raise ValueError("截图时间超出视频时长")
                source = Path(video["video_path"]).expanduser()
                reader = self._capability("frame_reader")(args.video_id, round(args.start_seconds * 1000))
                result = {"video_id": args.video_id, "time_seconds": args.start_seconds,
                          "evidence_type": "video_frame", "play_url": self._url(args.video_id, args.start_seconds)}
            else:
                source = Path(self._capability("slide_image")(args.video_id, args.slide_id))
                row = store.get_slide(args.slide_id)
                if row is None or row["video_id"] != args.video_id:
                    raise ValueError("PPT 提取结果已变化，请重新读取列表")
                slide = dict(row)
                reader = lambda: source
                result = {"video_id": args.video_id, "slide_id": args.slide_id,
                          "evidence_type": "extracted_slide"}
            identity = self._file_identity(source)
        # Decoding and JPEG encoding may wait on disk or decoder slots; keep task controls available.
        try:
            image = self._image(reader())
        except OSError:
            raise ValueError("图像来源无法读取，请检查视频或 PPT 文件后重试") from None
        with self.jobs._lock:
            if library_id != self.provider.library_id() or store.path != self.store.path:
                raise ValueError("视频库身份已变化，请重新读取图像")
            current = store.get_video(args.video_id)
            if current is None or tuple(current[key] for key in ("video_path", "duration_ms", "is_secondary")) != video_identity:
                raise ValueError("视频来源已变化，请重新读取图像")
            if self._file_identity(source) != identity:
                raise ValueError("图像来源文件已变化，请重新读取")
            if slide is not None:
                current_slide = store.get_slide(args.slide_id)
                if current_slide is None or dict(current_slide) != slide:
                    raise ValueError("PPT 提取结果已变化，请重新读取列表")
            return {"library_id": library_id, **result, "image": image}

    def read_analysis(self, args):
        self._video(args.video_id)
        rows = self.store.list_markers(args.video_id, args.analysis_mode)
        items = [{"marker_id": row["id"], "analysis_mode": row["analysis_mode"],
                  "start_seconds": row["start_ms"] / 1000, "end_seconds": row["end_ms"] / 1000,
                  "title": row["title"][:500], "note": row["note"][:1500],
                  "source_quote": row["source_quote"][:1500],
                  "text_truncated": len(row["title"]) > 500 or len(row["note"]) > 1500 or len(row["source_quote"]) > 1500,
                  "review_status": row["review_status"], "source": "manual" if row["trigger_phrase"].startswith("MANUAL-") else "analysis",
                  "jump_seconds": (row["user_start_ms"] if row["user_start_ms"] is not None else row["start_ms"]) / 1000,
                  "importance": row["importance"],
                  "play_url": self._url(args.video_id, (row["user_start_ms"] if row["user_start_ms"] is not None
                                                        else row["start_ms"]) / 1000)}
                 for row in rows]
        with self.store.connect() as db:
            runs = db.execute("SELECT analysis_mode,status,candidate_count,updated_at FROM video_analysis_runs "
                              "WHERE video_id=? ORDER BY analysis_mode", (args.video_id,)).fetchall()
        return {"video_id": args.video_id, **self._page(items, args),
                "analysis_runs": [dict(row) for row in runs if not args.analysis_mode
                                  or row["analysis_mode"] == args.analysis_mode],
                "evidence_type": "saved_analysis"}

    def list_jobs(self, args):
        if args.video_id is not None:
            self._video(args.video_id)
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT * FROM processing_jobs WHERE job_type IN "
                "('course_transcribe','course_sequence','batch_video_process','semantic_index','semantic_prepare',"
                "'semantic_setup','semantic_cleanup') "
                "ORDER BY CASE WHEN status IN ('queued','running','resource_paused','awaiting_api') THEN 0 ELSE 1 END,"
                "COALESCE(finished_at,started_at,created_at) DESC,rowid DESC").fetchall()
        items = []
        for row in rows:
            active = row["status"] in ACTIVE | {"awaiting_api"}
            if args.status != "all" and active != (args.status == "active"):
                continue
            state = json.loads(row["request_json"] or "{}")
            if args.video_id is not None and args.video_id != row["video_id"] and args.video_id not in {
                entry.get("video_id") for entry in state.get("entries", [])
            }:
                continue
            items.append({"job_id": row["id"], "job_type": row["job_type"], "status": row["status"],
                          "progress": row["progress"], "message": redact_text(row["message"] or "")[:500],
                          "created_at": row["created_at"]})
        return {**self._page(items, args), "scope": "course_and_batch_jobs"}

    def get_processing_status(self, args):
        return {"asr_selection": selection_snapshot(), "recall_selection": self.store.get_recall_selection(),
                **self._capability("processing_status")()}

    def read_transcript(self, args):
        video = self._video(args.video_id)
        if args.start_seconds * 1000 >= video["duration_ms"]:
            raise ValueError("开始时间超出视频时长")
        segments = self.store.get_segments(args.video_id)
        digest = hashlib.sha256(json.dumps([(s.id, s.start_ms, s.end_ms, s.text) for s in segments],
                                           ensure_ascii=False).encode()).hexdigest()
        if args.transcript_version and args.transcript_version != digest:
            raise ValueError("字幕已变化，请从 offset=0 重新读取")
        matching = [s for s in segments if s.end_ms > args.start_seconds * 1000
                    and s.start_ms < args.end_seconds * 1000]
        page, size = [], 0
        for s in matching[args.offset:]:
            if len(page) >= 100 or size + len(s.text) > 12000:
                if not page:
                    raise ValueError("单句字幕过长，无法在本工具的返回上限内读取")
                break
            page.append({"segment_id": s.id, "start_seconds": s.start_ms / 1000,
                         "end_seconds": s.end_ms / 1000, "text": s.text})
            size += len(s.text)
        following = args.offset + len(page)
        return {"video_id": args.video_id, "video_name": video["display_name"] or video["filename"],
                "has_subtitles": bool(segments), "transcript_version": digest, "items": page,
                "has_more": following < len(matching), "next_offset": following if following < len(matching) else None,
                "play_url": self._url(args.video_id, args.start_seconds)}

    def open_video(self, args):
        video = self._video(args.video_id)
        if args.start_seconds * 1000 >= video["duration_ms"] or not Path(video["video_path"]).is_file():
            raise ValueError("视频文件不可用或定位时间超出时长")
        return {"video_id": args.video_id, "start_seconds": args.start_seconds,
                "play_url": self._url(args.video_id, args.start_seconds), "navigation": "requested"}

    def start_transcription(self, args):
        with self.jobs._lock:
            rows = self._videos(args.course_id) if args.course_id is not None else [
                self._video(v) for v in sorted(set(args.video_ids))]
            ids = sorted(row["id"] for row in rows)
            for job in [*self.store.list_active_jobs(), *self.store.list_paused_jobs()]:
                if job["job_type"] == JOB_TYPE:
                    state = json.loads(job["request_json"] or "{}")
                    if state.get("target_ids") == ids:
                        return {**self._job(job), "reused": True}
            self.ensure_changes()
            entries = [{"video_id": row["id"], "status": "skipped" if self.store.has_transcript(row["id"])
                        else "pending", "error": ""} for row in rows]
            pending = [e for e in entries if e["status"] == "pending"]
            for entry in pending:
                row = self._video(entry["video_id"])
                if not Path(row["video_path"]).is_file():
                    raise ValueError(f"视频 {row['id']} 文件已不存在")
            if not pending:
                return {"job_id": None, "status": "completed", "message": "没有缺字幕的视频",
                        "summary": {"completed": 0, "skipped": len(entries), "failed": 0, "pending": 0}}
            selection = selection_snapshot()
            with frozen_selection(selection):
                if get_model(selection["model_id"]).engine == "firered":
                    require_firered_gpu()
                require_prepared_asr_model(selection["model_id"])
            state = {"kind": JOB_TYPE, "target_ids": ids, "selection": selection, "entries": entries}
            job_id = uuid4().hex
            self.jobs.submit(JOB_TYPE, None, self._operation(job_id, state),
                             request=state, job_id=job_id)
            return {**self.get_job(Job(job_id=job_id)), "reused": False}

    def _operation(self, job_id, state):
        store = self.store

        def save():
            store.update_job(job_id, request_json=json.dumps(state, ensure_ascii=False))

        def operation(update, should_cancel):
            with frozen_selection(state["selection"]):
                for index, entry in enumerate(state["entries"]):
                    if should_cancel():
                        raise ProcessingCancelled("用户已停止转写")
                    if entry["status"] == "skipped":
                        continue
                    video_id = entry["video_id"]
                    try:
                        row = store.get_video(video_id)
                        if row is None or row["is_secondary"] or not Path(row["video_path"]).is_file():
                            raise ValueError("视频文件不可用")
                        if store.has_transcript(video_id):
                            entry["status"] = "skipped"
                            save()
                            continue
                        entry["status"] = "running"
                        save()
                        self._transcribe_video(video_id, state["selection"], should_cancel,
                            lambda value, message: update(
                                (index + value) / len(state["entries"]), message, "transcribing"))
                        entry["status"] = "completed"
                    except (ProcessingCancelled, ResourcePaused) as exc:
                        if not store.has_transcript(video_id):
                            store.set_status(video_id, "failed")
                        entry["status"] = "pending"
                        if isinstance(exc, ResourcePaused):
                            state["interruption_reason"] = (
                                f"资源不足，请释放资源后重新提交缺字幕项：{redact_text(exc)[:500]}")
                        save()
                        raise ProcessingCancelled(state.get(
                            "interruption_reason", "转写已中断；重新提交缺字幕项可以继续")) from None
                    except Exception as exc:
                        if store.get_video(video_id) is not None and not store.has_transcript(video_id):
                            store.set_status(video_id, "failed")
                        entry.update(status="failed", error=redact_text(exc)[:500])
                    save()
            failures = sum(e["status"] == "failed" for e in state["entries"])
            return {"job_status": "failed" if failures else "completed",
                    "message": f"转写结束，{failures} 个视频失败"}
        return operation

    def _transcribe_video(self, video_id, selection, should_cancel, progress):
        from classcue.slide_runtime import release_idle_worker
        from classcue.video_pipeline import _transcribe_video_with_fallback, _audio_path
        row = self._video(video_id)
        if not Path(row["video_path"]).is_file():
            raise ValueError("视频文件不可用")
        if should_cancel():
            raise ProcessingCancelled("用户已停止转写")
        release_idle_worker("video_transcribe")
        if should_cancel():
            raise ProcessingCancelled("用户已停止转写")
        with frozen_selection(selection):
            require_prepared_asr_model(selection["model_id"])
            self.store.set_status(video_id, "processing")
            started = time.perf_counter()
            segments, _ = _transcribe_video_with_fallback(
                Path(row["video_path"]), _audio_path(self.store, row),
                asr_model_id=selection["model_id"], should_stop=should_cancel, progress=progress)
            if should_cancel():
                raise ProcessingCancelled("用户已停止转写")
            if not segments:
                raise ValueError("未识别到可保存的字幕，请确认视频包含可识别的语音后重试")
            self.store.mark_transcribed(video_id, segments, selection["model_id"])
            self.store.record_transcription_metrics(video_id, time.perf_counter() - started)

    def start_processing_sequence(self, args):
        def submit(marker):
            self.ensure_changes()
            if self.batch is None:
                raise RuntimeError("顺序处理连接不可用，请更新 ClassCue")
            rows = self._videos(args.course_id) if args.course_id is not None else [
                self._video(v) for v in dict.fromkeys(args.video_ids)]
            if not rows:
                raise ValueError("所选范围没有视频")
            for row in rows:
                if not Path(row["video_path"]).is_file():
                    raise ValueError(f"视频 {row['id']} 文件不可用")
            needs_asr = "transcribe" in args.steps and any(
                not self.store.has_transcript(row["id"]) for row in rows)
            selection = selection_snapshot() if "transcribe" in args.steps else None
            if needs_asr:
                with frozen_selection(selection):
                    if get_model(selection["model_id"]).engine == "firered":
                        require_firered_gpu()
                    require_prepared_asr_model(selection["model_id"])
            state = {"kind": SEQUENCE_TYPE, "target_ids": [row["id"] for row in rows],
                     "selection": selection, "mcp_request": marker,
                     "entries": [{"video_id": row["id"], "label": row["display_name"] or row["filename"],
                                  "status": "pending", "error": "", "steps": [
                                      {"name": step, "status": "pending"} for step in args.steps]} for row in rows]}
            job_id = uuid4().hex
            self.jobs.submit(SEQUENCE_TYPE, None, self._sequence_operation(job_id, state),
                             request=state, job_id=job_id)
            return {"job_id": job_id}
        return self._batch_request("start_processing_sequence", args, submit)

    def _sequence_operation(self, job_id, state):
        def save():
            self.store.update_job(job_id, request_json=json.dumps(state, ensure_ascii=False))

        def operation(update, should_cancel):
            total = sum(len(entry["steps"]) for entry in state["entries"])
            done = 0
            for entry in state["entries"]:
                video_id = entry["video_id"]
                for step in entry["steps"]:
                    try:
                        if should_cancel():
                            raise ProcessingCancelled("用户已停止顺序处理")
                        entry["status"] = step["status"] = "running"
                        save()
                        def progress(value, message, stage=None):
                            update((done + value) / total, message, stage or step["name"])
                        if step["name"] == "transcribe":
                            if self.store.has_transcript(video_id):
                                step["status"] = "skipped"
                            else:
                                self._transcribe_video(video_id, state["selection"], should_cancel, progress)
                                step["status"] = "completed"
                        else:
                            result = self.batch["slides"](video_id, progress, should_cancel)
                            step.update(status="skipped" if result.get("status") == "cached" else "completed",
                                        count=result.get("count", 0))
                        done += 1
                        save()
                    except (ProcessingCancelled, ResourcePaused) as exc:
                        entry["status"] = step["status"] = "pending"
                        state["interruption_reason"] = redact_text(exc)[:500]
                        if step["name"] == "transcribe" and not self.store.has_transcript(video_id):
                            self.store.set_status(video_id, "failed")
                        save()
                        raise ProcessingCancelled("顺序任务已中断，已保存结果保留；重新提交原步骤可复用") from None
                    except Exception as exc:
                        step["status"] = entry["status"] = "failed"
                        entry["error"] = redact_text(exc)[:500]
                        if step["name"] == "transcribe" and not self.store.has_transcript(video_id):
                            self.store.set_status(video_id, "failed")
                        save()
                        return {"job_status": "failed", "message": "顺序任务失败，后续步骤未执行"}
                entry["status"] = "completed"
                save()
            return {"message": "顺序任务完成", "committed": True}
        return operation

    def _job(self, row, *, offset=0, limit=50):
        if row is None or row["job_type"] not in {
            JOB_TYPE, SEQUENCE_TYPE, "batch_video_process", "semantic_index", "semantic_prepare",
            "semantic_setup", "semantic_cleanup"
        }:
            raise KeyError("课程转写或批处理任务不存在")
        if row["job_type"] in {"semantic_index", "semantic_prepare", "semantic_setup", "semantic_cleanup"}:
            state = json.loads(row["request_json"] or "{}")
            return {"job_id": row["id"], "job_type": row["job_type"], "status": row["status"],
                    "stage": row["stage"], "progress": row["progress"], "message": row["message"],
                    "error_message": row["error_message"], "summary": state.get("summary", {}),
                    "next_action": ("在 APP 设置 → 课程检索中重新清理"
                                    if row["job_type"] == "semantic_cleanup" else
                                    "使用新的 request_id 重新提交原范围，复用已准备模型和已完成索引")
                    if row["status"] in {"failed", "interrupted"} else None}
        if row["job_type"] == "batch_video_process":
            return self._batch_job(row, offset=offset, limit=limit)
        state = json.loads(row["request_json"] or "{}")
        entries = state.get("entries", [])
        counts = {key: sum(e["status"] == key for e in entries) for key in
                  ("completed", "failed", "skipped")}
        counts["pending"] = len(entries) - sum(counts.values())
        return {"job_id": row["id"], "status": row["status"], "stage": row["stage"],
                "job_type": row["job_type"],
                "progress": row["progress"], "message": state.get("interruption_reason") or row["message"],
                "error_message": row["error_message"],
                "resource": json.loads(row["resource_json"]) if row["resource_json"] else None,
                "model_id": (state.get("selection") or {}).get("model_id"),
                "selection": state.get("selection"), "summary": counts,
                **({"entries": entries[offset:offset + limit], "total_entries": len(entries),
                    "has_more": offset + limit < len(entries),
                    "next_offset": offset + limit if offset + limit < len(entries) else None}
                   if row["job_type"] == SEQUENCE_TYPE else {}),
                "current_video_id": (next((e["video_id"] for e in entries if e["status"] == "running"), None)
                                     if row["status"] == "running" else None),
                "failures": [e for e in entries if e["status"] == "failed"][:20],
                "next_action": ("使用新的 request_id 重新提交原步骤，复用已保存字幕和 PPT"
                                if row["job_type"] == SEQUENCE_TYPE else "重新提交原范围，跳过已有字幕")
                               if row["status"] in {"failed", "interrupted"} else None}

    def get_job(self, args):
        return self._job(self.store.get_job(args.job_id),
                         offset=getattr(args, "offset", 0), limit=getattr(args, "limit", 50))

    def cancel_job(self, args):
        with self.jobs._lock:
            self._job(self.store.get_job(args.job_id))
            self.jobs.cancel(args.job_id)
            return self.get_job(args)

    def _batch_job(self, row, *, offset=0, limit=50):
        from classcue.web_batch import batch_summary, normalize_batch
        state = normalize_batch(json.loads(row["request_json"] or "{}"), self.store)
        entries = state.get("entries", [])
        index = state.get("current_index")
        current = entries[index].get("video_id") if (
            row["status"] == "running" and isinstance(index, int) and 0 <= index < len(entries)) else None
        return {
            "job_id": row["id"], "job_type": row["job_type"], "status": row["status"],
            "stage": row["stage"], "progress": row["progress"], "message": (row["message"] or "")[:2000],
            "error_message": (row["error_message"] or "")[:2000],
            "resource": json.loads(row["resource_json"]) if row["resource_json"] else None,
            "model_id": state.get("options", {}).get("asr_model_id"),
            "analysis_modes": state.get("options", {}).get("analysis_modes", []),
            "import_only": bool(state.get("import_only")), "summary": batch_summary(state, self.store),
            "current_video_id": current, "total_entries": len(entries),
            "entries": [{key: (entry.get(key)[:1000] if isinstance(entry.get(key), str) else entry.get(key)) for key in
                         ("video_id", "label", "status", "error", "warning", "completed_modes")}
                        for entry in entries[offset:offset + limit]],
            "has_more": offset + limit < len(entries),
            "next_offset": offset + limit if offset + limit < len(entries) else None,
            "api_error": state.get("api_error", "")[:2000],
            "next_action": ("resume_batch" if row["status"] == "resource_paused" else
                            "retry_batch" if row["status"] in {"failed", "interrupted", "awaiting_api"}
                            and not state.get("import_only") else None),
        }

    def _batch_payload(self, args):
        if self.batch is None:
            raise RuntimeError("批处理连接不可用，请更新 ClassCue")
        rows = self._videos(args.course_id) if args.course_id is not None else [
            self._video(identity) for identity in dict.fromkeys(args.video_ids or [])]
        saved = self.store.get_recall_selection()
        explicit = "analysis_modes" in args.options.model_fields_set
        if (not args.import_only and not explicit and args.expected_recall_revision is not None
                and saved["revision"] != args.expected_recall_revision):
            raise ValueError("APP 处理方案在预览后已变化，请重新预览")
        payload = {"video_ids": [row["id"] for row in rows], "folders": args.folders or [],
                   "import_only": args.import_only, **args.options.model_dump(exclude_unset=True)}
        options = self.batch["options"](payload)
        payload.update(options)
        payload["analysis_modes_source"] = ("not_used" if args.import_only else "explicit" if explicit
                                            else "app_saved" if saved["initialized"] else "first_use_default")
        payload["recall_selection_revision"] = saved["revision"]
        return rows, payload

    def preview_batch(self, args):
        from classcue.web_batch import preview_directories
        rows, payload = self._batch_payload(args)
        inspection = preview_directories(payload["folders"]) if args.folders else {"valid": True, "folders": []}
        missing = [row["id"] for row in rows if not Path(row["video_path"]).is_file()]
        imports = sum(folder["video_count"] for folder in inspection["folders"])
        return {
            "valid": inspection["valid"] and not missing and bool(rows or imports),
            "total": len(rows) + imports, "registered_count": len(rows), "import_count": imports,
            "coverage": self._coverage(rows), "missing_video_ids": missing[:50],
            "videos": [{"video_id": row["id"], "name": row["display_name"] or row["filename"]}
                       for row in rows[:50]], "videos_truncated": len(rows) > 50,
            "folders": [{key: folder[key] for key in
                         ("path", "video_count", "non_video_count", "subfolder_count", "valid", "error")}
                        for folder in inspection["folders"]],
            "options": {key: payload[key] for key in (*BatchOptions.model_fields, "import_only")},
            "analysis_modes_source": payload["analysis_modes_source"],
            "recall_selection_revision": payload["recall_selection_revision"],
            "available_analysis_modes": self.batch["modes"](),
            "effects": {
                "calls_ai_service": not args.import_only,
                "ai_notice": "将使用应用当前 AI 连接发送字幕并分析，可能产生 API 费用" if not args.import_only else "",
                "moves_imported_sources": bool(args.folders) and self.batch["moves_sources"](),
                "import_notice": (
                    ("沿用应用导入：归档校验成功后删除来源文件，重复视频保留来源"
                     if self.batch["moves_sources"]() else "沿用应用导入：复制到视频库，保留来源文件")
                    if args.folders else ""
                ),
                "replaces_existing_transcripts": args.options.force_retranscribe,
                "may_prepare_asr": not args.import_only and (
                    args.options.force_retranscribe or bool(imports) or any(
                        not self.store.has_transcript(row["id"]) for row in rows)),
                "asr_notice": "缺字幕时沿用应用已有模型准备与转写流程，可能下载当前所选模型",
            },
        }

    def _batch_request(self, action, args, operation):
        body = args.model_dump(exclude={"request_id"})
        legacy_body = {key: value for key, value in body.items()
                       if key != "expected_recall_revision" or value is not None}
        legacy_digest = hashlib.sha256(json.dumps(legacy_body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if hasattr(args, "options"):
            body["analysis_modes_explicit"] = "analysis_modes" in args.options.model_fields_set
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self.jobs._lock:
            with self.store.connect() as db:
                candidates = db.execute(
                    "SELECT * FROM processing_jobs WHERE job_type IN "
                    "('batch_video_process','course_sequence','semantic_prepare','semantic_setup') "
                    "AND json_extract(CASE WHEN json_valid(request_json) THEN request_json ELSE '{}' END, "
                    "'$.mcp_request.request_id') = ?",
                    (args.request_id,),
                ).fetchall()
            if candidates:
                row = candidates[0]
                saved = json.loads(row["request_json"])["mcp_request"]
                expected = digest if saved.get("version") == 2 else legacy_digest
                if saved["action"] != action or saved["digest"] != expected:
                    raise ValueError("request_id 已用于其他参数或动作；新操作请使用新的 request_id")
                return {**self._job(row), "reused": True}
            marker = {"version": 2, "request_id": args.request_id, "action": action, "digest": digest}
            result = operation(marker)
            job_id = result.get("job_id") or result["id"]
            return {**self.get_job(Job(job_id=job_id)), "reused": False}

    def start_batch_processing(self, args):
        def operation(marker):
            rows, payload = self._batch_payload(args)
            if not args.folders and not rows:
                raise ValueError("所选课程中没有可处理的视频")
            return self.batch["start"](payload, marker)
        return self._batch_request("start_batch_processing", args, operation)

    def _batch_command(self, name, args):
        def operation(marker):
            row = self.store.get_job(args.job_id)
            if row is None or row["job_type"] != "batch_video_process":
                raise KeyError("批处理任务不存在")
            if self.batch is None:
                raise RuntimeError("批处理连接不可用，请更新 ClassCue")
            if name == "resume":
                from classcue.web_batch import normalize_batch
                state = normalize_batch(json.loads(row["request_json"] or "{}"), self.store)
                if not any(entry["status"] not in {"completed", "skipped", "failed"}
                           for entry in state["entries"]):
                    raise ValueError("没有需要恢复的视频，请查询任务结果")
            return self.batch[name](args.job_id, marker)
        return self._batch_request(name, args, operation)

    def retry_batch(self, args):
        return self._batch_command("retry", args)

    def resume_batch(self, args):
        return self._batch_command("resume", args)
