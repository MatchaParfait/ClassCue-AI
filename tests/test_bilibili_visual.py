"""Synthetic localization, visual evidence, budgeting and resume tests."""

from copy import deepcopy

import pytest

from classcue.bilibili import BilibiliError
from classcue.bilibili_harness import HarnessService, LIMITS
from classcue.bilibili_visual import (
    VisualAssessment, assess_frames, finalize_light, finalize_visual, initial_times, locate_unit, nearby_segments,
)
from classcue.processing_control import ProcessingCancelled
from classcue.question_analysis import QuestionImage
from tests.bilibili_visual_fakes import FakeFrameReader, MemoryFrames, assessment, light_assessment, localization
from tests.test_bilibili_harness import BV_A, COURSE, EXACT, FakeClient, FakeModel, FakeStore, run_state


UNIT = {"id": BV_A + ":101", "bvid": BV_A, "cid": 101, "page": 1, "part": "合成题型"}
TARGET = {"requirements": [{"id": "r1", "text": "演示配方法", "required": True, "depth": "example"}],
          "keywords": ["配方法"], "comparison": {"kind": "exercise", "conditions": ["合成原题"], "tasks": ["求解"]}}


def source(count=8):
    return {"language": "zh-CN", "automatic": False, "segments": [
        {"id": i * 3 + 7, "start_ms": i * 1000, "end_ms": (i + 1) * 1000, "text": "合成运算步骤"}
        for i in range(count)]}


class VisionModel(FakeModel):
    def __init__(self, *, status="yes", followup=False):
        super().__init__()
        self.status, self.followup, self.vision_calls = status, followup, []

    def ask(self, task, material, schema):
        if schema.__name__ == "SubtitleLocalization":
            self.calls.append((schema.__name__, deepcopy(material)))
            assert "不要求条件、定义和步骤全部口述" in task or "题干未念出也可以入选" in task
            return schema.model_validate(localization(material))
        return super().ask(task, material, schema)

    def ask_with_images(self, task, material, schema, images):
        self.vision_calls.append(deepcopy(material))
        assert len(images) == len(material["frames"]) + len(material["original_images"]) and all(image.data for image in images)
        followup = [material["segments"][-1]["id"]] if self.followup else []
        build = light_assessment if schema.__name__ == "LightVisualAssessment" else assessment
        return schema.model_validate(build(material, status=self.status, followup=followup))


def execute_visual(*, run=None, model=None, reader=None, frames=None, store=None, should_cancel=lambda: False,
                   original_images=()):
    run = run or run_state()
    run["input"]["marker"]["id"] = 42
    run["visual_verification"] = True
    model, reader, frames, store = model or VisionModel(), reader or FakeFrameReader(), frames or MemoryFrames(), store or FakeStore()
    service = HarnessService(store, model, FakeClient({EXACT: [BV_A], COURSE: [BV_A]}),
                             should_cancel=should_cancel, frame_reader=reader, frame_store=frames,
                             original_images=original_images)
    service.search(run)
    return run, model, reader, frames, store


def test_likely_example_localizes_early_with_honest_partial_coverage():
    state, calls = {}, []

    def request(task, material, schema):
        calls.append(material)
        return localization(material)

    result = locate_unit(TARGET, UNIT, source(800), state, request=request)
    assert len(calls) == 1
    assert result["hits"][0]["start_ms"] == 0
    assert result["coverage"]["partial"]
    assert result["coverage"]["selected_segments"] == 300
    assert "result" in state


@pytest.mark.parametrize("field,value", [
    ("start_segment_id", 99999), ("end_segment_id", 1), ("focus_segment_ids", [99999]),
])
def test_localization_rejects_invented_or_out_of_window_ids(field, value):
    def request(task, material, schema):
        result = localization(material)
        result["hits"][0][field] = value
        return result

    with pytest.raises(ValueError):
        locate_unit(TARGET, UNIT, source(), {}, request=request)


def test_completed_localization_is_reused_after_cancellation():
    state, calls = {}, []

    def request(task, material, schema):
        calls.append(material)
        return localization(material)

    def checkpoint():
        if state.get("chunks"):
            raise ProcessingCancelled("合成取消")

    with pytest.raises(ProcessingCancelled):
        locate_unit(TARGET, UNIT, source(), state, request=request, checkpoint=checkpoint)
    result = locate_unit(TARGET, UNIT, source(), state, request=request)
    assert result["hits"] and len(calls) == 1


def test_capture_times_and_context_derive_only_from_real_subtitles():
    rows = source(800)["segments"]
    hit = {"start_ms": 700000, "end_ms": 710000, "focus_segment_ids": [rows[703]["id"]]}
    context = nearby_segments(hit, rows, limit=100)
    assert any(row["start_ms"] == 703000 for row in context)
    assert all(0 <= t < 706000 for t in initial_times(hit, context, 706000))
    assert len(initial_times(hit, context, 706000)) <= 3


