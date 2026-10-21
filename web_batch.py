"""Durable WebUI batches; source directories are used only during import."""

from __future__ import annotations

from collections import Counter
from contextlib import ExitStack
from pathlib import Path
from typing import Callable

from api_failure import api_failure
from diagnostics import redact_text
from processing_control import ProcessingCancelled, ResourcePaused
from video_pipeline import (
    SUPPORTED_VIDEO_SUFFIXES, analyze_prepared_video_once,
    prepare_video_for_analysis, retranscribe_video,
)
from video_store import VideoStore, source_directory_key
from asr_models import get_model, selected_model_id
from early_extraction import early_analysis


DIRECTORY_NOTICE = "该文件夹或其子文件夹包含非视频文件，请移除非视频文件后再处理。"
DONE = {"completed", "skipped"}


def preview_directories(paths: list[str]) -> dict:
    results = []
    seen = set()
    roots = [Path(raw).expanduser().absolute() for raw in paths]
    # Selecting both an ancestor and its child imports the tree once.
    roots = [root for root in roots if not any(
        other != root and other in root.parents for other in roots
    )]
    for root in roots:
        row = {
            "path": str(root), "video_count": 0, "non_video_count": 0,
            "subfolder_count": 0, "examples": [], "valid": False, "error": "",
        }
        videos, directories = [], []
        try:
            key = source_directory_key(root)
            if key in seen:
                continue
            seen.add(key)
            if root.resolve() != root or root.is_symlink():
                raise ValueError("请选择真实文件夹，不能使用符号链接或目录联接")
            if not root.is_dir():
                raise ValueError("文件夹不存在")
            pending = [root]
            while pending:
                directory = pending.pop()
                directories.append(str(directory))
                children = sorted(directory.iterdir(), key=lambda child: child.name.casefold())
                for child in children:
                    # Never follow links/junctions, even if they point at videos.
                    linked = child.is_symlink() or child.resolve() != child
                    if not linked and child.is_dir():
                        row["subfolder_count"] += 1
                        pending.append(child)
                    elif not linked and child.is_file() and child.suffix.lower() in SUPPORTED_VIDEO_SUFFIXES:
                        videos.append(str(child))
                        row["video_count"] += 1
                        continue
                    else:
                        row["non_video_count"] += 1
                        if len(row["examples"]) < 5:
                            row["examples"].append(str(child.relative_to(root)))
            if row["non_video_count"]:
                row["error"] = DIRECTORY_NOTICE
            elif not videos:
                row["error"] = "该文件夹没有支持的视频文件"
            else:
                row["valid"] = True
        except (OSError, ValueError) as exc:
            row["error"] = str(exc)
        row["videos"] = sorted(videos)
        row["directories"] = directories
        results.append(row)
    return {"valid": bool(results) and all(row["valid"] for row in results), "folders": results}


def make_batch(preview: dict, options: dict, registered: list[dict] | None = None) -> dict:
    entries = [
        {"source_path": path, "source_root": folder["path"], "video_id": None,
         "status": "pending_import", "error": ""}
        for folder in preview["folders"] for path in folder["videos"]
    ]
    entries.extend(registered or [])
    return {
        "kind": "batch", "version": 2, "options": options, "entries": entries,
        "source_directories": [
            {"path": path, "root": folder["path"]}
            for folder in preview["folders"] for path in folder.get("directories", [folder["path"]])
        ],
        "folders": [], "items": [], "api_error": "", "current_index": None,
    }


def _reconcile_entry_status(entry: dict, store: VideoStore | None, modes: list[str], schemes: dict | None = None) -> None:
    """Refresh a stale batch entry after the video was repaired outside this batch."""
    if store is None or entry.get("status") in DONE or not entry.get("video_id"):
        return
    video_id = int(entry["video_id"])
    video = store.get_video(video_id)
    if video is None:
        return
    if video["status"] == "review_ready" and (
        not modes or all(
            (run := store.get_analysis_run(video_id, mode)) is not None
            and run["status"] == "completed"
            and run["scheme_revision"] == (schemes or {}).get(mode, {}).get("revision", 0)
            for mode in modes
        )
    ):
        entry.update(status="completed", error="")


def batch_summary(state: dict, store: VideoStore | None = None) -> dict:
    options = state.get("options", {})
    modes = options.get("analysis_modes") or ([options["analysis_mode"]] if options.get("analysis_mode") else [])
    for entry in state.get("entries", []):
        _reconcile_entry_status(entry, store, modes, options.get("analysis_schemes"))
    counts = Counter(entry["status"] for entry in state.get("entries", []))
    retryable = sum(
        1 for entry in state.get("entries", [])
        if entry.get("status") in {"pending_import", "pending_asr", "pending_ai"}
        or (entry.get("status") == "failed" and (
            store is None or (entry.get("video_id") and store.get_video(int(entry["video_id"])) is not None)
        ))
    )
    return {
        "total": len(state.get("entries", [])),
        "completed": counts["completed"], "skipped": counts["skipped"],
        "failed": counts["failed"], "awaiting_ai": counts["pending_ai"],
        "pending_asr": counts["pending_asr"] + counts["pending_import"],
        "retryable": retryable,
    }


