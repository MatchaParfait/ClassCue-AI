from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from classcue.bilibili import BilibiliClient, BilibiliError, normalize_cookie, wbi_sign
from classcue.bilibili_store import BilibiliStore, StudyContext, digest
from classcue.bilibili_study import (
    SearchPlan, StudyModel, StudyResponseError, StudyService, Verdict, subtitle_chunks, validate_verdict,
)
from classcue.core import TranscriptSegment
from classcue.processing_control import ProcessingCancelled
from classcue.video_store import VideoStore


BV = "BV1xx411c7mD"
BV2 = "BV1yy411c7mD"
KEYS = {"img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
        "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png"}


@pytest.fixture
def study(tmp_path):
    store = VideoStore(tmp_path / "library.db")
    video_id = store.create_video("synthetic", "lesson.mp4", "lesson.mp4", "lesson.wav", 100000)
    store.mark_transcribed(video_id, [
        TranscriptSegment(1, 0, 10000, "这类题先判断单调性"),
        TranscriptSegment(2, 10000, 30000, "单调性容易出错，需要分类讨论"),
        TranscriptSegment(3, 80000, 90000, "无关章节"),
    ])
    marker_id = store.add_marker(video_id, 10000, 30000, "单调性", "分类讨论", "容易出错", "重点", .8)
    return BilibiliStore(store), marker_id, video_id


def unit():
    return {"id": BV + ":1", "bvid": BV, "page": 1, "cid": 100,
            "duration_ms": 100000, "part": "例题", "url": f"https://www.bilibili.com/video/{BV}/?p=1"}


@pytest.mark.parametrize("raw,error_type", [
    ("not valid json", "invalid_json"),
    ('{"queries":[]}', "too_short"),
])
def test_model_format_feedback_is_private_and_does_not_change_public_error(raw, error_type):
    model = object.__new__(StudyModel)
    model.adapter = SimpleNamespace(
        profile=SimpleNamespace(max_tokens=1024), _request_content=lambda *_: raw,
    )
    with pytest.raises(StudyResponseError) as caught:
        model.ask("合成请求", {}, SearchPlan)
    assert caught.value.feedback["errors"][0]["type"] == error_type
    assert caught.value.feedback["previous_response"] == raw
    assert raw not in str(caught.value)


def response(data, code=0):
    return httpx.Response(200, json={"code": code, "data": data})


def test_context_snapshot_and_stale_results(study):
    repo, marker, video = study
    repo.save_context(marker, StudyContext(course="数学", notes="参数取值"))
    snapshot = repo.snapshot(marker)
    assert len(snapshot["classroom_segments"]) == 2
    assert snapshot["context"]["course"] == "数学"
    run = repo.create_run(snapshot, {"model": "fake"})
    run["status"] = "ready"
    repo.save_run(run)
    assert not repo.runs(marker)[0]["stale"]
    repo.save_context(marker, StudyContext(course="物理"))
    assert repo.runs(marker)[0]["stale"]
    repo.save_context(marker, StudyContext(course="数学", notes="参数取值"))
    with repo.store.connect() as connection:
        connection.execute("UPDATE video_transcript_segments SET text = '更新' WHERE video_id = ?", (video,))
    assert repo.runs(marker)[0]["stale"]
    assert BilibiliStore(VideoStore(repo.store.path)).runs(marker)[0]["id"] == run["id"]


def test_retired_context_fields_are_not_used_in_new_snapshots(study):
    repo, marker, _ = study
    legacy = {"course": "数学", "level": "大学", "textbook": "旧版教材", "notes": "需要完整例题"}
    with repo.store.connect() as connection:
        connection.execute(
            "INSERT INTO bili_marker_context(marker_id, payload_json) VALUES (?, ?)",
            (marker, json.dumps(legacy)),
        )
    expected = {"course": "数学", "notes": "需要完整例题"}
    assert repo.context(marker) == expected
    assert repo.snapshot(marker)["context"] == expected
    assert BilibiliStore(VideoStore(repo.store.path)).context(marker) == expected
    assert repo.save_context(marker, StudyContext(course="数据库", notes="范式分解")) == {
        "course": "数据库", "notes": "范式分解",
    }


