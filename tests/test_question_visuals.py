"""Synthetic two-pass question/PPT verification, without network requests."""

import hashlib
import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from classcue.ai_provider import ProviderProfile
from classcue.core import TranscriptSegment
from classcue.processing_control import ProcessingCancelled
from classcue.question_captures import QuestionCaptures
from classcue.question_store import QuestionStore
from classcue.question_analysis import QuestionMaterial
from classcue.question_visuals import (
    QuestionVisualStore, VisualDecision, VisualQuestionModel, question_marker, select_images, source_snapshot,
    verify_after_extraction, verify_one, verify_video,
)
from classcue.video_store import VideoStore
from tests.test_exam_point_judge import FakeClient


@pytest.fixture(autouse=True)
def enable_retained_review_for_unit_tests(monkeypatch):
    # Exercise the retained implementation only with synthetic images and fake clients.
    monkeypatch.setattr("classcue.question_visuals.LLM_VISUAL_REVIEW_ENABLED", True)


@pytest.fixture
def visual(tmp_path):
    store = VideoStore(tmp_path / "project.db")
    video_file = tmp_path / "lesson.mp4"
    video_file.write_bytes(b"synthetic")
    video = store.create_video("synthetic", video_file.name, str(video_file), "missing.wav", 300000)
    store.mark_transcribed(video, [TranscriptSegment(1, 10000, 20000, "已知集合，求元素个数")])
    marker = store.add_marker(video, 10000, 20000, "集合例题", "", "已知集合，求元素个数",
                              "AI-question", 1, "question_extraction")
    root = tmp_path / "slides"
    root.mkdir()
    rows = []
    for index, preferred in enumerate([True, False, False]):
        data = io.BytesIO()
        Image.new("RGB", (100, 60), (index * 60, 100, 200)).save(data, format="PNG")
        path = root / f"{index}.png"
        path.write_bytes(data.getvalue())
        rows.append({
            "start_ms": 10000 + index * 3000, "end_ms": 13000 + index * 3000,
            "representative_ms": 12000 + index * 3000,
            "visible_from_ms": 10000 + index * 3000, "visible_until_ms": 13000 + index * 3000,
            "image_path": path.name, "image_hash": hashlib.sha256(data.getvalue()).hexdigest(),
            "duration_ms": 10000, "retention_class": "primary", "confidence": 1,
            "is_llm_preferred": preferred, "roi_json": "{}", "extraction_version": "synthetic",
        })
    slides = store.replace_video_slides(video, rows)
    return SimpleNamespace(store=store, video=video, marker=marker, root=root,
                           repo=QuestionVisualStore(store), slides=slides, tmp=tmp_path)


class Model:
    def __init__(self, verdicts=("confirmed",)):
        self.verdicts = iter(verdicts)
        self.seen = []
        self.closed = False

    def check(self, snapshot, rows, images, attempt, should_cancel):
        self.seen.append([image.sha256 for image in images])
        result = next(self.verdicts)
        if isinstance(result, Exception):
            raise result
        return VisualDecision(verdict=result, slide_id=rows[0]["id"] if result == "confirmed" else None,
                              segment_ids=[snapshot["segments"][0]["id"]], reason="合成核验依据")

    def close(self):
        self.closed = True


def check(v, model=None, **options):
    model = model or Model()
    return verify_one(v.repo, v.marker, v.root, lambda: model, {"model": "fake"}, **options)


def test_preferred_confirmation_binds_durable_image_and_reuses_cache(visual):
    v = visual
    model = Model()
    result = check(v, model)
    assert result["status"] == "completed" and result["verdict"] == "confirmed"
    assert len(model.seen) == 1 and model.closed
    capture = result["binding"]["capture_id"]
    assert QuestionStore(v.store).material(v.marker)["capture_ids"] == [capture]
    question_id = result["question_id"]
    assert check(v, Model(()))["question_id"] == question_id
    v.store.replace_video_slides(v.video, [])
    projection = v.repo.projection(question_marker(v.store, v.marker))
    assert projection["stale"] and not projection["binding"]["missing"]
    assert QuestionCaptures(v.store).path(capture).is_file()


def test_uncertain_preferred_falls_back_without_repeating_image_hashes(visual):
    model = Model(("uncertain", "confirmed"))
    result = check(visual, model)
    assert result["verdict"] == "confirmed" and result["binding"]["source"] == "timeline"
    assert [attempt["stage"] for attempt in result["attempts"]] == ["preferred", "timeline"]
    assert set(model.seen[0]).isdisjoint(model.seen[1])
    assert result["coverage"]["sent_images"] == 3


