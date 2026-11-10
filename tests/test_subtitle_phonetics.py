"""Pronunciation retrieval over synthetic subtitles only."""

from concurrent.futures import ThreadPoolExecutor
import sqlite3
import pytest

from classcue.core import TranscriptSegment
from classcue.subtitle_phonetics import find_matches, pronunciation, query_key
from classcue.video_store import VideoStore
from tests.test_course_tools import fixture, video, call


def search(f, **args):
    return call(f, "search_library", sources=["subtitles"], **args)


@pytest.mark.parametrize("raw", ["这里的 B包可以访问外部变量", "这里的 b包可以访问外部变量",
                                     "这里的闭胞可以访问外部变量", "这里的比包可以访问外部变量"])
def test_auto_fallback_preserves_raw_evidence_time_and_marks_hypothesis(fixture, raw):
    f = fixture
    identity = video(f, "合成程序设计", text=raw)
    found = search(f, query="闭包")
    assert found["phonetic_mode"] == "auto" and found["phonetic_search_used"]
    assert found["match_type"] == "phonetic_passage"
    assert found["phonetic_terms"] == ["闭包"] and found["phonetic_index_pending"] == 0
    assert found["search_complete"] and found["total_is_exact"] and found["total"] == 1
    item = found["items"][0]
    assert item["match_type"] == "phonetic"
    assert item["position_seconds"] == 10 and item["end_seconds"] == 20
    evidence = item["evidence"][0]
    assert evidence["evidence_type"] == "asr_transcript" and evidence["text"] == raw
    assert evidence["matched_terms"] == ["闭包"] and evidence["literal_matched_terms"] == []
    match = evidence["phonetic_matches"][0]
    assert match["query_term"] == "闭包" and match["syllables"] == "bi bao"
    assert match["requires_verification"] is True
    assert raw[match["start_char"]:match["end_char"]] == match["source_text"]
    assert f.store.get_segments(identity)[0].text == raw
    assert f.store.list_active_jobs() == []


def test_literal_success_off_mode_and_explicit_wrong_word_do_not_build_index(fixture, monkeypatch):
    f = fixture
    exact = video(f, "原词", text="闭包能够访问外部变量")
    wrong = video(f, "错词", text="B包能够访问外部变量")
    def no_conversion(*_a, **_k):
        pytest.fail("A literal search must not convert any pronunciation")
    monkeypatch.setattr("classcue.library_search.query_key", no_conversion)
    for arguments in [
        {"query": "闭包"},
        {"query": "闭包", "phonetic_mode": "off"},
        {"queries": ["闭包", "B包"]},
    ]:
        found = search(f, **arguments)
        assert not found["phonetic_search_used"]
        assert found["items"][0]["video_id"] == exact
    assert search(f, query="找不到的内容", phonetic_mode="off")["total"] == 0
    with f.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM video_subtitle_phonetics").fetchone()[0] == 0
    assert wrong != exact


def test_always_adds_candidates_and_literal_hits_rank_first(fixture):
    f = fixture
    wrong = video(f, "错词", text="B包的作用")
    exact = video(f, "原词", text="闭包的作用")
    literal = search(f, query="闭包")
    assert [i["video_id"] for i in literal["items"]] == [exact]
    combined = search(f, query="闭包", phonetic_mode="always")
    assert [i["video_id"] for i in combined["items"]] == [exact, wrong]
    assert [i["match_type"] for i in combined["items"]] == ["literal", "phonetic"]
    assert combined["source_counts"]["subtitles"] == 2


def test_same_sound_is_syllable_bounded_and_short_terms_do_not_allow_near_sounds(fixture):
    f = fixture
    identity = video(f, "边界")
    f.store.mark_transcribed(identity, [
        TranscriptSegment(i + 1, i * 50_000, i * 50_000 + 1000, text)
        for i, text in enumerate(["比包", "皮包", "鼻胞", "笔报", "闭波", "比保",
                                 "B包装", "bi包", "闭，包", "闭1包", "闭book包", "闭abc包"])
    ])
    found = search(f, query="闭包")
    assert [e["text"] for item in found["items"] for e in item["evidence"]] == [
        "比包", "鼻胞", "笔报", "比保", "B包装"]
    single = search(f, query="闭")
    assert not single["phonetic_search_used"]
    assert single["total"] == search(f, query="闭", phonetic_mode="off")["total"]
    assert not search(f, query="Python")["phonetic_search_used"]
    assert not search(f, query="a' OR 1=1 --")["phonetic_search_used"]


