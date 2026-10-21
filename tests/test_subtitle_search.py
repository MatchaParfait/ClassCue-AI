from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
import classcue.video_store as video_store
from classcue.core import TranscriptSegment
from classcue.video_store import VideoStore


def add_video(store, name, texts, *, status="transcribed"):
    video_id = store.create_video(name, f"{name}.mp4", f"{name}.mp4", f"{name}.wav", 600_000)
    store.mark_transcribed(video_id, [
        TranscriptSegment(index + 1, 450_000 + index * 5000, 455_000 + index * 5000, text)
        for index, text in enumerate(texts)
    ])
    store.set_status(video_id, status)
    return video_id


@pytest.fixture
def library(tmp_path):
    store = VideoStore(tmp_path / "search.db")
    first = add_video(store, "算法第一课", ["我们先讲分治法", "分治法的时间复杂度", "课程小结"])
    second = add_video(store, "算法第二课", ["复习分治法", "动态规划"], status="failed")
    other = add_video(store, "其他课程", ["线性代数"])
    folder = store.create_folder("算法课程")
    store.set_video_folder(second, folder)
    return store, TestClient(api_main.create_app(store)), first, second, other, folder


def test_searches_across_folders_with_counts_grouping_and_jump_times(library):
    store, client, first, second, _, folder = library
    response = client.get("/api/library/search", params={"q": "  分治法  "})
    assert response.status_code == 200
    body = response.json()
    assert body["query"] == "分治法"
    assert (body["total"], body["total_videos"], body["has_more"]) == (3, 2, False)
    assert body["library_id"] == client.get("/api/health").json()["library_id"]
    assert [(item["video_id"], item["segment_id"], item["video_match_count"]) for item in body["items"]] == [
        (first, 1, 2), (first, 2, 2), (second, 1, 1),
    ]
    first_hit, _, second_hit = body["items"]
    assert first_hit["start_ms"] == 450_000
    assert first_hit["end_ms"] == 455_000
    assert first_hit["video_name"] == "算法第一课"
    assert first_hit["filename"] == "算法第一课.mp4"
    assert first_hit["folder_id"] is None
    assert first_hit["folder_name"] is None
    assert second_hit["folder_id"] == folder
    assert second_hit["folder_name"] == "算法课程"
    assert second_hit["video_status"] == "failed"
    assert "video_path" not in first_hit
    assert [part["text"] for part in first_hit["snippet_parts"] if part["match"]] == ["分治法"]
    # A video with no saved subtitles is not a subtitle search result.
    store.create_video("empty", "分治法.mp4", "missing.mp4", "missing.wav", 0)
    assert client.get("/api/library/search", params={"q": "分治法"}).json()["total"] == 3


def test_pagination_filtering_and_empty_pages(library):
    _, client, first, second, *_ = library
    pages = [client.get("/api/library/search", params={"q": "分治法", "limit": 1, "offset": i}).json()
             for i in range(4)]
    assert [page["has_more"] for page in pages] == [True, True, False, False]
    assert all(page["total"] == 3 and page["total_videos"] == 2 for page in pages)
    assert [page["items"][0]["video_id"] for page in pages[:3]] == [first, first, second]
    assert pages[3]["items"] == []
    filtered = client.get("/api/library/search", params={"q": "分治法", "video_id": second}).json()
    assert (filtered["total"], filtered["total_videos"]) == (1, 1)
    assert filtered["items"][0]["video_id"] == second
    for params in ({"q": "不存在的内容"}, {"q": "分治法", "video_id": 999999}):
        missing = client.get("/api/library/search", params=params).json()
        assert (missing["total"], missing["total_videos"], missing["items"], missing["has_more"]) == (0, 0, [], False)


