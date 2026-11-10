"""Unified Agent retrieval against synthetic, isolated video libraries."""

import json
from threading import Event, Thread
from urllib.parse import parse_qs, urlparse

import pytest

from classcue.core import TranscriptSegment
from classcue.course_connection import connection_token
from classcue.study_bookmarks import StudyBookmarkStore
from classcue.study_courses import StudyCourseStore
from tests.test_course_tools import fixture, video, call


def search(f, **args):
    return call(f, "search_library", **args)


def test_all_four_sources_are_fused_without_losing_evidence_or_inventing_time(fixture):
    f = fixture
    identity = video(f, "自注意力第一课", text="这里说明自注意力的计算。")
    bookmark = StudyBookmarkStore(f.store).create(identity, 14_000, "自注意力示例")
    marker = f.store.add_marker(identity, 12_000, 18_000, "自注意力", "模型概括", "课堂引用",
                                "重要", .9, "medium_recall")
    found = search(f, query="自注意力")
    assert found["searched_sources"] == ["catalog", "bookmarks", "subtitles", "analysis"]
    assert found["source_counts"] == dict.fromkeys(found["searched_sources"], 1)
    assert found["total"] == 2 and found["search_complete"] and found["total_is_exact"]
    timed = next(item for item in found["items"] if item["position_seconds"] is not None)
    assert timed["sources"] == ["bookmarks", "subtitles", "analysis"]
    evidence = {item["source"]: item for item in timed["evidence"]}
    assert evidence["bookmarks"]["record_id"] == bookmark["id"]
    assert evidence["bookmarks"]["evidence_type"] == "user_bookmark"
    assert evidence["analysis"]["record_id"] == marker
    assert evidence["analysis"]["evidence_type"] == "saved_analysis"
    assert evidence["subtitles"]["evidence_type"] == "asr_transcript"
    assert evidence["subtitles"]["record_id"] == 1
    transcript = call(f, "read_transcript", video_id=identity, **timed["context_range"])
    assert transcript["items"][0]["text"] == "这里说明自注意力的计算。"
    catalog = next(item for item in found["items"] if item["sources"] == ["catalog"])
    assert catalog["position_seconds"] is None and catalog["context_range"] is None
    assert parse_qs(urlparse(catalog["play_url"]).query)["start_ms"] == ["0"]
    assert str(f.root) not in json.dumps(found, ensure_ascii=False)
    assert f.store.list_active_jobs() == []


def test_keyword_groups_cover_multiple_sentences_but_not_distant_lectures(fixture):
    f = fixture
    identity = video(f, "合成课程")
    f.store.mark_transcribed(identity, [
        TranscriptSegment(3, 90_000, 92_000, "分解这个问题"),
        TranscriptSegment(1, 1000, 3000, "先解释规范化"),
        TranscriptSegment(2, 12_000, 14_000, "接着减少冗余"),
    ])
    args = {"sources": ["subtitles"], "term_groups": [["范式", "规范化"], ["冗余", "分解"]]}
    found = search(f, **args)
    assert found["total"] == 1
    hit = found["items"][0]
    assert (hit["start_seconds"], hit["end_seconds"]) == (1, 14)
    assert hit["matched_terms"] == ["规范化", "冗余"]
    assert [row["record_id"] for row in hit["evidence"]] == [1, 2]
    assert search(f, **args, window_seconds=5)["total"] == 0
    assert search(f, sources=["subtitles"], queries=["规范化", "冗余"], match_mode="all")["total"] == 1


def test_single_phrase_stays_literal_and_does_not_join_sentence_boundaries(fixture):
    f = fixture
    identity = video(f, "测试视频")
    f.store.mark_transcribed(identity, [
        TranscriptSegment(1, 1000, 2000, "动态"),
        TranscriptSegment(2, 2000, 3000, "规划"),
    ])
    assert search(f, query="动态规划", sources=["subtitles"])["total"] == 0
    assert search(f, term_groups=[["动态"], ["规划"]], sources=["subtitles"])["total"] == 1


def test_exclusions_apply_to_entire_matched_passage_including_nonmatching_sentences(fixture):
    f = fixture
    identity = video(f, "排除示例")
    f.store.mark_transcribed(identity, [
        TranscriptSegment(1, 1000, 2000, "范式概念"),
        TranscriptSegment(2, 5000, 6000, "此处是作业通知"),
        TranscriptSegment(3, 10_000, 11_000, "如何分解"),
    ])
    args = {"sources": ["subtitles"], "queries": ["范式", "分解"], "match_mode": "all"}
    assert search(f, **args)["total"] == 1
    assert search(f, **args, exclude_terms=["作业通知"])["total"] == 0


