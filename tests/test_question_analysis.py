from __future__ import annotations

import copy
import hashlib
import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from classcue.ai_provider import ProviderProfile
from classcue.bilibili_harness import HarnessService, Target, validate_sources
from classcue.bilibili_store import BilibiliStore, StudyContext
from classcue.core import TranscriptSegment
from classcue.processing_control import ProcessingCancelled
from classcue.question_analysis import (
    MAX_IMAGE_BYTES, QuestionAnalysis, QuestionImage, QuestionMaterial, to_target, validate_analysis,
)
from classcue.question_service import QuestionModel, analyze_question, load_slide_images
from classcue.question_store import QuestionStore
from classcue.video_store import VideoStore


def png():
    output = io.BytesIO()
    Image.new("RGB", (100, 60), "white").save(output, format="PNG")
    return output.getvalue()


def analysis(source="question.text"):
    return {
        "kind": "exercise",
        "facts": [
            {"id": "f1", "kind": "condition", "text": "已知关系模式与函数依赖", "sources": [source]},
            {"id": "f2", "kind": "task", "text": "求候选键", "sources": [source]},
        ],
        "knowledge_points": [{"id": "k1", "text": "候选键", "basis": ["f1", "f2"]}],
        "methods": [{"id": "m1", "text": "可使用属性闭包", "basis": ["f1", "f2"]}],
        "criteria": [{"id": "r1", "text": "实际演示求候选键", "depth": "example", "required": True,
                      "evidence_needed": "同一道例题的条件、求解步骤与理由", "basis": ["k1"]}],
        "allowed_variations": ["允许属性名称变化及其他有效解法"],
        "unknowns": [], "keywords": ["候选键", "属性闭包"],
    }


@pytest.fixture
def questions(tmp_path):
    store = VideoStore(tmp_path / "library.db")
    video = store.create_video("synthetic", "lesson.mp4", "lesson.mp4", "lesson.wav", 10000)
    store.mark_transcribed(video, [TranscriptSegment(1, 0, 5000, "重点是求候选键")])
    marker = store.add_marker(video, 0, 5000, "候选键", "", "重点是求候选键", "重点", .8)
    repo = QuestionStore(store)
    repo.save_material(marker, QuestionMaterial(text="已知关系模式及函数依赖，求候选键。"))
    BilibiliStore(store).save_context(marker, StudyContext(course="数据库"))
    return repo, marker, video


def add_slide(repo, video, root):
    root.mkdir(exist_ok=True)
    data = png()
    (root / "synthetic.png").write_bytes(data)
    return repo.store.replace_video_slides(video, [{
        "start_ms": 0, "end_ms": 5000, "representative_ms": 100,
        "image_path": "synthetic.png", "image_hash": hashlib.sha256(data).hexdigest(),
        "duration_ms": 5000, "retention_class": "primary", "confidence": 1,
        "roi_json": "{}", "extraction_version": "synthetic",
    }])[0]["id"]


class FakeModel:
    def __init__(self, value=None):
        self.value = value or analysis()

    def analyze(self, snapshot, images):
        return QuestionAnalysis.model_validate(self.value)


def completed(questions, value=None):
    repo, marker, _ = questions
    run = repo.create_run(repo.snapshot(marker), {"model": "fake-vision"})
    analyze_question(repo, run, FakeModel(value), [])
    return run


def test_question_target_preserves_example_depth_and_source(questions):
    repo, marker, _ = questions
    original = dict(repo.store.list_markers(1)[0])
    run = completed(questions)
    target = Target.model_validate(run["target"])
    assert target.requirements[0].depth == "example"
    assert target.requirements[0].sources == [f"question:{run['id']}:r1"]
    assert target.comparison.conditions == ["已知关系模式与函数依赖"]
    assert target.comparison.allowed_variations == ["允许属性名称变化及其他有效解法"]
    assert "条件、求解步骤" in target.requirements[0].text
    snapshot = BilibiliStore(repo.store).snapshot(marker)
    validate_sources(target, snapshot)
    assert dict(repo.store.list_markers(1)[0]) == original
    assert snapshot["question"]["analysis"]["methods"][0]["basis"] == ["f1", "f2"]


