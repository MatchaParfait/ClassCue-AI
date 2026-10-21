"""Sequential ASR-only corpus preparation with per-video persistence and exports."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import format_time
from firered_resilience import firered_queue
from processing_control import ProcessingCancelled, ResourcePaused
from qwen_resilience import atomic_json
from scripts.prepare_llm_asr import prepare_video
from video_pipeline import discover_videos, register_local_video
from video_store import VideoStore


def file_link(path: Path) -> str:
    absolute = path.resolve().as_posix()
    return "<" + ("/" if path.drive else "") + absolute + ">"


def write_index(workspace: Path, manifest: dict) -> None:
    lines = [
        "# ASR 语料与正例标注清单", "",
        "以下是实际保存的 ASR，不是 LLM 考点。原始错字保留，未自动添加正例标签。",
        "按视频 ID、时间位置和教师原话标注即可；没有标注的位置不自动视作负例。",
        "阅读文本使用原始 ASR 编号，独立于 LLM 实验的聚合编号。", "",
        f"批次：`{manifest['batch_id']}`；状态：`{manifest['status']}`；本批 LLM 调用为 0。",
        f"源目录：`{manifest['source_folder']}`", "",
        "| 视频 ID | 文件名 | 时长 | 状态 | 原 ASR 段数 | 阅读文本 | 字幕 |",
        "|---|---|---|---|---:|---|---|",
    ]
    for item in manifest["videos"]:
        summary = item.get("summary") or {}
        export = workspace / "exports" / f"video-{item['video_id']}"
        text_link = f"[文本]({file_link(export / 'transcript.txt')})" if summary else ""
        vtt_link = f"[VTT]({file_link(export / 'subtitles.vtt')})" if summary else ""
        lines.append(
            f"| {item['video_id']} | {Path(item['source']).name} | "
            f"{format_time(item['duration_ms'])} | {item['status']} | "
            f"{summary.get('segment_count', '')} | {text_link} | {vtt_link} |"
        )
        if item.get("error"):
            lines += ["", f"视频 {item['video_id']} 错误：`{item['error']}`", ""]
    lines += [
        "", "## 标注方式", "",
        "你可以直接告诉我：视频 ID、时间位置或原字幕编号、认为是明确正例的教师原话。",
        "可以附上考点名称；不确定的部分单独标为待定。需要前后文时按原字幕范围一起提供。",
        "例如只描述格式：视频 ID / 时间 / 教师原话 / 考点名称。此处不预填任何标准答案。",
        "每个视频的 exports/video-ID 下还保存 subtitles.json（含精确时间和字词时间）与 preparation.json。",
        "已有视频复用字幕，不覆盖首次转写的 preparation.json；本次复用情况保存于批次 manifest。",
        "此清单只为人工阅读和定位，不将标注写入正式复习标记。", "",
    ]
    (workspace / "corpus-index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_folder(folder: Path, workspace: Path, model_id: str = "fireredasr2-aed") -> dict:
    workspace = workspace.resolve()
    paths = discover_videos(folder)
    if not paths:
        raise ValueError("No supported videos found")
    store = VideoStore(workspace / "project.db")
    entries = []
    for path in paths:
        previous = store.get_video_by_path(path)
        if previous and store.get_segments(int(previous["id"])):
            stat = path.stat()
            if (stat.st_size != previous["source_size"]
                    or stat.st_mtime_ns != previous["source_mtime_ns"]):
                raise ValueError(f"Source changed for saved ASR; refusing to overwrite: {path}")
            if previous["transcription_model"] != model_id:
                raise ValueError(f"Saved ASR uses another model; refusing to overwrite: {path}")
        video_id, source, _ = register_local_video(store, path, workspace / "temporary_audio")
        video = store.get_video(video_id)
        entries.append({
            "video_id": video_id, "source": str(source), "duration_ms": int(video["duration_ms"]),
            "status": "pending", "has_saved_asr": bool(store.get_segments(video_id)),
        })
    # Export reusable transcripts first so they are available during the long batch.
    entries.sort(key=lambda item: not item["has_saved_asr"])
    batch_id = "asr-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    directory = workspace / "batches" / batch_id
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {
        "batch_id": batch_id, "source_folder": str(folder.resolve()),
        "database": str(store.path), "model_id": model_id, "status": "running",
        "total": len(entries), "llm_requests": 0, "videos": entries,
    }

    def persist() -> None:
        manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
        atomic_json(directory / "manifest.json", manifest)
        atomic_json(workspace / "asr-batch-latest.json", manifest)
        write_index(workspace, manifest)

    persist()
    try:
        with firered_queue():
            for index, item in enumerate(entries, 1):
                item["status"] = "transcribing"
                persist()
                print(f"[{index}/{len(entries)}] video_id={item['video_id']} {item['source']}", flush=True)

                def progress(fraction: float, message: str) -> None:
                    item.update(progress=fraction, message=message)
                    atomic_json(directory / "manifest.json", manifest)
                    atomic_json(workspace / "asr-batch-latest.json", manifest)
                    print(f"[{index}/{len(entries)}] {fraction:.3f} {message}", flush=True)

                try:
                    summary = prepare_video(Path(item["source"]), workspace, model_id, progress=progress)
                    item.update(
                        status="reused" if summary["reused_database_transcript"] else "completed",
                        summary=summary, progress=1.0,
                    )
                    print(f"[{index}/{len(entries)}] saved {summary['segment_count']} segments", flush=True)
                except (ResourcePaused, ProcessingCancelled, KeyboardInterrupt) as error:
                    item.update(status="paused", error=str(error))
                    manifest["status"] = "paused"
                    raise
                except Exception as error:
                    item.update(status="failed", error=str(error))
                    print(f"[{index}/{len(entries)}] failed: {error}", flush=True)
                persist()
        manifest["status"] = (
            "completed_with_errors" if any(item["status"] == "failed" for item in entries) else "completed"
        )
        return manifest
    finally:
        if manifest["status"] == "running":
            manifest["status"] = "interrupted"
        persist()


def main() -> None:
    from filelock import FileLock

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--videos", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--model", default="fireredasr2-aed")
    args = parser.parse_args()
    args.workspace.mkdir(parents=True, exist_ok=True)
    with FileLock(str(args.workspace / "asr-batch.lock"), timeout=0):
        manifest = run_folder(args.videos, args.workspace, args.model)
    print(json.dumps({
        "batch_id": manifest["batch_id"], "status": manifest["status"],
        "total": manifest["total"],
        "completed": sum(item["status"] == "completed" for item in manifest["videos"]),
        "reused": sum(item["status"] == "reused" for item in manifest["videos"]),
        "failed": sum(item["status"] == "failed" for item in manifest["videos"]),
        "llm_requests": 0,
        "index": str(args.workspace.resolve() / "corpus-index.md"),
    }, ensure_ascii=False, indent=2), flush=True)
    if manifest["status"] != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