def test_phrase_readings_and_offsets_with_polyphones_and_multisyllable_letters():
    # Phrase-level readings are supplied by pypinyin; do not enumerate every reading.
    assert query_key("银行") == " yin hang "
    assert pronunciation("这里讲银杭")[0].find(query_key("银行")) >= 0
    keys = {"艾姆包": query_key("艾姆包")}
    matches = find_matches("测试 M包 与 ordinaryWord", keys)
    assert matches[0]["source_text"] == "M包"
    assert find_matches("M包", {"姆包": query_key("姆包")}) == []
    assert query_key("闭包") == query_key("B包")
    assert query_key("闭，包") is None
    assert query_key("一" * 25) is None


def test_groups_match_across_nearby_sentences_and_keep_literal_terms(fixture):
    f = fixture
    identity = video(f, "组合")
    f.store.mark_transcribed(identity, [
        TranscriptSegment(1, 1000, 2000, "B包的定义"),
        TranscriptSegment(2, 5000, 6000, "它访问外部变量"),
        TranscriptSegment(3, 80_000, 81_000, "B包的示例"),
    ])
    found = search(f, term_groups=[["闭包"], ["外部变量"]])
    assert found["total"] == 1
    item = found["items"][0]
    assert item["match_type"] == "mixed"
    assert item["matched_terms"] == ["闭包", "外部变量"]
    assert (item["start_seconds"], item["end_seconds"]) == (1, 6)
    assert search(f, term_groups=[["闭包"], ["外部变量"]], window_seconds=2)["total"] == 0
    assert search(f, query="闭包外部变量")["total"] == 0


def test_exclusions_stay_literal_and_cover_nonmatching_passage_sentences(fixture):
    f = fixture
    identity = video(f, "排除")
    f.store.mark_transcribed(identity, [
        TranscriptSegment(1, 1000, 2000, "B包"),
        TranscriptSegment(2, 3000, 4000, "作业通知"),
        TranscriptSegment(3, 5000, 6000, "外部变量"),
    ])
    args = {"queries": ["闭包", "外部变量"], "match_mode": "all"}
    assert search(f, **args)["total"] == 1
    assert search(f, **args, exclude_terms=["作业通知"])["total"] == 0
    assert search(f, query="闭包", exclude_terms=["闭包"])["total"] == 1
    assert search(f, query="闭包", exclude_terms=["B包"])["total"] == 0


def test_scope_and_time_apply_to_index_building_and_result_selection(fixture):
    f = fixture
    parent = f.store.create_folder("范围")
    child = f.store.create_folder("子目录", parent)
    inside = video(f, "范围内", child)
    outside = video(f, "范围外", text="B包")
    f.store.mark_transcribed(inside, [
        TranscriptSegment(1, 1000, 2000, "B包"),
        TranscriptSegment(2, 50_000, 51_000, "B包"),
    ])
    found = search(f, query="闭包", course_id=f"folder:{parent}", start_seconds=0, end_seconds=5)
    assert [i["video_id"] for i in found["items"]] == [inside]
    with f.store.connect() as db:
        indexed = db.execute(
            "SELECT s.video_id,s.segment_id FROM video_subtitle_phonetics p "
            "JOIN video_transcript_segments s ON s.row_id=p.row_id").fetchall()
        assert [tuple(row) for row in indexed] == [(inside, 1)]
    assert search(f, query="闭包", course_id=f"folder:{parent}", video_id=outside)["empty_reason"] == "no_videos"
    assert [i["video_id"] for i in search(f, query="闭包", course_id="root")["items"]] == [outside]


def test_other_sources_remain_literal_and_do_not_trigger_conversion(fixture):
    from classcue.study_bookmarks import StudyBookmarkStore
    f = fixture
    identity = video(f, "B包", text="B包")
    StudyBookmarkStore(f.store).create(identity, 3000, "B包")
    f.store.add_marker(identity, 3000, 5000, "B包", "", "", "x", .9)
    found = call(f, "search_library", query="闭包", sources=["catalog", "bookmarks", "analysis"],
                 phonetic_mode="always")
    assert found["total"] == 0 and not found["phonetic_search_used"]
    found = call(f, "search_library", query="闭包", phonetic_mode="always")
    assert found["items"][0]["sources"] == ["subtitles"]


