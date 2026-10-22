"""Portable, data-only study packages. No media, paths, prompts or credentials."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from classcue.video_store import VideoStore


MAX_SHARE_BYTES = 32 * 1024 * 1024
MAX_TIME_MS = 3_600_000
TimeMs = Annotated[int, Field(strict=True, ge=0, le=MAX_TIME_MS + 1)]
Mode = Annotated[str, Field(pattern=r"^(high_recall|medium_recall|low_recall|strict_exam_intent|custom_[a-zA-Z0-9_-]{1,80})$")]


class ShareError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class ShareModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class ShareWord(ShareModel):
    word: str = Field(max_length=10_000)
    start: float = Field(ge=0, le=3600.001)
    end: float = Field(ge=0, le=3600.001)
    probability: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def ordered(self):
        if self.end < self.start:
            raise ValueError("逐字时间范围无效")
        return self


class ShareSegment(ShareModel):
    id: int = Field(ge=0, le=2**31 - 1)
    start_ms: TimeMs
    end_ms: TimeMs
    text: str = Field(min_length=1, max_length=100_000)
    words: list[ShareWord] = Field(default_factory=list, max_length=20_000)

    @model_validator(mode="after")
    def ordered(self):
        if self.end_ms < self.start_ms or not self.text.strip():
            raise ValueError("字幕时间或正文无效")
        return self


class ShareMarker(ShareModel):
    start_ms: TimeMs
    end_ms: TimeMs
    title: str = Field(min_length=1, max_length=2000)
    note: str = Field(max_length=100_000)
    source_quote: str = Field(max_length=100_000)
    trigger_phrase: str = Field(max_length=2000)
    confidence: float = Field(ge=0, le=1)
    analysis_mode: Mode
    user_start_ms: TimeMs | None = None
    importance: Literal["standard", "important", "critical"] = "standard"

    @model_validator(mode="after")
    def ordered(self):
        if self.end_ms <= self.start_ms or not self.title.strip():
            raise ValueError("考点时间或标题无效")
        return self


class ShareRun(ShareModel):
    analysis_mode: Mode
    name: str = Field(min_length=1, max_length=200)
    scheme_revision: int = Field(default=0, ge=0, le=2**31 - 1)


class ShareVideo(ShareModel):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    name: str = Field(min_length=1, max_length=200)
    size: int = Field(gt=0, le=1024**3)
    duration_ms: int = Field(gt=0, le=MAX_TIME_MS)


class StudyPackage(ShareModel):
    format: Literal["classroom-study-share"]
    version: int = Field(strict=True, ge=1, le=1)
    exported_at: str = Field(max_length=80)
    video: ShareVideo
    transcription_model: str = Field(default="", max_length=200)
    subtitles: list[ShareSegment] = Field(min_length=1, max_length=100_000)
    markers: list[ShareMarker] = Field(max_length=20_000)
    analysis_runs: list[ShareRun] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def consistent(self):
        ids = [segment.id for segment in self.subtitles]
        modes = [run.analysis_mode for run in self.analysis_runs]
        if len(set(ids)) != len(ids) or len(set(modes)) != len(modes):
            raise ValueError("字幕编号或召回方案重复")
        keys = [(m.start_ms, m.end_ms, m.trigger_phrase, m.analysis_mode) for m in self.markers]
        if len(set(keys)) != len(keys):
            raise ValueError("分享包包含重复考点")
        if any(m.analysis_mode not in modes for m in self.markers):
            raise ValueError("考点缺少已完成的召回方案记录")
        duration = self.video.duration_ms
        # Match the ASR validators' existing one-millisecond rounding allowance.
        if any(s.end_ms > duration + 1 or any(w.end * 1000 > duration + 1 for w in s.words) for s in self.subtitles):
            raise ValueError("字幕时间超出视频时长")
        if any(m.end_ms > duration + 1 or (m.user_start_ms is not None and m.user_start_ms > duration)
               for m in self.markers):
            raise ValueError("考点时间超出视频时长")
        return self


def encode_package(package: StudyPackage) -> bytes:
    return json.dumps(package.model_dump(), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def decode_package(data: bytes) -> StudyPackage:
    if len(data) > MAX_SHARE_BYTES:
        raise ShareError("分享包超过 32 MB", 413)
    try:
        return StudyPackage.model_validate_json(data)
    except (ValidationError, ValueError, RecursionError) as exc:
        raise ShareError("分享包格式、版本或内容无效，请使用本软件导出的 .studyshare 文件") from exc


def package_digest(package: StudyPackage) -> str:
    return hashlib.sha256(encode_package(package)).hexdigest()


def verify_video(store: VideoStore, video, expected: ShareVideo | None = None) -> None:
    path = Path(video["video_path"])
    try:
        before = path.stat()
        digest = store.file_hash(path)
        after = path.stat()
    except OSError as exc:
        raise ShareError("本地视频无法读取，请先重新导入视频") from exc
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ShareError("视频文件在校验期间发生变化，请重试", 409)
    if digest != video["content_hash"]:
        raise ShareError("本地视频内容已变化，请先重新导入视频", 409)
    if expected and (digest != expected.sha256 or after.st_size != expected.size
                     or video["duration_ms"] != expected.duration_ms):
        raise ShareError("分享包与本地视频不匹配，未导入任何资料", 409)


BUILTIN_NAMES = {
    "high_recall": "高召回", "medium_recall": "中召回",
    "low_recall": "低召回", "strict_exam_intent": "旧版考点",
}


def export_package(store: VideoStore, video_id: int) -> bytes:
    with store.connect() as connection:
        connection.execute("BEGIN")
        video = connection.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
        if video is None:
            raise ShareError("视频不存在", 404)
        if video["status"] != "review_ready":
            raise ShareError("请等待视频处理完成后再导出分享包", 409)
        verify_video(store, video)
        segments = connection.execute(
            "SELECT * FROM video_transcript_segments WHERE video_id = ? ORDER BY start_ms, segment_id",
            (video_id,),
        ).fetchall()
        runs = connection.execute(
            """SELECT r.*, s.name AS scheme_name FROM video_analysis_runs r
            LEFT JOIN recall_schemes s ON s.id = r.analysis_mode
            WHERE r.video_id = ? AND r.status = 'completed' ORDER BY r.analysis_mode""", (video_id,),
        ).fetchall()
        modes = {row["analysis_mode"] for row in runs}
        markers = connection.execute(
            "SELECT * FROM video_markers WHERE video_id = ? AND review_status != 'excluded' ORDER BY start_ms, id",
            (video_id,),
        ).fetchall()
        payload = {
            "format": "classroom-study-share", "version": 1,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "video": {"sha256": video["content_hash"], "name": video["display_name"] or video["filename"],
                      "size": Path(video["video_path"]).stat().st_size, "duration_ms": video["duration_ms"]},
            "transcription_model": video["transcription_model"],
            "subtitles": [
                {"id": s["segment_id"], "start_ms": s["start_ms"], "end_ms": s["end_ms"], "text": s["text"],
                 "words": json.loads(s["words_json"] or "[]")} for s in segments
            ],
            "markers": [
                {key: m[key] for key in ShareMarker.model_fields} for m in markers if m["analysis_mode"] in modes
            ],
            "analysis_runs": [
                {"analysis_mode": r["analysis_mode"],
                 "name": r["scheme_name"] or BUILTIN_NAMES.get(r["analysis_mode"], "自定义方案"),
                 "scheme_revision": r["scheme_revision"]} for r in runs
            ],
        }
    try:
        package = StudyPackage.model_validate(payload)
    except (ValidationError, ValueError) as exc:
        raise ShareError("视频的完整字幕或已完成考点记录不可分享，请检查处理结果", 409) from exc
    data = encode_package(package)
    if len(data) > MAX_SHARE_BYTES:
        raise ShareError("分享资料超过 32 MB，无法导出", 413)
    return data


def target_video(store: VideoStore, package: StudyPackage):
    video = store.get_video_by_hash(package.video.sha256)
    if video is None:
        raise ShareError("视频库中没有匹配的视频，请先仅导入同一份视频，再重新选择分享包", 404)
    return video


def import_block_reason(connection, video_id: int) -> str | None:
    for table in ("video_transcript_segments", "video_markers", "video_analysis_runs", "video_keyword_hits",
                  "video_ai_chunks", "video_experiments"):
        if connection.execute(f"SELECT 1 FROM {table} WHERE video_id = ? LIMIT 1", (video_id,)).fetchone():
            return "此视频已有字幕或分析记录，未覆盖任何资料"
    status = connection.execute("SELECT status FROM videos WHERE id = ?", (video_id,)).fetchone()
    if status is None:
        return "视频已不存在，请重新选择分享包"
    if status["status"] not in {"uploaded", "failed"}:
        return "此视频已处理或正在处理，无法导入分享资料"
    return None


def preview_package(store: VideoStore, package: StudyPackage, blocked: str | None = None) -> dict:
    video = target_video(store, package)
    verify_video(store, video, package.video)
    digest = package_digest(package)
    with store.connect() as connection:
        receipt = connection.execute(
            "SELECT 1 FROM video_share_imports WHERE video_id = ? AND package_hash = ?", (video["id"], digest),
        ).fetchone()
        reason = blocked or import_block_reason(connection, video["id"])
    return {
        "package_hash": digest,
        "video_id": int(video["id"]),
        "video_name": video["display_name"] or video["filename"],
        "shared_video_name": package.video.name,
        "duration_ms": package.video.duration_ms,
        "subtitle_count": len(package.subtitles),
        "marker_count": len(package.markers),
        "analysis_runs": [run.model_dump() for run in package.analysis_runs],
        "already_imported": bool(receipt),
        "can_import": not blocked and (bool(receipt) or not reason),
        "block_reason": blocked or (None if receipt else reason),
    }


def import_package(store: VideoStore, package: StudyPackage, *, expected_hash: str, expected_video_id: int) -> int:
    digest = package_digest(package)
    video = target_video(store, package)
    if digest != expected_hash or video["id"] != expected_video_id:
        raise ShareError("分享包或匹配视频已改变，请重新预览", 409)
    verify_video(store, video, package.video)
    with store.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute("SELECT * FROM videos WHERE id = ?", (video["id"],)).fetchone()
        if current is None or current["content_hash"] != package.video.sha256:
            raise ShareError("视频已改变，请重新预览", 409)
        receipt = connection.execute(
            "SELECT 1 FROM video_share_imports WHERE video_id = ? AND package_hash = ?", (video["id"], digest),
        ).fetchone()
        if receipt:
            return int(video["id"])
        reason = import_block_reason(connection, video["id"])
        if reason:
            raise ShareError(reason, 409)
        mapping = {}
        for run in package.analysis_runs:
            mode = run.analysis_mode
            if mode.startswith("custom_"):
                # Shared schemes are historical result labels, never executable imported prompts.
                mode = f"custom_{uuid4().hex}"
                config = {"name": run.name, "base_mode": "medium_recall", "rules": [], "notes": ""}
                connection.execute(
                    "INSERT INTO recall_schemes(id, name, config_json, revision, archived) VALUES (?, ?, ?, ?, 1)",
                    (mode, run.name, json.dumps(config, ensure_ascii=False), max(1, run.scheme_revision)),
                )
            mapping[run.analysis_mode] = mode
            count = sum(m.analysis_mode == run.analysis_mode for m in package.markers)
            connection.execute(
                """INSERT INTO video_analysis_runs(video_id, analysis_mode, status, candidate_count, scheme_revision)
                VALUES (?, ?, 'completed', ?, ?)""",
                (video["id"], mode, count, max(1, run.scheme_revision) if mode.startswith("custom_") else 0),
            )
        connection.executemany(
            """INSERT INTO video_transcript_segments(video_id, segment_id, start_ms, end_ms, text, words_json)
            VALUES (?, ?, ?, ?, ?, ?)""",
            [(video["id"], s.id, s.start_ms, s.end_ms, s.text,
              json.dumps([w.model_dump(exclude_none=True) for w in s.words], ensure_ascii=False))
             for s in package.subtitles],
        )
        connection.executemany(
            """INSERT INTO video_markers(video_id, start_ms, end_ms, title, note, source_quote, trigger_phrase,
            confidence, analysis_mode, user_start_ms, importance, review_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')""",
            [(video["id"], m.start_ms, m.end_ms, m.title, m.note, m.source_quote, m.trigger_phrase,
              m.confidence, mapping[m.analysis_mode], m.user_start_ms, m.importance) for m in package.markers],
        )
        connection.execute(
            "UPDATE videos SET status = 'review_ready', transcription_model = ? WHERE id = ?",
            (package.transcription_model, video["id"]),
        )
        connection.execute(
            "INSERT INTO video_share_imports(video_id, package_hash) VALUES (?, ?)", (video["id"], digest),
        )
    return int(video["id"])
