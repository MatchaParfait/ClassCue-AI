from __future__ import annotations

import importlib
import threading
import time

import pytest
import httpx
from fastapi.testclient import TestClient

from api.main import create_app
from classcue.ai_provider import ProviderProfile, ProviderSettings
from classcue.bilibili_store import BilibiliStore
from classcue.bilibili import BilibiliAccount
from classcue.bilibili_login import BilibiliQrLogin
from classcue.bilibili_study import Verdict
from classcue.core import TranscriptSegment
from classcue.video_store import VideoStore
from tests.test_bilibili_login import PassportNetwork


BV = "BV1xx411c7mD"


class Model:
    def __init__(self, *_):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def plan(self, snapshot):
        return [{"text": "数学 单调性 例题", "purpose": "分类讨论"}]

    def select(self, snapshot, candidates):
        return [item["id"] for item in candidates[:5]]

    def check(self, snapshot, unit, chunk):
        return Verdict(verdict="partial", reason="字幕涉及分类讨论", segment_ids=[1])

    def ask(self, task, material, schema):
        if schema.__name__ == "Plan":
            return schema.model_validate({"target": {"course": "数学", "requirements": [
                {"id": "r1", "text": "单调性分类讨论", "required": True, "depth": "concept", "sources": ["marker.title"]}],
                "keywords": ["单调性"]}, "exact_queries": self.plan(material),
                "course_queries": [{"text": "高等数学课程", "purpose": "课程补搜"}]})
        if schema.__name__ == "Ranking":
            return schema(items=[{"id": c["id"], "reason": "合成选择"} for c in material["candidates"]])
        if schema.__name__ == "EvidenceExtraction":
            return schema(findings=[{"requirement_id": "r1", "depth": "concept", "kind": "support",
                                     "reason": "合成解释", "segment_ids": [1]}])
        if schema.__name__ == "EvidenceAssessment":
            return schema(reason="合成核验", assessments=[{"requirement_id": "r1", "status": "supported",
                          "depth": "concept", "reason": "解释了知识点", "evidence_ids": [f["id"] for f in material["findings"]]}])
        return schema(queries=[], reason="没有补充搜索词")