def test_index_is_lazy_persistent_incremental_and_invalidated_by_sql_text_updates(fixture, monkeypatch):
    f = fixture
    identity = video(f, "增量", text="B包")
    with f.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM video_subtitle_phonetics").fetchone()[0] == 0
    assert search(f, query="闭包")["total"] == 1
    # Reopening an old store leaves the populated derived index intact.
    reopened = VideoStore(f.store.path)
    with reopened.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM video_subtitle_phonetics").fetchone()[0] == 1
    def no_rebuild(*_a, **_k):
        pytest.fail("Unchanged subtitles must reuse the index")
    with monkeypatch.context() as patch:
        patch.setattr("classcue.library_search.prepare_phonetic_index", no_rebuild)
        assert search(f, query="闭包")["total"] == 1
    with f.store.connect() as db:
        db.execute("UPDATE video_transcript_segments SET text='线性代数' WHERE video_id=?", (identity,))
        assert db.execute("SELECT COUNT(*) FROM video_subtitle_phonetics").fetchone()[0] == 0
    assert search(f, query="闭包")["total"] == 0
    f.store.mark_transcribed(identity, [TranscriptSegment(1, 1000, 2000, "闭胞")])
    assert search(f, query="闭包")["total"] == 1
    with f.store.connect() as db:
        db.execute("DELETE FROM video_transcript_segments WHERE video_id=?", (identity,))
        assert db.execute("SELECT COUNT(*) FROM video_subtitle_phonetics").fetchone()[0] == 0
        assert db.execute(
            'SELECT COUNT(*) FROM video_subtitle_phonetic_fts WHERE video_subtitle_phonetic_fts MATCH ?',
            ('" bi bao "',)).fetchone()[0] == 0


def test_pending_index_is_explicit_and_repeated_search_can_finish_it(fixture, monkeypatch):
    f = fixture
    identity = video(f, "预算")
    f.store.mark_transcribed(identity, [
        TranscriptSegment(1, 1000, 2000, "线性代数"),
        TranscriptSegment(2, 50_000, 51_000, "B包"),
    ])
    monkeypatch.setattr("classcue.subtitle_phonetics.INDEX_BUILD_LIMIT", 1)
    first = search(f, query="闭包")
    assert first["total"] == 0 and first["phonetic_index_pending"] == 1
    assert not first["search_complete"] and not first["total_is_exact"]
    assert first["empty_reason"] == "incomplete_search"
    second = search(f, query="闭包")
    assert second["total"] == 1 and second["phonetic_index_pending"] == 0
    assert second["search_complete"]
    assert second["snapshot_id"] != first["snapshot_id"]


def test_character_budget_limits_long_subtitle_backfill(fixture, monkeypatch):
    f = fixture
    identity = video(f, "字符预算")
    f.store.mark_transcribed(identity, [
        TranscriptSegment(i + 1, i * 50_000, i * 50_000 + 1000, "内容" * 100 + "B包")
        for i in range(3)
    ])
    monkeypatch.setattr("classcue.subtitle_phonetics.INDEX_CHARACTER_LIMIT", 100)
    found = search(f, query="闭包")
    assert found["total"] == 1 and found["phonetic_index_pending"] == 2
    assert not found["search_complete"]


def test_phonetic_candidate_cap_and_literal_candidate_cap_remain_explicit(fixture, monkeypatch):
    f = fixture
    identity = video(f, "候选上限")
    f.store.mark_transcribed(identity, [
        TranscriptSegment(i + 1, i * 50_000, i * 50_000 + 1000, "B包") for i in range(3)
    ])
    monkeypatch.setattr("classcue.library_search.CANDIDATE_LIMIT", 1)
    found = search(f, query="闭包")
    assert not found["search_complete"] and found["candidate_truncated_sources"] == ["subtitles"]
    f.store.mark_transcribed(identity, [
        TranscriptSegment(i + 1, i * 50_000, i * 50_000 + 1000, "闭包") for i in range(3)
    ])
    found = search(f, queries=["闭包", "找不到"], match_mode="all")
    assert not found["phonetic_search_used"] and not found["search_complete"]


