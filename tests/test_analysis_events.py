from threading import Event

from analysis_events import LiveAnalysisBuffer, analysis_observer, emit_analysis


def result(start=0, title="考点"):
    return {"start_ms": start, "end_ms": start + 1000, "title": title,
            "reason": "教师强调", "source_quote": "必须掌握"}


def test_observer_is_optional_scoped_and_failure_isolated():
    events = []
    def broken(*args, **kwargs):
        raise RuntimeError("UI must not abort processing")
    state = dict(completed=1, total=3, current=None, results=[result()])
    with analysis_observer(lambda *args, **kwargs: events.append(kwargs)):
        emit_analysis(1, "medium_recall", **state)
        with analysis_observer(broken):
            emit_analysis(1, "medium_recall", **state)
        emit_analysis(1, "medium_recall", **state)
    emit_analysis(1, "medium_recall", **state)
    assert events == [state, state]


def test_buffer_is_bounded_deduplicated_and_isolated():
    buffer = LiveAnalysisBuffer(max_jobs=2, max_markers=2, max_chars=100)
    def put(job, mode="medium_recall", results=None):
        buffer.publish(job, 1, mode, completed=1, total=3, current=2, results=results or [result()])
    put("a")
    first = buffer.snapshot("a", 1)
    marker_id = first["modes"]["medium_recall"]["markers"][0]["id"]
    first["modes"]["medium_recall"]["markers"][0]["title"] = "changed"
    put("a", results=[result(), result(1000), result(2000)])
    second = buffer.snapshot("a", 1)["modes"]["medium_recall"]
    assert second["markers"][0]["id"] == marker_id < 0
    assert second["markers"][0]["title"] == "考点"
    assert len(second["markers"]) == 2 and second["candidate_count"] == 3 and second["truncated"]
    put("a", "high_recall")
    assert buffer.snapshot("a", 1)["modes"]["high_recall"]["markers"][0]["id"] != marker_id
    assert buffer.snapshot("a", 2)["modes"] == {}
    put("b"); put("c")
    assert buffer.snapshot("a", 1)["modes"] == {}


def test_job_endpoint_retains_preview_without_publishing_markers(tmp_path):
    from api.main import create_app
    from fastapi.testclient import TestClient
    from video_store import VideoStore

    store = VideoStore(tmp_path / "test.db")
    video_id = store.create_video("hash", "lesson.mp4", "lesson.mp4", "lesson.wav", 1000)
    app = create_app(store)
    ready, release = Event(), Event()
    def operation(update, cancelled):
        emit_analysis(video_id, "medium_recall", completed=1, total=3, current=2, results=[result()])
        ready.set()
        release.wait(5)
        raise ValueError("second chunk failed")
    with TestClient(app) as client:
        manager = app.state.jobs
        job_id = manager.submit("video_analysis", video_id, operation)
        try:
            assert ready.wait(5)
            response = client.get(f"/api/jobs/{job_id}/analysis-preview")
            assert response.status_code == 200
            payload = response.json()
            assert payload["modes"]["medium_recall"]["current_chunk"] == 2
            assert payload["modes"]["medium_recall"]["markers"][0]["preview"] is True
            assert store.list_markers(video_id) == []
            assert client.get("/api/jobs/missing/analysis-preview").status_code == 404
        finally:
            release.set()
        manager.executor.submit(lambda: None).result(timeout=5)
        assert client.get(f"/api/jobs/{job_id}").json()["status"] == "failed"
        assert client.get(f"/api/jobs/{job_id}/analysis-preview").json() == payload
        assert store.list_markers(video_id) == []
