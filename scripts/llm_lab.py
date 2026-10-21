"""Preview classroom LLM prompts; execution requires an explicitly approved snapshot."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "experiments" / "llm-lab" / "project.db")
    parser.add_argument("--video-id", type=int, default=1)
    parser.add_argument("--scheme", choices=["rules", "few_shot", "context_review"], default="rules")
    parser.add_argument(
        "--prompt-variant", choices=["high_recall", "precision", "low_recall"], default="high_recall",
    )
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--reasoning-effort", choices=["low", "medium", "high"])
    parser.add_argument("--timeout-seconds", type=float)
    parser.add_argument("--chunk-seconds", type=int, default=300)
    parser.add_argument("--overlap-seconds", type=int, default=30)
    parser.add_argument("--max-format-repairs", type=int, choices=range(6), default=1)
    parser.add_argument("--max-evidence-repairs", type=int, choices=range(6), default=2)
    parser.add_argument("--merge-min-chars", type=int, default=20)
    parser.add_argument("--merge-max-gap-ms", type=int, default=2000)
    parser.add_argument("--merge-max-span-seconds", type=int, default=30)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--approved-snapshot-hash")
    parser.add_argument("--name")
    args = parser.parse_args()

    from ai_provider import load_provider_settings, resolve_interface_key, ProviderProfile
    from core import TranscriptSegment
    from llm_experiments import (
        LabConfig, execute_run, load_prompt_bundle, numbered_transcript, prepare_snapshot, register_run,
        render_examples, require_approval,
    )
    from qwen_resilience import atomic_json
    from video_store import VideoStore

    if not args.db.is_file():
        parser.error("Saved ASR database does not exist; this command will not run ASR.")
    store = VideoStore(args.db)
    settings = load_provider_settings()
    if args.execute:
        if args.snapshot is None or not args.approved_snapshot_hash:
            parser.error("--execute requires --snapshot and --approved-snapshot-hash after human review")
        snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
        require_approval(snapshot, args.approved_snapshot_hash)
        profile = ProviderProfile.model_validate(snapshot["profile"])
        key = resolve_interface_key(profile, settings)
        profile.require_ready(key)
        run_id = register_run(
            store, args.name or snapshot["config"]["scheme"], snapshot, args.approved_snapshot_hash,
        )
        try:
            report = execute_run(
                store, run_id, snapshot, key, args.approved_snapshot_hash,
                progress=lambda fraction, message: print(f"{fraction:.3f} {message}", flush=True),
            )
        finally:
            export = args.output or args.db.parent / "runs" / run_id
            export.mkdir(parents=True, exist_ok=True)
            atomic_json(export / "snapshot.json", snapshot)
            atomic_json(export / "calls.json", store.list_experiment_calls(run_id))
            atomic_json(export / "steps.json", store.list_experiment_steps(run_id))
            atomic_json(export / "report.json", store.get_experiment_results(run_id))
            atomic_json(export / "status.json", dict(store.get_experiment(run_id)))
        print(json.dumps(
            {"experiment_id": run_id, "candidate_count": report["candidate_count"],
             "call_count": report["call_count"], "format_metrics": report["format_metrics"],
             "export_directory": str(export)},
            ensure_ascii=False, indent=2,
        ))
        return

    config = LabConfig(
        scheme=args.scheme, max_tokens=args.max_tokens, chunk_seconds=args.chunk_seconds,
        overlap_seconds=args.overlap_seconds,
        max_format_repairs=args.max_format_repairs,
        max_evidence_repairs=args.max_evidence_repairs,
        reasoning_effort=args.reasoning_effort,
        merge_min_chars=args.merge_min_chars, merge_max_gap_ms=args.merge_max_gap_ms,
        merge_max_span_seconds=args.merge_max_span_seconds,
    )
    profile = settings.active_profile
    if args.timeout_seconds is not None:
        profile = ProviderProfile.model_validate({
            **profile.model_dump(), "timeout_seconds": args.timeout_seconds,
        })
    snapshot = prepare_snapshot(
        store, args.video_id, config, profile, bundle=load_prompt_bundle(variant=args.prompt_variant),
    )
    export = args.output or args.db.parent / "prompt-review" / (
        f"{config.scheme}-{snapshot['snapshot_hash'][:12]}"
    )
    export.mkdir(parents=True, exist_ok=True)
    atomic_json(export / "snapshot.json", snapshot)
    atomic_json(export / "requests.json", snapshot["requests"])
    atomic_json(export / "input_segments.json", snapshot["input_segments"])
    supplied_ids = {i for request in snapshot["requests"] for i in request["sentence_ids"]}
    input_text = numbered_transcript([
        TranscriptSegment(segment["id"], segment["start_ms"], segment["end_ms"], segment["text"])
        for segment in snapshot["input_segments"] if segment["id"] in supplied_ids
    ])
    (export / "transcript.txt").write_text(input_text + "\n", encoding="utf-8")
    review = [
        f"# {config.scheme} 提示词预览",
        "",
        "仅离线生成；未创建模型客户端、未发送任何请求。",
        f"提示词版本：{args.prompt_variant}；与 A/B/C 交互方案独立选择。",
        f"快照：`{snapshot['snapshot_hash']}`",
        f"ASR：`{snapshot['transcript_hash']}`",
        f"第一阶段请求数：{len(snapshot['requests'])}",
        "context_review 的二次请求依赖实际候选，模板已冻结在 snapshot.json 中。",
        f"实验输出预算：{config.max_tokens}；不修改全局 LLM 设置。",
        f"显式思考档位：{config.reasoning_effort or '未指定，使用服务端默认值'}。",
        f"单次请求等待上限：{snapshot['profile']['timeout_seconds']} 秒；不修改全局接口配置。",
        f"每个提取/审核步骤最多格式修正 {config.max_format_repairs} 次；记录首轮和最终格式合格情况。",
        f"证据先由后端匹配；仍不合规时每个候选最多打回模型 {config.max_evidence_repairs} 次，均留档。",
        f"原 ASR 段数：{snapshot['input_stats']['source_segment_count']}；"
        f"可用聚合段数：{snapshot['input_stats']['eligible_input_segment_count']}。",
        f"聚合目标：{config.merge_min_chars} 个非空白字符（0 为关闭）；"
        f"最长间隔 {config.merge_max_gap_ms} 毫秒，最长跨度 {config.merge_max_span_seconds} 秒。",
        "原始 ASR 不变；聚合段全视频从 1 连续编号，分块、审核和修正不重新编号。",
        "模型只收到 {编号} 原文，不收到时间或原片段编号；后端通过 input_segments.json 恢复精确时间。",
        "transcript.txt 是首阶段实际使用字幕的编号文本汇总；分块后的完整请求在 requests.json。",
        "",
        "## 系统提示词",
        "",
        snapshot["prompt_bundle"]["system"],
        "",
        "## 考点规则与输出结构",
        "",
        snapshot["prompt_bundle"]["rules"],
    ]
    if config.scheme == "few_shot":
        review += [
            "", "## 合成正反例", "",
            render_examples(snapshot["prompt_bundle"]["examples"]),
        ]
    if config.scheme == "context_review":
        review += ["", "## 二次审核模板", "", snapshot["prompt_bundle"]["review"]]
    review += [
        "", "## 格式修复模板", "", snapshot["prompt_bundle"]["repair"],
        "", "## 引用修复模板", "", snapshot["prompt_bundle"]["evidence_repair"],
        "", "## 字幕后任务提醒", "", snapshot["prompt_bundle"]["task"],
        "", "## 第一块实际发送的完整消息", "",
    ]
    for message in snapshot["requests"][0]["request"]["messages"]:
        review += [
            f"### {message['role']}", "",
            "`````text", message["content"], "`````", "",
        ]
    review += [
        "",
        "## 第一块的实际请求",
        "",
        "```json",
        json.dumps(snapshot["requests"][0], ensure_ascii=False, indent=2),
        "```",
    ]
    (export / "review.md").write_text("\n".join(review) + "\n", encoding="utf-8")
    print(json.dumps(
        {"mode": "preview_only", "llm_requests": 0, "scheme": config.scheme,
         "prompt_variant": args.prompt_variant,
         "video_id": args.video_id, "chunk_count": len(snapshot["requests"]),
         "input_stats": snapshot["input_stats"],
         "snapshot_hash": snapshot["snapshot_hash"], "transcript_hash": snapshot["transcript_hash"],
         "export_directory": str(export)},
        ensure_ascii=False, indent=2,
    ))


if __name__ == "__main__":
    main()
