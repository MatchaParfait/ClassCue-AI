"""Agent-facing retrieval uses isolated synthetic subtitles and images."""

import base64
import json
from io import BytesIO

from PIL import Image
import pytest

import api.main as api_main
from classcue.asr_storage import slide_directory
from classcue.core import TranscriptSegment
from classcue.course_connection import connection_token
from tests.test_course_tools import fixture, video, call


@pytest.fixture
def retrieval(fixture):
    fixture.service.batch = fixture.app.state.course_batch_tools
    return fixture


def seed_slides(store, root, video_id, length_ms=10_000):
    root.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (2000, 1000), (20, 170, 50)).save(root / "synthetic.jpg")
    return store.replace_video_slides(video_id, [{
        "start_ms": start, "end_ms": start + length_ms, "representative_ms": start + min(1000, length_ms - 1),
        "visible_from_ms": start, "visible_until_ms": start + length_ms,
        "image_path": "synthetic.jpg", "image_hash": "a" * 64,
        "duration_ms": length_ms, "retention_class": "primary", "confidence": 1,
        "roi_json": "{}", "extraction_version": "synthetic",
    } for start in (0, length_ms)])


def test_search_intersection_candidates_grouping_diversity_and_context(retrieval):
    f = retrieval
    folder = f.store.create_folder("算法")
    one = video(f, "第一课", folder)
    two = video(f, "第二课", folder, "分治")
    outside = video(f, "其他课", text="分治")
    f.store.mark_transcribed(one, [
        TranscriptSegment(1, 1000, 2000, "先看背景"),
        TranscriptSegment(2, 3000, 4000, "分治适合此题"),
        TranscriptSegment(3, 5000, 6000, "递归进行分治"),
        TranscriptSegment(4, 30_000, 31_000, "递归结束"),
        TranscriptSegment(5, 50_000, 51_000, "另一个分治例题"),
    ])
    found = call(f, "search_subtitles", course_id=f"folder:{folder}", queries=["分治", "递归"], limit=2)
    assert found["total_matches"] == 5 and found["total"] == 4 and found["total_videos"] == 2
    assert {row["video_id"] for row in found["items"]} == {one, two}
    merged = next(row for row in found["items"] if row["video_id"] == one)
    assert merged["match_count"] == 2 and merged["start_seconds"] == 3 and merged["end_seconds"] == 6
    assert any(row["text"] == "先看背景" for row in merged["context"])
    assert merged["context_range"] == {"start_seconds": 0, "end_seconds": 26}
    following = call(f, "search_subtitles", course_id=f"folder:{folder}", queries=["分治", "递归"],
                     limit=2, offset=found["next_offset"])
    assert not following["has_more"] and len(following["items"]) == 2
    both = call(f, "search_subtitles", video_id=one, queries=["分治", "递归"], match_mode="all")
    assert both["total_matches"] == 1
    empty = call(f, "search_subtitles", video_id=outside, course_id=f"folder:{folder}", query="分治")
    assert empty["total"] == 0 and empty["empty_reason"] == "no_videos"
    assert empty["coverage"]["video_count"] == 0


def test_search_empty_reasons_and_literal_boundaries(retrieval):
    f = retrieval
    identity = video(f, "无字幕")
    assert call(f, "search_subtitles", video_id=identity, query="分治")["empty_reason"] == "no_subtitles"
    f.store.mark_transcribed(identity, [
        TranscriptSegment(1, 0, 1000, "动态"),
        TranscriptSegment(2, 1000, 2000, "规划"),
        TranscriptSegment(3, 2000, 3000, "100%_literal"),
    ])
    assert call(f, "search_subtitles", video_id=identity, query="动态规划")["empty_reason"] == "no_literal_match"
    assert call(f, "search_subtitles", video_id=identity, queries=["%_", "不存在"])["total_matches"] == 1
    assert call(f, "search_subtitles", video_id=identity, query="动态", offset=50)["empty_reason"] is None


def test_merged_representative_matches_earliest_time_with_unsorted_input(retrieval):
    f = retrieval
    identity = video(f, "乱序字幕")
    f.store.mark_transcribed(identity, [
        TranscriptSegment(9, 5000, 6000, "匹配后一句"),
        TranscriptSegment(3, 1000, 2000, "匹配前一句"),
    ])
    row = call(f, "search_subtitles", video_id=identity, query="匹配")["items"][0]
    assert (row["segment_id"], row["start_seconds"], row["end_seconds"], row["match_count"]) == (3, 1, 6, 2)
    assert row["text"] == "匹配前一句"
    assert "start_ms=1000" in row["play_url"]