@pytest.mark.parametrize("query", ["分治", "分治法"])
def test_preferred_video_is_ranked_before_pagination_without_filtering(library, query):
    _, client, first, second, other, _ = library
    pages = [client.get("/api/library/search", params={
        "q": query, "preferred_video_id": second, "limit": 1, "offset": offset,
    }).json() for offset in range(4)]
    assert [page["items"][0]["video_id"] for page in pages[:3]] == [second, first, first]
    assert [page["has_more"] for page in pages] == [True, True, False, False]
    assert all(page["total"] == 3 and page["total_videos"] == 2 for page in pages)
    assert pages[-1]["items"] == []
    for missing in (other, 999999):
        body = client.get("/api/library/search", params={"q": query, "preferred_video_id": missing}).json()
        assert [item["video_id"] for item in body["items"]] == [first, first, second]
    filtered = client.get("/api/library/search", params={
        "q": query, "video_id": first, "preferred_video_id": second,
    }).json()
    assert filtered["total"] == 2
    assert all(item["video_id"] == first for item in filtered["items"])


@pytest.mark.parametrize("text, query", [
    ("分治法", "分"), ("分治法", "分治"), ("分治法", "分治法"),
    ("Divide AND Conquer", "divide and conquer"), ("Q N CPU gpu", "q"), ("CPU gpu", "CP"),
    ("time 100% valid", "100%"), ("a_b", "a_b"), ("值 % 和 _", "%"),
    (r"C:\lesson\video", r"C:\lesson"), ('a"b "quoted"', 'a"b'),
    ("这是😀分治法", "😀分治"), ("括号 AND OR NEAR (课程)", "AND OR"),
    ("O(n log n)", "O(n log n)"), ("abc\ndef", "abc\ndef"),
    ("a'b", "a'b"), ("中文全角ＡＢＣ", "ＡＢＣ"),
])
def test_literal_chinese_short_keywords_case_and_punctuation(tmp_path, text, query):
    store = VideoStore(tmp_path / "literal.db")
    video = add_video(store, "literal", [text])
    result = store.search_subtitles(query)
    assert result["total"] == 1
    assert result["items"][0]["video_id"] == video
    highlighted = [part["text"] for part in result["items"][0]["snippet_parts"] if part["match"]]
    assert highlighted
    assert highlighted[0].lower() == query.lower()


def test_wildcards_and_query_syntax_do_not_expand_search(tmp_path):
    store = VideoStore(tmp_path / "literal.db")
    add_video(store, "literal", ["a_b 100%", "axb 1000", "分治法", "动态规划"])
    assert store.search_subtitles("a_b")["total"] == 1
    assert store.search_subtitles("100%")["total"] == 1
    assert store.search_subtitles("_")["total"] == 1
    assert store.search_subtitles("分治法 OR 动态规划")["total"] == 0
    assert store.search_subtitles("' OR 1=1 --")["total"] == 0
    assert len(store.list_videos()) == 1


def test_snippet_centers_match_and_returns_text_not_html(tmp_path):
    store = VideoStore(tmp_path / "snippet.db")
    original = "前" * 260 + "<script>分治法</script>，分治法" + "后" * 260
    add_video(store, "snippet", [original])
    hit = store.search_subtitles("分治法")["items"][0]
    assert hit["text"] == original
    assert len(hit["snippet"]) <= 202
    assert hit["snippet"].startswith("…") and hit["snippet"].endswith("…")
    assert hit["snippet"] == "".join(part["text"] for part in hit["snippet_parts"])
    assert [part["text"] for part in hit["snippet_parts"] if part["match"]] == ["分治法", "分治法"]
    assert "<script>" in hit["snippet"]
    assert "<mark>" not in hit["snippet"]


@pytest.mark.parametrize("params", [
    {}, {"q": ""}, {"q": "  \t\n "}, {"q": "a" * 121}, {"q": "a\x00b"},
    {"q": "分治法", "limit": 0}, {"q": "分治法", "limit": 101},
    {"q": "分治法", "offset": -1}, {"q": "分治法", "video_id": 0},
    {"q": "分治法", "offset": 1 << 63}, {"q": "分治法", "video_id": 1 << 63},
    {"q": "分治法", "video_id": "invalid"},
    {"q": "分治法", "preferred_video_id": 0}, {"q": "分治法", "preferred_video_id": -1},
    {"q": "分治法", "preferred_video_id": 1 << 63}, {"q": "分治法", "preferred_video_id": "invalid"},
])
def test_api_rejects_invalid_search_parameters_before_database_access(tmp_path, monkeypatch, params):
    application = api_main.create_app(VideoStore(tmp_path / "input.db"))
    monkeypatch.setattr(api_main.StoreProvider, "get", lambda _: pytest.fail("Invalid search accessed the database"))
    assert TestClient(application).get("/api/library/search", params=params).status_code == 422