def test_nonquestion_never_removes_subtitle_candidate(visual):
    result = check(visual, Model(("not_question", "not_question")))
    assert result["verdict"] == "not_question"
    assert visual.store.list_markers(visual.video)[0]["id"] == visual.marker


def test_missing_slides_never_construct_model(visual):
    visual.store.replace_video_slides(visual.video, [])
    result = verify_one(visual.repo, visual.marker, visual.root,
                        lambda: pytest.fail("must not create model"), {})
    assert result["status"] == "waiting_slides"


def test_missing_files_are_uncertain_not_negative(visual):
    for path in visual.root.iterdir():
        path.unlink()
    result = check(visual, Model(()))
    assert result["verdict"] == "uncertain"
    assert result["coverage"]["unavailable_images"] == 3


@pytest.mark.parametrize("slide,segment", [(999, 1), (None, 999)])
def test_invalid_model_references_cannot_bind(visual, slide, segment):
    class Invalid(Model):
        def check(self, snapshot, rows, images, attempt, should_cancel):
            return {"verdict": "confirmed", "slide_id": slide or rows[0]["id"],
                    "segment_ids": [segment], "reason": "invalid"}
    result = check(visual, Invalid())
    assert result["status"] == "failed" and not result.get("binding")


def test_manual_binding_and_unbinding_survive_automatic_rechecks(visual):
    v = visual
    original = v.repo.manual_binding(v.marker, v.slides[1]["id"], v.root)
    result = check(v)
    assert result["binding"] == {key: val for key, val in original["binding"].items() if key != "missing"}
    assert result["binding_mode"] == "manual"
    v.repo.manual_binding(v.marker, None, v.root)
    result = check(v, force=True)
    assert result["binding"] is None and result["binding_mode"] == "manual"
    assert not QuestionStore(v.store).material(v.marker)["capture_ids"]


def test_binding_preserves_user_material_and_rejects_cross_video_slide(visual):
    v = visual
    QuestionStore(v.store).save_material(v.marker, QuestionMaterial(text="用户题干"))
    check(v)
    assert QuestionStore(v.store).material(v.marker)["text"] == "用户题干"
    assert not QuestionStore(v.store).material(v.marker)["capture_ids"]
    with pytest.raises(ValueError):
        v.repo.manual_binding(v.marker, 999, v.root)
    other = v.store.create_video("other", "other.mp4", "other.mp4", "", 300000)
    row = {**dict(v.slides[0]), "id": 999}
    row.pop("video_id")
    other_slide = v.store.replace_video_slides(other, [row])[0]
    with pytest.raises(ValueError):
        v.repo.manual_binding(v.marker, other_slide["id"], v.root)


def test_failure_and_cancellation_keep_previous_image_and_retry_history(visual):
    v = visual
    original = check(v)
    failed = check(v, Model((ConnectionError("synthetic"),)), force=True)
    assert failed["status"] == "failed" and failed["service_error"]
    assert failed["binding"] == original["binding"]
    assert failed["verdict"] == "uncertain" and failed["question_text"] == ""
    assert len(failed["history"]) == 1
    with pytest.raises(ProcessingCancelled):
        check(v, force=True, should_cancel=lambda: True)
    assert v.repo.get(v.marker)["status"] == "interrupted"
    assert v.repo.get(v.marker)["binding"] == original["binding"]


def test_service_failure_stops_video_requests(visual, monkeypatch):
    v = visual
    second = v.store.add_marker(v.video, 20000, 30000, "下一题", "", "", "AI-next", 1, "question_extraction")
    constructed = []
    def factory(*args, **kwargs):
        constructed.append(True)
        return Model((ConnectionError("synthetic"),))
    monkeypatch.setattr("classcue.question_visuals.VisualQuestionModel", factory)
    summary = verify_video(v.store, v.video, v.root, ProviderProfile(model="fake"), "synthetic")
    assert summary == {"failed": 1, "deferred": 1}
    assert len(constructed) == 1 and v.repo.get(second) is None


def test_bound_marker_survives_ai_cleanup_and_video_recycle(visual):
    v = visual
    result = check(v)
    v.store.delete_markers_for_mode(v.video, "question_extraction")
    v.store.publish_exam_results(v.video, "question_extraction", [], "signature", "run")
    v.store.publish_semantic_markers(v.video, "question_extraction", [], replace_existing=True)
    assert question_marker(v.store, v.marker)["id"] == v.marker
    v.store.trash_video(v.video, v.tmp / "trash")
    assert v.repo.get(v.marker) is None
    v.store.undo_latest_recycle(v.tmp / "trash")
    assert v.repo.get(v.marker)["question_id"] == result["question_id"]
    assert not v.repo.projection(question_marker(v.store, v.marker))["binding"]["missing"]


