"""Synthetic evidence tests; no platform or model network requests."""

import copy

import pytest

from classcue.bilibili_evidence import (
    EvidenceAssessment, EvidenceExtraction, select_chunks, verify_unit,
)
from classcue.processing_control import ProcessingCancelled


UNIT = {"bvid": "BV1xx411c7mD", "cid": 101, "page": 1}
TARGET = {"requirements": [{"id": "r1", "text": "配方法", "required": True, "depth": "example"}],
          "keywords": ["配方法"]}


def source(count=3, size=1):
    return {"language": "zh-CN", "automatic": False, "segments": [
        {"id": i + 1, "start_ms": i * 1000, "end_ms": (i + 1) * 1000, "text": "合成字幕" * size}
        for i in range(count)]}


class FakeRequest:
    def __init__(self, *, kind="support", depth="example", same_example=True):
        self.kind, self.depth, self.same_example = kind, depth, same_example
        self.calls = []

    def __call__(self, task, material, schema):
        self.calls.append((schema, copy.deepcopy(material)))
        if schema is EvidenceExtraction:
            return schema(findings=[{"requirement_id": "r1", "depth": self.depth,
                                     "kind": self.kind, "reason": "合成证据",
                                     "segment_ids": [row["id"] for row in material["segments"][:3]]}])
        refs = [row["id"] for row in material["findings"]]
        ids = material["findings"][0]["segment_ids"] if refs else []
        return schema(reason="综合检查合成证据", assessments=[{
            "requirement_id": "r1", "status": "mentioned" if self.kind == "mention" else "supported",
            "depth": self.depth, "reason": "有原文支持", "evidence_ids": refs,
            "same_example": self.same_example, "condition_ids": ids[:1],
            "method_ids": ids[1:2], "step_ids": ids[2:3],
        }])


def test_example_requires_continuous_evidence_and_real_timestamps():
    request = FakeRequest()
    result = verify_unit(TARGET, UNIT, source(), {}, request=request)
    assert result["verdict"] == "covers"
    assert result["missing_requirements"] == []
    assert result["evidence"][1]["start_ms"] == 1000
    assert result["evidence"][1]["url"].endswith("p=1&t=1")
    assert request.calls[-1][0] is EvidenceAssessment


@pytest.mark.parametrize("kind,depth,expected", [
    ("mention", "concept", "unrelated"), ("support", "concept", "partial"),
    ("support", "method", "partial"),
])
def test_term_or_concept_cannot_satisfy_worked_example(kind, depth, expected):
    result = verify_unit(TARGET, UNIT, source(), {}, request=FakeRequest(kind=kind, depth=depth))
    assert result["verdict"] == expected
    assert result["missing_requirements"] == ["r1"]


def test_different_examples_cannot_be_joined():
    result = verify_unit(TARGET, UNIT, source(), {}, request=FakeRequest(same_example=False))
    assert result["verdict"] == "insufficient"
    assert not result["assessments"][0]["satisfied"]


def test_same_example_is_internal_coherence_not_identical_to_original_problem():
    target = {
        "requirements": [{"id": "r1", "text": "演示函数依赖下的属性闭包计算",
                          "required": True, "depth": "example"}],
        "keywords": ["属性闭包"],
        "comparison": {"kind": "exercise", "conditions": ["V→W，W→X，X→Y"],
                       "tasks": ["求V的闭包"], "allowed_variations": ["属性名、依赖和闭包结果可不同"]},
    }
    candidate = {"language": "zh-CN", "automatic": False, "segments": [
        {"id": 1, "start_ms": 0, "end_ms": 1000, "text": "关系中给定S→T、T→Z，求S的闭包。"},
        {"id": 2, "start_ms": 1000, "end_ms": 2000, "text": "从S出发，根据依赖逐步加入T、Z。"},
        {"id": 3, "start_ms": 2000, "end_ms": 3000, "text": "没有新增属性，闭包为STZ，算法停止。"},
    ]}
    fake = FakeRequest()

    def request(task, material, schema):
        if schema is EvidenceExtraction:
            assert "不能仅因这些实例细节不同标为conflict" in task
        else:
            assert "same_example只表示候选视频内部属于同一道例题" in task
            assert "不要求候选复现原题的实例条件、中间步骤或答案" in task
        return fake(task, material, schema)

    result = verify_unit(target, UNIT, candidate, {}, request=request)
    assert result["verdict"] == "covers"
    assert result["assessments"][0]["same_example"]
    assert [row["text"] for row in result["evidence"]] == [row["text"] for row in candidate["segments"]]