@pytest.mark.parametrize("second_source", ["subtitles", "bookmarks"])
def test_merging_cannot_bridge_excluded_subtitles_or_discard_valid_separate_hits(fixture, second_source):
    f = fixture
    identity = video(f, "排除词合并边界")
    segments = [
        TranscriptSegment(1, 1000, 2000, "范式的定义"),
        TranscriptSegment(2, 3000, 4000, "作业通知"),
    ]
    if second_source == "subtitles":
        segments.append(TranscriptSegment(3, 5000, 6000, "范式的例子"))
    else:
        StudyBookmarkStore(f.store).create(identity, 5000, "范式的例子")
    f.store.mark_transcribed(identity, segments)
    args = {"query": "范式", "sources": list(dict.fromkeys(["subtitles", second_source]))}
    assert search(f, **args)["total"] == 1
    found = search(f, **args, exclude_terms=["作业通知"])
    assert found["search_complete"] and found["total"] == 2
    assert sorted(item["start_seconds"] for item in found["items"]) == [1, 5]
    assert all(item["end_seconds"] <= 2 or item["start_seconds"] >= 5 for item in found["items"])


def test_recursive_course_and_video_intersection_remain_strict(fixture):
    f = fixture
    parent = f.store.create_folder("算法")
    child = f.store.create_folder("第二章", parent)
    course = StudyCourseStore(f.store).create(folder_id=parent)
    one = video(f, "第一课", parent, "递归")
    two = video(f, "第二课", child, "递归")
    outside = video(f, "其他课", text="递归")
    for scope in (course["id"], f"folder:{parent}"):
        found = search(f, query="递归", course_id=scope)
        assert {item["video_id"] for item in found["items"]} == {one, two}
        item = next(item for item in found["items"] if item["video_id"] == two)
        assert item["folder_path"] == "算法 / 第二章"
        assert search(f, query="递归", course_id=scope, video_id=outside)["empty_reason"] == "no_videos"
    root = search(f, query="递归", course_id="root")
    assert [item["video_id"] for item in root["items"]] == [outside]
    with pytest.raises(KeyError):
        search(f, query="递归", course_id="folder:99999")


def test_catalog_search_includes_course_and_folder_names_without_claiming_speech(fixture):
    f = fixture
    course = StudyCourseStore(f.store).create(name="机器学习")
    folder = f.store.create_folder("第二学期", course["folder_id"])
    identity = video(f, "lecture", folder)
    found = search(f, query="机器学习", sources=["catalog"])
    assert found["items"][0]["video_id"] == identity
    assert found["items"][0]["position_seconds"] is None
    assert search(f, query="第二学期", sources=["subtitles"])["total"] == 0


def test_untimed_reminders_unknown_duration_manual_markers_and_excluded_analysis(fixture):
    f = fixture
    identity = video(f, "合成视频")
    StudyBookmarkStore(f.store).create(identity, None, "复习依赖关系")
    manual = f.store.create_manual_question(identity, 30_000)
    with f.store.connect() as db:
        db.execute("UPDATE video_markers SET title='依赖关系手动定位' WHERE id=?", (manual["id"],))
    removed = f.store.add_marker(identity, 60_000, 65_000, "依赖关系", "", "", "x", .9)
    f.store.set_marker_review_status(removed, "excluded")
    found = search(f, query="依赖关系")
    assert found["source_counts"] == {"catalog": 0, "bookmarks": 1, "subtitles": 0, "analysis": 1}
    reminder = next(item for item in found["items"] if "bookmarks" in item["sources"])
    assert reminder["position_seconds"] is None and reminder["context_range"] is None
    marker = next(item for item in found["items"] if "analysis" in item["sources"])
    assert marker["evidence"][0]["evidence_type"] == "manual_marker"
    with f.store.connect() as db:
        db.execute("UPDATE videos SET duration_ms=0 WHERE id=?", (identity,))
    assert all(item["context_range"] is None for item in search(f, query="依赖关系")["items"])


