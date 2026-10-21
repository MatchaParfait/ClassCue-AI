"""Sequential full-video B experiments with frozen prompts, efforts and chunk sizes."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ai_provider import ProviderProfile, load_provider_settings, resolve_interface_key
import llm_experiments as lab
from qwen_resilience import atomic_json
from scripts.llm_lab_batch import asr_hash, export_run, link, write_review
from scripts.repair_llm_evidence import preservation_state, verify_preservation
from video_store import VideoStore


def summarize(store: VideoStore, run_id: str) -> dict:
    report = store.get_experiment_results(run_id)
    calls = store.list_experiment_calls(run_id)
    reasoning = [
        call["usage"]["completion_tokens_details"]["reasoning_tokens"]
        for call in calls if isinstance(
            ((call.get("usage") or {}).get("completion_tokens_details") or {}).get("reasoning_tokens"), int
        )
    ]
    usage_calls = [call for call in calls if call.get("usage") is not None]
    extract_calls = [call for call in calls if call["stage"] == "extract"]
    return {
        **{key: report[key] for key in (
            "complete", "candidate_count", "call_count", "usage", "latency_seconds",
            "format_metrics", "format_repair_count", "transport_retry_count", "citation_metrics",
        )},
        "reasoning_tokens_reported": sum(reasoning) if reasoning else None,
        "reasoning_usage_call_count": len(reasoning),
        "missing_usage_call_count": len(calls) - len(usage_calls),
        "usage_sum_mismatch_count": sum(
            c["usage"].get("total_tokens") != (
                c["usage"].get("prompt_tokens", 0) + c["usage"].get("completion_tokens", 0)
            ) for c in usage_calls
        ),
        "extract_call_count": len(extract_calls),
        "extract_reported_tokens": {
            key: sum((c.get("usage") or {}).get(key, 0) or 0 for c in extract_calls)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        },
        "requested_efforts": sorted({c["request"].get("reasoning_effort", "missing") for c in calls}),
        "returned_models": sorted({c["returned_model"] for c in calls if c.get("returned_model")}),
        "all_calls_terminal": all(c["status"] != "started" for c in calls),
    }


def write_progress(manifest: dict, directory: Path) -> None:
    lines = [
        "# 推理档位与分片长度实验", "",
        f"2 个视频，共 {manifest['planned_experiments']} 个完整视频实验；请求并发为 1。",
        f"提示词版本：{manifest.get('prompt_variant', 'high_recall')}；具体档位与分片见下表。",
        "方案 B、字幕聚合、30 秒重叠、引用匹配与限次打回规则相同。所有实际请求显式指定 reasoning_effort。",
        "档位被接口接受不代表中转服务已证明透传；用量来自接口 usage，缺失用量单独记录。", "",
        f"批次：`{manifest['batch_id']}`；状态：`{manifest['status']}`。", "",
        "| 视频 | 档位 | 分片 | 状态 | 考点候选 | 调用 | 输入 token | 输出 token | 推理 token | 总 token | 实验耗时秒 |",
        "|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in manifest["experiments"]:
        summary = item.get("summary", {})
        usage = summary.get("usage", {}).get("totals", {})
        lines.append(
            f"| {item['video_id']} | {item['reasoning_effort']} | {item['chunk_seconds'] // 60}min | "
            f"{item['status']} | {summary.get('candidate_count', '')} | "
            f"{summary.get('call_count', '')} | {usage.get('prompt_tokens', '')} | "
            f"{usage.get('completion_tokens', '')} | {summary.get('reasoning_tokens_reported', '')} | "
            f"{usage.get('total_tokens', '')} | {item.get('wall_seconds', '')} |"
        )
    for item in manifest["experiments"]:
        if not item.get("experiment_id"):
            continue
        folder = directory / item["label"]
        lines += [
            "", f"## 视频 {item['video_id']} / {item['reasoning_effort']} / {item['chunk_seconds'] // 60}min",
            "", f"实验：`{item['experiment_id']}`。",
            f"[请求与响应]({link(folder / 'calls.json')}) / "
            f"[校验及修正]({link(folder / 'steps.json')}) / [报告]({link(folder / 'report.json')})",
        ]
    (directory / "progress.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def prepare_matrix(
    workspace: Path, *, efforts: tuple[str, ...] = ("low", "medium", "high"),
    chunk_sizes: tuple[int, ...] = (300, 600), prompt_variant: str = "high_recall",
) -> dict:
    if (not efforts or len(set(efforts)) != len(efforts)
            or any(effort not in ("low", "medium", "high") for effort in efforts)):
        raise ValueError("Invalid or duplicated reasoning efforts")
    if (not chunk_sizes or len(set(chunk_sizes)) != len(chunk_sizes)
            or any(seconds not in (300, 600) for seconds in chunk_sizes)):
        raise ValueError("Invalid or duplicated chunk sizes")
    if prompt_variant not in ("high_recall", "precision", "low_recall"):
        raise ValueError("Invalid prompt variant")
    source_path = workspace / "batches/few-shot-20261012T151954Z-772190d5/manifest.json"
    seed_path = workspace / "batches/citation-repair-20261012T162436Z-aff5c17a/video-5/snapshot.json"
    seed = json.loads(seed_path.read_text("utf-8"))
    if lab.digest({key: value for key, value in seed.items() if key != "snapshot_hash"}) != seed["snapshot_hash"]:
        raise ValueError("Seed snapshot changed")
    source = json.loads(source_path.read_text("utf-8"))
    store = VideoStore(Path(source["database"]))
    profile = ProviderProfile.model_validate({**seed["profile"], "timeout_seconds": 300})
    prefix = {
        "precision": "precision-pair-", "low_recall": "low-recall-pair-",
        "high_recall": "reasoning-matrix-",
    }[prompt_variant]
    batch_id = prefix + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    directory = workspace / "batches" / batch_id
    entries = []
    source_items = {item["video_id"]: item for item in source["videos"]}
    bundle = lab.load_prompt_bundle(variant=prompt_variant) if prompt_variant != "high_recall" else seed["prompt_bundle"]
    # Every level gets both videos and lengths before proceeding to the next level.
    for effort in efforts:
        for video_id in (5, 11):
            for seconds in chunk_sizes:
                config = lab.LabConfig.model_validate({
                    **seed["config"], "chunk_seconds": seconds, "reasoning_effort": effort,
                    "max_tokens": 32768,
                })
                if asr_hash(store, video_id) != source_items[video_id]["asr_sha256"]:
                    raise ValueError("Saved transcript changed")
                snapshot = lab.prepare_snapshot(store, video_id, config, profile, bundle=bundle)
                source_snapshot = json.loads((
                    Path(source["directory"]) / f"video-{video_id}/snapshot.json"
                ).read_text("utf-8"))
                if snapshot["transcript_hash"] != source_snapshot["transcript_hash"]:
                    raise ValueError("Saved transcript differs from previous B experiments")
                view_changed = snapshot["input_view_hash"] != source_snapshot["input_view_hash"]
                if view_changed:
                    def without_whitespace(view: list[dict]) -> list[dict]:
                        return [{**segment, "text": "".join(segment["text"].split())} for segment in view]

                    if (prompt_variant == "high_recall" or
                            without_whitespace(snapshot["input_segments"]) !=
                            without_whitespace(source_snapshot["input_segments"])):
                        raise ValueError("Input view differs beyond permitted ASR fragment spacing")
                unchanged = ("repair", "evidence_repair") if prompt_variant != "high_recall" else (
                    "system", "rules", "task", "examples",
                )
                for name in unchanged:
                    reference = seed if prompt_variant != "high_recall" else source_snapshot
                    if snapshot["prompt_bundle"][name] != reference["prompt_bundle"][name]:
                        raise ValueError("Extraction instructions changed")
                label = f"video-{video_id}-{effort}-{seconds}"
                atomic_json(directory / label / "snapshot.json", snapshot)
                entries.append({
                    "label": label, "video_id": video_id, "source": source_items[video_id]["source"],
                    "reasoning_effort": effort, "chunk_seconds": seconds,
                    "chunk_count": len(snapshot["requests"]), "snapshot_hash": snapshot["snapshot_hash"],
                    "asr_sha256": source_items[video_id]["asr_sha256"], "status": "prepared",
                    "baseline_input_view_hash": source_snapshot["input_view_hash"],
                    "input_view_hash": snapshot["input_view_hash"],
                    "baseline_spacing_changed": view_changed,
                })
    manifest = {
        "batch_id": batch_id, "operation": "reasoning_effort_chunk_matrix", "status": "prepared",
        "database": str(store.path), "directory": str(directory), "concurrency": 1,
        "prompt_variant": prompt_variant,
        "profile": profile.model_dump(), "source_manifest": str(source_path),
        "script_hash": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "source_seed_snapshot": str(seed_path), "experiments": entries,
        "planned_experiments": len(entries),
        "planned_extract_requests": sum(item["chunk_count"] for item in entries),
        "started_at": None,
        "authorization": f"User requested {len(entries)} selected full-video experiments, concurrency 1",
        "common_max_tokens": 32768, "common_timeout_seconds": 300,
        "effort_verification": "Explicitly sent; relay execution of the effort is not independently verified",
    }
    atomic_json(directory / "manifest.json", manifest)
    write_progress(manifest, directory)
    return manifest


def execute_matrix(manifest_path: Path) -> dict:
    manifest = json.loads(manifest_path.read_text("utf-8"))
    if manifest["status"] != "prepared":
        raise ValueError("Matrix can execute only once")
    if manifest["script_hash"] != hashlib.sha256(Path(__file__).read_bytes()).hexdigest():
        raise ValueError("Matrix runner changed; prepare again")
    directory = Path(manifest["directory"])
    store = VideoStore(Path(manifest["database"]))
    source = json.loads(Path(manifest["source_manifest"]).read_text("utf-8"))
    before = preservation_state(store, source)
    profile = ProviderProfile.model_validate(manifest["profile"])
    settings = load_provider_settings()
    if (settings.active_profile.base_url != profile.base_url
            or settings.active_profile.model != profile.model):
        raise ValueError("Active interface differs from frozen profile")
    api_key = resolve_interface_key(profile, settings)
    profile.require_ready(api_key)
    snapshots = {}
    for item in manifest["experiments"]:
        snapshot = json.loads((directory / item["label"] / "snapshot.json").read_text("utf-8"))
        lab.require_approval(snapshot, item["snapshot_hash"])
        if any(r["request"].get("reasoning_effort") != item["reasoning_effort"] for r in snapshot["requests"]):
            raise ValueError("Request effort does not match matrix")
        snapshots[item["label"]] = snapshot
    manifest.update(status="running", started_at=datetime.now(timezone.utc).isoformat())
    atomic_json(directory / "manifest.json", manifest)
    try:
        for index, item in enumerate(manifest["experiments"], 1):
            snapshot = snapshots[item["label"]]
            run_id = lab.register_run(store, f"Reasoning matrix {manifest['batch_id']} {item['label']}",
                                      snapshot, item["snapshot_hash"])
            item.update(experiment_id=run_id, status="processing", started_at=datetime.now(timezone.utc).isoformat())
            atomic_json(directory / "manifest.json", manifest)
            print(f"[{index}/{len(manifest['experiments'])}] START {item['label']} chunks={item['chunk_count']}", flush=True)
            started = time.perf_counter()

            def progress(fraction: float, message: str) -> None:
                item.update(progress=fraction, message=message)
                atomic_json(directory / "manifest.json", manifest)
                print(f"[{index}/{len(manifest['experiments'])}] {item['label']} {message}", flush=True)

            try:
                lab.execute_run(store, run_id, snapshot, api_key, item["snapshot_hash"], progress=progress)
            except KeyboardInterrupt:
                item["status"] = "interrupted"
                raise
            except Exception:
                item["status"] = "failed"
            finally:
                item["wall_seconds"] = round(time.perf_counter() - started, 3)
                report = store.get_experiment_results(run_id)
                if report:
                    item.update(status=report["status"], summary=summarize(store, run_id))
                item["finished_at"] = datetime.now(timezone.utc).isoformat()
                export_run(store, item, snapshot, directory / item["label"])
                atomic_json(directory / "manifest.json", manifest)
                write_progress(manifest, directory)
                print(f"[{index}/{len(manifest['experiments'])}] END {item['label']} status={item['status']} "
                      f"candidates={item.get('summary', {}).get('candidate_count')} "
                      f"tokens={item.get('summary', {}).get('usage', {}).get('totals')}", flush=True)
        manifest["status"] = "completed" if all(
            item.get("summary", {}).get("complete") for item in manifest["experiments"]
        ) else "completed_with_failures"
    finally:
        if manifest["status"] == "running":
            manifest["status"] = "interrupted"
        manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        atomic_json(directory / "verification.json", verify_preservation(store, before))
        atomic_json(directory / "manifest.json", manifest)
        write_progress(manifest, directory)
        write_review(store, {**manifest, "videos": manifest["experiments"]}, directory)
    return manifest


def main() -> None:
    from filelock import FileLock
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--efforts", choices=["low", "medium", "high"], nargs="+", default=["low", "medium", "high"])
    parser.add_argument("--chunk-seconds", type=int, choices=[300, 600], nargs="+", default=[300, 600])
    parser.add_argument("--prompt-variant", choices=["high_recall", "precision", "low_recall"], default="high_recall")
    args = parser.parse_args()
    workspace = ROOT / "experiments/llm-lab"
    with FileLock(str(workspace / "llm-batch.lock"), timeout=0):
        if args.execute:
            if args.manifest is None:
                parser.error("--execute requires the frozen --manifest")
            result = execute_matrix(args.manifest)
        else:
            result = prepare_matrix(
                workspace, efforts=tuple(args.efforts), chunk_sizes=tuple(args.chunk_seconds),
                prompt_variant=args.prompt_variant,
            )
    print(json.dumps({key: result[key] for key in (
        "batch_id", "status", "directory", "planned_experiments", "planned_extract_requests",
    )}, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