@pytest.mark.parametrize("tamper", ["requirement", "segment", "time", "evidence"])
def test_fabricated_references_and_times_are_rejected(tamper):
    fake = FakeRequest()

    def request(task, material, schema):
        result = fake(task, material, schema).model_dump()
        if schema is EvidenceExtraction:
            if tamper == "requirement":
                result["findings"][0]["requirement_id"] = "invented"
            if tamper == "segment":
                result["findings"][0]["segment_ids"] = [9999]
            if tamper == "time":
                result["findings"][0]["start_ms"] = 0
        elif tamper == "evidence":
            result["assessments"][0]["evidence_ids"] = ["invented"]
        return result

    with pytest.raises(ValueError):
        verify_unit(TARGET, UNIT, source(), {}, request=request)


def test_synthesis_cannot_reference_read_rows_outside_selected_findings():
    fake = FakeRequest()

    def request(task, material, schema):
        result = fake(task, material, schema).model_dump()
        if schema is EvidenceAssessment:
            assert "不能从segments中另挑未被这些findings引用的编号" in task
            assert "标为unknown" in task
            assert any(row["id"] == 4 for row in material["segments"])
            result["assessments"][0]["condition_ids"] = [4]
        return result

    with pytest.raises(ValueError, match="例题引用超出了已提取的证据"):
        verify_unit(TARGET, UNIT, source(4), {}, request=request)


def test_synthesis_repairs_invalid_citation_once_through_request_gate():
    fake = FakeRequest()
    calls = []

    def request(task, material, schema):
        result = fake(task, material, schema).model_dump()
        if schema is EvidenceAssessment:
            calls.append(copy.deepcopy(material))
            if len(calls) == 1:
                result["assessments"][0]["condition_ids"] = [4]
            else:
                assert material["validation_error"] == "例题引用超出了已提取的证据"
                assert material["previous_assessment"]["assessments"][0]["condition_ids"] == [4]
        return result

    state = {}
    result = verify_unit(TARGET, UNIT, source(4), state, request=request)
    assert result["verdict"] == "covers"
    assert len(calls) == 2
    assert state["result"] == result


def test_repeated_invalid_citation_never_saves_a_recommendation():
    fake, state, calls = FakeRequest(), {}, []

    def request(task, material, schema):
        result = fake(task, material, schema).model_dump()
        if schema is EvidenceAssessment:
            calls.append(material)
            result["assessments"][0]["step_ids"] = [9999]
        return result

    with pytest.raises(ValueError, match="例题引用超出了已提取的证据"):
        verify_unit(TARGET, UNIT, source(), state, request=request)
    assert len(calls) == 2
    assert state["chunks"]
    assert "result" not in state


def test_synthesis_repair_budget_stop_preserves_extraction():
    fake, state, calls = FakeRequest(), {}, []

    def request(task, material, schema):
        result = fake(task, material, schema).model_dump()
        if schema is EvidenceAssessment:
            calls.append(material)
            if len(calls) > 1:
                raise RuntimeError("合成纠正额度不足")
            result["assessments"][0]["step_ids"] = [9999]
        return result

    with pytest.raises(RuntimeError, match="额度不足"):
        verify_unit(TARGET, UNIT, source(), state, request=request)
    assert state["chunks"]
    assert "result" not in state


def test_time_chunks_reuse_classroom_overlap_without_losing_rows():
    material = source(750)
    chunks = select_chunks(material["segments"])
    assert len(chunks) == 3
    assert {row["id"] for chunk in chunks for row in chunk} == set(range(1, 751))
    assert all(chunk[-1]["end_ms"] - chunk[0]["start_ms"] <= 300000 for chunk in chunks)
    assert len({row["id"] for row in chunks[0]} & {row["id"] for row in chunks[1]}) >= 30


