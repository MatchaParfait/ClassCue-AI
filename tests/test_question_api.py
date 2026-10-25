from __future__ import annotations

import importlib
import threading

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from classcue.ai_provider import ProviderProfile, ProviderSettings
from classcue.question_analysis import QuestionMaterial
from tests.test_bilibili_api import Model as BiliModel, Network, wait
from tests.test_question_analysis import FakeModel, add_slide, questions
from tests.bilibili_visual_fakes import FakeFrameReader


@pytest.fixture
def api(questions, monkeypatch):
    repo, marker, video = questions
    module = importlib.import_module("api.questions")
    settings = ProviderSettings(profile=ProviderProfile(model="fake-vision", base_url="https://model.example/v1"))
    monkeypatch.setattr(module, "load_provider_settings", lambda: settings)
    monkeypatch.setattr(module, "resolve_interface_key", lambda *_: "synthetic-key")

    class Model(FakeModel):
        calls = 0

        def __init__(self, *_args):
            super().__init__()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def analyze(self, snapshot, images):
            Model.calls += 1
            return super().analyze(snapshot, images)

    monkeypatch.setattr(module, "QuestionModel", Model)
    with TestClient(create_app(repo.store)) as client:
        yield client, repo, marker, video, Model


def test_material_read_write_and_analysis_are_marker_scoped(api):
    client, repo, marker, video, model = api
    url = f"/api/markers/{marker}/question"
    assert client.get(url).status_code == 200
    assert model.calls == 0
    other = repo.store.add_marker(video, 5000, 9000, "其他考点", "", "", "重点", .7)
    assert client.put(url, json={"text": "已知关系模式和依赖，求候选键", "slide_ids": []}).status_code == 200
    assert client.get(f"/api/markers/{other}/question").json()["material"]["text"] == ""
    started = client.post(url + "/analyze")
    assert started.status_code == 202
    assert wait(client, started.json()["job_id"])["status"] == "completed"
    state = client.get(url).json()
    assert state["runs"][0]["target"]["requirements"][0]["depth"] == "example"
    assert "synthetic-key" not in str(state)
    assert client.get(f"/api/markers/{other}/question").json()["runs"] == []


def test_invalid_inputs_and_cross_video_slide_rejected_without_model(api, tmp_path):
    client, repo, marker, _, model = api
    url = f"/api/markers/{marker}/question"
    other = repo.store.create_video("other", "other.mp4", "other.mp4", "other.wav", 10000)
    slide = add_slide(repo, other, tmp_path / "slides")
    assert client.put(url, json={"slide_ids": [slide]}).status_code == 400
    for payload in [{"slide_ids": [True]}, {"slide_ids": [1, 1]}, {"path": "private.png"}, {"text": "x" * 12001}]:
        assert client.put(url, json=payload).status_code == 422
    assert client.get("/api/markers/999/question").status_code == 404
    assert model.calls == 0


def test_question_analysis_uses_existing_global_job_lock(api):
    client, repo, marker, video, model = api
    release, entered = threading.Event(), threading.Event()

    def operation(*_):
        entered.set()
        assert release.wait(5)

    job = client.app.state.jobs.submit("synthetic", video, operation)
    try:
        assert entered.wait(3)
        assert client.post(f"/api/markers/{marker}/question/analyze").status_code == 409
        assert client.put(f"/api/markers/{marker}/question", json={"text": "new"}).status_code == 409
        assert model.calls == 0
    finally:
        release.set()
        wait(client, job)


def test_search_rejects_unanalyzed_question_before_model(api):
    client, repo, marker, _, model = api
    assert client.post(f"/api/markers/{marker}/bilibili/plan", json={"consent": True}).status_code == 409
    assert model.calls == 0


def test_missing_selected_image_does_not_submit_job(api, tmp_path):
    client, repo, marker, video, model = api
    slide = add_slide(repo, video, tmp_path / "different-root")
    repo.save_material(marker, QuestionMaterial(slide_ids=[slide]))
    result = client.post(f"/api/markers/{marker}/question/analyze")
    assert result.status_code == 400
    assert "图片" in result.json()["detail"]
    assert model.calls == 0 and repo.runs(marker) == []


def test_question_target_flows_through_search_to_cited_example(api, monkeypatch):
    client, repo, marker, _, _ = api
    module = importlib.import_module("api.bilibili")
    settings = ProviderSettings(profile=ProviderProfile(model="fake", base_url="https://model.example/v1"))
    monkeypatch.setattr(module, "load_provider_settings", lambda: settings)
    monkeypatch.setattr(module, "resolve_interface_key", lambda *_: "synthetic-key")

    class SearchModel(BiliModel):
        def ask(self, task, material, schema):
            if schema.__name__ == "Plan":
                return schema(target=material["question"]["target"],
                              exact_queries=[{"text": "候选键 例题", "purpose": "寻找同类题"}])
            if schema.__name__ == "EvidenceExtraction":
                assert material["target"]["comparison"]["tasks"] == ["求候选键"]
                return schema(findings=[{"requirement_id": "r1", "kind": "support", "depth": "example",
                                         "reason": "同题型实例", "segment_ids": [1, 2, 3]}])
            if schema.__name__ == "EvidenceAssessment":
                return schema(reason="例题覆盖目标操作", assessments=[{
                    "requirement_id": "r1", "status": "supported", "depth": "example", "reason": "有条件及步骤",
                    "evidence_ids": [row["id"] for row in material["findings"]],
                    "same_example": True, "condition_ids": [1], "method_ids": [2], "step_ids": [3],
                }])
            return super().ask(task, material, schema)

    class Subtitles(Network):
        def subtitles(self, unit):
            return {"status": "available", "language": "zh-CN", "automatic": False, "segments": [
                {"id": i, "text": text, "start_ms": i * 1000, "end_ms": (i + 1) * 1000}
                for i, text in enumerate(["给定关系模式与依赖", "下面计算属性闭包", "据此求得候选键"], 1)
            ]}

    monkeypatch.setattr(module, "StudyModel", SearchModel)
    monkeypatch.setattr(module, "BilibiliClient", Subtitles)
    monkeypatch.setattr(module, "BrowserFrameReader", FakeFrameReader)
    monkeypatch.setattr(
        "classcue.bilibili_frames.library_scope", lambda _project, database: database.parent / "cache"
    )
    analyzed = client.post(f"/api/markers/{marker}/question/analyze").json()
    assert wait(client, analyzed["job_id"])["status"] == "completed"
    planned = client.post(f"/api/markers/{marker}/bilibili/plan", json={"consent": True}).json()
    assert wait(client, planned["job_id"])["status"] == "completed"
    plan = client.get(f"/api/markers/{marker}/bilibili").json()["runs"][0]
    started = client.post(f"/api/markers/{marker}/bilibili/search", json={
        "consent": True, "run_id": plan["id"], "queries": plan["queries"],
    }).json()
    assert wait(client, started["job_id"])["status"] == "completed"
    result = client.get(f"/api/markers/{marker}/bilibili").json()["runs"][0]
    assert result["input"]["question"]["analysis_id"] == analyzed["analysis_id"]
    unit = result["candidates"][0]["units"][0]
    assert unit["verdict"] == "covers"
    assert unit["evidence"][0]["start_ms"] == 1000
    assert unit["evidence"][0]["url"].endswith("p=1&t=1")
    assert result["target"]["comparison"]["allowed_variations"]