def normalize_batch(request: dict, store: VideoStore, *, restore_completed: bool = True) -> dict:
    if request.get("version") == 2:
        return request
    entries = []
    for video_id, _path in request.get("items", []):
        video = store.get_video(int(video_id))
        options = request.get("options", {})
        modes = options.get("analysis_modes") or [options.get("analysis_mode", "medium_recall")]
        previous = [store.get_analysis_run(int(video_id), mode) for mode in modes]
        completed = (
            restore_completed and video is not None and video["status"] == "review_ready"
            and all(run is not None and run["status"] == "completed" for run in previous)
        )
        entries.append({
            "video_id": int(video_id), "source_path": "", "source_root": "",
            "status": "completed" if completed else "pending_ai" if store.get_segments(int(video_id)) else "pending_asr",
            "error": "", "label": video["filename"] if video else str(video_id),
            "video_path": video["video_path"] if video else "",
        })
    return {**request, "version": 2, "entries": entries, "folders": [], "api_error": "", "current_index": None}


def run_web_batch(
    store: VideoStore, state: dict, options, *,
    save: Callable[[], None], update, should_cancel, import_video, judge_factory,
) -> dict:
    entries = state["entries"]

    def checkpoint():
        state["items"] = [
            [entry["video_id"], entry.get("video_path", "")]
            for entry in entries if entry.get("video_id")
        ]
        save()

    def stop():
        if should_cancel():
            raise ProcessingCancelled("用户已请求停止批处理")

    # Validate every still-unimported source before any move, including a resumed import.
    roots = list(dict.fromkeys(
        entry["source_root"] for entry in entries
        if entry["status"] == "pending_import" and Path(entry["source_path"]).exists()
    ))
    if roots:
        inspection = preview_directories(roots)
        if not inspection["valid"]:
            raise ValueError("；".join(row["error"] for row in inspection["folders"] if not row["valid"]))

    folder_ids = {}
    known_folder_ids = {folder["folder_id"] for folder in state["folders"]}

    def ensure_folder(source: Path, root: Path) -> int:
        key = str(source)
        if key in folder_ids:
            return folder_ids[key]
        if source != root and root not in source.parents:
            raise ValueError("视频路径不在所选文件夹内")
        parent_id = ensure_folder(source.parent, root) if source != root else None
        folder_id = store.import_folder(source, parent_id)
        folder_ids[key] = folder_id
        if folder_id not in known_folder_ids:
            state["folders"].append({
                "folder_id": folder_id, "name": store.get_folder(folder_id)["name"],
                "path": str(source),
            })
            known_folder_ids.add(folder_id)
        return folder_id

    if any(entry["status"] == "pending_import" for entry in entries):
        # Persist empty directories as well as the directories containing videos.
        for directory in state.get("source_directories", []):
            stop()
            ensure_folder(Path(directory["path"]), Path(directory["root"]))
        checkpoint()

    for index, entry in enumerate(entries):
        if entry["status"] != "pending_import":
            continue
        stop()
        state["current_index"] = index
        source = Path(entry["source_path"])
        try:
            update(0, f"{source.name}：正在保存到视频库", "importing")
            folder_id = ensure_folder(source.parent, Path(entry["source_root"]))
            entry["folder_id"] = folder_id
            if source.is_file():
                entry["content_hash"] = store.file_hash(source)
                checkpoint()
            existing = store.get_video_by_hash(entry.get("content_hash", ""))
            if existing is not None:
                entry["video_id"] = int(existing["id"])
                entry["video_path"] = existing["video_path"]
                entry["warning"] = "重复视频已复用，保留原视频库归属和来源文件"
                entry["status"] = "pending_asr"
                checkpoint()
            else:
                if not source.is_file():
                    raise ValueError("待导入视频已被移动或删除")

                def registered(video_id):
                    store.set_video_folder(video_id, folder_id)
                    entry.update(video_id=video_id, status="pending_asr")
                    entry["video_path"] = store.get_video(video_id)["video_path"]
                    checkpoint()

                _id, _path, _exists, warning = import_video(source, on_registered=registered)
                entry["warning"] = warning or ""
            if any(previous.get("video_id") == entry["video_id"] for previous in entries[:index]):
                entry["status"] = "skipped"
            checkpoint()
        except (ProcessingCancelled, ResourcePaused):
            raise
        except Exception as exc:
            entry.update(status="failed", error=redact_text(exc))
            checkpoint()

    if state.get("import_only"):
        state["current_index"] = None
        checkpoint()
        imported = sum(1 for entry in entries if entry.get("video_id"))
        failed = sum(1 for entry in entries if entry.get("status") == "failed")
        message = f"已导入 {imported} 个视频，尚未开始处理"
        if failed:
            message += f"，{failed} 个视频导入失败"
        return {"job_status": "completed", "message": message, "committed": True}

    judges = {}
    modes = options.analysis_modes or [options.analysis_mode]
    state["api_error"] = ""
    try:
        for index, entry in enumerate(entries):
            if entry.get("video_id") and any(
                previous.get("video_id") == entry["video_id"] for previous in entries[:index]
            ):
                entry["status"] = "skipped"
                checkpoint()
            if entry["status"] in DONE or entry["status"] == "failed":
                continue
            stop()
            state["current_index"] = index
            checkpoint()
            video_id = entry.get("video_id")
            video = store.get_video(video_id) if video_id else None
            label = video["filename"] if video else Path(entry.get("source_path", "")).name or str(video_id)
            entry["label"] = label
            if video:
                entry["video_path"] = video["video_path"]

            def report(value, message):
                update((index + min(1, max(0, value))) / max(1, len(entries)), f"{label}：{message}", None)

            ai_stage = False
            early_scope = ExitStack()
            try:
                if video is None:
                    raise ValueError("视频记录已删除")
                if not Path(video["video_path"]).is_file():
                    raise ValueError("视频文件已被移动或删除")
                groups = {"custom": {"用户关键词": options.keywords}} if options.keywords else None
                early_error = None
                if (
                    not state["api_error"]
                    and get_model(options.asr_model_id or selected_model_id()).engine == "firered"
                ):
                    try:
                        if modes[0] not in judges:
                            judges[modes[0]] = judge_factory(modes[0])
                        early_scope.enter_context(early_analysis(
                            judges[modes[0]], store, video_id,
                            asr_model_id=options.asr_model_id,
                            force_retranscribe=options.force_retranscribe,
                            should_stop=should_cancel,
                            use_keyword_prefilter=options.use_keyword_prefilter,
                            trigger_groups=groups, filter_formula_noise=options.filter_formula_noise,
                            filter_formula=options.filter_formula,
                        ))
                    except Exception as exc:
                        # Invalid/unavailable API settings must not prevent ASR.
                        early_error = exc
                if options.force_retranscribe and not entry.get("retranscribed"):
                    retranscribe_video(
                        store, video_id, options.asr_model_id, progress=report,
                        should_stop=should_cancel, qwen_options=options.qwen_options.model_dump(),
                    )
                    entry["retranscribed"] = True
                    checkpoint()
                prepare_video_for_analysis(
                    store, video_id, progress=report, analysis_mode=options.analysis_mode,
                    asr_model_id=options.asr_model_id, should_stop=should_cancel,
                    qwen_options=options.qwen_options.model_dump(), skip_completed=False,
                )
                entry.update(status="pending_ai", error="")
                ai_stage = True
                checkpoint()
                if early_error is not None:
                    raise early_error
                if state["api_error"]:
                    store.set_status(video_id, "review_ready" if video["status"] == "review_ready" else "transcribed")
                    report(1, "字幕已保存，API 暂不可用；继续剩余 ASR")
                    continue
                completed_modes = entry.setdefault("completed_modes", [])
                outcomes = []
                for mode_index, mode in enumerate(modes):
                    stop()
                    if mode in completed_modes:
                        continue
                    if mode not in judges:
                        judges[mode] = judge_factory(mode)
                    outcome, _count = analyze_prepared_video_once(
                        store, video_id, judges[mode],
                        progress=lambda value, message: report((mode_index + value) / len(modes), message),
                        use_keyword_prefilter=options.use_keyword_prefilter, trigger_groups=groups,
                        analysis_mode=mode, filter_formula_noise=options.filter_formula_noise,
                        filter_formula=options.filter_formula, should_stop=should_cancel,
                    )
                    outcomes.append(outcome)
                    completed_modes.append(mode)
                    checkpoint()
                entry["status"] = "skipped" if outcomes and all(value == "skipped" for value in outcomes) else "completed"
                checkpoint()
            except ResourcePaused as exc:
                exc.resume_request = state
                checkpoint()
                raise
            except ProcessingCancelled:
                store.mark_interrupted_video_failed(video_id) if video_id else None
                checkpoint()
                raise
            except Exception as exc:
                failure = api_failure(exc) if ai_stage else None
                if failure is not None:
                    state["api_error"] = redact_text(exc)
                    entry.update(status="pending_ai", error=state["api_error"])
                    store.set_status(video_id, "review_ready" if video["status"] == "review_ready" else "transcribed")
                    report(1, "API 暂不可用，字幕已保存；继续剩余 ASR")
                else:
                    entry.update(status="failed", error=redact_text(exc))
                    if video_id:
                        store.mark_interrupted_video_failed(video_id)
                    report(1, f"处理失败，继续下一个（{entry['error']}）")
                checkpoint()
            finally:
                early_scope.close()
        state["current_index"] = None
        checkpoint()
        summary = batch_summary(state)
        waiting = summary["awaiting_ai"] > 0
        message = (
            f"字幕已保存，等待 AI 恢复：{summary['awaiting_ai']} 个待 AI，{summary['failed']} 个失败"
            if waiting else
            f"批处理结束：{summary['completed']} 个完成，{summary['skipped']} 个跳过，{summary['failed']} 个失败"
        )
        update(1, message, "awaiting_api" if waiting else "completed")
        return {"job_status": "awaiting_api" if waiting else "completed", "message": message, "summary": summary}
    finally:
        for judge in judges.values():
            judge.close()