def test_context_is_bounded_and_declares_truncation(retrieval):
    f = retrieval
    identity = video(f, "长上下文")
    f.store.mark_transcribed(identity, [
        TranscriptSegment(i + 1, i * 1000, (i + 1) * 1000, "匹配" + "文" * 500)
        for i in range(20)
    ])
    row = call(f, "search_subtitles", video_id=identity, query="匹配")["items"][0]
    assert row["match_count"] == 20 and row["context_truncated"]
    assert len(row["context"]) <= 5 and all(len(s["text"]) <= 300 for s in row["context"])


def test_slide_list_and_authenticated_image_preserve_video_ownership(retrieval):
    f = retrieval
    identity = video(f, "有PPT")
    other = video(f, "其他视频")
    root = slide_directory(api_main.DIRECTORY_PROJECT_ROOT, f.app.state.jobs.provider.data_root())
    slides = seed_slides(f.store, root, identity)
    listed = call(f, "list_slides", video_id=identity, start_seconds=5, end_seconds=15, limit=1)
    assert listed["total"] == 2 and listed["has_more"] and listed["extracted_slide_count"] == 2
    assert listed["items"][0]["visible_from_seconds"] == 0
    headers = {"Authorization": f"Bearer {connection_token()}"}
    payload = {"library_id": f.app.state.jobs.provider.library_id(),
               "arguments": {"video_id": identity, "slide_id": slides[0]["id"]}}
    response = f.client.post("/api/course-tools/read_slide", json=payload, headers=headers)
    assert response.status_code == 200
    image = response.json()["image"]
    assert (image["width"], image["height"]) == (1600, 800)
    pixels = Image.open(BytesIO(base64.b64decode(image["data"])))
    assert pixels.getpixel((10, 10))[1] > 150
    assert "image_path" not in response.text and str(root) not in response.text
    payload["arguments"]["video_id"] = other
    assert f.client.post("/api/course-tools/read_slide", json=payload, headers=headers).status_code == 404
    with f.store.connect() as db:
        db.execute("UPDATE video_slides SET image_path='../escape.jpg' WHERE id=?", (slides[0]["id"],))
    payload["arguments"]["video_id"] = identity
    assert f.client.post("/api/course-tools/read_slide", json=payload, headers=headers).status_code == 404


def test_video_slide_count_includes_auxiliary_states_in_every_scope(retrieval):
    f = retrieval
    folder = f.store.create_folder("PPT课程")
    identity = video(f, "状态数量", folder)
    root = slide_directory(api_main.DIRECTORY_PROJECT_ROOT, f.app.state.jobs.provider.data_root())
    slides = seed_slides(f.store, root, identity)
    with f.store.connect() as db:
        db.execute("UPDATE video_slides SET retention_class='auxiliary' WHERE id=?", (slides[1]["id"],))
    global_row = call(f, "list_videos")["items"][0]
    folder_row = call(f, "list_videos", course_id=f"folder:{folder}")["items"][0]
    assert global_row["slide_count"] == folder_row["slide_count"] == 2


def test_frame_is_an_image_and_checks_time(retrieval, monkeypatch):
    f = retrieval
    identity = video(f, "画面")
    image_path = f.root / "frame.jpg"
    Image.new("RGB", (320, 180), (200, 20, 10)).save(image_path)
    calls = []
    monkeypatch.setattr(api_main, "video_preview_frame", lambda path, cache, time_ms:
                        calls.append(time_ms) or image_path)
    frame = call(f, "read_video_frame", video_id=identity, start_seconds=12.5)
    assert calls == [12500] and frame["evidence_type"] == "video_frame"
    assert frame["image"]["width"] == 320
    with pytest.raises(ValueError, match="时长"):
        call(f, "read_video_frame", video_id=identity, start_seconds=120)


def test_frame_decodes_actual_synthetic_video(retrieval):
    from tests.test_slide_extractor import make_slide_video
    f = retrieval
    source = f.root / "synthetic-frame.mkv"
    make_slide_video(source, [((220, 20, 10), 1), ((10, 180, 20), 1)])
    identity = f.store.create_video("synthetic-frame", source.name, str(source), "unused.wav", 2000)
    early = call(f, "read_video_frame", video_id=identity, start_seconds=0.3)
    late = call(f, "read_video_frame", video_id=identity, start_seconds=1.3)
    first = Image.open(BytesIO(base64.b64decode(early["image"]["data"]))).getpixel((10, 10))
    second = Image.open(BytesIO(base64.b64decode(late["image"]["data"]))).getpixel((10, 10))
    assert first[0] > 180 and first[1] < 50
    assert second[1] > 140 and second[0] < 50