class Network:
    account_scope = "anonymous"
    calls = 0

    def __init__(self, *_args, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def search(self, _):
        Network.calls += 1
        return [{"bvid": BV, "title": "合成例题", "author": "合成作者", "description": "分类讨论",
                 "url": f"https://www.bilibili.com/video/{BV}/?p=1"}]

    def metadata(self, bvid):
        return {"page_count": 1, "pages": [{"bvid": bvid, "page": 1, "cid": 123, "part": "例题",
                                           "duration_ms": 10000, "url": f"https://www.bilibili.com/video/{bvid}/?p=1"}]}

    def subtitles(self, unit):
        return {"status": "available", "language": "zh-CN", "automatic": False,
                "segments": [{"id": 1, "start_ms": 1500, "end_ms": 2500, "text": "单调性分类讨论"}]}


@pytest.fixture
def client_data(tmp_path, monkeypatch):
    module = importlib.import_module("api.bilibili")
    settings = ProviderSettings(profile=ProviderProfile(base_url="https://model.example/v1", model="synthetic"))
    monkeypatch.setattr(module, "load_provider_settings", lambda: settings)
    monkeypatch.setattr(module, "resolve_interface_key", lambda *_: "synthetic-key")
    monkeypatch.setattr(module, "StudyModel", Model)
    monkeypatch.setattr(module, "BilibiliClient", Network)
    passport = PassportNetwork()
    monkeypatch.setattr(module, "BilibiliQrLogin", lambda account: BilibiliQrLogin(
        account, transport=httpx.MockTransport(passport.handle), clock=lambda: passport.now,
    ))
    Network.calls = 0
    store = VideoStore(tmp_path / "library.db")
    video = store.create_video("synthetic", "lesson.mp4", "lesson.mp4", "lesson.wav", 10000)
    store.mark_transcribed(video, [TranscriptSegment(1, 0, 5000, "重点是单调性")])
    marker = store.add_marker(video, 0, 5000, "单调性", "分类讨论", "重点是单调性", "重点", .8)
    other = store.add_marker(video, 5000, 9000, "概率", "条件", "概率", "重点", .8)
    with TestClient(create_app(store)) as client:
        client.app.state.passport_fixture = passport
        yield client, store, marker, other, settings


def wait(client, job_id):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = client.get("/api/jobs/" + job_id).json()
        if result["status"] not in {"queued", "running"}:
            return result
        time.sleep(.02)
    pytest.fail("Synthetic Bilibili job did not complete")


def plan(client, marker):
    result = client.post(f"/api/markers/{marker}/bilibili/plan", json={"consent": True})
    assert result.status_code == 202
    assert wait(client, result.json()["job_id"])["status"] == "completed"
    return client.get(f"/api/markers/{marker}/bilibili").json()["runs"][0]


def test_full_flow_is_marker_scoped_and_planning_never_visits_bilibili(client_data):
    client, store, marker, other, _ = client_data
    original = dict(store.list_markers(1)[0])
    context = {"course": "数学", "notes": "分类讨论"}
    assert client.put(f"/api/markers/{marker}/bilibili/context", json=context).json() == context
    assert client.get(f"/api/markers/{other}/bilibili").json()["context"]["course"] == ""
    source = plan(client, marker)
    assert Network.calls == 0
    assert source["queries"][0]["text"] == "数学 单调性 例题"
    started = client.post(f"/api/markers/{marker}/bilibili/search", json={
        "run_id": source["id"], "queries": source["queries"], "consent": True,
    })
    assert started.status_code == 202
    assert wait(client, started.json()["job_id"])["status"] == "completed"
    runs = client.get(f"/api/markers/{marker}/bilibili").json()["runs"]
    assert runs[0]["candidates"][0]["units"][0]["evidence"][0]["start_ms"] == 1500
    assert len(runs) == 2
    assert dict(store.list_markers(1)[0]) == original
    assert client.get(f"/api/markers/{other}/bilibili").json()["runs"] == []
    assert "synthetic-key" not in str(runs)


def test_consent_invalid_marker_and_cross_marker_run_rejected(client_data):
    client, _, marker, other, _ = client_data
    assert client.post(f"/api/markers/{marker}/bilibili/plan", json={"consent": False}).status_code == 400
    assert client.get("/api/markers/999/bilibili").status_code == 404
    source = plan(client, marker)
    assert client.post(f"/api/markers/{other}/bilibili/search", json={
        "run_id": source["id"], "queries": source["queries"], "consent": True,
    }).status_code == 404
    assert client.post(f"/api/markers/{marker}/bilibili/search", json={
        "run_id": source["id"], "queries": [], "consent": True,
    }).status_code == 422
    assert client.post(f"/api/markers/{marker}/bilibili/search", json={
        "run_id": source["id"], "queries": source["queries"], "consent": False,
    }).status_code == 400


def test_stale_inputs_or_model_prevent_search(client_data):
    client, _, marker, _, settings = client_data
    source = plan(client, marker)
    request = {"run_id": source["id"], "queries": source["queries"], "consent": True}
    settings.profile.model = "changed"
    assert client.post(f"/api/markers/{marker}/bilibili/search", json=request).status_code == 409
    settings.profile.model = "synthetic"
    client.put(f"/api/markers/{marker}/bilibili/context", json={"notes": "更新"})
    assert client.get(f"/api/markers/{marker}/bilibili").json()["runs"][0]["stale"]
    assert client.post(f"/api/markers/{marker}/bilibili/search", json=request).status_code == 409
    assert Network.calls == 0


def test_pause_needs_explicit_acknowledgement_and_blocks_search(client_data):
    client, store, marker, _, _ = client_data
    source = plan(client, marker)
    BilibiliStore(store).set_control("platform_pause", {"reason": "http_412", "at": 1})
    assert client.post(f"/api/markers/{marker}/bilibili/search", json={
        "run_id": source["id"], "queries": source["queries"], "consent": True,
    }).status_code == 409
    assert client.post("/api/bilibili/resume", json={"acknowledge_account_risk": False}).status_code == 400
    assert client.get("/api/bilibili/account").json()["pause"]
    assert client.post("/api/bilibili/resume", json={"acknowledge_account_risk": True}).json()["pause"] is None


def test_manual_cookie_endpoint_is_removed_without_echoing_credentials(client_data):
    client, _, _, _, _ = client_data
    secret = "synthetic-sensitive-cookie"
    for payload in [{"cookie": secret}, {"cookie": secret, "extra": secret}, {"cookie": [secret]}]:
        result = client.put("/api/bilibili/account", json=payload)
        assert result.status_code == 405
        assert secret not in result.text
    assert client.get("/api/bilibili/account").json()["configured"] is False


def test_encrypted_cookie_is_not_in_database_or_api(client_data):
    client, store, _, _, _ = client_data
    passport = client.app.state.passport_fixture
    passport.codes = [0]
    started = client.post("/api/bilibili/login/start")
    assert started.status_code == 200
    assert started.headers["cache-control"] == "no-store"
    result = client.post("/api/bilibili/login/poll", json={"session_id": started.json()["session_id"]})
    assert result.status_code == 200
    assert result.json()["status"] == "success"
    assert client.get("/api/bilibili/account").json() == {"configured": True, "pause": None}
    assert "synthetic-private-session" not in result.text
    assert b"synthetic-private-session" not in (store.path.parent / "bilibili_cookie.dpapi").read_bytes()
    with store.connect() as connection:
        assert "synthetic-private-session" not in "\n".join(connection.iterdump())
    assert client.delete("/api/bilibili/account").json()["configured"] is False


def test_mutual_exclusion_uses_existing_job_manager(client_data):
    client, store, marker, _, _ = client_data
    store.create_job("busy", "slide_extract", 1)
    assert client.post(f"/api/markers/{marker}/bilibili/plan", json={"consent": True}).status_code == 409
    assert client.post("/api/bilibili/login/start").status_code == 409
    assert client.put(f"/api/markers/{marker}/bilibili/context", json={"notes": "x"}).status_code == 409
    store.update_job("busy", status="completed")


def test_login_rejects_cross_site_and_invalid_sessions_without_requests(client_data):
    client, _, _, _, _ = client_data
    passport = client.app.state.passport_fixture
    assert client.post("/api/bilibili/login/start", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/bilibili/login/start", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.post("/api/bilibili/login/poll", json={"session_id": "invalid"}).status_code == 422
    assert passport.calls == []
    assert client.post("/api/bilibili/login/poll", json={"session_id": "a" * 32}).status_code == 404


def test_login_cancel_prevents_completion_and_logout_cancels_pending_qr(client_data):
    client, store, _, _, _ = client_data
    passport = client.app.state.passport_fixture
    session = client.post("/api/bilibili/login/start").json()
    payload = {"session_id": session["session_id"]}
    assert client.post("/api/bilibili/login/cancel", json=payload).json()["status"] == "cancelled"
    assert client.post("/api/bilibili/login/poll", json=payload).json()["status"] == "cancelled"
    assert not BilibiliAccount(store.path.parent).path.exists()
    passport.now += 6
    session = client.post("/api/bilibili/login/start").json()
    client.delete("/api/bilibili/account")
    assert client.post(
        "/api/bilibili/login/poll", json={"session_id": session["session_id"]},
    ).json()["status"] == "cancelled"
    assert len(passport.calls) == 2


def completed_search(client, marker):
    source = plan(client, marker)
    started = client.post(f"/api/markers/{marker}/bilibili/search", json={
        "run_id": source["id"], "queries": source["queries"], "course_queries": source["course_queries"],
        "target": source["target"], "consent": True,
    })
    assert started.status_code == 202
    assert wait(client, started.json()["job_id"])["status"] == "completed"
    return started.json()["run_id"]


def test_slide_evidence_and_bilibili_results_coexist_after_reopen(client_data):
    client, store, marker, _, _ = client_data
    slide = {
        "start_ms": 0, "end_ms": 5000, "representative_ms": 2000,
        "image_path": "synthetic.jpg", "image_hash": "synthetic-slide",
        "duration_ms": 5000, "retention_class": "primary", "confidence": 1.0,
        "roi_json": "{}", "extraction_version": "synthetic",
    }
    store.replace_video_slides(1, [slide])
    before = client.get("/api/videos/1/slides").json()
    assert len(before) == 1
    run_id = completed_search(client, marker)
    assert client.get("/api/videos/1/slides").json() == before
    visual = client.get(
        "/api/videos/1/visual-evidence?start_ms=0&end_ms=5000&context_ms=0",
    ).json()
    assert visual["slides"] == before
    assert len(visual["subtitles"]) == 1
    reopened = VideoStore(store.path)
    assert len(reopened.list_slides(1)) == 1
    assert BilibiliStore(reopened).get_run(marker, run_id)["stop_reason"] == "success"
    assert not BilibiliStore(reopened).runs(marker)[0]["stale"]
    with store.connect() as connection:
        connection.execute("DELETE FROM videos WHERE id=1")
        for table in ("video_slides", "bili_study_runs", "bili_run_trace"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_real_slide_and_bilibili_jobs_share_the_same_execution_lock(client_data, monkeypatch):
    client, _, marker, _, _ = client_data
    entered, release = threading.Event(), threading.Event()
    original_ask = Model.ask

    def blocked_ask(self, task, material, schema):
        if schema.__name__ == "Plan":
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Synthetic model timeout")
        return original_ask(self, task, material, schema)

    monkeypatch.setattr(Model, "ask", blocked_ask)
    started = client.post(f"/api/markers/{marker}/bilibili/plan", json={"consent": True})
    assert started.status_code == 202
    try:
        assert entered.wait(3)
        assert client.post("/api/videos/1/slides/extract").status_code == 409
    finally:
        release.set()
        result = wait(client, started.json()["job_id"])
    assert result["status"] == "completed"

    entered.clear()
    release.clear()

    def blocked_extract(*_args, **_kwargs):
        entered.set()
        if not release.wait(5):
            raise RuntimeError("Synthetic extraction timeout")
        return {"message": "Synthetic slide extraction"}

    monkeypatch.setattr(importlib.import_module("api.main"), "extract_and_store_slides", blocked_extract)
    started = client.post("/api/videos/1/slides/extract")
    assert started.status_code == 202
    try:
        assert entered.wait(3)
        assert client.post(
            f"/api/markers/{marker}/bilibili/plan", json={"consent": True},
        ).status_code == 409
    finally:
        release.set()
        result = wait(client, started.json()["job_id"])
    assert result["status"] == "completed"


def test_continue_keeps_identity_budget_and_completed_evidence(client_data):
    client, store, marker, _, _ = client_data
    run_id = completed_search(client, marker)
    repo = BilibiliStore(store)
    run = repo.get_run(marker, run_id)
    before = dict(run["budget"])
    run["status"] = "interrupted"
    repo.save_run(run)
    started = client.post(f"/api/markers/{marker}/bilibili/continue", json={"run_id": run_id, "consent": True})
    assert started.status_code == 202
    assert started.json()["run_id"] == run_id
    assert wait(client, started.json()["job_id"])["status"] == "completed"
    saved = repo.get_run(marker, run_id)
    assert saved["stop_reason"] == "success"
    assert saved["budget"]["llm"] == before["llm"]
    assert saved["budget"]["verified"] == before["verified"]
    assert len(repo.runs(marker)) == 2
    assert "subtitle" not in repo.runs(marker)[0]["candidates"][0]["units"][0]


def test_continuation_requires_query_and_budget_confirmation(client_data):
    client, store, marker, _, _ = client_data
    run_id = completed_search(client, marker)
    repo = BilibiliStore(store)
    run = repo.get_run(marker, run_id)
    run.update(status="awaiting_approval", phase="course", pending_queries=[{"text": "新课程", "purpose": "补搜"}])
    repo.save_run(run)
    endpoint = f"/api/markers/{marker}/bilibili/continue"
    assert client.post(endpoint, json={"run_id": run_id, "consent": True}).status_code == 400
    assert client.post(endpoint, json={"run_id": run_id, "consent": False}).status_code == 400
    run.update(status="budget_exhausted")
    repo.save_run(run)
    assert client.post(endpoint, json={"run_id": run_id, "consent": True}).status_code == 400
    response = client.post(endpoint, json={"run_id": run_id, "consent": True, "increase_budget": True})
    assert response.status_code == 202
    wait(client, response.json()["job_id"])
    saved = repo.get_run(marker, run_id)
    assert saved["limits"]["llm"] == 96
    assert saved["budget"]["llm"] >= run["budget"]["llm"]
    saved["status"] = "budget_exhausted"
    repo.save_run(saved)
    assert client.post(endpoint, json={"run_id": run_id, "consent": True, "increase_budget": True}).status_code == 409


def test_continue_rejects_other_marker_changed_account_and_changed_input(client_data):
    client, store, marker, other, _ = client_data
    run_id = completed_search(client, marker)
    repo = BilibiliStore(store)
    run = repo.get_run(marker, run_id)
    run["status"] = "interrupted"
    repo.save_run(run)
    payload = {"run_id": run_id, "consent": True}
    assert client.post(f"/api/markers/{other}/bilibili/continue", json=payload).status_code == 404
    run["account_scope"] = "different-account"
    repo.save_run(run)
    assert client.post(f"/api/markers/{marker}/bilibili/continue", json=payload).status_code == 409
    run["account_scope"] = "anonymous"
    repo.save_run(run)
    client.put(f"/api/markers/{marker}/bilibili/context", json={"notes": "改变目标"})
    assert client.post(f"/api/markers/{marker}/bilibili/continue", json=payload).status_code == 409


def test_trace_survives_reopen_and_video_deletion_cleans_runs(client_data):
    client, store, marker, _, _ = client_data
    run_id = completed_search(client, marker)
    reopened = BilibiliStore(VideoStore(store.path))
    assert reopened.get_run(marker, run_id)["stop_reason"] == "success"
    with store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM bili_run_trace WHERE run_id=?", (run_id,)).fetchone()[0] > 0
        connection.execute("DELETE FROM videos WHERE id=1")
        assert connection.execute("SELECT count(*) FROM bili_run_trace").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM bili_study_runs").fetchone()[0] == 0


def test_invalid_target_and_legacy_plan_are_rejected_without_new_run(client_data):
    client, store, marker, _, _ = client_data
    source = plan(client, marker)
    target = source["target"]
    target["requirements"][0]["sources"] = ["classroom:999999"]
    payload = {"run_id": source["id"], "queries": source["queries"], "target": target, "consent": True}
    assert client.post(f"/api/markers/{marker}/bilibili/search", json=payload).status_code == 400
    assert len(BilibiliStore(store).runs(marker)) == 1
    source["version"] = "bilibili-study-v1"
    BilibiliStore(store).save_run(source)
    assert client.post(f"/api/markers/{marker}/bilibili/search", json=payload).status_code == 409


def test_orphaned_active_run_can_continue_after_restart(client_data):
    client, store, marker, _, _ = client_data
    run_id = completed_search(client, marker)
    repo = BilibiliStore(store)
    run = repo.get_run(marker, run_id)
    run.update(status="checking", job_id="missing-after-restart")
    repo.save_run(run)
    assert repo.runs(marker)[0]["status"] == "interrupted"
    response = client.post(f"/api/markers/{marker}/bilibili/continue", json={"run_id": run_id, "consent": True})
    assert response.status_code == 202
    assert wait(client, response.json()["job_id"])["status"] == "completed"
