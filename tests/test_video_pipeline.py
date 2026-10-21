import threading
import time

import pytest

from core import SemanticCandidate, SemanticSelection, TranscriptSegment
from judge import SemanticResponseError
from video_pipeline import (
    MAX_CONCURRENT_AI_REQUESTS,
    analyze_prepared_video_once,
    build_screening_chunks,
    create_semantic_markers,
    prepare_video_for_analysis,
    process_saved_analysis_once,
    _transcribe_video_with_fallback,
    register_local_video,
)
from video_store import VideoStore


class FakeSemanticJudge:
    def judge_semantic_chunk(self, chunk):
        return SemanticSelection(
            candidates=[
                SemanticCandidate(
                    level="possible",
                    start_sentence_id=chunk[0].id,
                    end_sentence_id=chunk[-1].id,
                    title="配方法步骤",
                    reason="教师解释了完整解题步骤。",
                    evidence_quote=chunk[0].text,
                    confidence=0.8,
                )
            ]
        )


class BrokenSemanticJudge:
    def judge_semantic_chunk(self, chunk):
        raise SemanticResponseError("invalid JSON")


class TrackingJudge:
    def __init__(self):
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.total_calls = 0

    def judge_semantic_chunk(self, chunk):
        with self.lock:
            self.active += 1
            self.total_calls += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.03)
        with self.lock:
            self.active -= 1
        return SemanticSelection()


def test_semantic_pipeline_marks_candidates_without_keyword_filtering(tmp_path):
    store = VideoStore(tmp_path / "video.db")
    video_id = store.create_video("hash", "lesson.mp4", "lesson.mp4", "lesson.wav", 10000)
    segments = [
        TranscriptSegment(1, 0, 3000, "二次函数的顶点可以用配方法得到。"),
        TranscriptSegment(2, 3200, 6000, "先提出二次项系数，再补上同一个数。"),
    ]
    count = create_semantic_markers(store, video_id, segments, FakeSemanticJudge())
    markers = store.list_markers(video_id)
    assert count == 1
    assert len(markers) == 1
    assert markers[0]["title"] == "配方法步骤"
    assert markers[0]["trigger_phrase"] == "AI-possible"


def test_semantic_pipeline_preserves_bad_json_chunk_for_review(tmp_path):
    store = VideoStore(tmp_path / "video.db")
    video_id = store.create_video("hash", "lesson.mp4", "lesson.mp4", "lesson.wav", 10000)
    segments = [TranscriptSegment(1, 0, 3000, "这一段需要人工查看。")]
    count = create_semantic_markers(store, video_id, segments, BrokenSemanticJudge())
    marker = store.list_markers(video_id)[0]
    assert count == 1
    assert marker["title"] == "AI 输出待复核"


def test_semantic_pipeline_limits_api_screening_to_four_parallel_requests(tmp_path, monkeypatch):
    import video_pipeline

    store = VideoStore(tmp_path / "video.db")
    video_id = store.create_video("hash", "lesson.mp4", "lesson.mp4", "lesson.wav", 10000)
    segments = [TranscriptSegment(index, index * 1000, index * 1000 + 500, f"第 {index} 句") for index in range(1, 7)]
    monkeypatch.setattr(video_pipeline, "build_semantic_chunks", lambda values: [(segment,) for segment in values])
    judge = TrackingJudge()

    assert create_semantic_markers(store, video_id, segments, judge) == 0
    assert judge.max_active == MAX_CONCURRENT_AI_REQUESTS


def test_keyword_prefilter_skips_chunks_without_configured_signals(monkeypatch):
    import video_pipeline

    segments = [
        TranscriptSegment(1, 0, 1000, "普通讲解。"),
        TranscriptSegment(2, 20000, 21000, "这个会考。"),
        TranscriptSegment(3, 40000, 41000, "另一段普通讲解。"),
    ]
    chunks = [(segments[0],), (segments[1],), (segments[2],)]
    monkeypatch.setattr(video_pipeline, "build_semantic_chunks", lambda values: chunks)

    selected = build_screening_chunks(segments, use_keyword_prefilter=True)

    assert chunks[1] in selected
    assert chunks[0] not in selected
    assert chunks[2] not in selected