def test_model_format_repair_and_tokens_are_recorded(visual):
    v = visual
    snapshot = source_snapshot(v.store, question_marker(v.store, v.marker))
    rows = snapshot["slides"][:1]
    client = FakeClient(["not json", {"slide_id": rows[0]["id"]}])
    original_create = client.chat.completions.create
    def create(**request):
        response = original_create(**request)
        response.usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        return response
    client.chat.completions.create = create
    model = VisualQuestionModel(ProviderProfile(model="fake", base_url="https://model.example/v1"), "synthetic", client=client)
    attempt = {}
    decision = model.check(snapshot, rows, [], attempt, lambda: False)
    assert decision.verdict == "confirmed" and len(client.requests) == 2
    assert attempt["format_repair_count"] == 1
    assert attempt["error_counts"] == {"json_invalid": 1}
    assert sum(call["usage"]["total_tokens"] for call in attempt["calls"]) == 30
    assert len(attempt["outputs"]) == 2
    model.close()


def test_model_repairs_at_most_once_and_rejects_boolean_image_id(visual):
    v = visual
    snapshot = source_snapshot(v.store, question_marker(v.store, v.marker))
    rows = snapshot["slides"][:1]
    raw = json.dumps({"slide_id": True})
    client = FakeClient([raw, raw])
    model = VisualQuestionModel(ProviderProfile(model="fake", base_url="https://model.example/v1"), "synthetic", client=client)
    with pytest.raises(ValueError):
        model.check(snapshot, rows, [], {}, lambda: False)
    assert len(client.requests) == 2


def test_location_caps_long_question_and_excludes_later_unrelated_pages(visual):
    v = visual
    with v.store.connect() as connection:
        connection.execute("UPDATE video_markers SET end_ms=250000 WHERE id=?", (v.marker,))
    rows = [dict(row) for row in v.slides]
    rows.append({**rows[0], "start_ms": 200000, "end_ms": 210000, "representative_ms": 205000,
                 "visible_from_ms": 200000, "visible_until_ms": 210000,
                 "page_start_ms": 200000, "page_end_ms": 210000})
    stored = v.store.replace_video_slides(v.video, rows)
    snapshot = source_snapshot(v.store, question_marker(v.store, v.marker))
    assert snapshot["marker"]["end_ms"] == 250000
    assert snapshot["location"] == {"start_ms": 10000, "end_ms": 30000, "source": "question_start"}
    assert snapshot["search_ranges"] == {
        "preferred": {"start_ms": 0, "end_ms": 50000},
        "timeline": {"start_ms": 0, "end_ms": 90000},
    }
    assert stored[-1]["id"] not in {row["id"] for row in snapshot["slides"]}


def test_user_adjusted_jump_moves_location_without_changing_original_question(visual):
    v = visual
    v.store.set_marker_jump_time(v.marker, 120000)
    snapshot = source_snapshot(v.store, question_marker(v.store, v.marker))
    assert snapshot["location"] == {"start_ms": 120000, "end_ms": 140000, "source": "user_jump"}
    assert snapshot["marker"]["start_ms"] == 10000
    assert snapshot["slides"] == []


def test_late_complete_preferred_image_remains_eligible_via_same_page(visual):
    v = visual
    rows = [{**dict(row), "is_llm_preferred": False} for row in v.slides]
    rows.append({**rows[0], "is_llm_preferred": True,
                 "start_ms": 120000, "end_ms": 130000, "representative_ms": 125000,
                 "visible_from_ms": 120000, "visible_until_ms": 130000,
                 "page_start_ms": 10000, "page_end_ms": 130000})
    stored = v.store.replace_video_slides(v.video, rows)
    snapshot = source_snapshot(v.store, question_marker(v.store, v.marker))
    selected = select_images(snapshot, True, set())
    assert [row["id"] for row in selected] == [stored[-1]["id"]]
    result = check(v)
    assert result["binding"]["slide_id"] == stored[-1]["id"]
    assert result["binding"]["representative_ms"] == 125000
    assert result["binding"]["source"] == "preferred"
    assert len(result["attempts"]) == 1