def test_source_filters_and_time_ranges_apply_before_matching_and_fusion(fixture):
    f = fixture
    identity = video(f, "依赖关系", text="依赖关系的定义")
    repo = StudyBookmarkStore(f.store)
    repo.create(identity, None, "依赖关系")
    repo.create(identity, 50_000, "依赖关系")
    found = search(f, query="依赖关系", sources=["subtitles", "bookmarks"],
                   start_seconds=5, end_seconds=25)
    assert found["total"] == 1 and found["items"][0]["sources"] == ["subtitles"]
    assert search(f, query="依赖关系", sources=["catalog"], start_seconds=5, end_seconds=25)["total"] == 0
    assert search(f, query="依赖关系", sources=["bookmarks"])["total"] == 2


def test_more_query_coverage_ranks_before_frequent_single_term_and_diversifies(fixture):
    f = fixture
    noisy = video(f, "很多重复")
    focused = video(f, "相关讲解", text="同时解释范式和冗余")
    f.store.mark_transcribed(noisy, [
        TranscriptSegment(i + 1, i * 50_000, i * 50_000 + 1000, "范式") for i in range(8)
    ])
    found = search(f, queries=["范式", "冗余"], sources=["subtitles"], limit=2)
    assert found["items"][0]["video_id"] == focused
    assert {item["video_id"] for item in found["items"]} == {focused, noisy}


def test_repeated_matches_never_merge_into_an_unbounded_lecture(fixture):
    f = fixture
    identity = video(f, "密集讲解")
    f.store.mark_transcribed(identity, [
        TranscriptSegment(i + 1, i * 4000, i * 4000 + 1000, "范式") for i in range(30)
    ])
    found = search(f, query="范式", sources=["subtitles"])
    assert found["total"] >= 3
    assert all(item["end_seconds"] - item["start_seconds"] <= 45 for item in found["items"])
    assert any(item["evidence_truncated"] for item in found["items"])
    assert all(len(item["evidence"]) <= 6 for item in found["items"])


def test_literal_wildcards_quotes_ascii_case_and_injection(fixture):
    f = fixture
    video(f, "标点", text='100%_literal SELF-Attention "quoted"')
    for term in ("%_", "self-attention", '"quoted"'):
        assert search(f, query=term, sources=["subtitles"])["total"] == 1
    assert search(f, query="' OR 1=1 --")["total"] == 0


def test_pagination_has_stable_global_order_and_rejects_changed_data(fixture):
    f = fixture
    for name in ["第一课", "第二课", "第三课"]:
        video(f, name, text="范式")
    args = {"query": "范式", "sources": ["subtitles"], "limit": 1}
    first = search(f, **args)
    second = search(f, **args, offset=first["next_offset"], snapshot_id=first["snapshot_id"])
    third = search(f, **args, offset=second["next_offset"], snapshot_id=second["snapshot_id"])
    assert len({item["result_id"] for page in [first, second, third] for item in page["items"]}) == 3
    assert not third["has_more"] and third["next_offset"] is None
    identity = first["items"][0]["video_id"]
    f.store.update_video_display_name(identity, "新名字")
    with pytest.raises(ValueError, match="资料或参数已变化"):
        search(f, **args, offset=first["next_offset"], snapshot_id=first["snapshot_id"])


def test_bookmark_edits_and_folder_moves_do_not_need_reindexing(fixture):
    f = fixture
    folder = f.store.create_folder("原目录")
    identity = video(f, "第一课", folder)
    repo = StudyBookmarkStore(f.store)
    bookmark = repo.create(identity, 3000, "范式")
    assert search(f, query="范式")["total"] == 1
    repo.edit(bookmark["id"], "索引")
    assert search(f, query="范式")["total"] == 0
    f.store.set_video_folder(identity, None)
    assert search(f, query="索引", course_id=f"folder:{folder}")["empty_reason"] == "no_videos"
    assert search(f, query="索引", course_id="root")["total"] == 1


def test_candidate_cap_is_explicit_and_cannot_claim_no_matching_content(fixture, monkeypatch):
    f = fixture
    monkeypatch.setattr("classcue.library_search.CANDIDATE_LIMIT", 2)
    identity = video(f, "宽泛搜索")
    f.store.mark_transcribed(identity, [
        TranscriptSegment(i + 1, i * 50_000, i * 50_000 + 1000, "范式") for i in range(4)
    ] + [TranscriptSegment(5, 201_000, 202_000, "冗余")])
    found = search(f, queries=["范式", "冗余"], match_mode="all", sources=["subtitles"])
    assert not found["search_complete"] and not found["total_is_exact"]
    assert found["candidate_truncated_sources"] == ["subtitles"]
    assert found["empty_reason"] == "incomplete_search"
    assert found["candidate_limit_per_source"] == 2