@pytest.mark.parametrize("change", ["source", "basis", "id", "condition", "empty_criteria"])
def test_fabricated_structure_or_references_rejected(questions, change):
    repo, marker, _ = questions
    value = analysis()
    if change == "source":
        value["facts"][0]["sources"] = ["slide:999"]
    elif change == "basis":
        value["methods"][0]["basis"] = ["missing"]
    elif change == "id":
        value["criteria"][0]["id"] = "f1"
    elif change == "condition":
        value["facts"][0]["kind"] = "topic"
    else:
        value["criteria"] = []
    with pytest.raises(ValueError):
        validate_analysis(QuestionAnalysis.model_validate(value), repo.snapshot(marker))


def test_unclear_question_never_becomes_search_target(questions):
    run = completed(questions, {"kind": "unclear", "unknowns": ["关键条件看不清"]})
    assert run["status"] == "completed"
    assert run["target"] is None
    repo, marker, _ = questions
    assert BilibiliStore(repo.store).snapshot(marker)["question"]["status"] == "needs_clarification"


def test_topic_can_request_example_without_inventing_question(questions):
    repo, marker, _ = questions
    value = analysis("marker.title")
    value.update(kind="topic", facts=[{"id": "f1", "kind": "topic", "text": "候选键", "sources": ["marker.title"]}],
                 methods=[], knowledge_points=[{"id": "k1", "text": "候选键", "basis": ["f1"]}])
    target = to_target(QuestionAnalysis.model_validate(value), repo.snapshot(marker), "fake")
    assert target.requirements[0].depth == "example"


def test_image_request_uses_real_bytes_and_never_local_paths(questions):
    repo, marker, _ = questions
    image = QuestionImage.from_bytes("slide:1", png())
    snapshot = repo.snapshot(marker)
    snapshot["question"]["images"] = [{"source": "slide:1", "image_hash": image.sha256}]
    calls = []

    def create(**request):
        calls.append(request)
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content=json.dumps(analysis("slide:1"))),
        )])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with QuestionModel(ProviderProfile(model="fake-vision", base_url="https://model.example/v1"),
                       "synthetic-key", client=client) as model:
        result = model.analyze(snapshot, [image])
    content = calls[0]["messages"][1]["content"]
    assert content[2]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "slide:1" in content[1]["text"]
    assert "tools" not in calls[0]
    assert "synthetic-key" not in str(calls)
    validate_analysis(result, snapshot)


def test_missing_image_rejected_before_model_request(questions):
    repo, marker, _ = questions
    snapshot = repo.snapshot(marker)
    snapshot["question"]["images"] = [{"source": "slide:1", "image_hash": "hash"}]
    with QuestionModel(ProviderProfile(model="fake", base_url="https://model.example/v1"),
                       "synthetic-key", client=SimpleNamespace()) as model:
        with pytest.raises(ValueError, match="快照"):
            model.analyze(snapshot, [])


@pytest.mark.parametrize("data", [b"bad image", b"<svg/>", b"x" * (MAX_IMAGE_BYTES + 1)],
                         ids=["invalid", "svg", "oversized"])
def test_invalid_or_oversized_images_rejected(data):
    with pytest.raises(ValueError):
        QuestionImage.from_bytes("slide:1", data)


def test_slide_read_validates_owner_content_and_path(questions, tmp_path):
    repo, marker, video = questions
    root = tmp_path / "slides"
    slide_id = add_slide(repo, video, root)
    repo.save_material(marker, QuestionMaterial(slide_ids=[slide_id]))
    snapshot = repo.snapshot(marker)
    images = load_slide_images(repo.store, snapshot, root)
    assert images[0].data == png()
    assert snapshot["question"]["images"][0]["representative_ms"] == 100
    with repo.store.connect() as connection:
        connection.execute("UPDATE video_slides SET image_path = '../escape.png' WHERE id = ?", (slide_id,))
    with pytest.raises(ValueError, match="路径"):
        load_slide_images(repo.store, snapshot, root)
    with repo.store.connect() as connection:
        connection.execute("UPDATE video_slides SET image_path = 'synthetic.png' WHERE id = ?", (slide_id,))
    other = io.BytesIO()
    Image.new("RGB", (100, 60), "red").save(other, format="PNG")
    (root / "synthetic.png").write_bytes(other.getvalue())
    with pytest.raises(ValueError, match="已变化"):
        load_slide_images(repo.store, snapshot, root)