def test_keyword_prefilter_uses_custom_trigger_library(monkeypatch):
    import video_pipeline

    segments = [
        TranscriptSegment(1, 0, 1000, "普通讲解。"),
        TranscriptSegment(2, 20000, 21000, "老师说这是自定义信号。"),
    ]
    chunks = [(segments[0],), (segments[1],)]
    monkeypatch.setattr(video_pipeline, "build_semantic_chunks", lambda values: chunks)

    selected = build_screening_chunks(segments, use_keyword_prefilter=True, trigger_groups={"custom": {"分类": ["自定义信号"]}})

    assert selected == [chunks[1]]


def test_screening_chunks_filter_formula_noise_by_default(monkeypatch):
    import video_pipeline

    segments = [
        TranscriptSegment(1, 0, 1000, "x加y"),
        TranscriptSegment(2, 3000, 4000, "普通讲解"),
    ]
    monkeypatch.setattr(video_pipeline, "build_semantic_chunks", lambda values: [tuple(values)])

    filtered = build_screening_chunks(segments)
    unfiltered = build_screening_chunks(segments, filter_formula_noise=False)

    assert [segment.id for segment in filtered[0]] == [2]
    assert [segment.id for segment in unfiltered[0]] == [1, 2]


def test_saved_analysis_reuses_transcript_and_keeps_module_results(tmp_path):
    store = VideoStore(tmp_path / "video.db")
    video_id = store.create_video("hash", "lesson.mp4", "lesson.mp4", "lesson.wav", 10000)
    segments = [TranscriptSegment(1, 0, 3000, "这个会考。")]
    store.mark_transcribed(video_id, segments)
    store.add_marker(video_id, 0, 3000, "旧结果", "", "这个会考。", "AI-confirmed", 0.8, "high_recall")

    outcome, count = process_saved_analysis_once(store, video_id, FakeSemanticJudge(), "strict_exam_intent")

    assert outcome == "completed"
    assert count == 1
    assert len(store.list_markers(video_id, "high_recall")) == 1
    assert len(store.list_markers(video_id, "strict_exam_intent")) == 1
    assert store.get_analysis_run(video_id, "strict_exam_intent")["status"] == "completed"
    assert store.get_video(video_id)["status"] == "review_ready"


@pytest.mark.parametrize("cached", [False, True])
def test_saved_analysis_recovers_failed_video_even_with_zero_candidates(tmp_path, cached):
    store = VideoStore(tmp_path / "video.db")
    video_id = store.create_video("hash", "lesson.mp4", "lesson.mp4", "lesson.wav", 10000)
    store.mark_transcribed(video_id, [TranscriptSegment(1, 0, 3000, "普通讲解。")])
    store.set_status(video_id, "failed")
    store.set_analysis_run(video_id, "medium_recall", "completed" if cached else "failed")

    outcome, count = process_saved_analysis_once(store, video_id, TrackingJudge(), "medium_recall")

    assert (outcome, count) == ("skipped" if cached else "completed", 0)
    assert store.get_video(video_id)["status"] == "review_ready"


def test_saved_analysis_can_be_forced_to_rebuild_with_new_input_filter(tmp_path):
    store = VideoStore(tmp_path / "video.db")
    video_id = store.create_video("hash", "lesson.mp4", "lesson.mp4", "lesson.wav", 10000)
    segments = [TranscriptSegment(1, 0, 3000, "这个会考。")]
    store.mark_transcribed(video_id, segments)
    store.set_analysis_run(video_id, "strict_exam_intent", "completed", 0)

    outcome, count = process_saved_analysis_once(
        store,
        video_id,
        FakeSemanticJudge(),
        "strict_exam_intent",
        force=True,
    )

    assert outcome == "completed"
    assert count == 1


def test_preparation_and_ai_analysis_can_run_as_separate_pipeline_stages(tmp_path):
    store = VideoStore(tmp_path / "video.db")
    video_path = tmp_path / "lesson.mp4"
    video_path.write_bytes(b"video fixture")
    video_id = store.create_video("hash", "lesson.mp4", str(video_path), "lesson.wav", 10000)
    store.mark_transcribed(video_id, [TranscriptSegment(1, 0, 3000, "这个会考。")])

    prepared, segment_count = prepare_video_for_analysis(store, video_id)
    assert (prepared, segment_count) == ("prepared", 1)
    assert store.get_video(video_id)["status"] == "processing"

    completed, candidate_count = analyze_prepared_video_once(store, video_id, FakeSemanticJudge())
    assert (completed, candidate_count) == ("completed", 1)
    assert store.get_video(video_id)["status"] == "review_ready"