def test_new_subtitles_replacement_updates_and_deletes_are_immediately_searchable(library):
    store, client, first, second, *_ = library
    store.mark_transcribed(first, [TranscriptSegment(30, 2000, 3000, "现在讲动态规划")])
    assert store.search_subtitles("分治法")["total"] == 1
    assert store.search_subtitles("动态规划")["total"] == 2
    with store.connect() as connection:
        connection.execute(
            "UPDATE video_transcript_segments SET text = ?, start_ms = ? WHERE video_id = ? AND segment_id = 30",
            ("改成贪心算法", 2500, first),
        )
    assert store.search_subtitles("动态规划")["total"] == 1
    assert store.search_subtitles("贪心算法")["items"][0]["start_ms"] == 2500
    with store.connect() as connection:
        connection.execute("DELETE FROM videos WHERE id = ?", (second,))
    assert store.search_subtitles("分治法")["total"] == 0
    assert store.search_subtitles("动态规划")["total"] == 0
    assert client.get("/api/library/search", params={"q": "贪心算法"}).json()["total"] == 1
    with store.connect() as connection:
        connection.execute("INSERT INTO video_subtitle_fts(video_subtitle_fts, rank) VALUES ('integrity-check', 1)")


def test_failed_replacement_rolls_back_original_subtitles_and_index(library):
    store, _, first, *_ = library
    original = store.search_subtitles("分治法")
    with pytest.raises(sqlite3.IntegrityError):
        store.replace_transcript_for_reanalysis(first, [
            TranscriptSegment(1, 0, 1000, "新转写内容"),
            TranscriptSegment(1, 1000, 2000, "重复 ID 导致事务失败"),
        ], "test-model")
    assert store.search_subtitles("分治法") == original
    assert store.search_subtitles("新转写内容")["total"] == 0
    store.replace_transcript_for_reanalysis(first, [TranscriptSegment(1, 1000, 2000, "新转写内容")], "test-model")
    assert store.search_subtitles("分治法")["total"] == 1
    assert store.search_subtitles("新转写内容")["total"] == 1


def test_video_and_folder_metadata_are_live_without_reindexing(library):
    store, _, first, _, _, folder = library
    store.update_video_display_name(first, "新的课程名称")
    store.set_video_folder(first, folder)
    store.update_folder(folder, "改名的文件夹")
    with store.connect() as connection:
        connection.execute("UPDATE videos SET filename = ?, video_path = ? WHERE id = ?", ("renamed.mp4", "new/location.mp4", first))
    hit = store.search_subtitles("分治法")["items"][0]
    assert hit["video_id"] == first
    assert hit["filename"] == "renamed.mp4"
    assert hit["video_name"] == "新的课程名称"
    assert hit["folder_name"] == "改名的文件夹"
    assert hit["start_ms"] == 450_000


def remove_search_schema(store):
    with store.connect() as connection:
        for name in ("insert", "delete", "update"):
            connection.execute(f"DROP TRIGGER video_subtitle_search_{name}")
        connection.execute("DROP TABLE video_subtitle_fts")


