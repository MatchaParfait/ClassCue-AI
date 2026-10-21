"""Idempotent, high-recall batch processing for local classroom videos."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Callable

from typing import Protocol

from core import (
    SemanticCandidate,
    SemanticResponseError,
    SemanticSelection,
    TranscriptSegment,
    build_semantic_chunks,
    filter_analysis_segments,
    find_all_trigger_matches,
    merge_candidate_windows,
)
from media import extract_audio, video_duration_ms
from asr_models import ModelBusyError, get_model, selected_model_id
from transcriber import ASRSetupError, asr_session, transcribe_audio
from video_store import VideoStore
from processing_control import ProcessingCancelled, ResourcePaused
from asr_storage import configured_audio_directory
from early_extraction import early_analysis


SUPPORTED_VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".ts", ".flv"}
MAX_VIDEO_BYTES = 1024 * 1024 * 1024
MAX_VIDEO_DURATION_MS = 60 * 60 * 1000
MAX_CONCURRENT_AI_REQUESTS = 4
AI_CHUNK_CACHE_VERSION = "semantic-v3-analysis-filter"


def _audio_path(store: VideoStore, video) -> Path:
    directory = configured_audio_directory(Path(__file__).resolve().parent, Path(store.path).resolve().parent)
    path = directory / Path(video["audio_path"]).name if directory else Path(video["audio_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class SemanticJudge(Protocol):
    def judge_semantic_chunk(self, chunk: tuple[TranscriptSegment, ...]) -> SemanticSelection: ...


def _chunk_cache_key(
    chunk: tuple[TranscriptSegment, ...],
    judge: SemanticJudge,
    analysis_mode: str,
    filter_formula_noise: bool,
    filter_formula: str | None = None,
) -> str:
    payload = {
        "version": AI_CHUNK_CACHE_VERSION,
        "mode": analysis_mode,
        "filter_formula_noise": filter_formula_noise,
        "judge": getattr(judge, "cache_identity", judge.__class__.__name__),
        "segments": [
            [segment.id, segment.start_ms, segment.end_ms, segment.text] for segment in chunk
        ],
    }
    if filter_formula:
        payload["filter_formula"] = filter_formula
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _transcribe_video_with_fallback(
    video_path: Path,
    temporary_audio_path: Path,
    progress: Callable[[float, str], None] | None = None,
    asr_model_id: str | None = None,
    should_stop: Callable[[], bool] | None = None,
    qwen_options=None,
) -> tuple[list[TranscriptSegment], int]:
    """Read the video audio track directly; use a temporary WAV only for incompatible media."""
    kwargs = {"progress": progress}
    if should_stop is not None:
        kwargs["should_stop"] = should_stop
    if asr_model_id is not None:
        kwargs["model_id"] = asr_model_id
    engine = get_model(asr_model_id or selected_model_id()).engine
    if engine in {"qwen", "firered"}:
        kwargs["cache_directory"] = temporary_audio_path.parent / f"{engine}_jobs"
        if engine == "qwen" and qwen_options is not None:
            kwargs["qwen_options"] = qwen_options
        return transcribe_audio(video_path, **kwargs)
    temporary_audio_path.unlink(missing_ok=True)
    try:
        return transcribe_audio(video_path, **kwargs)
    except (ASRSetupError, ModelBusyError, ProcessingCancelled, ResourcePaused):
        raise
    except Exception as direct_error:
        if progress:
            progress(0.02, "直接读取视频音轨失败，正在使用临时 WAV 兼容模式...")
        try:
            extract_audio(video_path, temporary_audio_path)
            return transcribe_audio(temporary_audio_path, **kwargs)
        except Exception as fallback_error:
            raise RuntimeError(
                f"无法转写视频音轨；直接读取失败：{direct_error}；临时 WAV 回退失败：{fallback_error}"
            ) from fallback_error
        finally:
            temporary_audio_path.unlink(missing_ok=True)


def discover_videos(folder: str | Path) -> list[Path]:
    root = Path(folder).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("找不到该本机视频文件夹")
    return sorted(path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in SUPPORTED_VIDEO_SUFFIXES)


def register_local_video(store: VideoStore, raw_path: str | Path, audio_dir: str | Path) -> tuple[int, Path, bool]:
    video_path = Path(raw_path).expanduser().resolve()
    if not video_path.is_file():
        raise ValueError("找不到该本机视频文件")
    if video_path.suffix.lower() not in SUPPORTED_VIDEO_SUFFIXES:
        raise ValueError("仅支持 mp4、mov、mkv、avi 视频文件")
    stat = video_path.stat()
    if stat.st_size > MAX_VIDEO_BYTES:
        raise ValueError("视频超过 1GB，请压缩后再识别")
    existing_path = store.get_video_by_path(video_path)
    if existing_path and int(existing_path["source_size"] or 0) == stat.st_size and int(existing_path["source_mtime_ns"] or 0) == stat.st_mtime_ns:
        return int(existing_path["id"]), video_path, True

    # The full hash is only needed for a new file or a file whose cheap identity changed.
    digest = store.file_hash(video_path)
    existing = store.get_video_by_hash(digest)
    if existing:
        store.update_video_path(int(existing["id"]), str(video_path), stat.st_size, stat.st_mtime_ns)
        return int(existing["id"]), video_path, True
    duration = video_duration_ms(video_path)
    if duration <= 0:
        raise ValueError("无法读取视频时长")
    if duration > MAX_VIDEO_DURATION_MS:
        raise ValueError("仅接受不超过 60 分钟的视频")
    audio_path = Path(audio_dir) / f"{digest}.wav"
    if existing_path:
        store.reset_video_content(
            int(existing_path["id"]), digest, video_path.name, str(video_path), str(audio_path), duration,
            stat.st_size, stat.st_mtime_ns,
        )
        return int(existing_path["id"]), video_path, False
    return store.create_video(
        digest, video_path.name, str(video_path), str(audio_path), duration,
        source_size=stat.st_size, source_mtime_ns=stat.st_mtime_ns,
    ), video_path, False


@dataclass(frozen=True)
class MarkerDraft:
    start_ms: int
    end_ms: int
    title: str
    note: str
    source_quote: str
    level: str
    confidence: float


def _draft_from_candidate(candidate: SemanticCandidate, segments_by_id: dict[int, TranscriptSegment]) -> MarkerDraft:
    start = segments_by_id[candidate.start_sentence_id]
    end = segments_by_id[candidate.end_sentence_id]
    evidence = candidate.evidence_quote.strip() or start.text
    note = f"AI {candidate.level} 候选：{candidate.reason.strip() or '建议回听确认。'}"
    return MarkerDraft(start.start_ms, end.end_ms, candidate.title.strip() or "疑似重点", note, evidence, candidate.level, candidate.confidence)


def merge_semantic_drafts(drafts: list[MarkerDraft], adjacent_seconds: int = 5) -> list[MarkerDraft]:
    """Remove duplicate candidates caused by overlapping transcript chunks."""
    if not drafts:
        return []
    ordered = sorted(drafts, key=lambda draft: (draft.start_ms, draft.end_ms))
    merged: list[MarkerDraft] = []
    current = ordered[0]
    for draft in ordered[1:]:
        if draft.start_ms <= current.end_ms + adjacent_seconds * 1000:
            title = current.title if current.level == "confirmed" or draft.level != "confirmed" else draft.title
            evidence = " | ".join(dict.fromkeys([current.source_quote, draft.source_quote]))
            note = " | ".join(dict.fromkeys([current.note, draft.note]))
            current = MarkerDraft(
                min(current.start_ms, draft.start_ms),
                max(current.end_ms, draft.end_ms),
                title,
                note,
                evidence,
                "confirmed" if "confirmed" in {current.level, draft.level} else "possible",
                max(current.confidence, draft.confidence),
            )
        else:
            merged.append(current)
            current = draft
    merged.append(current)
    return merged


def build_screening_chunks(
    segments: list[TranscriptSegment],
    use_keyword_prefilter: bool = False,
    trigger_groups: dict[str, dict[str, list[str]]] | None = None,
    filter_formula_noise: bool = True,
    filter_formula: str | None = None,
) -> list[tuple[TranscriptSegment, ...]]:
    """Build AI input chunks after optional low-information filtering."""
    excluded = [
        "".join(value.split()).casefold()
        for value in re.split(r"[,，;；\n]+", filter_formula or "") if value.strip()
    ]
    eligible_segments = [
        segment for segment in segments
        if not any(value in "".join(segment.text.split()).casefold() for value in excluded)
    ]
    analysis_segments = (
        filter_analysis_segments(eligible_segments, trigger_groups)
        if filter_formula_noise else eligible_segments
    )
    chunks = build_semantic_chunks(analysis_segments)
    if not use_keyword_prefilter:
        return chunks
    windows = merge_candidate_windows(
        analysis_segments,
        find_all_trigger_matches(analysis_segments, trigger_groups),
    )
    if not windows:
        return []
    return [
        chunk
        for chunk in chunks
        if any(chunk[-1].end_ms >= window.start_ms and chunk[0].start_ms <= window.end_ms for window in windows)
    ]


def create_semantic_markers(
    store: VideoStore,
    video_id: int,
    segments: list[TranscriptSegment],
    judge: SemanticJudge,
    progress: Callable[[float, str], None] | None = None,
    use_keyword_prefilter: bool = False,
    trigger_groups: dict[str, dict[str, list[str]]] | None = None,
    analysis_mode: str = "high_recall",
    filter_formula_noise: bool = True,
    filter_formula: str | None = None,
) -> int:
    """Screen all transcript chunks with at most four in-flight API requests."""
    chunks = build_screening_chunks(
        segments,
        use_keyword_prefilter,
        trigger_groups,
        filter_formula_noise,
        filter_formula,
    )
    segments_by_id = {segment.id: segment for segment in segments}
    positions = {segment.id: index for index, segment in enumerate(segments)}
    drafts: list[MarkerDraft] = []

    def validate_selection(
        selection: SemanticSelection, chunk: tuple[TranscriptSegment, ...]
    ) -> SemanticSelection:
        provided_ids = {segment.id for segment in chunk}
        candidates = []
        for candidate in selection.candidates:
            start_id, end_id = candidate.start_sentence_id, candidate.end_sentence_id
            if start_id not in provided_ids or end_id not in provided_ids:
                continue
            start, end = positions[start_id], positions[end_id]
            # Removed formula spans must not become an inferred continuous point.
            if start <= end and all(
                segment.id in provided_ids for segment in segments[start : end + 1]
            ):
                candidates.append(candidate)
        return SemanticSelection(candidates=candidates)

    def drafts_from_selection(selection: SemanticSelection) -> list[MarkerDraft]:
        return [_draft_from_candidate(candidate, segments_by_id) for candidate in selection.candidates]

    def review_chunk(chunk: tuple[TranscriptSegment, ...]) -> tuple[list[MarkerDraft], SemanticSelection | None]:
        try:
            selection = validate_selection(judge.judge_semantic_chunk(chunk), chunk)
            return drafts_from_selection(selection), selection
        except SemanticResponseError:
            # Keep this entire chunk for review instead of losing a video to malformed model JSON.
            return [
                MarkerDraft(
                    chunk[0].start_ms,
                    chunk[-1].end_ms,
                    "AI 输出待复核",
                    "该字幕段的 AI 返回格式异常，已保留整段供人工回听。",
                    chunk[0].text,
                    "possible",
                    0.0,
                )
            ], None

    pending_chunks: list[tuple[tuple[TranscriptSegment, ...], str]] = []
    for chunk in chunks:
        cache_key = _chunk_cache_key(chunk, judge, analysis_mode, filter_formula_noise, filter_formula)
        cached = store.get_ai_chunk_selection(video_id, analysis_mode, cache_key)
        if cached is None:
            pending_chunks.append((chunk, cache_key))
        else:
            selection = validate_selection(SemanticSelection.model_validate_json(cached), chunk)
            drafts.extend(drafts_from_selection(selection))

    if progress:
        if use_keyword_prefilter:
            mode = "关键词预筛选"
        elif filter_formula_noise:
            mode = "公式/低信息降噪后的字幕"
        else:
            mode = "完整字幕"
        cached_count = len(chunks) - len(pending_chunks)
        progress(
            cached_count / max(len(chunks), 1),
            f"AI 筛选{mode}片段：已复用 {cached_count}，待请求 {len(pending_chunks)}",
        )
    with ThreadPoolExecutor(
        max_workers=min(MAX_CONCURRENT_AI_REQUESTS, len(pending_chunks) or 1),
        thread_name_prefix="subtitle-ai",
    ) as executor:
        futures = {
            executor.submit(review_chunk, chunk): (chunk, cache_key)
            for chunk, cache_key in pending_chunks
        }
        cached_count = len(chunks) - len(pending_chunks)
        for completed, future in enumerate(as_completed(futures), start=1):
            _chunk, cache_key = futures[future]
            chunk_drafts, selection = future.result()
            drafts.extend(chunk_drafts)
            if selection is not None:
                store.save_ai_chunk_selection(
                    video_id, analysis_mode, cache_key, selection.model_dump_json()
                )
            if progress:
                progress(
                    (cached_count + completed) / max(len(chunks), 1),
                    f"AI 正在筛选{mode}片段 {cached_count + completed}/{len(chunks)}（最多 {MAX_CONCURRENT_AI_REQUESTS} 路）",
                )
    for draft in merge_semantic_drafts(drafts):
        store.add_marker(
            video_id,
            draft.start_ms,
            draft.end_ms,
            draft.title,
            draft.note,
            draft.source_quote,
            f"AI-{draft.level}",
            draft.confidence,
            analysis_mode,
        )
    return len(merge_semantic_drafts(drafts))


@asr_session()
def prepare_video_for_analysis(
    store: VideoStore,
    video_id: int,
    progress: Callable[[float, str], None] | None = None,
    trigger_groups: dict[str, dict[str, list[str]]] | None = None,
    analysis_mode: str = "high_recall",
    asr_model_id: str | None = None,
    should_stop: Callable[[], bool] | None = None,
    qwen_options=None,
    skip_completed: bool = True,
) -> tuple[str, int]:
    """Transcribe and persist one video before its API screening stage."""
    video = store.get_video(video_id)
    if not video:
        raise ValueError("视频记录不存在")
    if skip_completed and video["status"] == "review_ready":
        previous = store.get_analysis_run(video_id, analysis_mode)
        if previous and previous["status"] == "completed":
            return "skipped", len(store.list_markers(video_id, analysis_mode))
    if video["status"] == "processing":
        return "in_progress", 0
    video_path = Path(video["video_path"])
    if not video_path.is_file():
        store.set_status(video_id, "failed")
        raise ValueError("原视频已被移动或删除")
    try:
        store.set_status(video_id, "processing")
        segments = store.get_segments(video_id)
        if not segments:
            chosen_model = get_model(asr_model_id or selected_model_id()).id
            if progress:
                progress(0.02, f"{video['filename']}：直接读取视频音轨")
            transcription_started = time.perf_counter()
            segments, _duration = _transcribe_video_with_fallback(
                video_path,
                _audio_path(store, video),
                progress=(lambda value, message: progress(0.05 + value * 0.55, message)) if progress else None,
                asr_model_id=chosen_model,
                should_stop=should_stop,
                **({"qwen_options": qwen_options} if qwen_options is not None else {}),
            )
            store.record_transcription_metrics(video_id, time.perf_counter() - transcription_started)
            store.mark_transcribed(video_id, segments, chosen_model)
            # mark_transcribed records a durable transcript state; this batch still owns the video until AI finishes.
            store.set_status(video_id, "processing")
        store.replace_keyword_hits(video_id, find_all_trigger_matches(segments, trigger_groups), segments)
        if progress:
            progress(0.62, f"{video['filename']}：字幕已就绪，等待 AI 筛选")
        return "prepared", len(segments)
    except ResourcePaused:
        store.set_status(video_id, "resource_paused")
        raise
    except Exception:
        store.set_analysis_run(video_id, analysis_mode, "failed")
        store.set_status(video_id, "failed")
        raise


def analyze_prepared_video_once(
    store: VideoStore,
    video_id: int,
    judge: SemanticJudge,
    progress: Callable[[float, str], None] | None = None,
    use_keyword_prefilter: bool = False,
    trigger_groups: dict[str, dict[str, list[str]]] | None = None,
    analysis_mode: str = "high_recall",
    filter_formula_noise: bool = True,
    filter_formula: str | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> tuple[str, int]:
    """Run API screening against already persisted subtitles and mark the video review-ready."""
    video = store.get_video(video_id)
    if not video:
        raise ValueError("视频记录不存在")
    if hasattr(judge, "analyze_video"):
        try:
            outcome, count = judge.analyze_video(
                store, video_id, progress=progress,
                use_keyword_prefilter=use_keyword_prefilter, trigger_groups=trigger_groups,
                filter_formula_noise=filter_formula_noise, filter_formula=filter_formula,
                should_stop=should_stop,
            )
            store.set_status(video_id, "review_ready")
            return outcome, count
        except Exception:
            store.set_analysis_run(video_id, analysis_mode, "failed")
            store.set_status(video_id, "failed")
            raise
    if video["status"] == "review_ready":
        previous = store.get_analysis_run(video_id, analysis_mode)
        if previous and previous["status"] == "completed":
            return "skipped", len(store.list_markers(video_id, analysis_mode))
    segments = store.get_segments(video_id)
    if not segments:
        raise ValueError("该视频还没有可复用的字幕，无法开始 AI 筛选")
    try:
        store.set_analysis_run(video_id, analysis_mode, "processing")
        if progress:
            progress(0.0, f"{video['filename']}：AI 正在筛选完整字幕")
        chunk_count = len(
            build_screening_chunks(
                segments,
                use_keyword_prefilter,
                trigger_groups,
                filter_formula_noise,
                filter_formula,
            )
        )
        ai_started = time.perf_counter()
        count = create_semantic_markers(
            store,
            video_id,
            segments,
            judge,
            progress=(lambda value, message: progress(value, f"{video['filename']}：{message}")) if progress else None,
            use_keyword_prefilter=use_keyword_prefilter,
            trigger_groups=trigger_groups,
            analysis_mode=analysis_mode,
            filter_formula_noise=filter_formula_noise,
            filter_formula=filter_formula,
        )
        ai_elapsed = time.perf_counter() - ai_started
        # A cache-only rerun should not replace useful historical speed data with near-zero time.
        if ai_elapsed >= 0.2 and chunk_count:
            store.record_ai_metrics(video_id, ai_elapsed, chunk_count)
        store.set_analysis_run(video_id, analysis_mode, "completed", count)
        store.set_status(video_id, "review_ready")
        if progress:
            progress(1.0, f"{video['filename']}：完成，生成 {count} 个候选")
        return "completed", count
    except Exception:
        store.set_analysis_run(video_id, analysis_mode, "failed")
        store.set_status(video_id, "failed")
        raise


@asr_session()
def process_video_once(
    store: VideoStore,
    video_id: int,
    judge: SemanticJudge,
    progress: Callable[[float, str], None] | None = None,
    use_keyword_prefilter: bool = False,
    trigger_groups: dict[str, dict[str, list[str]]] | None = None,
    analysis_mode: str = "high_recall",
    asr_model_id: str | None = None,
    filter_formula_noise: bool = True,
    should_stop: Callable[[], bool] | None = None,
    filter_formula: str | None = None,
    force_retranscribe: bool = False,
    qwen_options=None,
) -> tuple[str, int]:
    """Keep final analysis unchanged; overlap FireRed's stable chunks with ASR."""
    with early_analysis(
        judge, store, video_id, asr_model_id=asr_model_id,
        force_retranscribe=force_retranscribe, should_stop=should_stop,
        use_keyword_prefilter=use_keyword_prefilter,
        trigger_groups=trigger_groups,
        filter_formula_noise=filter_formula_noise,
        filter_formula=filter_formula,
    ):
        if force_retranscribe:
            retranscribe_video(
                store, video_id, asr_model_id or selected_model_id(),
                progress=(lambda value, message: progress(value * 0.6, message)) if progress else None,
                should_stop=should_stop,
                **({"qwen_options": qwen_options} if qwen_options is not None else {}),
            )
        outcome, count = prepare_video_for_analysis(
            store, video_id, progress, trigger_groups, analysis_mode, asr_model_id, should_stop,
            **({"qwen_options": qwen_options} if qwen_options is not None else {}),
            skip_completed=not hasattr(judge, "analyze_video"),
        )
        if outcome != "prepared":
            return outcome, count
        return analyze_prepared_video_once(
            store,
            video_id,
            judge,
            progress=(lambda value, message: progress(0.62 + value * 0.38, message)) if progress else None,
            use_keyword_prefilter=use_keyword_prefilter,
            trigger_groups=trigger_groups,
            analysis_mode=analysis_mode,
            filter_formula_noise=filter_formula_noise,
            filter_formula=filter_formula,
            should_stop=should_stop,
        )


