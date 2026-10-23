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