def test_literal_candidates_cannot_be_crowded_out_by_earlier_homophones(fixture, monkeypatch):
    f = fixture
    video(f, "同音候选", text="B包")
    exact = video(f, "字面候选", text="闭包")
    monkeypatch.setattr("classcue.library_search.CANDIDATE_LIMIT", 1)
    found = search(f, query="闭包", phonetic_mode="always")
    assert found["items"][0]["video_id"] == exact
    assert found["items"][0]["match_type"] == "literal"
    assert not found["search_complete"]


def test_phonetic_pagination_rejects_changed_sources_or_modes(fixture):
    f = fixture
    for i in range(3):
        video(f, f"分页{i}", text="B包")
    args = {"query": "闭包", "limit": 1}
    first = search(f, **args)
    second = search(f, **args, offset=1, snapshot_id=first["snapshot_id"])
    assert second["items"][0]["result_id"] != first["items"][0]["result_id"]
    with pytest.raises(ValueError, match="资料或参数已变化"):
        search(f, **args, phonetic_mode="always", offset=1, snapshot_id=first["snapshot_id"])
    f.store.mark_transcribed(first["items"][0]["video_id"], [TranscriptSegment(1, 1000, 2000, "代数")])
    with pytest.raises(ValueError, match="资料或参数已变化"):
        search(f, **args, offset=1, snapshot_id=first["snapshot_id"])


def test_long_excerpt_contains_raw_phonetic_match_and_relative_offsets(fixture):
    f = fixture
    video(f, "长句", text="前" * 1200 + "B包" + "后" * 600)
    item = search(f, query="闭包")["items"][0]["evidence"][0]
    assert item["text_truncated"] and len(item["text"]) <= 500
    match = item["phonetic_matches"][0]
    assert match["source_text"] == item["text"][match["start_char"]:match["end_char"]] == "B包"


def test_concurrent_cache_warmups_do_not_duplicate_or_corrupt_entries(fixture):
    f = fixture
    video(f, "并发", text="B包")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: search(f, query="闭包"), range(2)))
    assert all(r["total"] == 1 and r["search_complete"] for r in results)
    with f.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM video_subtitle_phonetics").fetchone()[0] == 1
        db.execute("INSERT INTO video_subtitle_phonetic_fts(video_subtitle_phonetic_fts,rank) "
                   "VALUES('integrity-check',1)")


def test_stale_text_during_conversion_cannot_publish_wrong_index(fixture, monkeypatch):
    import classcue.subtitle_phonetics as phonetics
    f = fixture
    identity = video(f, "转换时改字", text="B包")
    original = phonetics.pronunciation
    changed = False
    def change_source(text):
        nonlocal changed
        if text == "B包" and not changed:
            changed = True
            with f.store.connect() as db:
                db.execute("UPDATE video_transcript_segments SET text='线性代数' WHERE video_id=?", (identity,))
        return original(text)
    monkeypatch.setattr(phonetics, "pronunciation", change_source)
    found = search(f, query="闭包")
    assert changed and found["total"] == 0 and found["search_complete"]
    with f.store.connect() as db:
        assert db.execute("SELECT pronunciation FROM video_subtitle_phonetics").fetchone()[0] != " bi bao "


def test_failed_transcript_replacement_rolls_back_cached_index_too(fixture):
    f = fixture
    identity = video(f, "失败保留", text="B包")
    original = search(f, query="闭包")
    with pytest.raises(sqlite3.IntegrityError):
        f.store.replace_transcript_for_reanalysis(identity, [
            TranscriptSegment(1, 0, 1000, "新内容"),
            TranscriptSegment(1, 1000, 2000, "重复编号"),
        ], "synthetic")
    assert search(f, query="闭包") == original
    assert f.store.get_segments(identity)[0].text == "B包"


def test_changed_index_version_rebuilds_without_changing_sources(fixture):
    f = fixture
    identity = video(f, "索引版本", text="B包")
    assert search(f, query="闭包")["total"] == 1
    with f.store.connect() as db:
        db.execute("UPDATE video_subtitle_phonetics SET version=0,pronunciation=' old key '")
    assert search(f, query="闭包")["total"] == 1
    assert f.store.get_segments(identity)[0].text == "B包"
    with f.store.connect() as db:
        assert db.execute("SELECT version FROM video_subtitle_phonetics").fetchone()[0] == 1
        db.execute("INSERT INTO video_subtitle_phonetic_fts(video_subtitle_phonetic_fts,rank) "
                   "VALUES('integrity-check',1)")