def test_image_budget_and_decode_error_are_explicit(retrieval):
    import numpy as np
    f = retrieval
    path = f.root / "large-synthetic.png"
    pixels = np.random.default_rng(7).integers(0, 256, (1600, 1600, 3), dtype=np.uint8)
    Image.fromarray(pixels).save(path)
    image = f.service._image(path)
    assert image["width"] == 1024 and len(base64.b64decode(image["data"])) <= 1024 * 1024
    path.write_bytes(b"not an image")
    with pytest.raises(ValueError, match="无法解码"):
        f.service._image(path)


def test_analysis_paging_manual_source_and_completed_empty_run(retrieval):
    f = retrieval
    identity = video(f, "考点题目")
    manual = f.store.create_manual_question(identity, 1000)
    marker = f.store.add_marker(identity, 3000, 4000, "合成考点", "模型概括", "原句",
                                "必须掌握", 0.9, "medium_recall")
    f.store.set_marker_jump_time(marker, 2000)
    f.store.set_marker_importance(marker, "critical")
    excluded = f.store.add_marker(identity, 5000, 6000, "已移除", "", "", "x", 0.8)
    with f.store.connect() as db:
        db.execute("UPDATE video_markers SET review_status='excluded' WHERE id=?", (excluded,))
        db.execute("INSERT INTO video_analysis_runs(video_id,analysis_mode,status,candidate_count) VALUES(?,?,'completed',0)",
                   (identity, "question_extraction"))
    first = call(f, "read_analysis", video_id=identity, limit=1)
    assert first["total"] == 2 and first["items"][0]["marker_id"] == manual["id"]
    assert first["items"][0]["source"] == "manual"
    second = call(f, "read_analysis", video_id=identity, offset=first["next_offset"])
    assert second["items"][0]["marker_id"] == marker and second["items"][0]["note"] == "模型概括"
    assert second["items"][0]["jump_seconds"] == 2 and second["items"][0]["importance"] == "critical"
    assert "start_ms=2000" in second["items"][0]["play_url"]
    filtered = call(f, "read_analysis", video_id=identity, analysis_mode="low_recall")
    assert filtered["total"] == 0 and filtered["analysis_runs"] == []
    assert first["analysis_runs"][0]["candidate_count"] == 0


def test_list_jobs_filters_before_pagination_and_does_not_return_private_request(retrieval):
    f = retrieval
    identity = video(f, "任务")
    other = video(f, "其他")
    for name, kind, status, target in [
        ("done", "batch_video_process", "completed", identity),
        ("active", "course_sequence", "queued", identity),
        ("outside", "course_transcribe", "running", other),
        ("unrelated", "asr_prepare", "running", identity),
    ]:
        f.store.create_job(name, kind)
        f.store.update_job(name, status=status, request_json=json.dumps({
            "entries": [{"video_id": target}], "private": "not-for-agent"}))
    active = call(f, "list_jobs", video_id=identity, status="active", limit=1)
    assert active["total"] == 1 and active["items"][0]["job_id"] == "active"
    finished = call(f, "list_jobs", status="finished")
    assert [item["job_id"] for item in finished["items"]] == ["done"]
    assert "not-for-agent" not in str(finished)


def test_processing_status_uses_observation_only_without_credentials_or_model_calls(retrieval, monkeypatch):
    f = retrieval
    monkeypatch.setattr(api_main, "resolve_interface_key", lambda *_: pytest.fail("must not read credentials"))
    monkeypatch.setattr(api_main, "prepare_asr_model", lambda *_a, **_k: pytest.fail("must not prepare models"))
    monkeypatch.setattr(api_main, "asr_session", lambda *_a, **_k: pytest.fail("must not load models"))
    result = call(f, "get_processing_status")
    assert result["service_status"] == "running" and result["ai_connection"]["credential_checked"] is False
    assert result["asr_observation"]["inspection_status"] in {"cached", "unknown"}
    assert "base_url" not in result["ai_connection"] and "api_key" not in str(result)


@pytest.mark.parametrize("name,args", [
    ("search_subtitles", {}),
    ("search_subtitles", {"query": "x", "queries": ["y"]}),
    ("search_subtitles", {"queries": [" "] }),
    ("search_subtitles", {"query": "x", "video_id": True}),
    ("search_subtitles", {"query": "x", "match_mode": "fuzzy"}),
    ("list_slides", {"video_id": 1, "start_seconds": 5, "end_seconds": 1}),
    ("read_slide", {"video_id": 1, "slide_id": -1}),
    ("list_jobs", {"status": "unknown"}),
])
def test_rejects_ambiguous_or_invalid_retrieval_arguments(retrieval, name, args):
    with pytest.raises(ValueError):
        call(retrieval, name, **args)
