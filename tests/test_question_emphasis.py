import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from classcue.video_store import VideoStore


@pytest.fixture
def lesson(tmp_path):
    store = VideoStore(tmp_path / "questions.db")
    video_id = store.create_video("fixture", "lesson.mp4", "lesson.mp4", "lesson.wav", 3_600_000)
    application = create_app(store)
    with TestClient(application) as client:
        yield store, video_id, client


def add(store, video_id, start, end, mode="question_extraction", title="Example"):
    return store.add_marker(video_id, start, end, title, "", "", "AI-test", .9, mode)


def questions(client, video_id):
    response = client.get(f"/api/videos/{video_id}/markers", params={"mode": "question_extraction"})
    assert response.status_code == 200
    return response.json()


@pytest.mark.parametrize("start,end,expected", [
    (659_000, 660_000, True),
    (658_000, 659_999, False),
    (960_000, 961_000, True),
    (960_001, 961_000, False),
    (780_000, 781_000, True),
])
def test_nearby_intervals_include_exact_one_minute_boundary(lesson, start, end, expected):
    store, video_id, client = lesson
    question = add(store, video_id, 720_000, 900_000)
    point = add(store, video_id, start, end, "medium_recall", "Exam point")
    store.set_marker_jump_time(question, 1_500_000)
    store.set_marker_jump_time(point, 2_000_000)
    result = questions(client, video_id)[0]
    assert result["question_emphasized"] is expected
    assert result["question_emphasis_override"] is None
    assert result["start_ms"] == 720_000


def test_two_minute_distance_and_other_questions_do_not_highlight(lesson):
    store, video_id, client = lesson
    add(store, video_id, 1_440_000, 1_620_000)
    add(store, video_id, 1_320_000, 1_321_000, "low_recall")
    add(store, video_id, 1_740_000, 1_741_000, "high_recall")
    add(store, video_id, 1_500_000, 1_510_000)
    assert not any(row["question_emphasized"] for row in questions(client, video_id))


@pytest.mark.parametrize("question_first", [True, False])
def test_publishing_both_results_automatically_highlights_in_either_order(lesson, question_first):
    store, video_id, client = lesson
    result = {
        "start_ms": 720_000, "end_ms": 900_000, "title": "Example",
        "reason": "", "source_quote": "saved source", "signal_type": "emphasis",
    }
    point = {**result, "start_ms": 660_000, "end_ms": 661_000, "title": "Exam point"}
    modes = [("question_extraction", result), ("medium_recall", point)]
    if not question_first:
        modes.reverse()
    for index, (mode, item) in enumerate(modes):
        store.publish_exam_results(video_id, mode, [item], f"signature-{index}", f"run-{index}")
    assert questions(client, video_id)[0]["question_emphasized"] is True
    assert len(store.list_markers(video_id)) == 2


def test_only_active_same_video_exam_points_count_and_status_changes_recompute(lesson):
    store, video_id, client = lesson
    add(store, video_id, 720_000, 900_000)
    other = store.create_video("other", "other.mp4", "other.mp4", "other.wav", 3_600_000)
    add(store, other, 780_000, 781_000, "medium_recall")
    point = add(store, video_id, 780_000, 781_000, "custom_fixture")
    store.set_marker_review_status(point, "excluded")
    assert questions(client, video_id)[0]["question_emphasized"] is False
    client.patch(f"/api/markers/{point}", json={"review_status": "mastered"})
    assert questions(client, video_id)[0]["question_emphasized"] is True
    client.patch(f"/api/markers/{point}", json={"review_status": "excluded"})
    assert questions(client, video_id)[0]["question_emphasized"] is False


