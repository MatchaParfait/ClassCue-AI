from pathlib import Path
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from classcue.video_store import VideoStore


@pytest.fixture
def saved_marker(tmp_path: Path):
    store = VideoStore(tmp_path / "jump-time.db")
    video_id = store.create_video("hash", "lesson.mp4", "lesson.mp4", "lesson.wav", 600_000)
    marker_id = store.add_marker(video_id, 521_000, 530_000, "Topic", "note", "quote", "AI-test", .9, "medium_recall")
    return store, video_id, marker_id


def test_saves_separate_time_and_survives_store_and_api_restart(saved_marker):
    store, video_id, marker_id = saved_marker
    client = TestClient(create_app(store))
    before = dict(store.list_markers(video_id)[0])
    response = client.patch(f"/api/markers/{marker_id}/jump-time", json={"user_start_ms": 450_000})
    assert response.status_code == 200
    saved = response.json()
    assert saved["user_start_ms"] == 450_000
    assert saved["user_start_updated_at"]
    for field, value in before.items():
        if field not in {"user_start_ms", "user_start_updated_at"}:
            assert saved[field] == value
    restarted = TestClient(create_app(VideoStore(store.path)))
    markers = restarted.get(f"/api/videos/{video_id}/markers").json()
    assert markers[0]["user_start_ms"] == 450_000
    assert markers[0]["start_ms"] == 521_000


@pytest.mark.parametrize("time", [0, 600_000])
def test_zero_and_video_end_are_valid_jump_times(saved_marker, time):
    store, _, marker_id = saved_marker
    response = TestClient(create_app(store)).patch(
        f"/api/markers/{marker_id}/jump-time", json={"user_start_ms": time},
    )
    assert response.status_code == 200
    assert response.json()["user_start_ms"] == time


def test_restore_clears_only_user_override_and_preserves_status(saved_marker):
    store, video_id, marker_id = saved_marker
    client = TestClient(create_app(store))
    store.set_marker_jump_time(marker_id, 0)
    client.patch(f"/api/markers/{marker_id}", json={"review_status": "mastered"})
    response = client.patch(f"/api/markers/{marker_id}/jump-time", json={"user_start_ms": None})
    assert response.status_code == 200
    assert response.json()["user_start_ms"] is None
    assert response.json()["user_start_updated_at"] is None
    assert response.json()["start_ms"] == 521_000
    assert VideoStore(store.path).list_markers(video_id)[0]["review_status"] == "mastered"


@pytest.mark.parametrize("body, code", [
    ({}, 422), ({"user_start_ms": -1}, 422), ({"user_start_ms": 1.5}, 422),
    ({"user_start_ms": True}, 422), ({"user_start_ms": "450000"}, 422),
    ({"user_start_ms": 600_001}, 400),
    ({"user_start_ms": 1000, "start_ms": 1000}, 422),
    ({"user_start_ms": 1000, "review_status": "excluded"}, 422),
])
def test_invalid_requests_do_not_change_saved_values(saved_marker, body, code):
    store, video_id, marker_id = saved_marker
    store.set_marker_jump_time(marker_id, 450_000)
    before = dict(store.list_markers(video_id)[0])
    response = TestClient(create_app(store)).patch(f"/api/markers/{marker_id}/jump-time", json=body)
    assert response.status_code == code
    assert dict(store.list_markers(video_id)[0]) == before


def test_unknown_marker_returns_404(saved_marker):
    store, _, _ = saved_marker
    response = TestClient(create_app(store)).patch("/api/markers/99999/jump-time", json={"user_start_ms": 0})
    assert response.status_code == 404


def test_existing_schema_is_migrated_idempotently_without_modifying_originals(saved_marker):
    store, video_id, _ = saved_marker
    with store.connect() as connection:
        connection.execute("ALTER TABLE video_markers DROP COLUMN user_start_ms")
        connection.execute("ALTER TABLE video_markers DROP COLUMN user_start_updated_at")
    migrated = VideoStore(store.path)
    marker = migrated.list_markers(video_id)[0]
    assert marker["start_ms"] == 521_000
    assert marker["user_start_ms"] is None
    migrated.set_marker_jump_time(marker["id"], 0)
    reopened = VideoStore(store.path).list_markers(video_id)[0]
    assert reopened["user_start_ms"] == 0
    assert reopened["user_start_updated_at"]


def test_reanalysis_keeps_matching_and_unmatched_user_adjustments(saved_marker):
    store, video_id, marker_id = saved_marker
    store.set_marker_jump_time(marker_id, 450_000)
    results = [{
        "start_ms": 521_000, "end_ms": 530_000, "title": "Topic",
        "reason": "new note", "source_quote": "new quote", "signal_type": "explicit",
    }]
    store.publish_exam_results(video_id, "medium_recall", results, "signature", "run")
    marker = store.list_markers(video_id)[0]
    assert marker["id"] == marker_id
    assert marker["user_start_ms"] == 450_000
    assert marker["start_ms"] == 521_000
    store.publish_exam_results(video_id, "medium_recall", [], "signature-2", "run-2")
    assert store.list_markers(video_id)[0]["id"] == marker_id
    store.delete_markers_for_mode(video_id, "medium_recall")
    assert store.list_markers(video_id)[0]["user_start_ms"] == 450_000


def test_reanalysis_does_not_transfer_user_times_to_new_points(saved_marker):
    store, video_id, marker_id = saved_marker
    store.set_marker_jump_time(marker_id, 450_000)
    store.publish_exam_results(video_id, "medium_recall", [{
        "start_ms": 522_000, "end_ms": 531_000, "title": "Different topic",
        "reason": "note", "source_quote": "quote", "signal_type": "explicit",
    }], "new-signature", "run")
    markers = store.list_markers(video_id)
    assert len(markers) == 2
    assert next(row for row in markers if row["id"] != marker_id)["user_start_ms"] is None


def test_reanalysis_rechecks_adjustments_saved_after_its_snapshot(saved_marker, monkeypatch):
    store, video_id, marker_id = saved_marker
    connect = store.connect

    class SnapshotCursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def fetchall(self):
            rows = self.cursor.fetchall()
            store.set_marker_jump_time(marker_id, 450_000)
            return rows

    class ConnectionProxy:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, *args):
            cursor = self.connection.execute(sql, *args)
            if sql == "SELECT * FROM video_markers WHERE video_id = ? AND analysis_mode = ?":
                return SnapshotCursor(cursor)
            return cursor

    @contextmanager
    def interleaved_connection():
        with connect() as connection:
            yield ConnectionProxy(connection)

    monkeypatch.setattr(store, "connect", interleaved_connection)
    store.publish_exam_results(video_id, "medium_recall", [], "signature", "run")
    assert store.list_markers(video_id)[0]["user_start_ms"] == 450_000