def test_visual_dimensions_can_confirm_a_different_instance_without_definition_recital():
    rows = source()["segments"]
    frames = [{"id": "a" * 32}]
    material = {"frames": [{"source": "frame:" + frames[0]["id"]}], "segments": rows}
    result = finalize_visual(TARGET, UNIT, rows, frames, assessment(material))
    assert result["verdict"] == "covers"
    assert result["evidence"][0]["start_ms"] == 0
    assert result["evidence"][0]["url"].endswith("p=1&t=0")


@pytest.mark.parametrize("tamper", ["image", "subtitle", "no_image", "no_steps", "followup", "time"])
def test_visual_judgment_rejects_forged_or_missing_evidence(tamper):
    rows, frames = source()["segments"], [{"id": "a" * 32}]
    result = assessment({"frames": [{"source": "frame:" + frames[0]["id"]}], "segments": rows})
    if tamper == "image":
        result["problem_type"]["frame_ids"] = ["frame:" + "b" * 32]
    if tamper == "subtitle":
        result["knowledge"]["segment_ids"] = [999]
    if tamper == "no_image":
        result["problem_type"]["frame_ids"] = []
    if tamper == "no_steps":
        result["explanation"]["segment_ids"] = []
    if tamper == "followup":
        result["followup_segment_ids"] = [999]
    if tamper == "time":
        result["start_ms"] = 1000
    with pytest.raises(ValueError):
        finalize_visual(TARGET, UNIT, rows, frames, result)


def test_visual_reference_repair_is_bounded_and_does_not_relax_validation():
    rows, calls = source()["segments"], []
    frames = [{"id": "a" * 32, "bvid": UNIT["bvid"], "cid": UNIT["cid"], "page": UNIT["page"],
               "requested_ms": 1000, "captured_ms": 1000, "time_source": "decoded_frame",
               "image_hash": "synthetic", "width": 960, "height": 540}]

    def request(task, material, schema):
        calls.append(material)
        result = assessment(material)
        result["problem_type"]["frame_ids"] = ["invented"]
        return result

    with pytest.raises(ValueError, match="未提供"):
        assess_frames(TARGET, UNIT, rows, frames, None, request=request)
    assert len(calls) == 2 and "validation_error" in calls[-1]


def test_complete_visual_pipeline_captures_three_frames_and_stops_on_evidence():
    run, model, reader, frames, _ = execute_visual()
    assert run["stop_reason"] == "success"
    assert run["budget"]["visual_units"] == 1 and run["budget"]["frames"] == 3
    assert len(reader.calls) == len(frames.saved) == 3
    assert len(model.vision_calls) == 1
    unit = run["candidates"][0]["units"][0]
    assert unit["verification_mode"] == "visual" and unit["verdict"] == "covers"
    assert unit["dimensions"]["problem_type"]["status"] == "yes"


def test_light_comparison_sends_both_sides_without_prior_requirements():
    from tests.bilibili_visual_fakes import synthetic_frame

    image = QuestionImage.from_bytes("capture:" + "a" * 32, synthetic_frame(100))
    run = run_state()
    run["prompt_style"] = "light"
    run["input"]["question"] = {
        "images": [{"source": image.source, "image_hash": image.sha256}],
        "analysis": {"kind": "exercise", "search_plan": {"summary": "配方法合成题", "task": "解方程"},
                     "criteria": ["MUST_NOT_REACH_COMPARISON"]},
    }

    class LightModel(VisionModel):
        def ask_with_images(self, task, material, schema, images):
            assert images[0] is image
            assert material["original_images"] == [image.source]
            assert "target" not in material and "original_analysis" not in material
            assert "MUST_NOT_REACH_COMPARISON" not in str(material)
            assert "直接看两边图片" in task
            assert "逐条" not in task and len(task) < 400
            return super().ask_with_images(task, material, schema, images)

    run, *_ = execute_visual(run=run, model=LightModel(), original_images=[image])
    assert run["stop_reason"] == "success"


def test_missing_original_image_is_rejected_before_visual_requests():
    run = run_state()
    run["prompt_style"] = "light"
    run["input"]["question"] = {
        "images": [{"source": "capture:" + "a" * 32, "image_hash": "missing"}],
        "analysis": {"kind": "exercise", "search_plan": {"summary": "合成原题", "task": "求解"}},
    }
    model, reader = VisionModel(), FakeFrameReader()
    with pytest.raises(ValueError, match="原题图片缺失"):
        execute_visual(run=run, model=model, reader=reader)
    assert not model.vision_calls and not reader.calls


