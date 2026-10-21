"""Run a frozen few-shot baseline once per newly transcribed video."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from threading import Event, RLock
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ai_provider import ProviderProfile, load_provider_settings, resolve_interface_key
from core import format_time
from llm_experiments import LabConfig, digest, execute_run, prepare_snapshot, register_run
from processing_control import ProcessingCancelled
from qwen_resilience import atomic_json
from video_store import VideoStore


def asr_hash(store: VideoStore, video_id: int) -> str:
    payload = [asdict(segment) for segment in store.get_segments(video_id)]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def link(path: Path) -> str:
    return "<" + ("/" if path.drive else "") + path.resolve().as_posix() + ">"


def export_run(store: VideoStore, item: dict, snapshot: dict, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    run_id = item["experiment_id"]
    for name, payload in (
        ("snapshot", snapshot), ("calls", store.list_experiment_calls(run_id)),
        ("steps", store.list_experiment_steps(run_id)),
        ("report", store.get_experiment_results(run_id)),
        ("status", dict(store.get_experiment(run_id))),
    ):
        atomic_json(directory / f"{name}.json", payload)


def write_review(store: VideoStore, manifest: dict, directory: Path) -> None:
    lines = [
        "# B 批次考点审查", "",
        "以下是模型候选，不是人工标准答案。未提取或未标注的内容不自动视作负例。",
        "引用失败、后端匹配与模型修正均保留在 steps.json，不改写首轮合规记录。失败视频不记为零考点。",
        ("本批仅离线重匹配既有 B 回答，没有发送新的 LLM 请求；调用和用量栏仅表示本批新增。"
         if manifest.get("operation") == "offline_citation_rematch"
         else "仅对离线匹配后仍不合规的候选请求引用修正，没有重新 ASR 或重新提取。"
         if manifest.get("operation") == "evidence_repair_replay"
         else "每个视频只运行 B；没有 A/C 比对或二次考点审核。"), "",
        f"批次：`{manifest['batch_id']}`；状态：`{manifest['status']}`。",
        f"接口配置模型：`{manifest['profile']['model']}`，不独立验证中转接口底层模型。",
        "", "| 视频 ID | 状态 | 最终候选 | 证据拒绝 | 调用 | 格式修正 | 总 token | 原始日志 |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for item in manifest["videos"]:
        r = item.get("summary", {})
        log = directory / item.get("label", f"video-{item['video_id']}")
        lines.append(
            f"| {item['video_id']} | {item['status']} | "
            f"{r.get('candidate_count', '')} | "
            f"{r.get('rejected_count', r.get('citation_metrics', {}).get('unresolved_count', ''))} | "
            f"{r.get('call_count', '')} | {r.get('format_repair_count', '')} | "
            f"{r.get('usage', {}).get('totals', {}).get('total_tokens', '')} | "
            f"[calls]({link(log / 'calls.json')}) / [steps]({link(log / 'steps.json')}) |"
        )
    for item in manifest["videos"]:
        if not item.get("experiment_id"):
            continue
        run_id = item["experiment_id"]
        report = store.get_experiment_results(run_id)
        if not report:
            continue
        log = directory / item.get("label", f"video-{item['video_id']}")
        snapshot = json.loads((log / "snapshot.json").read_text(encoding="utf-8"))
        segments = snapshot["input_segments"]
        lines += [
            "", f"## 视频 {item['video_id']}", "", f"文件：`{Path(item['source']).name}`",
            f"实验：`{run_id}`；状态：`{report['status']}`。",
            f"[完整结果]({link(log / 'report.json')}) / [冻结请求]({link(log / 'snapshot.json')})",
        ]
        if not report["complete"]:
            lines += ["", "实验未完成，不能据此认定无考点；错误见 status.json 和 calls.json。"]
        elif not report["results"]:
            lines += ["", "本次没有通过校验的最终候选，不代表人工确认无考点。"]
        for index, result in enumerate(report["results"], 1):
            first = next(i for i, segment in enumerate(segments)
                         if segment["id"] == result["start_sentence_id"])
            last = next(i for i, segment in enumerate(segments)
                        if segment["id"] == result["end_sentence_id"])
            lines += [
                "", f"### {index}. {result['title']}", "",
                f"位置：{format_time(result['start_ms'])}–{format_time(result['end_ms'])}；"
                f"信号：`{result['signal_type']}`；"
                f"原 ASR 编号：{result['source_start_sentence_id']}–{result['source_end_sentence_id']}。",
                f"理由：{result['reason']}",
                f"模型原始引文：{result.get('model_evidence_quote', result['evidence_quote'])}",
                *([f"后端定位的 ASR 原文：{result['evidence_quote']}；"
                   f"匹配方式：`{result['citation_match_method']}`。"]
                  if result.get("citation_match_method") else []),
                "", "附近字幕（聚合编号；时间由本地映射恢复）：", "", "`````text",
            ]
            for segment in segments[max(0, first - 2):last + 3]:
                lines.append(
                    f"{{{segment['id']}}} [{format_time(segment['start_ms'])}] {segment['text']}"
                )
            lines += ["`````"]
    (directory / "review.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_batch(source_manifest: Path, baseline_path: Path, *, execute: bool = False,
              concurrency: int = 2, publish_latest: bool = True) -> dict:
    if concurrency < 1:
        raise ValueError("Concurrency must be positive")
    source = json.loads(source_manifest.read_text(encoding="utf-8"))
    if source["status"] != "completed":
        raise ValueError("ASR batch must be complete")
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    # Historical baseline is input data only; fresh execution snapshots still require approval.
    if digest({key: value for key, value in baseline.items() if key != "snapshot_hash"}) != baseline["snapshot_hash"]:
        raise ValueError("Baseline snapshot was changed")
    config = LabConfig.model_validate(baseline["config"])
    if config.scheme != "few_shot":
        raise ValueError("Baseline must be scheme B (few_shot)")
    profile = ProviderProfile.model_validate(baseline["profile"])
    key = ""
    if execute:
        settings = load_provider_settings()
        if (settings.active_profile.base_url != profile.base_url
                or settings.active_profile.model != profile.model):
            raise ValueError("Active interface differs from the frozen baseline")
        key = resolve_interface_key(profile, settings)
        profile.require_ready(key)
    store = VideoStore(Path(source["database"]))
    workspace = store.path.parent
    entries, snapshots = [], {}
    for item in source["videos"]:
        if item["status"] != "completed" or item["summary"]["reused_database_transcript"]:
            continue
        video_id = item["video_id"]
        if asr_hash(store, video_id) != item["summary"]["transcript_sha256"]:
            raise ValueError(f"Saved ASR changed: video {video_id}")
        snapshot = prepare_snapshot(
            store, video_id, config, profile, bundle=baseline["prompt_bundle"],
        )
        snapshots[video_id] = snapshot
        entries.append({
            "video_id": video_id, "source": item["source"], "status": "prepared",
            "asr_sha256": item["summary"]["transcript_sha256"],
            "snapshot_hash": snapshot["snapshot_hash"],
            "chunk_count": len(snapshot["requests"]),
        })
    if not entries:
        raise ValueError("No newly transcribed videos in the source ASR batch")
    batch_id = "few-shot-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    directory = workspace / "batches" / batch_id
    manifest = {
        "batch_id": batch_id, "source_asr_batch": source["batch_id"],
        "database": str(store.path), "baseline_snapshot_hash": baseline["snapshot_hash"],
        "baseline_path": str(baseline_path.resolve()), "config": config.model_dump(),
        "profile": profile.model_dump(), "total": len(entries),
        "planned_extract_calls": sum(item["chunk_count"] for item in entries),
        "concurrency": concurrency,
        "status": "running" if execute else "preview_only",
        "authorization": "User requested scheme B for newly transcribed videos" if execute else None,
        "started_at": datetime.now(timezone.utc).isoformat(), "videos": entries,
        "directory": str(directory),
    }

    state_lock = RLock()
    cancelled = Event()

    def persist() -> None:
        with state_lock:
            manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
            atomic_json(directory / "manifest.json", manifest)
            if execute and publish_latest:
                atomic_json(workspace / "llm-batch-latest.json", manifest)

    for item in entries:
        atomic_json(directory / f"video-{item['video_id']}" / "snapshot.json",
                    snapshots[item["video_id"]])
    persist()
    if not execute:
        return manifest
    def process(index: int, item: dict) -> None:
        if cancelled.is_set():
            return
        try:
            video_id = item["video_id"]
            snapshot = snapshots[video_id]
            run_id = register_run(
                store, f"B batch {batch_id} video {video_id}", snapshot, snapshot["snapshot_hash"],
            )
            with state_lock:
                item.update(experiment_id=run_id, status="processing")
                persist()
            print(f"[{index}/{len(entries)}] video={video_id} experiment={run_id}", flush=True)

            def progress(fraction: float, message: str) -> None:
                with state_lock:
                    item.update(progress=fraction, message=message)
                    persist()
                print(f"[{index}/{len(entries)}] {fraction:.3f} {message}", flush=True)

            try:
                execute_run(
                    store, run_id, snapshot, key, snapshot["snapshot_hash"],
                    progress=progress, should_cancel=cancelled.is_set,
                )
            except (KeyboardInterrupt, ProcessingCancelled):
                with state_lock:
                    item["status"] = "interrupted"
                cancelled.set()
                raise
            except Exception:
                # The executor already persists a redacted error and a partial report.
                with state_lock:
                    item["status"] = "failed"
            finally:
                export_run(store, item, snapshot, directory / f"video-{video_id}")
                report = store.get_experiment_results(run_id)
                with state_lock:
                    if report:
                        item["status"] = report["status"]
                        steps = store.list_experiment_steps(run_id)
                        item["summary"] = {
                            name: report[name] for name in (
                                "complete", "candidate_count", "call_count", "usage",
                                "format_metrics", "format_repair_count", "transport_retry_count",
                                "response_format_fallback_count", "latency_seconds",
                            )
                        }
                        item["summary"]["rejected_count"] = report["citation_metrics"]["unresolved_count"]
                    persist()
                    write_review(store, manifest, directory)
            print(f"[{index}/{len(entries)}] saved status={item['status']} "
                  f"candidates={item.get('summary', {}).get('candidate_count')}", flush=True)
        except BaseException:
            cancelled.set()
            raise

    try:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(process, index, item) for index, item in enumerate(entries, 1)]
            try:
                for future in as_completed(futures):
                    future.result()
            except BaseException:
                cancelled.set()
                for future in futures:
                    future.cancel()
                raise
        manifest["status"] = (
            "completed_with_failures" if any(not item.get("summary", {}).get("complete")
                                            for item in entries) else "completed"
        )
        for item in entries:
            if asr_hash(store, item["video_id"]) != item["asr_sha256"]:
                raise AssertionError("ASR changed during LLM execution")
        return manifest
    finally:
        if manifest["status"] == "running":
            manifest["status"] = "interrupted"
        manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        persist()
        write_review(store, manifest, directory)


def main() -> None:
    from filelock import FileLock

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asr-manifest", type=Path, required=True)
    parser.add_argument("--baseline-snapshot", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--concurrency", type=int, default=2)
    args = parser.parse_args()
    source = json.loads(args.asr_manifest.read_text(encoding="utf-8"))
    with FileLock(str(Path(source["database"]).parent / "llm-batch.lock"), timeout=0):
        manifest = run_batch(
            args.asr_manifest, args.baseline_snapshot,
            execute=args.execute, concurrency=args.concurrency,
        )
    print(json.dumps({k: manifest[k] for k in (
        "batch_id", "status", "total", "planned_extract_calls", "directory",
    )}, ensure_ascii=False, indent=2), flush=True)
    if manifest["status"] not in ("completed", "preview_only"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
