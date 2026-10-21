from datetime import datetime, timedelta, timezone

import pytest

from qwen_resilience import atomic_json
from scripts.analyze_reasoning_matrix import analyze


@pytest.mark.parametrize("multi_group", [False, True])
def test_semantic_alignment_handles_rewording_nearby_jumps_and_splits(tmp_path, multi_group):
    experiments, groups, assignments = [], [], {}
    sequence = 0
    for video_id in (5, 11):
        group = {"id": f"v{video_id}", "video_id": video_id, "name": "同一原文知识主题"}
        groups.append(group)
        if multi_group:
            groups.append({"id": f"v{video_id}-other", "video_id": video_id, "name": "合并候选的另一对象"})
        for effort in ("low", "medium", "high"):
            for seconds in (300, 600):
                label = f"video-{video_id}-{effort}-{seconds}"
                count = 2 if seconds == 600 else 1
                candidates = [{
                    "title": f"不同标题 {label} {i}", "reason": "教师强调",
                    "evidence_quote": "必须掌握", "signal_type": "emphasis",
                    "start_ms": 10000 + (29000 if seconds == 600 else 0) + i * 1000,
                    "end_ms": 50000 + i * 1000, "start_sentence_id": 1, "end_sentence_id": 2,
                } for i in range(count)]
                atomic_json(tmp_path / label / "snapshot.json", {
                    "input_segments": [{"id": 1, "text": "必须掌握", "start_ms": 0, "end_ms": 60000}],
                })
                atomic_json(tmp_path / label / "report.json", {"complete": True, "results": candidates})
                atomic_json(tmp_path / label / "steps.json", [])
                started = datetime(2026, 10, 13, tzinfo=timezone.utc) + timedelta(seconds=sequence * 2)
                sequence += 1
                usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
                atomic_json(tmp_path / label / "calls.json", [{
                    "call_id": label, "request": {"reasoning_effort": effort},
                    "returned_model": "alias", "usage": usage, "response": {"usage": usage},
                    "status": "completed", "started_at": started.isoformat(),
                    "finished_at": (started + timedelta(seconds=1)).isoformat(),
                }])
                experiments.append({
                    "label": label, "video_id": video_id, "reasoning_effort": effort,
                    "chunk_seconds": seconds, "wall_seconds": 1,
                    "summary": {"complete": True, "candidate_count": count, "call_count": 1,
                                "citation_metrics": {"unresolved_count": 0},
                                "usage": {"totals": usage}},
                })
                for index in range(1, count + 1):
                    assignments[f"{label}/c{index:03d}"] = (
                        [group["id"], f"v{video_id}-other"] if multi_group else group["id"]
                    )
    atomic_json(tmp_path / "manifest.json", {
        "directory": str(tmp_path), "batch_id": "test", "experiments": experiments,
    })
    atomic_json(tmp_path / "semantic-alignment.json", {
        "method": "Source-backed manual topic alignment", "groups": groups, "assignments": assignments,
    })
    result = analyze(tmp_path)
    assert result["audit"]["recorded_peak_concurrency"] == 1
    assert result["audit"]["actual_api_calls"] == 12
    assert result["audit"]["request_effort_mismatches"] == []
    assert result["audit"]["raw_usage_mismatches"] == []
    assert result["unresolved_citations"] == []
    for pair in result["comparisons"]:
        assert pair["candidate_counts"] == [1, 2]
        assert pair["concept_counts"] == ([2, 2] if multi_group else [1, 1])
        assert pair["only_300"] == pair["only_600"] == []
        assert pair["positions"][0]["same_neighborhood"]
        assert pair["positions"][0]["start_delta_ms"] == 29000
    for totals in result["aggregate_by_effort"]:
        assert totals["candidate_counts"] == [2, 4]
        assert totals["total_tokens_300"] == totals["total_tokens_600"] == 240
    text = (tmp_path / "comparison.md").read_text("utf-8")
    assert "几乎没变化" in text and "拆分或重复差异" in text