def test_range_evidence_expands_real_rows_without_numeric_id_assumptions():
    material = source(50)
    for index, row in enumerate(material["segments"]):
        row["id"] = 10 + index * 3
    fake = FakeRequest()

    def request(task, payload, schema):
        if schema is EvidenceExtraction:
            return {"findings": [{
                "requirement_id": "r1", "depth": "example", "kind": "support", "reason": "完整例题",
                "start_segment_id": 13, "end_segment_id": 139,
            }]}
        return fake(task, payload, schema)

    result = verify_unit(TARGET, UNIT, material, {}, request=request)
    assert result["verdict"] == "covers"
    assert [row["id"] for row in result["evidence"]] == list(range(13, 140, 3))
    assert result["evidence"][-1]["start_ms"] == 43000


@pytest.mark.parametrize("start,end", [(1, 9999), (3, 1), (None, 2), (1, None)])
def test_invalid_extracted_ranges_are_rejected(start, end):
    def request(task, payload, schema):
        return {"findings": [{
            "requirement_id": "r1", "depth": "example", "kind": "support", "reason": "合成范围",
            "start_segment_id": start, "end_segment_id": end,
        }]}

    state = {}
    with pytest.raises(ValueError):
        verify_unit(TARGET, UNIT, source(), state, request=request)
    assert not state["chunks"]
    assert "result" not in state


def test_range_cannot_reference_another_time_chunk():
    def request(task, payload, schema):
        return {"findings": [{
            "requirement_id": "r1", "depth": "example", "kind": "support", "reason": "跨块范围",
            "start_segment_id": 1, "end_segment_id": 600,
        }]}

    with pytest.raises(ValueError, match="无效的连续字幕范围"):
        verify_unit(TARGET, UNIT, source(650), {}, request=request)


def test_range_cannot_bridge_filtered_out_subtitles(monkeypatch):
    import classcue.bilibili_evidence as module

    material = source()
    monkeypatch.setattr(module, "select_chunks", lambda *_: [
        [material["segments"][0], material["segments"][2]],
    ])

    def request(task, payload, schema):
        return {"findings": [{
            "requirement_id": "r1", "depth": "example", "kind": "support", "reason": "缺失中间句",
            "start_segment_id": 1, "end_segment_id": 3,
        }]}

    with pytest.raises(ValueError, match="跨越了未发送的字幕"):
        verify_unit(TARGET, UNIT, material, {}, request=request)


def test_cancellation_before_citation_repair_sends_no_second_request():
    fake, state, invalid = FakeRequest(), {}, False

    def request(task, material, schema):
        nonlocal invalid
        result = fake(task, material, schema).model_dump()
        if schema is EvidenceAssessment:
            result["assessments"][0]["step_ids"] = [9999]
            invalid = True
        return result

    def checkpoint():
        if invalid:
            raise ProcessingCancelled("合成取消")

    with pytest.raises(ProcessingCancelled):
        verify_unit(TARGET, UNIT, source(), state, request=request, checkpoint=checkpoint)
    assert sum(schema is EvidenceAssessment for schema, _ in fake.calls) == 1
    assert state["chunks"]
    assert "result" not in state


def test_long_subtitles_include_late_target_and_remain_partial():
    material = source(100, 250)
    material["segments"][-1]["text"] += "配方法"
    chunks = select_chunks(material["segments"], TARGET["keywords"])
    selected = {row["id"]: row for chunk in chunks for row in chunk}
    assert 100 in selected
    assert sum(len(row["text"]) for row in selected.values()) <= 60000
    assert len(selected) < 100
    result = verify_unit(TARGET, UNIT, material, {}, request=FakeRequest(kind="mention", depth="concept"))
    assert result["verdict"] == "insufficient"
    assert result["coverage"]["partial"]
    assert result["coverage"]["selected_characters"] <= 60000


