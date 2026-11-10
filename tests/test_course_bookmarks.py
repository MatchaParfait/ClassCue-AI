"""Read user bookmarks through the MCP backend using an isolated library."""

from urllib.parse import parse_qs, urlparse

import pytest

from classcue.course_connection import connection_token
from classcue.study_bookmarks import StudyBookmarkStore
from classcue.study_courses import StudyCourseStore
from tests.test_course_tools import fixture, video, call


def test_user_note_finds_video_and_readable_subtitle_range_without_transcription(fixture):
    f = fixture
    identity = video(f, "注意力课程", text="这里比较注意力机制的计算。")
    repo = StudyBookmarkStore(f.store)
    bookmark = repo.create(identity, 14_220, "自注意力：Q/K/V 计算示例")
    found = call(f, "search_bookmarks", query="自注意力")
    assert found["evidence_type"] == "user_bookmark"
    assert found["match_type"] == "literal" and found["queries"] == ["自注意力"]
    assert found["total"] == found["total_videos"] == found["searched_bookmarks"] == 1
    item = found["items"][0]
    assert item["bookmark_id"] == bookmark["id"] and item["video_id"] == identity
    assert item["note"] == "自注意力：Q/K/V 计算示例"
    assert item["position_seconds"] == 14.22
    assert item["has_subtitles"]
    assert item["context_range"] == {"start_seconds": 0, "end_seconds": 44.22}
    query = parse_qs(urlparse(item["play_url"]).query)
    assert query["start_ms"] == ["14220"]
    assert query["library_id"] == [found["library_id"]]
    transcript = call(f, "read_transcript", video_id=identity, **item["context_range"])
    assert transcript["items"][0]["text"] == "这里比较注意力机制的计算。"
    assert f.store.list_active_jobs() == []
    assert repo.bookmarks()[0]["note"] == item["note"]


def test_search_uses_notes_only_and_can_find_bookmarks_without_subtitles(fixture):
    f = fixture
    identity = video(f, "自注意力", text="字幕里有自注意力")
    repo = StudyBookmarkStore(f.store)
    repo.create(identity, 10_000, "重要公式")
    other = video(f, "没有字幕")
    saved = repo.create(other, 30_000, "Self-Attention / 自注意力")
    found = call(f, "search_bookmarks", query="自注意力")
    assert [item["bookmark_id"] for item in found["items"]] == [saved["id"]]
    assert not found["items"][0]["has_subtitles"]
    assert found["empty_reason"] is None
    assert call(f, "search_bookmarks", query="字幕里")["empty_reason"] == "no_literal_match"
    assert call(f, "search_bookmarks", query="self-attention")["total"] == 1


def test_recursive_course_video_intersection_and_root_scopes(fixture):
    f = fixture
    parent = f.store.create_folder("机器学习")
    child = f.store.create_folder("第二章", parent)
    binding = StudyCourseStore(f.store).create(folder_id=parent)
    one, two = video(f, "第一课", parent), video(f, "第二课", child)
    outside = video(f, "其他课")
    repo = StudyBookmarkStore(f.store)
    for identity in [one, two, outside]:
        repo.create(identity, 1000, "自注意力讲解")
    for scope in [binding["id"], f"folder:{parent}"]:
        found = call(f, "search_bookmarks", course_id=scope, query="自注意力")
        assert {item["video_id"] for item in found["items"]} == {one, two}
        row = next(item for item in found["items"] if item["video_id"] == two)
        assert row["folder_id"] == child and row["folder_path"] == "机器学习 / 第二章"
        selected = call(f, "search_bookmarks", course_id=scope, video_id=two, query="自注意力")
        assert [item["video_id"] for item in selected["items"]] == [two]
        empty = call(f, "search_bookmarks", course_id=scope, video_id=outside, query="自注意力")
        assert empty["searched_bookmarks"] == 0 and empty["empty_reason"] == "no_bookmarks"
    root = call(f, "search_bookmarks", course_id="root")
    assert [item["video_id"] for item in root["items"]] == [outside]
    assert root["items"][0]["folder_path"] == ""
    with pytest.raises(KeyError):
        call(f, "search_bookmarks", course_id="folder:99999")
    with pytest.raises(KeyError):
        call(f, "search_bookmarks", video_id=99999)


def test_whole_video_reminder_keeps_null_position_and_browsing_is_paginated(fixture):
    f = fixture
    identity = video(f, "整段复习")
    repo = StudyBookmarkStore(f.store)
    first = repo.create(identity, None, "复习整个视频")
    second = repo.create(identity, 0, "")
    newest = repo.create(identity, 120_000, "末尾总结")
    page = call(f, "search_bookmarks", limit=2)
    assert page["queries"] == [] and page["empty_reason"] is None
    assert page["total"] == 3 and page["total_videos"] == 1 and page["has_more"]
    assert [item["bookmark_id"] for item in page["items"]] == [newest["id"], second["id"]]
    assert page["items"][0]["context_range"] == {"start_seconds": 90, "end_seconds": 120}
    assert page["items"][1]["position_seconds"] == 0
    last = call(f, "search_bookmarks", limit=2, offset=page["next_offset"])
    assert not last["has_more"] and last["next_offset"] is None
    assert last["items"][0]["bookmark_id"] == first["id"]
    assert last["items"][0]["position_seconds"] is None
    assert last["items"][0]["context_range"] is None
    assert call(f, "search_bookmarks", offset=100)["empty_reason"] is None