def test_overlapping_sentences_cannot_hide_an_overlong_combined_span(fixture):
    f = fixture
    identity = video(f, "重叠时间")
    f.store.mark_transcribed(identity, [
        TranscriptSegment(1, 0, 100_000, "范式"),
        TranscriptSegment(2, 1000, 2000, "冗余"),
    ])
    assert search(f, queries=["范式", "冗余"], match_mode="all", sources=["subtitles"])["total"] == 0


def test_slow_search_does_not_hold_the_task_control_lock(fixture, monkeypatch):
    import classcue.course_tools as tools

    f = fixture
    video(f, "锁测试", text="范式")
    entered, release = Event(), Event()
    original = tools.search_library

    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(tools, "search_library", slow)
    responses = []
    worker = Thread(target=lambda: responses.append(f.client.post(
        "/api/course-tools/search_library",
        json={"library_id": f.app.state.jobs.provider.library_id(), "arguments": {"query": "范式"}},
        headers={"Authorization": f"Bearer {connection_token()}"},
    )))
    worker.start()
    try:
        assert entered.wait(5)
        acquired = f.app.state.jobs._lock.acquire(timeout=.5)
        assert acquired, "Search must not prevent job cancellation"
        if acquired:
            f.app.state.jobs._lock.release()
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and responses[0].status_code == 200


def test_library_change_during_search_rejects_the_old_result(fixture, monkeypatch):
    import classcue.course_tools as tools

    f = fixture
    video(f, "换库测试", text="范式")
    original = tools.search_library

    def changing(*args, **kwargs):
        result = original(*args, **kwargs)
        monkeypatch.setattr(f.service.provider, "library_id", lambda: "another-library")
        return result

    monkeypatch.setattr(tools, "search_library", changing)
    with pytest.raises(ValueError, match="视频库身份已变化"):
        search(f, query="范式")


def test_secondary_sources_and_deleted_videos_are_not_returned(fixture):
    f = fixture
    identity = video(f, "范式", text="范式")
    StudyBookmarkStore(f.store).create(identity, 1000, "范式")
    with f.store.connect() as db:
        db.execute("UPDATE videos SET is_secondary=1 WHERE id=?", (identity,))
    assert search(f, query="范式")["empty_reason"] == "no_videos"
    with pytest.raises(KeyError):
        search(f, query="范式", video_id=identity)


def test_search_authentication_library_binding_and_argument_details(fixture):
    f = fixture
    identity = video(f, "认证测试", text="范式")
    payload = {"library_id": f.app.state.jobs.provider.library_id(), "arguments": {"query": "范式"}}
    endpoint = "/api/course-tools/search_library"
    assert f.client.post(endpoint, json=payload).status_code == 403
    headers = {"Authorization": f"Bearer {connection_token()}"}
    result = f.client.post(endpoint, json=payload, headers=headers)
    assert result.status_code == 200
    assert result.json()["items"][0]["video_id"] == identity
    assert f.client.post(endpoint, json={**payload, "library_id": "other"}, headers=headers).status_code == 409
    invalid = f.client.post(endpoint, json={**payload, "arguments": {"query": "范式", "limit": 51}},
                            headers=headers)
    assert invalid.status_code == 422 and "limit" in invalid.json()["detail"]


@pytest.mark.parametrize("arguments", [
    {}, {"query": ""}, {"query": " "}, {"query": "a\0b"}, {"queries": []},
    {"query": "闭包", "phonetic_mode": "fuzzy"}, {"query": "闭包", "phonetic_mode": True},
    {"query": "a", "queries": ["b"]}, {"query": "a", "term_groups": [["b"]]},
    {"term_groups": [[]]}, {"term_groups": [["a"]], "match_mode": "all"},
    {"term_groups": [["a"] * 8] * 6}, {"query": "a", "exclude_terms": [" "]},
    {"query": "a", "sources": []}, {"query": "a", "sources": ["unknown"]},
    {"query": "a", "sources": ["subtitles", "subtitles"]},
    {"query": "a", "video_id": True}, {"query": "a", "limit": True},
    {"query": "a", "start_seconds": 0}, {"query": "a", "start_seconds": 3, "end_seconds": 2},
    {"query": "a", "window_seconds": 121}, {"query": "a", "offset": 1},
    {"query": "a", "snapshot_id": "arbitrary"}, {"query": "a", "unknown": 1},
])
def test_unified_arguments_are_bounded_and_unambiguous(fixture, arguments):
    with pytest.raises(ValueError):
        search(fixture, **arguments)