@asr_session()
def retranscribe_video(
    store: VideoStore,
    video_id: int,
    asr_model_id: str,
    progress: Callable[[float, str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    qwen_options=None,
) -> int:
    """Replace old results atomically only after a new transcription succeeds."""
    model_id = get_model(asr_model_id).id
    video = store.get_video(video_id)
    if not video:
        raise ValueError("视频记录不存在")
    if not Path(video["video_path"]).is_file():
        raise ValueError("找不到原视频")
    if not store.claim_retranscription(video_id):
        raise ValueError("该视频正在处理中，不能重新转写")
    try:
        transcription_started = time.perf_counter()
        segments, _duration = _transcribe_video_with_fallback(
            Path(video["video_path"]), _audio_path(store, video), progress, model_id,
            should_stop=should_stop,
            **({"qwen_options": qwen_options} if qwen_options is not None else {}),
        )
        if should_stop and should_stop():
            raise ProcessingCancelled("用户已请求停止处理")
        if not segments:
            raise ValueError("新模型没有识别到字幕，原字幕和复习状态已保留。")
        store.replace_transcript_for_reanalysis(video_id, segments, model_id)
        store.record_transcription_metrics(video_id, time.perf_counter() - transcription_started)
        return len(segments)
    except ResourcePaused:
        store.set_status(video_id, "resource_paused")
        raise
    except Exception:
        store.set_status(video_id, video["status"])
        raise


def process_saved_analysis_once(
    store: VideoStore,
    video_id: int,
    judge: SemanticJudge,
    analysis_mode: str,
    progress: Callable[[float, str], None] | None = None,
    use_keyword_prefilter: bool = False,
    trigger_groups: dict[str, dict[str, list[str]]] | None = None,
    filter_formula_noise: bool = True,
    force: bool = False,
    filter_formula: str | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> tuple[str, int]:
    """Analyze cached subtitles and restore review readiness after a successful retry."""
    if hasattr(judge, "analyze_video"):
        outcome, count = judge.analyze_video(
            store, video_id, progress=progress, force=force,
            use_keyword_prefilter=use_keyword_prefilter, trigger_groups=trigger_groups,
            filter_formula_noise=filter_formula_noise, filter_formula=filter_formula,
            should_stop=should_stop,
        )
        if outcome in {"completed", "skipped"}:
            store.set_status(video_id, "review_ready")
        return outcome, count
    previous = store.get_analysis_run(video_id, analysis_mode)
    if previous and previous["status"] == "completed" and not force:
        store.set_status(video_id, "review_ready")
        return "skipped", int(previous["candidate_count"])
    segments = store.get_segments(video_id)
    if not segments:
        raise ValueError("该视频还没有可复用的字幕，请先完成一次基础处理。")
    try:
        store.set_analysis_run(video_id, analysis_mode, "processing")
        store.delete_markers_for_mode(video_id, analysis_mode)
        chunk_count = len(
            build_screening_chunks(
                segments,
                use_keyword_prefilter,
                trigger_groups,
                filter_formula_noise,
                filter_formula,
            )
        )
        ai_started = time.perf_counter()
        count = create_semantic_markers(
            store,
            video_id,
            segments,
            judge,
            progress=progress,
            use_keyword_prefilter=use_keyword_prefilter,
            trigger_groups=trigger_groups,
            analysis_mode=analysis_mode,
            filter_formula_noise=filter_formula_noise,
            filter_formula=filter_formula,
        )
        ai_elapsed = time.perf_counter() - ai_started
        if ai_elapsed >= 0.2 and chunk_count:
            store.record_ai_metrics(video_id, ai_elapsed, chunk_count)
        store.set_analysis_run(video_id, analysis_mode, "completed", count)
        store.set_status(video_id, "review_ready")
        return "completed", count
    except Exception:
        store.set_analysis_run(video_id, analysis_mode, "failed")
        raise