def test_preferred_uses_narrow_window_and_timeline_expands_only_after_null(visual):
    v = visual
    rows = [dict(v.slides[0])]
    rows.append({**dict(v.slides[1]), "is_llm_preferred": False,
                 "start_ms": 70000, "end_ms": 80000, "representative_ms": 75000,
                 "visible_from_ms": 70000, "visible_until_ms": 80000,
                 "page_start_ms": 70000, "page_end_ms": 80000})
    stored = v.store.replace_video_slides(v.video, rows)
    snapshot = source_snapshot(v.store, question_marker(v.store, v.marker))
    assert stored[-1]["id"] not in {row["id"] for row in select_images(snapshot, True, set())}
    model = Model(("uncertain", "confirmed"))
    result = check(v, model)
    assert len(model.seen) == 2
    assert result["attempts"][0]["search_range"]["end_ms"] == 40000
    assert result["attempts"][1]["search_range"]["end_ms"] == 80000
    assert result["binding"]["slide_id"] == stored[-1]["id"]


@pytest.mark.parametrize("output", [
    lambda slide: {"slide_id": slide},
    lambda slide: {"slide_id": str(slide)},
    lambda slide: {"slide_id": slide, "reason": "", "segment_ids": [999], "knowledge_point": ["extra"]},
    lambda slide: '```json\n' + json.dumps({"slide_id": slide}) + '\n```',
])
def test_minimal_and_locally_normalized_json_succeed_without_repair(visual, output):
    v = visual
    slide = v.slides[0]["id"]
    client = FakeClient([output(slide)])
    factory = lambda: VisualQuestionModel(
        ProviderProfile(model="fake", base_url="https://model.example/v1"), "synthetic", client=client)
    result = verify_one(v.repo, v.marker, v.root, factory, {"model": "fake"})
    assert result["verdict"] == "confirmed" and result["binding"]["source"] == "preferred"
    assert len(client.requests) == 1 and len(result["attempts"]) == 1
    assert result["attempts"][0]["format_repair_count"] == 0
    assert result["attempts"][0]["error_counts"] == {}
    assert result["segment_ids"] == [1]


def test_answered_preferred_question_stops_with_one_field_output(visual):
    v = visual
    client = FakeClient([{"slide_id": v.slides[0]["id"], "reason": "题干清楚，附有完整答案和解题步骤"}])
    model = VisualQuestionModel(ProviderProfile(model="fake", base_url="https://model.example/v1"),
                                "synthetic", client=client)
    result = check(v, model)
    assert result["verdict"] == "confirmed" and result["binding"]["source"] == "preferred"
    assert len(client.requests) == 1 and len(result["attempts"]) == 1
    prompt = client.requests[0]["messages"][0]["content"]
    assert "不能仅因出现答案或解析而否定它" in prompt
    assert "不要求逐字一致" in prompt
    material = json.loads(client.requests[0]["messages"][1]["content"][0]["text"])
    assert set(material) == {"title", "location", "subtitles", "slides"}
    assert "segment_ids" not in prompt and "properties" not in prompt
    assert not any("id" in row for row in material["subtitles"])
    assert result["reason"] == "题干清楚，附有完整答案和解题步骤"


def test_unknown_image_id_still_needs_one_repair(visual):
    v = visual
    client = FakeClient([{"slide_id": 999}, {"slide_id": v.slides[0]["id"]}])
    model = VisualQuestionModel(ProviderProfile(model="fake", base_url="https://model.example/v1"),
                                "synthetic", client=client)
    result = check(v, model)
    assert result["verdict"] == "confirmed" and len(client.requests) == 2
    assert result["attempts"][0]["format_repair_count"] == 1
    assert result["attempts"][0]["error_counts"] == {"reference_invalid": 1}


def test_many_short_asr_segments_do_not_trigger_format_repair(visual):
    v = visual
    snapshot = source_snapshot(v.store, question_marker(v.store, v.marker))
    snapshot["segments"] = [
        {"id": index + 1, "start_ms": 10000 + index * 500, "end_ms": 10500 + index * 500,
         "text": "题目的条件和求解目标"}
        for index in range(20)
    ]
    client = FakeClient([{"slide_id": v.slides[0]["id"]}])
    model = VisualQuestionModel(ProviderProfile(model="fake", base_url="https://model.example/v1"),
                                "synthetic", client=client)
    attempt = {}
    decision = model.check(snapshot, snapshot["slides"][:1], [], attempt, lambda: False)
    assert decision.verdict == "confirmed" and len(decision.segment_ids) == 8
    assert len(client.requests) == 1 and attempt["format_repair_count"] == 0