def test_deletion_never_migrates_results_to_same_title(study):
    repo, marker, video = study
    old = repo.create_run(repo.snapshot(marker), {})
    with repo.store.connect() as connection:
        connection.execute("DELETE FROM video_markers WHERE id = ?", (marker,))
    new_id = repo.store.add_marker(video, 10000, 30000, "单调性", "", "", "重点", .8)
    assert repo.runs(new_id) == []
    with repo.store.connect() as connection:
        assert connection.execute("SELECT marker_id FROM bili_study_runs WHERE id = ?", (old["id"],)).fetchone()[0] is None
        connection.execute("DELETE FROM videos WHERE id = ?", (video,))
        assert connection.execute("SELECT COUNT(*) FROM bili_study_runs").fetchone()[0] == 0


def test_orphan_jobs_are_reported_interrupted(study):
    repo, marker, _ = study
    repo.create_run(repo.snapshot(marker), {})
    assert repo.runs(marker)[0]["status"] == "interrupted"


def test_wbi_sign_fixed_protocol_vector():
    result = wbi_sign({"foo": "114", "bar": "514", "baz": 1919810},
                      KEYS["img_url"], KEYS["sub_url"], 1702204169)
    assert result["w_rid"] == "6149fdadf571698ca7e6a567265cd0ee"
    assert wbi_sign({"q": "a!'()*b"}, KEYS["img_url"], KEYS["sub_url"], 1)["q"] == "ab"


@pytest.mark.parametrize("with_collection", [False, True])
def test_metadata_supports_plain_video_and_optional_incomplete_collection(study, with_collection):
    repo, *_ = study
    payload = {"title": "合成数据库课程", "pages": [{"cid": 7, "page": 1, "duration": 20, "part": "规范理论"}]}
    if with_collection:
        payload["ugc_season"] = {"id": 42, "title": "合成计算机课程", "ep_count": 5,
                                 "sections": [{"title": "数据库", "episodes": [
                                     {"id": 1, "bvid": BV2, "title": "范式分解"}]}]}
    with BilibiliClient(repo, transport=httpx.MockTransport(lambda _: response(payload))) as client:
        metadata = client.metadata(BV)
    assert metadata["page_count"] == 1
    assert metadata["pages"][0]["cid"] == 7
    assert metadata["pages_complete"]
    if with_collection:
        assert metadata["collection"]["episodes"][0]["bvid"] == BV2
        assert metadata["collection"]["complete"] is False
        assert metadata["collection"]["total"] == 5
    else:
        assert not metadata["collection"]["id"]


def test_cookie_filtering_and_rejection():
    assert normalize_cookie("SESSDATA=synthetic-session; bili_jct=not-needed; buvid3=synthetic-device") == (
        "SESSDATA=synthetic-session; buvid3=synthetic-device")
    for raw in ["", "SESSDATA=x\r\nInjected: yes", "unknown=value", "SESSDATA="]:
        with pytest.raises(ValueError):
            normalize_cookie(raw)


def test_subtitle_language_selection_times_and_cookie_isolation(study):
    repo, *_ = study
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/nav"):
            return response({"wbi_img": KEYS})
        if request.url.path.endswith("/v2"):
            return response({"subtitle": {"subtitles": [
                {"lan": "en", "subtitle_url": "https://i0.hdslb.com/en.json"},
                {"lan": "ai-zh", "subtitle_url": "https://i0.hdslb.com/ai.json"},
                {"lan": "zh-CN", "subtitle_url": "https://aisubtitle.hdslb.com/zh.json"},
            ]}})
        assert request.url.path == "/zh.json"
        assert "cookie" not in request.headers
        return httpx.Response(200, json={"body": [{"from": 1.25, "to": 3.75, "content": "单调性"}]})
    with BilibiliClient(repo, "SESSDATA=synthetic-session", transport=httpx.MockTransport(handler)) as client:
        result = client.subtitles(unit())
    assert result["automatic"] is False
    assert result["segments"] == [{"id": 1, "start_ms": 1250, "end_ms": 3750, "text": "单调性"}]
    assert requests[0].headers["cookie"] == "SESSDATA=synthetic-session"


def test_anonymous_nav_keys_are_not_expired_credentials(study):
    repo, *_ = study
    def handler(request):
        if request.url.path.endswith("/nav"):
            return response({"wbi_img": KEYS}, code=-101)
        return response({"result": []})
    with BilibiliClient(repo, transport=httpx.MockTransport(handler)) as client:
        assert client.search("单调性") == []
    assert repo.control("platform_pause") is None