@pytest.mark.parametrize("change", ["material", "context", "subtitles", "slide"])
def test_changed_inputs_invalidate_question_and_bilibili_history(questions, tmp_path, change):
    repo, marker, video = questions
    if change == "slide":
        slide = add_slide(repo, video, tmp_path / "slides")
        repo.save_material(marker, QuestionMaterial(text="题目", slide_ids=[slide]))
    completed(questions)
    bili = BilibiliStore(repo.store)
    run = bili.create_run(bili.snapshot(marker), {})
    run.update(status="ready")
    bili.save_run(run)
    if change == "material":
        repo.save_material(marker, QuestionMaterial(text="修改后的题目"))
    elif change == "context":
        bili.save_context(marker, StudyContext(notes="修改后的要求"))
    elif change == "subtitles":
        with repo.store.connect() as connection:
            connection.execute("UPDATE video_transcript_segments SET text = '修改字幕' WHERE video_id = ?", (video,))
    else:
        with repo.store.connect() as connection:
            connection.execute("UPDATE video_slides SET image_hash = 'updated' WHERE id = ?", (slide,))
    assert repo.runs(marker)[0]["stale"]
    assert bili.runs(marker)[0]["stale"]
    assert bili.snapshot(marker)["question"]["status"] == "needs_analysis"


def test_question_history_survives_restart_and_deletes_with_marker(questions):
    repo, marker, _ = questions
    run = completed(questions)
    reopened = QuestionStore(VideoStore(repo.store.path))
    assert reopened.runs(marker)[0]["id"] == run["id"]
    with repo.store.connect() as connection:
        connection.execute("DELETE FROM video_markers WHERE id = ?", (marker,))
        assert connection.execute("SELECT COUNT(*) FROM question_materials").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM question_analyses").fetchone()[0] == 0


def test_cancel_before_and_after_request_preserves_completed_analysis(questions):
    repo, marker, _ = questions
    run = repo.create_run(repo.snapshot(marker), {})
    with pytest.raises(ProcessingCancelled):
        analyze_question(repo, run, FakeModel(), [], should_cancel=lambda: True)
    assert run["status"] == "interrupted"
    cancelled = False

    class CancellingModel(FakeModel):
        def analyze(self, snapshot, images):
            nonlocal cancelled
            cancelled = True
            return super().analyze(snapshot, images)

    with pytest.raises(ProcessingCancelled):
        analyze_question(repo, run, CancellingModel(), [], should_cancel=lambda: cancelled)
    assert run["status"] == "completed"
    assert repo.runs(marker)[0]["target"] is not None


def test_invalid_response_saved_without_raw_content(questions):
    repo, marker, _ = questions
    value = analysis()
    value["facts"][0]["sources"] = ["private-invalid-reference"]
    run = repo.create_run(repo.snapshot(marker), {})
    with pytest.raises(RuntimeError):
        analyze_question(repo, run, FakeModel(value), [])
    saved = repo.runs(marker)[0]
    assert saved["status"] == "failed" and saved["analysis"] is None
    assert "private-invalid-reference" not in str(saved)


def test_search_planner_cannot_silently_weaken_question_target(questions):
    repo, marker, _ = questions
    completed(questions)
    bili = BilibiliStore(repo.store)
    run = bili.create_run(bili.snapshot(marker), {"model": "fake", "max_tokens": 1024})

    class Planner:
        def ask(self, task, material, schema):
            target = copy.deepcopy(material["question"]["target"])
            target["requirements"][0]["depth"] = "concept"
            return schema(target=target, exact_queries=[{"text": "候选键 例题", "purpose": "例题"}])

    HarnessService(bili, Planner(), SimpleNamespace()).plan(run)
    assert run["target"]["requirements"][0]["depth"] == "example"