def test_minimal_null_uses_fallback_without_marking_candidate_not_question(visual):
    client = FakeClient([{"slide_id": None}, {"slide_id": None}])
    model = VisualQuestionModel(ProviderProfile(model="fake", base_url="https://model.example/v1"),
                                "synthetic", client=client)
    result = check(visual, model)
    assert result["verdict"] == "uncertain" and result["status"] == "completed"
    assert len(client.requests) == 2
    assert [attempt["format_repair_count"] for attempt in result["attempts"]] == [0, 0]
    assert visual.store.list_markers(visual.video)[0]["id"] == visual.marker


def test_auto_wrapper_retains_publication_on_cancellation(visual, monkeypatch):
    def stop(*args, **kwargs):
        raise ProcessingCancelled("synthetic")
    monkeypatch.setattr("classcue.question_visuals.verify_video", stop)
    assert verify_after_extraction(visual.store, visual.video, slide_root=visual.root,
                                   profile=ProviderProfile(model="fake", base_url="https://model.example/v1"),
                                   key="synthetic") == {"interrupted": 1}
    assert len(visual.store.list_markers(visual.video)) == 1


def test_reverification_repairs_missing_bound_capture(visual):
    original = check(visual)
    path = QuestionCaptures(visual.store).path(original["binding"]["capture_id"])
    path.unlink()
    assert visual.repo.projection(question_marker(visual.store, visual.marker))["binding"]["missing"]
    result = check(visual)
    assert result["status"] == "completed" and path.is_file()


def test_empty_saved_material_receives_main_image(visual):
    QuestionStore(visual.store).save_material(visual.marker, QuestionMaterial())
    result = check(visual)
    assert QuestionStore(visual.store).material(visual.marker)["capture_ids"] == [result["binding"]["capture_id"]]


def test_manual_unbinding_survives_marker_cleanup(visual):
    check(visual)
    visual.repo.manual_binding(visual.marker, None, visual.root)
    visual.store.delete_markers_for_mode(visual.video, "question_extraction")
    visual.store.publish_exam_results(visual.video, "question_extraction", [], "signature", "run")
    assert question_marker(visual.store, visual.marker)["id"] == visual.marker


def test_subtitle_extraction_and_cached_results_never_start_visual_review(visual, monkeypatch):
    from classcue.exam_point_judge import ExamPointJudge
    v = visual
    monkeypatch.setattr("classcue.asr_storage.slide_directory", lambda *_: v.root)
    text = {"candidates": [{"start_sentence_id": 1, "end_sentence_id": 1,
                             "evidence_sentence_ids": [1], "title": "集合例题"}]}
    def unexpected_review(*args, **kwargs):
        pytest.fail("Subtitle extraction must not trigger image review")
    monkeypatch.setattr("classcue.question_visuals.verify_after_extraction", unexpected_review)
    client = FakeClient([text])
    profile = ProviderProfile(model="fake", base_url="https://model.example/v1", max_retries=0)
    judge = ExamPointJudge(profile, "synthetic", mode="question_extraction", client=client)
    assert judge.analyze_video(v.store, v.video, filter_formula_noise=False) == ("completed", 1)
    assert v.store.get_analysis_run(v.video, "question_extraction")["status"] == "completed"
    assert v.repo.get(v.marker)["algorithm"] == "question-image-time-v1"
    assert len(client.requests) == 1
    assert judge.analyze_video(v.store, v.video, filter_formula_noise=False) == ("skipped", 1)
    assert len(client.requests) == 1


def test_disabled_review_preserves_existing_results_and_never_creates_clients(visual, monkeypatch):
    v = visual
    existing = check(v)
    monkeypatch.setattr("classcue.question_visuals.LLM_VISUAL_REVIEW_ENABLED", False)
    def unexpected(*args, **kwargs):
        pytest.fail("Disabled review must not read settings or create model clients")
    monkeypatch.setattr("classcue.ai_provider.load_provider_settings", unexpected)
    assert verify_one(v.repo, v.marker, v.root, unexpected, {}, force=True)["status"] == "disabled"
    assert verify_video(v.store, v.video, v.root, None, "") == {"disabled": 1}
    assert verify_after_extraction(v.store, v.video) == {"disabled": 1}
    with pytest.raises(RuntimeError, match="暂时停用"):
        VisualQuestionModel(None, "")
    assert v.repo.get(v.marker) == existing
    assert QuestionCaptures(v.store).path(existing["binding"]["capture_id"]).is_file()