@pytest.mark.parametrize("emphasized", [True, False])
def test_manual_choice_survives_reopening_reanalysis_and_removal(lesson, emphasized):
    store, video_id, client = lesson
    question = add(store, video_id, 720_000, 900_000)
    add(store, video_id, 780_000, 781_000, "medium_recall")
    store.set_marker_jump_time(question, 700_000)
    store.set_marker_importance(question, "critical")
    before = dict(store.list_markers(video_id, "question_extraction")[0])
    response = client.patch(f"/api/markers/{question}/question-emphasis", json={"emphasized": emphasized})
    assert response.status_code == 200
    result = response.json()
    assert result["question_emphasized"] is emphasized
    assert {key: value for key, value in result.items()
            if key not in {"question_emphasis_override", "question_emphasized"}} == {
        key: value for key, value in before.items() if key != "question_emphasis_override"
    }
    reopened = VideoStore(store.path)
    assert reopened.question_emphasis_states(video_id)[question] is emphasized
    reopened.publish_exam_results(video_id, "question_extraction", [{
        "start_ms": 720_000, "end_ms": 900_000, "title": "Example",
        "reason": "updated", "source_quote": "updated", "signal_type": "emphasis",
    }], "signature", "run")
    assert questions(client, video_id)[0]["id"] == question
    assert questions(client, video_id)[0]["question_emphasized"] is emphasized
    client.patch(f"/api/markers/{question}", json={"review_status": "excluded"})
    assert questions(client, video_id) == []
    client.patch(f"/api/markers/{question}", json={"review_status": "active"})
    assert questions(client, video_id)[0]["question_emphasized"] is emphasized


@pytest.mark.parametrize("emphasized", [True, False])
def test_reanalysis_cleanup_preserves_both_manual_choices(lesson, emphasized):
    store, video_id, _ = lesson
    question = add(store, video_id, 720_000, 900_000)
    store.set_question_emphasis(question, emphasized)
    store.publish_exam_results(video_id, "question_extraction", [], "signature", "run")
    store.delete_markers_for_mode(video_id, "question_extraction")
    assert store.question_emphasis_states(video_id) == {question: emphasized}


def test_schema_upgrade_and_legacy_recovery_preserve_manual_choices(lesson):
    store, video_id, _ = lesson
    question = add(store, video_id, 720_000, 900_000)
    with store.connect() as connection:
        connection.execute("ALTER TABLE video_markers DROP COLUMN question_emphasis_override")
    migrated = VideoStore(store.path)
    assert migrated.list_markers(video_id)[0]["question_emphasis_override"] is None
    migrated.set_question_emphasis(question, False)
    with migrated.connect() as connection:
        connection.execute("ALTER TABLE video_markers RENAME TO video_markers_legacy")
    recovered = VideoStore(store.path)
    assert recovered.list_markers(video_id)[0]["question_emphasis_override"] == 0


@pytest.mark.parametrize("payload", [
    {}, {"emphasized": 1}, {"emphasized": "false"},
    {"emphasized": True, "start_ms": 0},
])
def test_invalid_request_does_not_save_override(lesson, payload):
    store, video_id, client = lesson
    question = add(store, video_id, 720_000, 900_000)
    assert client.patch(f"/api/markers/{question}/question-emphasis", json=payload).status_code == 422
    assert store.list_markers(video_id)[0]["question_emphasis_override"] is None


def test_missing_and_non_question_marker_are_rejected(lesson):
    store, video_id, client = lesson
    point = add(store, video_id, 780_000, 781_000, "medium_recall")
    assert client.patch("/api/markers/99999/question-emphasis", json={"emphasized": True}).status_code == 404
    assert client.patch(f"/api/markers/{point}/question-emphasis", json={"emphasized": True}).status_code == 400


@pytest.mark.parametrize("initial", [True, False])
def test_clear_override_restores_live_automatic_judgment(lesson, initial):
    store, video_id, client = lesson
    question = add(store, video_id, 720_000, 900_000)
    point = add(store, video_id, 780_000, 781_000, "medium_recall")
    store.set_marker_review_status(point, "active" if initial else "excluded")
    assert client.patch(f"/api/markers/{question}/question-emphasis",
                        json={"emphasized": not initial}).status_code == 200
    cleared = client.patch(f"/api/markers/{question}/question-emphasis", json={"emphasized": None})
    assert cleared.status_code == 200
    assert cleared.json()["question_emphasis_override"] is None
    assert cleared.json()["question_emphasized"] is initial
    store.set_marker_review_status(point, "excluded" if initial else "active")
    assert questions(client, video_id)[0]["question_emphasized"] is not initial
