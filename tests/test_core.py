import pytest

from classcue.core import (
    CandidateDecision,
    TranscriptSegment,
    build_candidate_window,
    build_semantic_chunks,
    filter_analysis_segments,
    find_all_trigger_matches,
    find_trigger_matches,
    is_low_information_analysis_segment,
    merge_candidate_windows,
    parse_model_json,
    parse_semantic_json,
    transcript_to_vtt,
    validate_decision,
    validate_trigger_groups,
)
from classcue.db import Database
from classcue.runtime import select_whisper_backend


@pytest.fixture
def segments():
    return [
        TranscriptSegment(1, 0, 4000, "二次函数的顶点可以用配方法求出。"),
        TranscriptSegment(2, 4200, 8000, "这个会考，特别是配方法的步骤。"),
        TranscriptSegment(3, 8200, 13000, "先提出二次项系数，再补上和减去同一个数。"),
        TranscriptSegment(4, 17000, 21000, "接下来我们看判别式。"),
    ]


def test_expanded_search_finds_alias_and_exact_search_does_not(segments):
    assert find_trigger_matches(segments, "重点", expanded=True) == []
    assert [match.phrase for match in find_trigger_matches(segments, "会考", expanded=True)] == ["会考"]


def test_expanded_search_uses_querys_synonym_group(segments):
    alternative = [TranscriptSegment(10, 0, 1000, "这个知识点是高频考点。")]
    assert [match.phrase for match in find_trigger_matches(alternative, "必考", expanded=True)] == ["高频考点"]


def test_high_recall_search_and_window_merging(segments):
    extra = segments + [TranscriptSegment(5, 13200, 16000, "这个步骤一定要会，也很容易丢分。")]
    matches = find_all_trigger_matches(extra)
    assert {match.phrase for match in matches} >= {"会考", "一定要会", "容易丢分"}
    candidates = merge_candidate_windows(extra, matches)
    assert len(candidates) == 1
    assert {match.phrase for match in candidates[0].triggers} >= {"会考", "一定要会", "容易丢分"}


def test_high_recall_terms_cover_learning_instruction_and_exam_variants():
    candidates = [
        TranscriptSegment(1, 0, 1000, "这个公式圈起来，考卷会有。"),
        TranscriptSegment(2, 1200, 2200, "这个地方是易错点。"),
    ]
    phrases = {match.phrase for match in find_all_trigger_matches(candidates)}
    assert {"圈起来", "考卷会有", "易错点"}.issubset(phrases)


def test_candidate_stops_at_topic_boundary(segments):
    window = build_candidate_window(segments, 2)
    assert [segment.id for segment in window] == [1, 2, 3]


def test_json_parser_accepts_fenced_output():
    decision = parse_model_json('```json\n{"is_exam_point": true, "start_sentence_id": 1, "end_sentence_id": 2, "knowledge_point": "配方法", "confidence": 0.8}\n```')
    assert decision.is_exam_point is True
    assert decision.confidence == 0.8


def test_semantic_chunks_cover_full_transcript_with_overlap():
    source = [
        TranscriptSegment(1, 0, 50000, "第一段"),
        TranscriptSegment(2, 51000, 100000, "第二段"),
        TranscriptSegment(3, 101000, 150000, "第三段"),
    ]
    chunks = build_semantic_chunks(source, max_seconds=100, overlap_seconds=20)
    assert [segment.id for segment in chunks[0]] == [1, 2]
    assert [segment.id for segment in chunks[1]] == [2, 3]
    assert {segment.id for chunk in chunks for segment in chunk} == {1, 2, 3}


def test_default_semantic_chunks_cover_about_five_minutes():
    source = [
        TranscriptSegment(index, (index - 1) * 60000, index * 60000 - 1000, f"第 {index} 分钟")
        for index in range(1, 8)
    ]
    chunks = build_semantic_chunks(source)

    assert [segment.id for segment in chunks[0]] == [1, 2, 3, 4, 5]
    assert {segment.id for chunk in chunks for segment in chunk} == set(range(1, 8))


def test_semantic_json_parser_accepts_possible_candidate():
    selection = parse_semantic_json('{"candidates": [{"level": "possible", "start_sentence_id": 1, "end_sentence_id": 2, "title": "配方法"}]}')
    assert selection.candidates[0].title == "配方法"