@pytest.mark.parametrize("tamper", ["original_as_candidate", "no_image", "invented_subtitle"])
def test_light_judgment_keeps_candidate_evidence_validation(tamper):
    rows, frames = source()["segments"], [{"id": "a" * 32}]
    value = light_assessment({"frames": [{"source": "frame:" + frames[0]["id"]}], "segments": rows})
    if tamper == "original_as_candidate":
        value["frame_ids"] = ["capture:" + "a" * 32]
    elif tamper == "no_image":
        value["frame_ids"] = []
    else:
        value["segment_ids"] = [99999]
    with pytest.raises(ValueError):
        finalize_light(TARGET, UNIT, rows, frames, value)


def test_light_accepts_exact_bare_candidate_id_and_long_valid_citation_list():
    rows, frames = source(40)["segments"], [{"id": "a" * 32}]
    value = light_assessment({"frames": [{"source": "frame:" + frames[0]["id"]}], "segments": rows})
    value.update(frame_ids=[frames[0]["id"]], segment_ids=[r["id"] for r in rows[:20]])
    result = finalize_light(TARGET, UNIT, rows, frames, value)
    assert result["verdict"] == "covers"
    assert result["comparison"]["frame_ids"] == ["frame:" + frames[0]["id"]]
    assert len(result["evidence"]) == 20


@pytest.mark.parametrize("count", [21, 28])
def test_light_rejects_subtitle_citations_beyond_twenty(count):
    rows, frames = source(40)["segments"], [{"id": "a" * 32}]
    value = light_assessment({"frames": [{"source": "frame:" + frames[0]["id"]}], "segments": rows})
    value["segment_ids"] = [r["id"] for r in rows[:count]]
    with pytest.raises(ValueError):
        finalize_light(TARGET, UNIT, rows, frames, value)


def test_followup_is_only_one_round_and_at_most_two_extra_frames():
    run, model, reader, frames, _ = execute_visual(model=VisionModel(status="partial", followup=True))
    unit = run["candidates"][0]["units"][0]
    assert 3 < len(reader.calls) <= 5
    assert len(model.vision_calls) == 2
    assert unit["visual"]["rounds"] == 2
    assert unit["verdict"] == "partial"
    assert any(event["action"] == "reshoot" for event in run["trace"])


def test_cancel_after_frame_save_resumes_without_recapture():
    run, model, frames, store = run_state(), VisionModel(), MemoryFrames(), FakeStore()
    cancelled = False

    def after_capture():
        nonlocal cancelled
        cancelled = True

    first = FakeFrameReader(after_capture=after_capture)
    with pytest.raises(ProcessingCancelled):
        execute_visual(run=run, model=model, reader=first, frames=frames, store=store,
                       should_cancel=lambda: cancelled)
    assert len(frames.saved) == 1 and not model.vision_calls
    second = FakeFrameReader()
    execute_visual(run=run, model=model, reader=second, frames=frames, store=store)
    assert run["stop_reason"] == "success"
    assert len(second.calls) == 2 and run["budget"]["frames"] == 3


@pytest.mark.parametrize("resource", ["visual_units", "frames", "browser_requests", "browser_bytes"])
def test_visual_limits_pause_before_further_external_work(resource):
    run = run_state()
    run.update(phase="course", limits={**LIMITS, resource: 0})
    run, model, reader, frames, _ = execute_visual(run=run)
    assert run["status"] == "budget_exhausted" and run["budget_stop"]["resource"] == resource
    assert not model.vision_calls and not frames.saved


def test_risk_pause_stops_without_model_recommendation():
    run, frames, model = run_state(), MemoryFrames(), VisionModel()
    reader = FakeFrameReader(error=BilibiliError("platform_paused", "合成风控暂停"))
    with pytest.raises(BilibiliError):
        execute_visual(run=run, model=model, reader=reader, frames=frames)
    assert run["stop_reason"] == "platform_pause" and len(reader.calls) == 1
    assert not model.vision_calls


def test_black_or_failed_capture_does_not_become_a_positive_match():
    reader = FakeFrameReader(error=BilibiliError("capture_failed", "合成截图失败"))
    run, model, _, _, _ = execute_visual(reader=reader)
    unit = run["candidates"][0]["units"][0]
    assert unit["verdict"] == "insufficient" and unit["visual"]["status"] == "capture_failed"
    assert not model.vision_calls


def test_visual_model_failure_preserves_frames_for_manual_retry():
    class Broken(VisionModel):
        def ask_with_images(self, *args):
            raise RuntimeError("合成视觉服务失败")

    run, frames, store, reader = run_state(), MemoryFrames(), FakeStore(), FakeFrameReader()
    with pytest.raises(RuntimeError):
        execute_visual(run=run, model=Broken(), reader=reader, frames=frames, store=store)
    assert len(frames.saved) == 3
    assert run["candidates"][0]["units"][0]["visual"]["status"] == "model_failed"
    execute_visual(run=run, frames=frames, reader=reader, store=store)
    assert len(reader.calls) == 3 and run["stop_reason"] == "success"
    assert "error" not in run["candidates"][0]["units"][0]["visual"]
