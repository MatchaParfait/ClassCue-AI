"""Deterministic image assignment with synthetic slides; no model requests."""

import importlib
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from api.main import create_app
from classcue.question_captures import QuestionCaptures
from classcue.question_store import QuestionStore
from classcue.question_visuals import (
    IMAGE_ALGORITHM, assign_after_extraction, assign_question_image, choose_question_slide,
)
from tests.test_question_visuals import visual


def slide(id, start, end, preferred=True):
    return {"id": id, "start_ms": start, "end_ms": end, "is_llm_preferred": preferred}


@pytest.mark.parametrize("rows,expected,rule", [
    ([slide(1, 12000, 15000)], 1, "preferred_overlap"),
    ([slide(1, 0, 14000), slide(2, 14000, 20000)], 2, "preferred_overlap"),
    ([slide(1, 0, 12000), slide(2, 15000, 19000)], 2, "preferred_overlap"),
    ([slide(1, 0, 5000), slide(2, 21000, 25000)], 2, "preferred_nearby"),
    ([slide(1, 40000, 45000), slide(2, 10000, 20000, False)], 1, "preferred_nearby"),
    ([slide(1, 40001, 45000), slide(2, 10000, 20000, False)], 2, "timeline_nearest"),
    ([slide(1, 200000, 210000, False)], 1, "timeline_nearest"),
    ([slide(2, 10000, 15000), slide(1, 15000, 20000)], 2, "preferred_overlap"),
    ([], None, "no_slides"),
])
def test_selection_rules(rows, expected, rule):
    selected, chosen_rule = choose_question_slide({"start_ms": 10000, "end_ms": 20000}, rows)
    assert (selected["id"] if selected else None) == expected
    assert chosen_rule == rule


def test_long_lecture_and_adjusted_jump_use_short_location():
    rows = [slide(1, 10000, 30000), slide(2, 30000, 600000)]
    marker = {"start_ms": 10000, "end_ms": 600000}
    assert choose_question_slide(marker, rows)[0]["id"] == 1
    marker["user_start_ms"] = 50000
    assert choose_question_slide(marker, rows)[0]["id"] == 2


def test_assignment_is_cached_durable_and_never_loads_ai(visual, monkeypatch):
    v = visual
    def unexpected(*args, **kwargs):
        pytest.fail("Local image assignment must not use AI settings")
    monkeypatch.setattr("classcue.ai_provider.load_provider_settings", unexpected)
    first = assign_question_image(v.store, v.marker, v.root)
    assert first["algorithm"] == IMAGE_ALGORITHM and first["verdict"] == "uncertain"
    assert first["binding"]["slide_id"] == v.slides[0]["id"]
    assert assign_question_image(v.store, v.marker, v.root)["binding"] == first["binding"]
    assert len(QuestionCaptures(v.store).list(v.marker)) == 1
    v.store.replace_video_slides(v.video, [])
    assert QuestionCaptures(v.store).path(first["binding"]["capture_id"]).is_file()


def test_manual_capture_and_removal_win_over_automatic_assignment(visual):
    v = visual
    original = assign_question_image(v.store, v.marker, v.root)
    capture = QuestionCaptures(v.store).add(v.marker, 250000, v.root.joinpath("1.png").read_bytes())
    manual = v.repo.capture_binding(v.marker, capture["id"])
    assert manual["binding"]["source"] == "capture"
    assert assign_question_image(v.store, v.marker, v.root)["binding"] == manual["binding"]
    assert QuestionStore(v.store).material(v.marker)["capture_ids"] == [capture["id"]]
    restored = v.repo.capture_binding(v.marker, original["binding"]["capture_id"], binding_mode="auto")
    assert restored["binding"]["source"] == "preferred"
    v.repo.capture_binding(v.marker, None)
    assert assign_question_image(v.store, v.marker, v.root)["binding"] is None
    assert QuestionStore(v.store).material(v.marker)["capture_ids"] == []


def test_missing_ppt_waits_and_unreadable_selected_image_keeps_previous(visual):
    v = visual
    first = assign_question_image(v.store, v.marker, v.root)
    v.root.joinpath("0.png").unlink()
    with pytest.raises(OSError):
        assign_question_image(v.store, v.marker, v.root, force=True)
    assert v.repo.get(v.marker)["binding"] == {k: val for k, val in first["binding"].items() if k != "missing"}
    assert assign_after_extraction(v.store, v.video, slide_root=v.root) == {"completed": 1}
    v.store.replace_video_slides(v.video, [])
    assert assign_question_image(v.store, v.marker, v.root)["status"] == "waiting_slides"


def test_local_assignment_api_and_one_click_capture(visual, monkeypatch):
    v = visual
    monkeypatch.setattr(importlib.import_module("api.main").asr_storage, "slide_directory", lambda *_: v.root)
    with TestClient(create_app(v.store)) as client:
        result = client.post(f"/api/videos/{v.video}/question-images/assign")
        assert result.status_code == 200
        original = result.json()["items"][0]["binding"]["capture_id"]
        data = io.BytesIO()
        Image.new("RGB", (120, 80), "red").save(data, format="PNG")
        url = f"/api/markers/{v.marker}/question"
        response = client.post(url + "/captures?captured_at_ms=250000&as_question_image=true",
                               content=data.getvalue(), headers={"content-type": "image/png"})
        assert response.status_code == 201
        value = response.json()
        assert value["previous_image"] == {"capture_id": original, "binding_mode": "auto"}
        assert value["visual"]["binding"]["source"] == "capture"
        assert value["visual"]["binding"]["representative_ms"] == 250000
        assert v.store.list_markers(v.video)[0]["start_ms"] == 10000
        undo = client.put(url + "/visual/capture-binding", json=value["previous_image"])
        assert undo.status_code == 200 and undo.json()["binding"]["capture_id"] == original
        assert client.put(url + "/visual/capture-binding", json={"capture_id": "0" * 32}).status_code == 404
        assert client.post(url + "/visual/verify").status_code == 503
