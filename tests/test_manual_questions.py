import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from classcue.core import TranscriptSegment
from classcue.video_store import VideoStore


@pytest.fixture
def lesson(tmp_path):
    store = VideoStore(tmp_path / "manual.db")
    video_id = store.create_video("fixture", "lesson.mp4", "lesson.mp4", "lesson.wav", 600_000)
    store.mark_transcribed(video_id, [TranscriptSegment(1, 100_000, 200_000, "Example subtitle")])
    with TestClient(create_app(store)) as client:
        yield store, video_id, client


def create(client, video_id, time_ms=150_425):
    response = client.post(f"/api/videos/{video_id}/manual-questions", json={"time_ms": time_ms})
    assert response.status_code == 201, response.text
    return response.json()


def test_creates_independent_point_at_exact_time_without_completing_ai_analysis(lesson):
    store, video_id, client = lesson
    before_video = dict(store.get_video(video_id))
    store.add_marker(video_id, 160_000, 170_000, "Exam point", "", "", "AI-test", 1, "medium_recall")
    created = create(client, video_id)
    assert created["start_ms"] == 150_425
    assert created["end_ms"] == 150_426
    assert created["title"] == "手动题目 · 02:30"
    assert created["source_quote"] == "Example subtitle"
    assert created["analysis_mode"] == "question_extraction"
    assert created["review_status"] == "active"
    assert created["trigger_phrase"].startswith("MANUAL-question:")
    assert created["question_emphasized"] is True
    assert store.get_analysis_run(video_id, "question_extraction") is None
    assert dict(store.get_video(video_id)) == before_video
    assert store.list_experiments(video_id) == []
    assert VideoStore(store.path).list_markers(video_id, "question_extraction")[0]["id"] == created["id"]


def test_new_points_are_separate_and_listed_in_time_order(lesson):
    _, video_id, client = lesson
    later = create(client, video_id, 500_000)
    first = create(client, video_id, 0)
    same_time = create(client, video_id, 500_000)
    assert len({later["id"], first["id"], same_time["id"]}) == 3
    rows = client.get(f"/api/videos/{video_id}/markers?mode=question_extraction").json()
    assert [row["start_ms"] for row in rows] == [0, 500_000, 500_000]
    assert all(row["source_quote"] == "" for row in rows)


def test_video_end_uses_last_valid_millisecond(lesson):
    _, video_id, client = lesson
    created = create(client, video_id, 600_000)
    assert created["start_ms"] == 599_999
    assert created["end_ms"] == 600_000
    assert client.post(f"/api/videos/{video_id}/manual-questions", json={"time_ms": 600_001}).status_code == 400


@pytest.mark.parametrize("payload", [
    {}, {"time_ms": -1}, {"time_ms": 1.5}, {"time_ms": True}, {"time_ms": "1000"},
    {"time_ms": None}, {"time_ms": 1000, "analysis_mode": "medium_recall"},
])
def test_invalid_creation_requests_leave_database_unchanged(lesson, payload):
    store, video_id, client = lesson
    assert client.post(f"/api/videos/{video_id}/manual-questions", json=payload).status_code == 422
    assert not store.list_markers(video_id)


def test_unknown_video_and_unknown_title_target_return_404(lesson):
    _, _, client = lesson
    assert client.post("/api/videos/99999/manual-questions", json={"time_ms": 0}).status_code == 404
    assert client.patch("/api/markers/99999/title", json={"title": "New title"}).status_code == 404


def test_rename_changes_only_manual_title_and_survives_reopening(lesson):
    store, video_id, client = lesson
    created = create(client, video_id)
    response = client.patch(f"/api/markers/{created['id']}/title", json={"title": "  Renamed example  "})
    assert response.status_code == 200
    renamed = response.json()
    assert renamed["title"] == "Renamed example"
    assert {key: value for key, value in renamed.items() if key != "title"} == {
        key: value for key, value in created.items() if key not in {"title", "question_emphasized"}
    }
    assert VideoStore(store.path).list_markers(video_id)[0]["title"] == "Renamed example"


@pytest.mark.parametrize("payload", [
    {}, {"title": ""}, {"title": "   "}, {"title": 123}, {"title": "x" * 201},
    {"title": "Valid", "start_ms": 0},
])
def test_invalid_titles_do_not_change_point(lesson, payload):
    store, video_id, client = lesson
    created = create(client, video_id)
    assert client.patch(f"/api/markers/{created['id']}/title", json=payload).status_code == 422
    assert store.list_markers(video_id)[0]["title"] == created["title"]


def test_ai_markers_cannot_be_renamed_through_manual_endpoint(lesson):
    store, video_id, client = lesson
    marker_id = store.add_marker(video_id, 1000, 2000, "AI example", "", "", "AI-test", 1, "question_extraction")
    assert client.patch(f"/api/markers/{marker_id}/title", json={"title": "Changed"}).status_code == 400


def test_reanalysis_does_not_consume_manual_point_with_identical_time_and_title(lesson):
    store, video_id, client = lesson
    manual = create(client, video_id)
    result = {
        "start_ms": manual["start_ms"], "end_ms": manual["end_ms"], "title": manual["title"],
        "reason": "AI note", "source_quote": "AI quote", "signal_type": "emphasis",
    }
    for index in range(2):
        store.publish_exam_results(video_id, "question_extraction", [result], f"sig-{index}", f"run-{index}")
    rows = store.list_markers(video_id, "question_extraction")
    assert len(rows) == 2
    preserved = next(row for row in rows if row["id"] == manual["id"])
    assert dict(preserved) == {key: value for key, value in manual.items() if key != "question_emphasized"}
    assert next(row for row in rows if row["id"] != manual["id"])["trigger_phrase"].startswith("AI-")


def test_manual_point_survives_all_ai_cleanup_paths_and_user_operations(lesson):
    store, video_id, client = lesson
    manual = create(client, video_id)
    marker_id = manual["id"]
    store.publish_exam_results(video_id, "question_extraction", [], "sig", "run")
    store.delete_markers_for_mode(video_id, "question_extraction")
    store.publish_semantic_markers(video_id, "question_extraction", [], replace_existing=True)
    assert [row["id"] for row in store.list_markers(video_id)] == [marker_id]
    assert client.patch(f"/api/markers/{marker_id}/question-emphasis", json={"emphasized": True}).status_code == 200
    assert client.patch(f"/api/markers/{marker_id}", json={"review_status": "excluded"}).status_code == 200
    assert not store.list_markers(video_id)
    assert client.patch(f"/api/markers/{marker_id}", json={"review_status": "active"}).status_code == 200
    result = client.get(f"/api/videos/{video_id}/markers").json()[0]
    assert result["question_emphasized"] is True
    assert result["trigger_phrase"] == manual["trigger_phrase"]
