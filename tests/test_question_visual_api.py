import importlib
import threading

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from classcue.ai_provider import ProviderProfile, ProviderSettings
from classcue.question_visuals import question_marker, source_snapshot
from tests.test_bilibili_api import wait
from tests.test_question_visuals import Model, visual


@pytest.fixture
def api(visual, monkeypatch):
    monkeypatch.setattr("classcue.question_visuals.LLM_VISUAL_REVIEW_ENABLED", True)
    module = importlib.import_module("api.questions")
    main = importlib.import_module("api.main")
    settings = ProviderSettings(profile=ProviderProfile(model="fake-vision", base_url="https://model.example/v1"))
    monkeypatch.setattr(module, "load_provider_settings", lambda: settings)
    monkeypatch.setattr(module, "resolve_interface_key", lambda *_: "synthetic")
    monkeypatch.setattr(main.asr_storage, "slide_directory", lambda *_: visual.root)
    created = []
    def factory(*args):
        model = Model()
        created.append(model)
        return model
    monkeypatch.setattr(module, "VisualQuestionModel", factory)
    with TestClient(create_app(visual.store)) as client:
        yield client, visual, created


def test_viewing_never_calls_ai_but_individual_verify_does(api):
    client, v, created = api
    url = f"/api/markers/{v.marker}/question/visual"
    assert client.get(url).json()["status"] == "unchecked"
    assert client.get(url).json()["location"] == {
        "start_ms": 10000, "end_ms": 20000, "source": "question_start",
    }
    listing = client.get(f"/api/videos/{v.video}/question-visuals").json()
    assert listing["items"][0]["marker_id"] == v.marker and not listing["active"]
    assert not created
    submitted = client.post(url + "/verify")
    assert submitted.status_code == 202
    assert wait(client, submitted.json()["job_id"])["status"] == "completed"
    result = client.get(url).json()
    assert result["verdict"] == "confirmed"
    assert len(created) == 1
    assert client.get(f"/api/markers/{v.marker}/question/captures/{result['binding']['capture_id']}").status_code == 200
    assert "synthetic" not in str(result.get("profile", {}))
    assert "attempts" not in client.get(f"/api/videos/{v.video}/question-visuals").json()["items"][0]
    assert len(created) == 1


def test_manual_bind_unbind_and_validation_are_free(api):
    client, v, created = api
    url = f"/api/markers/{v.marker}/question/visual/binding"
    response = client.put(url, json={"slide_id": v.slides[1]["id"]})
    assert response.status_code == 200
    assert response.json()["binding_mode"] == "manual" and response.json()["verdict"] != "confirmed"
    assert client.put(url, json={"slide_id": None}).json()["binding"] is None
    assert client.put(url, json={"slide_id": 999}).status_code == 400
    assert client.put(url, json={"slide_id": True}).status_code == 422
    assert not created


def test_busy_jobs_block_writes_and_interrupted_checks_can_retry(api):
    client, v, created = api
    release = threading.Event()
    job = client.app.state.jobs.submit("synthetic", v.video, lambda *_: release.wait(5))
    url = f"/api/markers/{v.marker}/question/visual"
    try:
        assert client.post(url + "/verify").status_code == 409
        assert client.put(url + "/binding", json={"slide_id": None}).status_code == 409
        assert client.get(url).status_code == 200
    finally:
        release.set()
        wait(client, job)
    marker = question_marker(v.store, v.marker)
    v.repo.save(marker, {"question_id": "stable", "status": "checking", "binding": None,
                         "input_hash": "old", "attempts": [], "reason": ""})
    assert client.get(url).json()["status"] == "interrupted"
    assert not created
    assert client.post(url + "/verify").status_code == 202


def test_ppt_extraction_never_starts_visual_review(api, monkeypatch):
    client, v, _ = api
    main = importlib.import_module("api.main")
    calls = []
    monkeypatch.setattr(main, "extract_and_store_slides", lambda *args, **kwargs: {"slides": 3})
    def automatic(store, video_id, **kwargs):
        calls.append(video_id)
    monkeypatch.setattr("classcue.question_visuals.verify_after_extraction", automatic)
    result = client.post(f"/api/videos/{v.video}/slides/extract", json={})
    assert result.status_code == 202
    assert wait(client, result.json()["job_id"])["status"] == "completed"
    assert calls == []


def test_disabled_review_rejects_old_clients_and_preserves_saved_images(api, monkeypatch):
    client, v, created = api
    url = f"/api/markers/{v.marker}/question/visual"
    assert client.put(url + "/binding", json={"slide_id": v.slides[0]["id"]}).status_code == 200
    before = v.repo.get(v.marker)
    monkeypatch.setattr("classcue.question_visuals.LLM_VISUAL_REVIEW_ENABLED", False)
    def unexpected():
        pytest.fail("Disabled API must not load credentials")
    monkeypatch.setattr("api.questions.load_provider_settings", unexpected)
    response = client.post(url + "/verify")
    assert response.status_code == 503
    assert "暂时停用" in response.json()["detail"]
    assert not client.app.state.jobs.is_running()
    assert not created and v.repo.get(v.marker) == before
    result = client.get(url).json()
    assert client.get(f"{url.rsplit('/visual', 1)[0]}/captures/{result['binding']['capture_id']}").status_code == 200


def test_missing_marker_mode_or_slides_rejected(api):
    client, v, created = api
    assert client.get("/api/markers/999/question/visual").status_code == 404
    point = v.store.add_marker(v.video, 100, 200, "普通考点", "", "", "", 1)
    assert client.get(f"/api/markers/{point}/question/visual").status_code == 400
    v.store.replace_video_slides(v.video, [])
    assert client.post(f"/api/markers/{v.marker}/question/visual/verify").status_code == 400
    assert not created
