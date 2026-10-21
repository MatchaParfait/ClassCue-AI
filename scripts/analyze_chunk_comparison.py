"""Offline comparison of preserved 300-second and completed 600-second B results."""

from __future__ import annotations

from collections import defaultdict
from difflib import SequenceMatcher
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from qwen_resilience import atomic_json


def norm(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def load_report(directory: Path, video_id: int) -> dict:
    return json.loads((directory / f"video-{video_id}" / "report.json").read_text("utf-8"))


def compare_one(baseline: list[dict], variant: list[dict]) -> dict:
    unused = list(enumerate(baseline))
    pairs, added = [], []
    for current in variant:
        exact = [item for item in unused if norm(item[1]["title"]) == norm(current["title"])]
        if not exact:
            scored = []
            for item in unused:
                previous = item[1]
                title_similarity = SequenceMatcher(None, norm(previous["title"]), norm(current["title"])).ratio()
                left = max(previous["start_sentence_id"], current["start_sentence_id"])
                right = min(previous["end_sentence_id"], current["end_sentence_id"])
                intersection = max(0, right - left + 1)
                union = max(previous["end_sentence_id"], current["end_sentence_id"]) - min(previous["start_sentence_id"], current["start_sentence_id"]) + 1
                overlap = intersection / union
                distance = max(0, max(previous["start_sentence_id"], current["start_sentence_id"]) - min(previous["end_sentence_id"], current["end_sentence_id"]))
                score = title_similarity + 0.7 * overlap
                scored.append((score, title_similarity, overlap, distance, item))
            scored.sort(reverse=True, key=lambda value: value[0])
            if scored and (scored[0][1] >= 0.45 and scored[0][2] >= 0.15 or scored[0][1] >= 0.72 or scored[0][3] <= 4 and scored[0][1] >= 0.35):
                exact = [scored[0][4]]
        if not exact:
            added.append(current)
            continue
        index, previous = exact[0]
        unused.remove((index, previous))
        start_delta = current["start_sentence_id"] - previous["start_sentence_id"]
        end_delta = current["end_sentence_id"] - previous["end_sentence_id"]
        pairs.append({
            "title_300": previous["title"], "title_600": current["title"],
            "ids_300": [previous["start_sentence_id"], previous["end_sentence_id"]],
            "ids_600": [current["start_sentence_id"], current["end_sentence_id"]],
            "delta": [start_delta, end_delta],
            "classification": "almost_unchanged" if max(abs(start_delta), abs(end_delta)) <= 3 else "location_changed",
            "quote_300": previous["evidence_quote"], "quote_600": current["evidence_quote"],
        })
    return {"matched": pairs, "added_in_600": added,
            "missing_in_600": [item for _, item in unused]}


def main() -> None:
    root = ROOT / "experiments" / "llm-lab"
    comparison = sorted((root / "comparisons").glob("chunks-300-vs-600-*/comparison.json"))[-1]
    meta = json.loads(comparison.read_text("utf-8"))
    variant = json.loads((Path(meta["variant_manifest"]).read_text("utf-8")))
    baseline = json.loads((comparison.parent / "baseline-300.json").read_text("utf-8"))
    baseline_by_video = {item["video_id"]: item["report"]["results"] for item in baseline}
    variant_by_video = {}
    for item in variant["videos"]:
        if item.get("status") == "completed":
            variant_by_video[item["video_id"]] = load_report(Path(variant["directory"]), item["video_id"])["results"]
    report = {"comparison": str(comparison), "tolerance_merged_segments": 3,
              "videos": {}, "aggregate": {"completed": len(variant_by_video),
              "almost_unchanged": 0, "location_changed": 0, "added": 0, "missing": 0}}
    for video_id, current in sorted(variant_by_video.items()):
        result = compare_one(baseline_by_video[video_id], current)
        report["videos"][str(video_id)] = result
        report["aggregate"]["almost_unchanged"] += sum(x["classification"] == "almost_unchanged" for x in result["matched"])
        report["aggregate"]["location_changed"] += sum(x["classification"] == "location_changed" for x in result["matched"])
        report["aggregate"]["added"] += len(result["added_in_600"])
        report["aggregate"]["missing"] += len(result["missing_in_600"])
    output = comparison.parent / "location-comparison.json"
    atomic_json(output, report)
    lines = ["# 5 分钟与 10 分钟分片对照", "", "仅比较 10 分钟批次成功完成的视频。标题相同且起止聚合编号最大偏移不超过 3，记为几乎没变化。", "",
             f"完成视频：{report['aggregate']['completed']}；几乎没变化：{report['aggregate']['almost_unchanged']}；明显定位变化：{report['aggregate']['location_changed']}；10 分钟新增：{report['aggregate']['added']}；10 分钟未出现：{report['aggregate']['missing']}", ""]
    for video_id, result in report["videos"].items():
        lines += [f"## 视频 {video_id}", ""]
        for item in result["matched"]:
            if item["classification"] == "location_changed":
                lines += [f"- **定位变化**：{item['title_600']}；5 分钟 {item['ids_300']} -> 10 分钟 {item['ids_600']}，偏移 {item['delta']}。"]
        for item in result["added_in_600"]:
            lines += [f"- **10 分钟新增**：{item['title']}（{item['start_sentence_id']}-{item['end_sentence_id']}）。"]
        for item in result["missing_in_600"]:
            lines += [f"- **10 分钟未出现**：{item['title']}（5 分钟 {item['start_sentence_id']}-{item['end_sentence_id']}）。"]
        if not result["added_in_600"] and not result["missing_in_600"] and not any(x["classification"] == "location_changed" for x in result["matched"]):
            lines.append("- 没有明显变化，候选均为相同或附近定位。")
        lines.append("")
    (comparison.parent / "location-comparison.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"json": str(output), "markdown": str(comparison.parent / 'location-comparison.md'), **report["aggregate"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
