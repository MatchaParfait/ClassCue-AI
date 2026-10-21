"""Shared resumable batch runner used by the Streamlit UI and CLI."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from classcue.asr_models import get_model, selected_model_id
from classcue.video_pipeline import (
    analyze_prepared_video_once,
    prepare_video_for_analysis,
    register_local_video,
)
from classcue.video_store import VideoStore
from classcue.transcriber import asr_session
from classcue.processing_control import ResourcePaused
from classcue.qwen_resilience import qwen_queue
from classcue.firered_resilience import firered_queue


@dataclass(frozen=True)
class BatchEstimate:
    remaining_videos: int
    remaining_media_seconds: float
    seconds: float | None


@dataclass(frozen=True)
class BatchEvent:
    total: int
    completed: int
    skipped: int
    failed: int
    in_progress: int
    message: str
    estimate: BatchEstimate


@dataclass(frozen=True)
class BatchSummary:
    total: int
    completed: int
    skipped: int
    failed: int
    in_progress: int
    paused: bool = False
    remaining_paths: tuple[str, ...] = ()


def _average_transcription_rate(store: VideoStore) -> float:
    rows = [row for row in store.list_processing_metrics() if row["transcription_seconds"] > 0 and row["duration_ms"] > 0]
    if rows:
        return sum(float(row["transcription_seconds"]) for row in rows) / sum(float(row["duration_ms"]) / 1000 for row in rows)
    try:
        from classcue.runtime import whisper_backend

        return 0.14 if whisper_backend().is_gpu else 0.75
    except Exception:
        return 0.5


def _average_ai_chunk_seconds(store: VideoStore) -> float:
    rows = [row for row in store.list_processing_metrics() if row["ai_seconds"] > 0 and row["ai_chunks"] > 0]
    if rows:
        return sum(float(row["ai_seconds"]) for row in rows) / sum(int(row["ai_chunks"]) for row in rows)
    return 20.0


def estimate_remaining(
    store: VideoStore,
    video_ids: list[int],
    analysis_mode: str = "medium_recall",
) -> BatchEstimate:
    """Estimate the bottleneck of the one-transcriber/one-AI pipeline."""
    if not video_ids:
        return BatchEstimate(0, 0.0, 0.0)
    transcribe_rate = _average_transcription_rate(store)
    ai_chunk_seconds = _average_ai_chunk_seconds(store)
    transcription_seconds = 0.0
    ai_seconds = 0.0
    remaining_media_seconds = 0.0
    remaining_videos = 0
    for video_id in video_ids:
        video = store.get_video(video_id)
        if not video:
            continue
        run = store.get_analysis_run(video_id, analysis_mode)
        segments = store.get_segments(video_id)
        needs_transcription = not segments and video["status"] != "review_ready"
        needs_ai = not run or run["status"] != "completed"
        if needs_transcription or needs_ai:
            remaining_videos += 1
            duration_seconds = max(1.0, int(video["duration_ms"]) / 1000)
            remaining_media_seconds += duration_seconds
        if needs_transcription:
            transcription_seconds += max(1.0, int(video["duration_ms"]) / 1000) * transcribe_rate
        if needs_ai:
            # Five-minute chunks with a thirty-second overlap are a useful upper estimate.
            chunk_count = max(1, int((max(1.0, int(video["duration_ms"]) / 1000) + 269) // 270))
            ai_seconds += chunk_count * ai_chunk_seconds
    if not remaining_videos:
        return BatchEstimate(0, 0.0, 0.0)
    return BatchEstimate(
        remaining_videos,
        remaining_media_seconds,
        max(transcription_seconds, ai_seconds) + 5.0,
    )


def format_eta(seconds: float | None) -> str:
    if seconds is None:
        return "正在估算"
    if seconds <= 0:
        return "即将完成"
    minutes = max(1, round(seconds / 60))
    hours, minutes = divmod(minutes, 60)
    return f"约 {hours} 小时 {minutes} 分钟" if hours else f"约 {minutes} 分钟"


@asr_session()
@qwen_queue()
@firered_queue()
def run_batch(
    store: VideoStore,
    paths: list[str | Path],
    audio_dir: str | Path,
    judge: object,
    *,
    use_keyword_prefilter: bool = False,
    trigger_groups: dict[str, dict[str, list[str]]] | None = None,
    analysis_mode: str = "medium_recall",
    on_event: Callable[[BatchEvent], None] | None = None,
    asr_model_id: str | None = None,
    filter_formula_noise: bool = True,
    qwen_options=None,
) -> BatchSummary:
    """Process selected videos while overlapping one AI pipeline with transcription."""
    chosen_model = get_model(asr_model_id or selected_model_id()).id
    total = len(paths)
    completed = skipped = in_progress = failed = 0
    video_ids: list[int] = []
    registered: list[tuple[int, Path]] = []

    def emit(message: str) -> None:
        if on_event:
            estimate = estimate_remaining(store, video_ids, analysis_mode)
            on_event(BatchEvent(total, completed, skipped, failed, in_progress, message, estimate))

    for raw_path in paths:
        try:
            video_id, path, _exists = register_local_video(store, raw_path, audio_dir)
            video_ids.append(video_id)
            registered.append((video_id, path))
            emit(f"已登记：{path.name}")
        except Exception as exc:
            failed += 1
            emit(f"{Path(raw_path).name} 登记失败：{exc}")

    analysis_futures: dict[Future[tuple[str, int]], Path] = {}
    paused = False
    remaining_paths = ()
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="batch-ai") as ai_executor:
        for index, (video_id, path) in enumerate(registered, start=1):
            try:
                outcome, _count = prepare_video_for_analysis(
                    store,
                    video_id,
                    trigger_groups=trigger_groups,
                    analysis_mode=analysis_mode,
                    asr_model_id=chosen_model,
                    progress=lambda _fraction, message: emit(message),
                    **({"qwen_options": qwen_options} if qwen_options is not None else {}),
                    skip_completed=not hasattr(judge, "analyze_video"),
                )
                if outcome == "prepared":
                    emit(f"{path.name}：字幕完成，AI 在后台筛选；继续处理下一个视频。")
                    future = ai_executor.submit(
                        analyze_prepared_video_once,
                        store,
                        video_id,
                        judge,
                        None,
                        use_keyword_prefilter,
                        trigger_groups,
                        analysis_mode,
                        filter_formula_noise,
                    )
                    analysis_futures[future] = path
                elif outcome == "skipped":
                    skipped += 1
                else:
                    in_progress += 1
                emit(f"已完成转写阶段：{index}/{len(registered)}")
            except ResourcePaused as exc:
                from classcue.qwen_resilience import atomic_json
                paused = True
                remaining_paths = tuple(str(p) for _id, p in registered[index - 1:])
                atomic_json(Path(audio_dir) / "batch_queue.json", {
                    "status": "resource_paused", "paths": list(remaining_paths),
                    "asr_model_id": chosen_model, "qwen_options": qwen_options,
                    "message": str(exc),
                })
                emit(f"{path.name}：资源不足，剩余队列已暂停；已启动的 AI 筛选将继续完成。{exc}")
                break
            except Exception as exc:
                failed += 1
                emit(f"{path.name} 转写失败：{exc}")

        emit(f"转写阶段完成，等待 {len(analysis_futures)} 个 AI 筛选任务。")
        for future in as_completed(list(analysis_futures)):
            path = analysis_futures[future]
            try:
                outcome, _count = future.result()
                if outcome == "skipped":
                    skipped += 1
                else:
                    completed += 1
                emit(f"{path.name}：AI 筛选完成。")
            except Exception as exc:
                failed += 1
                emit(f"{path.name} AI 筛选失败：{exc}")

    if not paused:
        from classcue.qwen_resilience import atomic_json, read_json
        queue_path = Path(audio_dir) / "batch_queue.json"
        saved_queue = read_json(queue_path, {})
        handled = {str(Path(path).resolve()) for path in paths}
        if (isinstance(saved_queue, dict) and saved_queue.get("status") == "resource_paused"
                and saved_queue.get("paths")
                and all(str(Path(path).resolve()) in handled for path in saved_queue["paths"])):
            atomic_json(queue_path, {**saved_queue, "status": "completed", "paths": []})
    return BatchSummary(total, completed, skipped, failed, in_progress, paused, remaining_paths)
