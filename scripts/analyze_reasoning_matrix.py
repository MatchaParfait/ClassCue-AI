"""Offline audit and semantic comparisons of a completed reasoning matrix."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import format_time
from qwen_resilience import atomic_json
from scripts.llm_lab_batch import link


def candidate_index(manifest: dict) -> list[dict]:
    directory = Path(manifest["directory"])
    result = []
    for item in manifest["experiments"]:
        folder = directory / item["label"]
        if not (folder / "report.json").is_file():
            continue
        report = json.loads((folder / "report.json").read_text("utf-8"))
        if not report["complete"]:
            continue
        snapshot = json.loads((folder / "snapshot.json").read_text("utf-8"))
        segments = snapshot["input_segments"]
        for index, candidate in enumerate(report["results"], 1):
            context = [s for s in segments if (
                candidate["start_sentence_id"] - 2 <= s["id"] <= candidate["end_sentence_id"] + 2
            )]
            result.append({
                "key": f"{item['label']}/c{index:03d}", "label": item["label"],
                "video_id": item["video_id"], "effort": item["reasoning_effort"],
                "chunk_seconds": item["chunk_seconds"],
                "candidate": candidate, "nearby_transcript": context,
            })
    return result


def audit_calls(manifest: dict) -> dict:
    events = []
    mismatch = []
    usage_mismatch = []
    returned = set()
    reasoning_counts = {}
    all_calls = []
    for item in manifest["experiments"]:
        path = Path(manifest["directory"]) / item["label"] / "calls.json"
        if not path.is_file():
            continue
        calls = json.loads(path.read_text("utf-8"))
        all_calls.extend(calls)
        reasoning_counts[item["label"]] = sum(
            isinstance(((c.get("usage") or {}).get("completion_tokens_details") or {}).get("reasoning_tokens"), int)
            for c in calls
        )
        for call in calls:
            if call["request"].get("reasoning_effort") != item["reasoning_effort"]:
                mismatch.append(call["call_id"])
            if call["usage"] != (call.get("response") or {}).get("usage"):
                usage_mismatch.append(call["call_id"])
            if call.get("returned_model"):
                returned.add(call["returned_model"])
            events.append((datetime.fromisoformat(call["started_at"]), 1))
            if call.get("finished_at"):
                events.append((datetime.fromisoformat(call["finished_at"]), -1))
    active = peak = 0
    for _, change in sorted(events, key=lambda event: (event[0], event[1])):
        active += change
        peak = max(peak, active)
    return {
        "actual_api_calls": len(all_calls),
        "usage_reported_calls": sum(c.get("usage") is not None for c in all_calls),
        "missing_usage_calls": sum(c.get("usage") is None for c in all_calls),
        "call_status_counts": {status: sum(c["status"] == status for c in all_calls)
                               for status in sorted({c["status"] for c in all_calls})},
        "recorded_peak_concurrency": peak, "request_effort_mismatches": mismatch,
        "raw_usage_mismatches": usage_mismatch, "returned_models": sorted(returned),
        "reasoning_detail_call_counts": reasoning_counts,
        "tokens": {
            name: sum((c.get("usage") or {}).get(name, 0) or 0 for c in all_calls)
            for name in ("prompt_tokens", "completion_tokens", "total_tokens")
        },
        "scope": "API-reported usage; relay effort execution and billing not independently verified",
    }


def analyze(directory: Path, *, index_only: bool = False) -> dict:
    manifest = json.loads((directory / "manifest.json").read_text("utf-8"))
    index = candidate_index(manifest)
    atomic_json(directory / "candidate-index.json", index)
    listing = ["# 各实验候选索引", ""]
    for item in index:
        c = item["candidate"]
        listing += [
            f"## {item['key']} / {c['title']}", "",
            f"视频 {item['video_id']}，{item['effort']}，{item['chunk_seconds'] // 60}min；"
            f"{format_time(c['start_ms'])}–{format_time(c['end_ms'])}；"
            f"聚合编号 {c['start_sentence_id']}–{c['end_sentence_id']}；{c['signal_type']}。",
            f"理由：{c['reason']}", f"引文：{c['evidence_quote']}", "",
        ]
    (directory / "candidate-index.md").write_text("\n".join(listing), encoding="utf-8")
    if index_only:
        return {"indexed_candidates": len(index)}
    alignment = json.loads((directory / "semantic-alignment.json").read_text("utf-8"))
    assignments = alignment["assignments"]
    groups = {group["id"]: group for group in alignment["groups"]}
    if set(assignments) != {item["key"] for item in index}:
        raise ValueError("Semantic alignment must cover every completed candidate exactly once")
    by_slot = {}
    for item in index:
        assigned = assignments[item["key"]]
        assigned = [assigned] if isinstance(assigned, str) else assigned
        if not assigned or len(assigned) != len(set(assigned)):
            raise ValueError("Empty or duplicated semantic assignments")
        for group_id in assigned:
            if group_id not in groups or groups[group_id]["video_id"] != item["video_id"]:
                raise ValueError("Invalid semantic group/video")
            by_slot.setdefault(item["label"], {}).setdefault(group_id, []).append(item)
    comparisons = []
    experiments = {item["label"]: item for item in manifest["experiments"]}
    for video_id in (5, 11):
        for effort in ("low", "medium", "high"):
            labels = [f"video-{video_id}-{effort}-{seconds}" for seconds in (300, 600)]
            if not all(experiments[label].get("summary", {}).get("complete") for label in labels):
                comparisons.append({"video_id": video_id, "effort": effort, "comparable": False})
                continue
            left, right = [by_slot.get(label, {}) for label in labels]
            common = sorted(left.keys() & right.keys())
            positions = []
            for group_id in common:
                starts = [[item["candidate"]["start_ms"] for item in side[group_id]] for side in (left, right)]
                first_delta = min(starts[1]) - min(starts[0])
                closest_delta = min(abs(b - a) for a in starts[0] for b in starts[1])
                positions.append({
                    "group_id": group_id, "start_delta_ms": first_delta,
                    "nearest_start_distance_ms": closest_delta,
                    "same_neighborhood": abs(first_delta) <= 60000,
                    "count_300": len(left[group_id]), "count_600": len(right[group_id]),
                    "ranges_300_ms": [[item["candidate"]["start_ms"], item["candidate"]["end_ms"]]
                                      for item in left[group_id]],
                    "ranges_600_ms": [[item["candidate"]["start_ms"], item["candidate"]["end_ms"]]
                                      for item in right[group_id]],
                    "candidates_300": [item["key"] for item in left[group_id]],
                    "candidates_600": [item["key"] for item in right[group_id]],
                })
            a, b = [experiments[label] for label in labels]
            comparisons.append({
                "video_id": video_id, "effort": effort, "comparable": True,
                "candidate_counts": [a["summary"]["candidate_count"], b["summary"]["candidate_count"]],
                "concept_counts": [len(left), len(right)], "shared": common,
                "only_300": sorted(left.keys() - right.keys()), "only_600": sorted(right.keys() - left.keys()),
                "positions": positions,
                "total_token_change_percent": round(
                    (b["summary"]["usage"]["totals"]["total_tokens"] /
                     a["summary"]["usage"]["totals"]["total_tokens"] - 1) * 100, 2),
                "wall_time_change_percent": round((b["wall_seconds"] / a["wall_seconds"] - 1) * 100, 2),
            })
    audit = audit_calls(manifest)
    aggregate = []
    for effort in ("low", "medium", "high"):
        slots = [[item for item in manifest["experiments"]
                  if item["reasoning_effort"] == effort and item["chunk_seconds"] == seconds]
                 for seconds in (300, 600)]
        totals = [sum(item.get("summary", {}).get("usage", {}).get("totals", {}).get("total_tokens", 0)
                      for item in items) for items in slots]
        durations = [sum(item.get("wall_seconds", 0) for item in items) for items in slots]
        aggregate.append({
            "effort": effort, "total_tokens_300": totals[0], "total_tokens_600": totals[1],
            "token_saving_percent": round((1 - totals[1] / totals[0]) * 100, 2) if totals[0] else None,
            "wall_seconds_300": round(durations[0], 3), "wall_seconds_600": round(durations[1], 3),
            "time_saving_percent": round((1 - durations[1] / durations[0]) * 100, 2) if durations[0] else None,
            "candidate_counts": [sum(item.get("summary", {}).get("candidate_count") or 0 for item in items)
                                 for items in slots],
        })
    result = {
        "batch_id": manifest["batch_id"], "audit": audit, "comparisons": comparisons,
        "aggregate_by_effort": aggregate,
        "alignment_method": alignment["method"], "location_tolerance_ms": 60000,
        "location_note": "Same source-backed concept and first jump within 60 seconds = same neighborhood; semantic groups are manually reviewed, no LLM calls",
    }
    atomic_json(directory / "comparison.json", result)
    lines = [
        "# 两视频推理档位与分片实验", "",
        "方案 B，视频 5 和 11，low/medium/high，各测 5/10 分钟，共 12 个完整视频实验。",
        "请求并发 1；每个提取、格式修正、引用修正请求均显式发送相同 reasoning_effort。",
        "统一输出上限 32768、超时 300 秒；30 秒重叠，原 ASR 与 20 字聚合不变。",
        "本轮每格只运行一次，没有人工标准答案，候选数不是准确率。中转接口接受档位并不证明底层执行。",
        "token 均取自原始响应 usage；推理用量缺失显示未知，不当作零。实际收费未独立核验。", "",
        "| 视频 | 档位 | 分片 | 通过候选 | 未解决 | 输入 | 输出含推理 | 已报推理 | 总 token | 耗时秒 | 调用 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in manifest["experiments"]:
        s = item.get("summary", {})
        totals = s.get("usage", {}).get("totals", {})
        lines.append(
            f"| {item['video_id']} | {item['reasoning_effort']} | {item['chunk_seconds']//60}min | "
            f"{s.get('candidate_count')} | {s.get('citation_metrics', {}).get('unresolved_count')} | "
            f"{totals.get('prompt_tokens')} | {totals.get('completion_tokens')} | "
            f"{s.get('reasoning_tokens_reported') if s.get('reasoning_tokens_reported') is not None else '未知'} | "
            f"{totals.get('total_tokens')} | {item.get('wall_seconds')} | {s.get('call_count')} |"
        )
    lines += [
        "",
        "视频 11 / low / 10min 的 0 条表示 0 条通过校验，另有 1 条“排序与搜索”引用修正未解决；不表示未识别到考点。",
    ]
    lines += [
        "", "## 两视频合计", "",
        "| 档位 | 5min 总 token | 10min 总 token | 10min 节省 | 5min 耗时秒 | 10min 耗时秒 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for totals in aggregate:
        lines.append(
            f"| {totals['effort']} | {totals['total_tokens_300']} | {totals['total_tokens_600']} | "
            f"{totals['token_saving_percent']}% | {totals['wall_seconds_300']} | {totals['wall_seconds_600']} |"
        )
    lines += [
        "", "耗时为单次实验实际墙钟时间，包含网关等待、有限网络重试、后端校验及引用打回；不能归因于模型计算速度本身。",
        "每实验的 extract_reported_tokens 在 manifest 中单独保存，便于区分提取成本与引用修正开销。",
    ]
    lines += [
        "", "## 同档位 5/10 分钟对照", "",
        "先按实际原文把同一知识对象归组，允许标题改写、拆分和重复，不把改名直接算新增。",
        "一个合并候选明确覆盖多个对象时可同时对应多组（例如“排序与搜索”），原始候选数量仍只计一次。",
        "定位在同一知识讲述附近、首个跳转相差不超过约一分钟时记为几乎没变化；范围变宽和拆分另述。",
        "仅某方案出现只说明本次提取差异，不自动判断正误。", "",
    ]
    for pair in comparisons:
        lines += [f"### 视频 {pair['video_id']} / {pair['effort']}", ""]
        if not pair["comparable"]:
            lines += ["至少一个实验未完成，不能把失败当零候选。", ""]
            continue
        lines += [
            f"原始候选 {pair['candidate_counts'][0]} → {pair['candidate_counts'][1]}；"
            f"语义概念组 {pair['concept_counts'][0]} → {pair['concept_counts'][1]}；共同 {len(pair['shared'])} 组。",
            f"10min 总 token 变化 {pair['total_token_change_percent']}%；耗时变化 {pair['wall_time_change_percent']}%。",
            "仅 5min：" + ("；".join(groups[key]["name"] for key in pair["only_300"]) or "无"),
            "仅 10min：" + ("；".join(groups[key]["name"] for key in pair["only_600"]) or "无"),
        ]
        if pair["video_id"] == 11 and pair["effort"] == "low":
            lines.append("注意：这里只比较通过校验的候选；10min 曾返回排序与搜索，但引用未修复，不能把上面的差异解读为未识别。")
        for position in pair["positions"]:
            name = groups[position["group_id"]]["name"]
            note = "几乎没变化" if position["same_neighborhood"] else "首个跳转位置变化较大，需核对原文"
            count_note = ""
            if position["count_300"] != position["count_600"]:
                count_note = f"；该组候选条数 {position['count_300']} → {position['count_600']}，存在拆分或重复差异"
            lines.append(f"- {name}：{note}，首个跳转偏移 {position['start_delta_ms']/1000:.1f} 秒{count_note}。")
            ranges = ["；".join(f"{format_time(a)}–{format_time(b)}" for a, b in position[key])
                      for key in ("ranges_300_ms", "ranges_600_ms")]
            lines.append(f"  5min 范围 {ranges[0]}；10min 范围 {ranges[1]}。")
        lines.append("")
    lines += ["## 原文审读备注", ""]
    for observation in alignment.get("observations", []):
        lines += [f"- {observation['text']}"]
    lines.append("")
    lines += ["## 概念覆盖", ""]
    for video_id in (5, 11):
        lines += [f"### 视频 {video_id}", "",
                  "| 来源原文知识对象 | low 5 | low 10 | medium 5 | medium 10 | high 5 | high 10 |",
                  "|---|---:|---:|---:|---:|---:|---:|"]
        for group in alignment["groups"]:
            if group["video_id"] != video_id:
                continue
            counts = [str(len(by_slot.get(f"video-{video_id}-{effort}-{seconds}", {}).get(group["id"], [])))
                      for effort in ("low", "medium", "high") for seconds in (300, 600)]
            lines.append("| " + group["name"] + " | " + " | ".join(counts) + " |")
        lines.append("")
    lines += ["## 未解决引用", ""]
    unresolved = []
    for item in manifest["experiments"]:
        steps_path = directory / item["label"] / "steps.json"
        if not steps_path.is_file():
            continue
        steps = json.loads(steps_path.read_text("utf-8"))
        for step in steps:
            if step["stage"] not in ("extract", "review"):
                continue
            for rejection in step.get("rejected", []):
                unresolved.append({"label": item["label"], "step_id": step["step_id"], **rejection})
                c = rejection["candidate"]
                child = next((s for s in steps if s["step_id"].startswith(step["step_id"] + "-evidence-")), {})
                lines += [
                    f"- {item['label']} / {c['title']}：{rejection['reason']}；"
                    f"修正状态 {child.get('status', '未修正')}；{child.get('error', '')}。",
                ]
    if not unresolved:
        lines.append("没有最终未解决引用。")
    result["unresolved_citations"] = unresolved
    atomic_json(directory / "comparison.json", result)
    lines += [
        "## 日志与核验", "",
        f"实际调用 {audit['actual_api_calls']}；有 usage {audit['usage_reported_calls']}；缺失 {audit['missing_usage_calls']}。",
        f"时间戳核验峰值并发 {audit['recorded_peak_concurrency']}；档位字段不匹配 {len(audit['request_effort_mismatches'])}；"
        f"汇总 usage 与原始响应不一致 {len(audit['raw_usage_mismatches'])}。",
        f"[批次及全部实验]({link(directory / 'manifest.json')}) / "
        f"[候选与原文索引]({link(directory / 'candidate-index.md')}) / "
        f"[候选附近字幕 JSON]({link(directory / 'candidate-index.json')}) / "
        f"[历史数据保留]({link(directory / 'verification.json')})",
        "",
    ]
    (directory / "comparison.md").write_text("\n".join(lines), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--index-only", action="store_true")
    args = parser.parse_args()
    result = analyze(args.directory, index_only=args.index_only)
    print(json.dumps(result if args.index_only else result["audit"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
