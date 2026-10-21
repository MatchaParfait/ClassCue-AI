from batch_processing import estimate_remaining, format_eta, run_batch
from core import SemanticSelection, TranscriptSegment
from video_store import VideoStore


def test_eta_uses_recorded_stage_speed_and_pipeline_bottleneck(tmp_path):
    store = VideoStore(tmp_path / "video.db")
    first_path = tmp_path / "first.mp4"
    second_path = tmp_path / "second.mp4"
    first_path.write_bytes(b"first")
    second_path.write_bytes(b"second")
    first = store.create_video("first", first_path.name, str(first_path), "first.wav", 60000)
    second = store.create_video("second", second_path.name, str(second_path), "second.wav", 120000)
    store.mark_transcribed(first, [TranscriptSegment(1, 0, 60000, "已完成")])
    store.record_transcription_metrics(first, 10)
    store.record_ai_metrics(first, 20, 2)
    store.set_analysis_run(first, "strict_exam_intent", "completed", 1)

    estimate = estimate_remaining(store, [first, second], "strict_exam_intent")

    assert estimate.remaining_videos == 1
    assert estimate.seconds is not None
    assert estimate.seconds > 10
    assert format_eta(estimate.seconds).startswith("约 ")


def test_eta_is_finished_when_all_requested_videos_are_ready(tmp_path):
    store = VideoStore(tmp_path / "video.db")
    path = tmp_path / "ready.mp4"
    path.write_bytes(b"ready")
    video_id = store.create_video("ready", path.name, str(path), "ready.wav", 1000)
    store.mark_transcribed(video_id, [TranscriptSegment(1, 0, 1000, "完成")])
    store.set_analysis_run(video_id, "strict_exam_intent", "completed", 0)
    estimate = estimate_remaining(store, [video_id], "strict_exam_intent")
    assert estimate.remaining_videos == 0
    assert estimate.seconds == 0.0


def test_shared_batch_runner_processes_a_cached_transcript(tmp_path):
    class EmptyJudge:
        def judge_semantic_chunk(self, chunk):
            return SemanticSelection()

    store = VideoStore(tmp_path / "video.db")
    path = tmp_path / "lesson.mp4"
    path.write_bytes(b"video")
    stat = path.stat()
    digest = store.file_hash(path)
    video_id = store.create_video(
        digest,
        path.name,
        str(path.resolve()),
        str(tmp_path / "temporary.wav"),
        1000,
        stat.st_size,
        stat.st_mtime_ns,
    )
    store.mark_transcribed(video_id, [TranscriptSegment(1, 0, 1000, "普通字幕")])
    events = []

    summary = run_batch(
        store,
        [path],
        tmp_path,
        EmptyJudge(),
        analysis_mode="strict_exam_intent",
        on_event=events.append,
    )

    assert summary.completed == 1
    assert summary.failed == 0
    assert store.get_video(video_id)["status"] == "review_ready"
    assert events[-1].estimate.remaining_videos == 0
