from threading import Event

from core import TranscriptSegment
from live_transcript import LiveTranscriptBuffer
from transcript_events import emit_transcript, transcript_listener, transcript_observer, transcript_requested


def test_preview_coexists_with_analysis_and_restores_context():
    analysis, preview = [], []
    words = [TranscriptSegment(1, 0, 1000, "真实字幕")]
    with transcript_observer(lambda rows, end: preview.extend(rows)):
        assert transcript_requested()
        with transcript_listener(lambda rows, end: analysis.extend(rows)):
            emit_transcript(words, 1000)
    assert analysis == preview == words
    assert not transcript_requested()


def test_preview_failure_does_not_abort_asr_or_analysis():
    def broken(*_):
        raise RuntimeError("preview")
    received = []
    with transcript_observer(broken), transcript_listener(lambda rows, end: received.extend(rows)):
        emit_transcript([TranscriptSegment(1, 0, 1, "字幕")], 1)
    assert len(received) == 1


def test_preview_is_bounded_deduplicated_and_isolated():
    buffer = LiveTranscriptBuffer(max_jobs=2, max_chars=6, max_segments=2)
    buffer.publish("a", 1, [TranscriptSegment(i, i, i + 1, "字幕文字") for i in range(4)], 4)
    snap = buffer.snapshot("a", 1)
    assert [row["id"] for row in snap["subtitles"]] == [3]
    snap["subtitles"][0]["text"] = "changed"
    assert buffer.snapshot("a", 1)["subtitles"][0]["text"] == "字幕文字"
    buffer.publish("a", 1, [TranscriptSegment(3, 3, 4, "校正")], 3)
    assert len(buffer.snapshot("a", 1)["subtitles"]) == 1
    assert buffer.snapshot("a", 1)["settled_until_ms"] == 4
    assert buffer.snapshot("a", 2)["subtitles"] == []
    buffer.publish("b", 2, [], 0)
    buffer.publish("c", 3, [], 0)
    assert buffer.snapshot("a", 1)["subtitles"] == []


def test_api_preview_taps_job_without_publishing_durable_subtitles(tmp_path):
    from api.main import create_app
    from fastapi.testclient import TestClient
    from video_store import VideoStore

    store = VideoStore(tmp_path / "test.db")
    video_id = store.create_video("hash", "lesson.mp4", "lesson.mp4", "lesson.wav", 1000)
    app = create_app(store)
    ready, release = Event(), Event()
    def operation(update, cancelled):
        with transcript_listener(lambda *_: None):
            emit_transcript([TranscriptSegment(1, 0, 1000, "本次文字")], 1000)
        ready.set()
        release.wait(5)
        return {}
    with TestClient(app) as client:
        manager = app.state.jobs
        job_id = manager.submit("video_process", video_id, operation)
        try:
            assert ready.wait(5)
            response = client.get(f"/api/jobs/{job_id}/transcript")
            assert response.status_code == 200
            assert response.json()["subtitles"][0]["text"] == "本次文字"
            assert store.get_segments(video_id) == []
            assert client.get("/api/jobs/missing/transcript").status_code == 404
        finally:
            release.set()