@pytest.mark.parametrize("cookie", ["", "SESSDATA=synthetic-session"])
def test_empty_preferred_track_does_not_hide_available_automatic_subtitles(study, cookie):
    repo, *_ = study
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/nav"):
            return response({"wbi_img": KEYS})
        if request.url.path.endswith("/v2"):
            return response({"subtitle": {"subtitles": [
                {"lan": "zh-CN", "subtitle_url": ""},
                {"lan": "zh-CN", "subtitle_url": "   "},
                {"lan": "zh-CN", "subtitle_url": None},
                {"lan": "ai-zh", "subtitle_url": "//aisubtitle.hdslb.com/automatic.json"},
            ]}})
        assert request.url.path == "/automatic.json" and "cookie" not in request.headers
        return httpx.Response(200, json={"body": [{"from": 1, "to": 2, "content": "合成例题"}]})

    with BilibiliClient(repo, cookie, transport=httpx.MockTransport(handler)) as client:
        result = client.subtitles(unit())
    assert result["status"] == "available" and result["automatic"] is True
    assert result["language"] == "ai-zh" and len(result["segments"]) == 1
    assert len(calls) == 3


@pytest.mark.parametrize("cookie,needs_login,expected", [
    ("", False, "requires_login"),
    ("SESSDATA=synthetic-session", False, "read_failed"),
    ("SESSDATA=synthetic-session", True, "requires_login"),
])
def test_all_empty_subtitle_urls_have_explicit_status(study, cookie, needs_login, expected):
    repo, *_ = study

    def handler(request):
        if request.url.path.endswith("/nav"):
            return response({"wbi_img": KEYS})
        return response({"need_login_subtitle": needs_login, "subtitle": {
            "subtitles": [{"lan": "zh-CN", "subtitle_url": ""}],
        }})

    with BilibiliClient(repo, cookie, transport=httpx.MockTransport(handler)) as client:
        assert client.subtitles(unit())["status"] == expected