def test_formula_noise_filter_removes_isolated_formula_asr_but_keeps_exam_signal():
    noise = TranscriptSegment(1, 0, 2000, "x加y的平方")
    exam_formula = TranscriptSegment(2, 3000, 5000, "x加y的平方，这个会考")
    explanatory = TranscriptSegment(3, 6000, 9000, "这个公式要掌握")

    assert is_low_information_analysis_segment(noise) is True
    assert is_low_information_analysis_segment(exam_formula) is False
    assert is_low_information_analysis_segment(explanatory) is False


def test_formula_noise_filter_keeps_original_context_near_meaningful_text():
    source = [
        TranscriptSegment(1, 0, 2000, "x加y"),
        TranscriptSegment(2, 3000, 5000, "这是一个重要的定义"),
        TranscriptSegment(3, 6000, 8000, "x减y"),
        TranscriptSegment(4, 40000, 42000, "嗯嗯嗯"),
    ]

    filtered = filter_analysis_segments(source)

    assert [segment.id for segment in filtered] == [1, 2, 3]
    assert [segment.id for segment in source] == [1, 2, 3, 4]


def test_formula_noise_filter_keeps_formula_near_exam_signal():
    source = [
        TranscriptSegment(1, 0, 2000, "x加y"),
        TranscriptSegment(2, 3000, 5000, "这个会考"),
    ]

    assert [segment.id for segment in filter_analysis_segments(source)] == [1, 2]


def test_transcript_vtt_uses_timestamped_cues():
    vtt = transcript_to_vtt([TranscriptSegment(1, 1250, 3125, "第一 行 --> 字幕")])
    assert vtt.startswith("WEBVTT")
    assert "00:00:01.250 --> 00:00:03.125" in vtt
    assert "第一 行 → 字幕" in vtt


def test_user_trigger_library_discards_empty_and_duplicate_phrases():
    groups = validate_trigger_groups({"custom": {"考试": [" 会考 ", "会考", "", "重点"]}})
    assert groups == {"custom": {"考试": ["会考", "重点"]}}


def test_validation_rejects_out_of_window_range(segments):
    decision = CandidateDecision(is_exam_point=True, start_sentence_id=1, end_sentence_id=99, confidence=0.8)
    checked = validate_decision(decision, segments[:3])
    assert checked.is_exam_point is False
    assert checked.confidence == 0


def test_database_keeps_evidence_and_allows_reused_segment_ids(tmp_path):
    database = Database(tmp_path / "demo.db")
    first = database.create_recording("first.mp3", "first.mp3")
    second = database.create_recording("second.mp3", "second.mp3")
    source = [TranscriptSegment(1, 0, 1000, "这个会考。")]
    database.replace_transcript(first, source)
    database.replace_transcript(second, source)
    assert database.get_segments(first)[0].id == 1
    assert database.get_segments(second)[0].id == 1

    decision = CandidateDecision(
        is_exam_point=True,
        trigger_sentence_ids=[1],
        start_sentence_id=1,
        end_sentence_id=1,
        knowledge_point="原始标题",
        summary="原始摘要",
        exam_signal_quote="这个会考。",
        confidence=0.9,
    )
    candidate_id = database.create_candidate(first, "会考", decision, source)
    database.confirm_candidate(candidate_id, "用户编辑后的标题", "用户笔记", 0, 1000)
    point = database.list_points(first)[0]
    assert point["title"] == "用户编辑后的标题"
    assert point["source_quote"] == "这个会考。"
    database.update_point(point["id"], "再次编辑", "更新笔记", 100, 900)
    updated = database.list_points(first)[0]
    assert updated["title"] == "再次编辑"
    assert updated["source_quote"] == "这个会考。"


def test_whisper_uses_ctranslate2_cuda_not_pytorch_state():
    backend = select_whisper_backend(1, {"float16", "int8_float16"})
    assert backend.device == "cuda"
    assert backend.compute_type == "float16"


def test_whisper_falls_back_to_cpu_without_ctranslate2_cuda():
    backend = select_whisper_backend(0, set())
    assert backend.device == "cpu"
    assert backend.compute_type == "int8"