def test_candidate_queries_are_literal_and_apply_to_one_note(fixture):
    f = fixture
    identity = video(f, "候选词")
    repo = StudyBookmarkStore(f.store)
    one = repo.create(identity, 1000, "Self-Attention")
    two = repo.create(identity, 2000, "自注意力与 Transformer")
    repo.create(identity, None, "100%_literal")
    args = {"queries": ["自注意力", "Self-Attention"]}
    assert call(f, "search_bookmarks", **args)["total"] == 2
    assert call(f, "search_bookmarks", match_mode="all", **args)["total"] == 0
    both = call(f, "search_bookmarks", queries=["自注意力", "Transformer"], match_mode="all")
    assert [item["bookmark_id"] for item in both["items"]] == [two["id"]]
    assert call(f, "search_bookmarks", query="%_")["total"] == 1
    assert call(f, "search_bookmarks", query="' OR 1=1 --")["total"] == 0
    assert call(f, "search_bookmarks", queries=["Self-Attention", "Self-Attention"])["queries"] == ["Self-Attention"]
    assert call(f, "search_bookmarks", query=" Self-Attention ")["items"][0]["bookmark_id"] == one["id"]


def test_unknown_duration_does_not_advertise_an_unreadable_transcript_range(fixture):
    f = fixture
    identity = video(f, "时长未知")
    with f.store.connect() as db:
        db.execute("UPDATE videos SET duration_ms=0 WHERE id=?", (identity,))
    StudyBookmarkStore(f.store).create(identity, 1000, "自注意力")
    item = call(f, "search_bookmarks", query="自注意力")["items"][0]
    assert item["position_seconds"] == 1
    assert item["context_range"] is None


def test_app_edits_deletes_and_folder_moves_are_visible_without_a_separate_index(fixture):
    f = fixture
    parent, other = f.store.create_folder("原课程"), f.store.create_folder("新课程")
    identity = video(f, "原视频", parent)
    library_id = f.app.state.jobs.provider.library_id()
    created = f.client.post(f"/api/videos/{identity}/bookmarks", json={
        "library_id": library_id, "note": "自注意力讲解", "position_ms": 1000,
    })
    assert created.status_code == 201
    bookmark_id = created.json()["id"]
    assert call(f, "search_bookmarks", query="自注意力")["items"][0]["bookmark_id"] == bookmark_id
    edited = f.client.patch(f"/api/study-bookmarks/{bookmark_id}", json={
        "library_id": library_id, "note": "交叉注意力讲解",
    })
    assert edited.status_code == 200
    assert call(f, "search_bookmarks", query="自注意力")["total"] == 0
    f.store.update_video_display_name(identity, "新视频名")
    f.store.set_video_folder(identity, other)
    assert call(f, "search_bookmarks", course_id=f"folder:{parent}")["total"] == 0
    found = call(f, "search_bookmarks", course_id=f"folder:{other}", query="交叉注意力")["items"][0]
    assert found["video_name"] == "新视频名" and found["folder_path"] == "新课程"
    deleted = f.client.request("DELETE", f"/api/study-bookmarks/{bookmark_id}",
                               json={"library_id": library_id})
    assert deleted.status_code == 200
    assert call(f, "search_bookmarks")["empty_reason"] == "no_bookmarks"


def test_secondary_sources_are_excluded_and_missing_notes_are_distinguished(fixture):
    f = fixture
    assert call(f, "search_bookmarks", query="自注意力")["empty_reason"] == "no_bookmarks"
    identity = video(f, "副视角")
    StudyBookmarkStore(f.store).create(identity, 1000, "自注意力")
    with f.store.connect() as db:
        db.execute("UPDATE videos SET is_secondary=1 WHERE id=?", (identity,))
    assert call(f, "search_bookmarks")["total"] == 0
    with pytest.raises(KeyError):
        call(f, "search_bookmarks", video_id=identity)


def test_authenticated_bookmark_tool_uses_the_current_library(fixture):
    f = fixture
    identity = video(f, "认证测试")
    StudyBookmarkStore(f.store).create(identity, 1000, "用户提醒")
    payload = {"library_id": f.app.state.jobs.provider.library_id(), "arguments": {"query": "提醒"}}
    endpoint = "/api/course-tools/search_bookmarks"
    assert f.client.post(endpoint, json=payload).status_code == 403
    headers = {"Authorization": f"Bearer {connection_token()}"}
    result = f.client.post(endpoint, json=payload, headers=headers)
    assert result.status_code == 200 and result.json()["items"][0]["note"] == "用户提醒"
    assert f.client.post(endpoint, json={**payload, "library_id": "another-library"},
                         headers=headers).status_code == 409
    invalid = f.client.post(endpoint, json={**payload, "arguments": {"limit": 51}}, headers=headers)
    assert invalid.status_code == 422


@pytest.mark.parametrize("arguments", [
    {"query": ""}, {"query": " "}, {"query": "a\0b"}, {"queries": []}, {"queries": [" "]},
    {"query": "a", "queries": ["b"]}, {"queries": ["a"] * 9}, {"match_mode": "semantic"},
    {"video_id": True}, {"limit": True}, {"context_seconds": 0}, {"context_seconds": 121},
    {"offset": -1}, {"unknown": "ignored"},
])
def test_bookmark_arguments_are_bounded_and_unambiguous(fixture, arguments):
    with pytest.raises(ValueError):
        call(fixture, "search_bookmarks", **arguments)