@pytest.mark.parametrize("status", [401, 403, 412, 429])
def test_risk_persists_and_blocks_cache_and_next_request(study, status):
    repo, *_ = study
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status)
    with BilibiliClient(repo, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(BilibiliError, match="暂停"):
            client.metadata(BV)
        with pytest.raises(BilibiliError):
            client.metadata(BV2)
    assert len(calls) == 1
    assert BilibiliStore(VideoStore(repo.store.path)).control("platform_pause")["reason"] == f"http_{status}"


@pytest.mark.parametrize("url", [
    "https://evil.example/sub.json", "https://api.bilibili.com/secret.json",
    "http://i0.hdslb.com/sub.json", "https://i0.hdslb.com:8080/sub.json",
    "https://i0.hdslb.com@evil.example/sub.json",
])
def test_subtitle_url_allowlist(study, url):
    repo, *_ = study
    with BilibiliClient(repo) as client:
        with pytest.raises(BilibiliError, match="拒绝"):
            client.get(url, subtitle=True)


def test_redirect_not_followed_and_bad_subtitle_time_rejected(study):
    repo, *_ = study
    with BilibiliClient(repo, transport=httpx.MockTransport(
        lambda _: httpx.Response(302, headers={"Location": "https://evil.example/"})
    )) as client:
        with pytest.raises(BilibiliError, match="读取失败"):
            client.metadata(BV)


def test_cache_is_account_scoped(study):
    repo, *_ = study
    calls = []
    def handler(request):
        calls.append(request)
        return response({"pages": [{"cid": 100, "page": 1, "duration": 100, "part": "题目"}]})
    transport = httpx.MockTransport(handler)
    for cookie in ["", "", "SESSDATA=synthetic-session"]:
        with BilibiliClient(repo, cookie, transport=transport) as client:
            client.metadata(BV)
    assert len(calls) == 2


def test_chunk_budget_overlap_and_long_segment():
    segments = [{"id": index, "start_ms": index * 1000, "end_ms": index * 1000 + 1000, "text": "字" * 1000}
                for index in range(1, 71)]
    chunks, coverage = subtitle_chunks(segments)
    assert coverage["partial"]
    assert coverage["selected_characters"] == 60000
    assert {row["id"] for chunk in chunks for row in chunk} == set(range(1, 61))
    assert set(row["id"] for row in chunks[0]) & set(row["id"] for row in chunks[1])
    chunks, coverage = subtitle_chunks([{**segments[0], "text": "字" * 20000}])
    assert len(chunks) == 1 and not coverage["partial"]


def test_evidence_must_reference_sent_segments_and_never_model_times():
    chunk = [{"id": 7, "start_ms": 1250, "end_ms": 3750, "text": "必须分类讨论"}]
    for verdict in [Verdict(verdict="covers", reason="相关", segment_ids=[]),
                    Verdict(verdict="partial", reason="相关", segment_ids=[8])]:
        with pytest.raises(ValueError):
            validate_verdict(verdict, chunk, unit())
    result = validate_verdict(Verdict(verdict="covers", reason="涉及分类", segment_ids=[7]), chunk, unit())
    assert result["evidence"][0]["start_ms"] == 1250
    assert result["evidence"][0]["url"].endswith("p=1&t=1")
    with pytest.raises(ValueError):
        Verdict.model_validate({"verdict": "covers", "reason": "相关", "segment_ids": [7], "at": 100})


class FakeModel:
    def ask(self, task, material, schema):
        from tests.test_bilibili_api import Model
        return Model().ask(task, material, schema)

    def plan(self, _):
        return [{"text": "单调性 分类讨论", "purpose": "例题"}]

    def select(self, snapshot, rows):
        return [row["id"] for row in rows[:5]]

    def check(self, snapshot, part, chunk):
        return Verdict(verdict="covers", reason="字幕讲解分类讨论", segment_ids=[chunk[0]["id"]])


class FakeClient:
    account_scope = "synthetic"

    def search(self, keyword):
        return [{"bvid": BV, "title": "单调性", "description": "讲解", "author": "合成作者", "url": unit()["url"]}]

    def metadata(self, bvid):
        return {"page_count": 6, "pages": [{**unit(), "page": index} for index in range(1, 7)]}

    def subtitles(self, part):
        return {"status": "available", "language": "zh-CN", "automatic": False,
                "segments": [{"id": 1, "start_ms": 0, "end_ms": 1000, "text": "单调性需要分类讨论"}]}


def test_pipeline_deduplicates_caps_parts_and_keeps_original_marker(study):
    repo, marker, _ = study
    run = repo.create_run(repo.snapshot(marker), {"model": "fake"})
    run["visual_verification"] = False
    service = StudyService(repo, FakeModel(), FakeClient())
    service.plan(run)
    run["account_scope"] = "synthetic"
    run["queries"] *= 3
    before = repo.marker(marker)
    service.search(run)
    assert run["status"] == "completed"
    assert len(run["candidates"]) == 1
    candidate = run["candidates"][0]
    assert len(candidate["units"]) == 1
    assert candidate["unchecked_pages"] == 5
    assert all(part["verdict"] == "covers" for part in candidate["units"])
    assert repo.marker(marker) == before


def test_cancellation_and_model_failure_preserve_completed_checks(study):
    repo, marker, _ = study
    run = repo.create_run(repo.snapshot(marker), {})
    run["visual_verification"] = False
    run["queries"] = FakeModel().plan({})
    StudyService(repo, FakeModel(), FakeClient()).plan(run)
    run["account_scope"] = "synthetic"
    class BrokenModel(FakeModel):
        def ask(self, task, material, schema):
            if schema.__name__ == "EvidenceAssessment":
                raise RuntimeError("synthetic failure")
            return super().ask(task, material, schema)
    with pytest.raises(RuntimeError):
        StudyService(repo, BrokenModel(), FakeClient()).search(run)
    assert run["status"] == "failed"
    assert run["candidates"][0]["units"][0]["verification"]["chunks"]
    with pytest.raises(ProcessingCancelled):
        StudyService(repo, FakeModel(), FakeClient(), should_cancel=lambda: True).search(run)
    assert run["status"] == "interrupted"


def test_failed_positive_evidence_never_recommended(study):
    repo, marker, _ = study
    run = repo.create_run(repo.snapshot(marker), {})
    run["visual_verification"] = False
    run["queries"] = FakeModel().plan({})
    StudyService(repo, FakeModel(), FakeClient()).plan(run)
    run["account_scope"] = "synthetic"
    class InvalidModel(FakeModel):
        def ask(self, task, material, schema):
            result = super().ask(task, material, schema)
            if schema.__name__ == "EvidenceExtraction":
                result.findings[0].segment_ids = [999]
            return result
    with pytest.raises(ValueError):
        StudyService(repo, InvalidModel(), FakeClient()).search(run)
    saved = repo.get_run(marker, run["id"])
    assert not any(part.get("verdict") == "covers" for item in saved["candidates"] for part in item["units"])