def test_cancellation_checkpoints_and_resume_only_missing_requests():
    state, saved, fake = {}, [], FakeRequest()
    material = source(30, 200)

    def checkpoint():
        if len(state.get("chunks", {})) == 1:
            raise ProcessingCancelled("合成取消")

    with pytest.raises(ProcessingCancelled):
        verify_unit(TARGET, UNIT, material, state, request=fake, checkpoint=checkpoint,
                    save_state=lambda value: saved.append(copy.deepcopy(value)))
    assert len(state["chunks"]) == 1
    assert state["coverage"]["partial"]
    assert "result" not in state
    first_material = fake.calls[0][1]
    result = verify_unit(TARGET, UNIT, material, state, request=fake)
    assert result["verdict"] == "covers"
    assert sum(payload == first_material for _, payload in fake.calls) == 1


def test_aggregate_budget_failure_preserves_all_extraction():
    state, fake = {}, FakeRequest()

    def request(task, material, schema):
        if schema is EvidenceAssessment:
            raise RuntimeError("合成预算不足")
        return fake(task, material, schema)

    with pytest.raises(RuntimeError, match="预算"):
        verify_unit(TARGET, UNIT, source(), state, request=request)
    assert state["chunks"]
    assert "result" not in state
    resumed = FakeRequest()
    assert verify_unit(TARGET, UNIT, source(), state, request=resumed)["verdict"] == "covers"
    assert [schema for schema, _ in resumed.calls] == [EvidenceAssessment]


def test_single_video_and_part_use_identical_verification_with_distinct_links():
    a = verify_unit(TARGET, UNIT, source(), {}, request=FakeRequest())
    b = verify_unit(TARGET, {**UNIT, "cid": 102, "page": 3}, source(), {}, request=FakeRequest())
    assert a["assessments"] == b["assessments"]
    assert a["evidence"][0]["url"].endswith("p=1&t=0")
    assert b["evidence"][0]["url"].endswith("p=3&t=0")


def test_source_or_target_change_invalidates_saved_result():
    state, fake = {}, FakeRequest()
    verify_unit(TARGET, UNIT, source(), state, request=fake)
    count = len(fake.calls)
    verify_unit(TARGET, UNIT, source(), state, request=fake)
    assert len(fake.calls) == count
    changed = source()
    changed["segments"][0]["text"] += "修订"
    verify_unit(TARGET, UNIT, changed, state, request=fake)
    assert len(fake.calls) > count


def test_missing_core_requirement_cannot_be_overruled_by_positive_summary():
    target = {"requirements": TARGET["requirements"] + [
        {"id": "r2", "text": "分类讨论", "required": True, "depth": "method"}]}
    result = verify_unit(target, UNIT, source(), {}, request=FakeRequest())
    assert result["verdict"] == "partial"
    assert result["missing_requirements"] == ["r2"]


def test_conflicting_chunk_prevents_positive_aggregate():
    fake = FakeRequest()

    def request(task, material, schema):
        result = fake(task, material, schema).model_dump()
        if schema is EvidenceExtraction:
            result["findings"].append({**result["findings"][0], "kind": "conflict"})
        return result

    result = verify_unit(TARGET, UNIT, source(), {}, request=request)
    assert result["verdict"] == "insufficient"
    assert result["assessments"][0]["status"] == "conflict"


@pytest.mark.parametrize("count,expected", [(30, "covers"), (100, "insufficient")])
def test_cross_chunk_example_requires_all_intervening_context_checked(count, expected):
    fake = FakeRequest()

    def request(task, material, schema):
        result = fake(task, material, schema).model_dump()
        if schema is EvidenceAssessment:
            first, last = material["findings"][0], material["findings"][-1]
            result["assessments"][0].update(condition_ids=first["segment_ids"][:1],
                                             method_ids=last["segment_ids"][:1],
                                             step_ids=last["segment_ids"][-1:])
        return result

    result = verify_unit(TARGET, UNIT, source(count, 250), {}, request=request)
    assert result["verdict"] == expected


def test_cancel_during_request_keeps_completed_extraction():
    state, fake, cancelled = {}, FakeRequest(), False

    def request(task, material, schema):
        nonlocal cancelled
        value = fake(task, material, schema)
        cancelled = True
        return value

    def checkpoint():
        if cancelled:
            raise ProcessingCancelled("合成取消")

    with pytest.raises(ProcessingCancelled):
        verify_unit(TARGET, UNIT, source(), state, request=request, checkpoint=checkpoint)
    assert len(state["chunks"]) == 1
    assert "result" not in state