def test_review_ready_video_can_run_a_new_analysis_mode_with_saved_subtitles(tmp_path):
    store = VideoStore(tmp_path / "video.db")
    video_path = tmp_path / "lesson.mp4"
    video_path.write_bytes(b"video fixture")
    video_id = store.create_video("hash", "lesson.mp4", str(video_path), "lesson.wav", 10000)
    store.mark_transcribed(video_id, [TranscriptSegment(1, 0, 3000, "这个会考。")])
    store.set_status(video_id, "review_ready")
    store.set_analysis_run(video_id, "strict_exam_intent", "completed", 0)

    prepared, segment_count = prepare_video_for_analysis(
        store, video_id, analysis_mode="high_recall",
    )
    assert (prepared, segment_count) == ("prepared", 1)
    completed, candidate_count = analyze_prepared_video_once(
        store, video_id, FakeSemanticJudge(), analysis_mode="high_recall",
    )
    assert (completed, candidate_count) == ("completed", 1)
    assert store.get_analysis_run(video_id, "high_recall")["status"] == "completed"


def test_semantic_pipeline_reuses_successful_ai_chunk_cache(tmp_path, monkeypatch):
    import video_pipeline

    store = VideoStore(tmp_path / "video.db")
    video_id = store.create_video("hash", "lesson.mp4", "lesson.mp4", "temporary.wav", 10000)
    segments = [TranscriptSegment(index, index * 1000, index * 1000 + 500, f"第 {index} 句") for index in range(1, 4)]
    monkeypatch.setattr(video_pipeline, "build_semantic_chunks", lambda values: [(segment,) for segment in values])
    judge = TrackingJudge()

    create_semantic_markers(store, video_id, segments, judge, analysis_mode="strict_exam_intent")
    assert judge.total_calls == 3
    create_semantic_markers(store, video_id, segments, judge, analysis_mode="strict_exam_intent")
    assert judge.total_calls == 3


def test_video_transcription_reads_original_media_without_creating_wav(tmp_path, monkeypatch):
    import video_pipeline
    monkeypatch.setattr(video_pipeline, "selected_model_id", lambda: "whisper-turbo")

    video_path = tmp_path / "lesson.mp4"
    video_path.write_bytes(b"fixture")
    temporary_audio = tmp_path / "temporary.wav"
    expected = ([TranscriptSegment(1, 0, 1000, "字幕")], 1000)
    monkeypatch.setattr(video_pipeline, "transcribe_audio", lambda path, progress=None: expected)
    monkeypatch.setattr(video_pipeline, "extract_audio", lambda *args: (_ for _ in ()).throw(AssertionError("不应提取 WAV")))

    assert _transcribe_video_with_fallback(video_path, temporary_audio) == expected
    assert not temporary_audio.exists()


def test_video_transcription_uses_and_cleans_temporary_wav_on_decode_failure(tmp_path, monkeypatch):
    import video_pipeline
    monkeypatch.setattr(video_pipeline, "selected_model_id", lambda: "whisper-turbo")

    video_path = tmp_path / "lesson.mp4"
    video_path.write_bytes(b"fixture")
    temporary_audio = tmp_path / "temporary.wav"
    expected = ([TranscriptSegment(1, 0, 1000, "字幕")], 1000)

    def fake_transcribe(path, progress=None):
        if str(path).endswith(".mp4"):
            raise RuntimeError("container decode failed")
        return expected

    def fake_extract(_video_path, audio_path):
        audio_path.write_bytes(b"wav")

    monkeypatch.setattr(video_pipeline, "transcribe_audio", fake_transcribe)
    monkeypatch.setattr(video_pipeline, "extract_audio", fake_extract)

    assert _transcribe_video_with_fallback(video_path, temporary_audio) == expected
    assert not temporary_audio.exists()


def test_register_local_video_uses_size_and_mtime_before_rehashing(tmp_path, monkeypatch):
    import video_pipeline

    video_path = tmp_path / "lesson.mp4"
    video_path.write_bytes(b"video fixture")
    monkeypatch.setattr(video_pipeline, "video_duration_ms", lambda _path: 10000)
    store = VideoStore(tmp_path / "video.db")

    first_id, _path, first_exists = register_local_video(store, video_path, tmp_path / "audio")
    assert first_exists is False

    def unexpected_hash(_path):
        raise AssertionError("未变化的视频不应重新计算完整哈希")

    monkeypatch.setattr(store, "file_hash", unexpected_hash)
    second_id, _path, second_exists = register_local_video(store, video_path, tmp_path / "audio")
    assert (second_id, second_exists) == (first_id, True)