def test_existing_database_backfill_preserves_subtitles_and_review_edits(library, monkeypatch):
    store, _, first, *_ = library
    marker = store.add_marker(first, 450_000, 455_000, "考点", "说明", "分治法", "重点", .9)
    store.set_marker_jump_time(marker, 440_000)
    store.set_marker_importance(marker, "critical")
    store.set_marker_review_status(marker, "mastered")
    original_marker = dict(store.list_markers(first)[0])
    original_subtitles = store.get_segments(first)
    remove_search_schema(store)
    reopened = VideoStore(store.path)
    assert reopened.search_subtitles("分治法")["total"] == 3
    assert reopened.get_segments(first) == original_subtitles
    assert dict(reopened.list_markers(first)[0]) == original_marker
    base = video_store.ClosingConnection

    class NoRebuild(base):
        def execute(self, sql, *args, **kwargs):
            assert "'rebuild'" not in sql, "Existing search index should not be rebuilt on every startup"
            return super().execute(sql, *args, **kwargs)

    monkeypatch.setattr(video_store, "ClosingConnection", NoRebuild)
    assert VideoStore(store.path).search_subtitles("分治法")["total"] == 3


def test_failed_backfill_rolls_back_and_restart_recovers(library, monkeypatch):
    store, _, first, *_ = library
    original = store.get_segments(first)
    remove_search_schema(store)
    base = video_store.ClosingConnection

    class FailRebuild(base):
        def execute(self, sql, *args, **kwargs):
            if "'rebuild'" in sql:
                raise sqlite3.OperationalError("injected backfill failure")
            return super().execute(sql, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(video_store, "ClosingConnection", FailRebuild)
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            VideoStore(store.path)
    with store.connect() as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE name LIKE 'video_subtitle_%'").fetchall() == []
    assert store.get_segments(first) == original
    assert VideoStore(store.path).search_subtitles("分治法")["total"] == 3


def test_concurrent_upgrade_does_not_duplicate_search_results(library):
    store, *_ = library
    remove_search_schema(store)
    with ThreadPoolExecutor(max_workers=3) as pool:
        totals = list(pool.map(lambda _: VideoStore(store.path).search_subtitles("分治法")["total"], range(3)))
    assert totals == [3, 3, 3]


def test_separate_application_instances_keep_search_identity_and_data_isolated(library, tmp_path, monkeypatch):
    store, client, *_ = library
    second_store = VideoStore(tmp_path / "isolated-test-app" / "project.db")
    add_video(second_store, "另一测试实例", ["另一实例的分治法"])
    for name in ("create_judge", "model_is_downloaded", "process_video_once"):
        monkeypatch.setattr(api_main, name, lambda *a, **kw: pytest.fail("Search must stay local and read-only"))
    first_response = client.get("/api/library/search", params={"q": "分治法"}).json()
    with TestClient(api_main.create_app(second_store)) as second:
        second_response = second.get("/api/library/search", params={"q": "分治法"}).json()
        assert second_response["total"] == 1
        assert second_response["library_id"] != first_response["library_id"]
    assert store.search_subtitles("分治法")["total"] == 3


def test_search_counts_and_page_share_snapshot_during_transcription(library, monkeypatch):
    store, _, first, *_ = library
    base = video_store.ClosingConnection
    changed = False

    class ConcurrentWrite(base):
        def execute(self, sql, *args, **kwargs):
            nonlocal changed
            if "counts AS" in sql and not changed:
                changed = True
                store.mark_transcribed(first, [TranscriptSegment(1, 0, 1000, "替换后的其他内容")])
            return super().execute(sql, *args, **kwargs)

    monkeypatch.setattr(video_store, "ClosingConnection", ConcurrentWrite)
    page = store.search_subtitles("分治法")
    assert changed
    assert page["total"] == len(page["items"]) == 3
    assert store.search_subtitles("分治法")["total"] == 1


def test_search_is_read_only_and_uses_one_closed_connection(library, monkeypatch):
    store, client, *_ = library
    base = video_store.ClosingConnection
    connections = []

    class ReadOnlyConnection(base):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.closed = False
            self.execute("PRAGMA query_only = ON")
            connections.append(self)

        def close(self):
            self.closed = True
            super().close()

    monkeypatch.setattr(video_store, "ClosingConnection", ReadOnlyConnection)
    result = client.get("/api/library/search", params={"q": "分治法"})
    assert result.status_code == 200
    assert result.json()["total"] == 3
    assert len(connections) == 1
    assert connections[0].closed
