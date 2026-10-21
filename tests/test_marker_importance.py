from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from core import TranscriptSegment
from video_store import VideoStore


@pytest.fixture
def saved_marker(tmp_path):
    store = VideoStore(tmp_path / "importance.db")
    video_id = store.create_video("hash", "lesson.mp4", "lesson.mp4", "lesson.wav", 600_000)
    store.mark_transcribed(video_id, [TranscriptSegment(1, 0, 1000, "saved subtitle")])
    marker_id = store.add_marker(video_id, 521_000, 530_000, "Topic", "note", "quote", "AI-test", .9, "medium_recall")
    application = create_app(store)
    try:
        yield store, video_id, marker_id, TestClient(application)
    finally:
        application.state.jobs.shutdown(wait=True)


@pytest.mark.parametrize("importance", ["standard", "important", "critical"])
def test_saves_only_importance_and_survives_reopening(saved_marker, importance):
    store, video_id, marker_id, client = saved_marker
    store.set_marker_jump_time(marker_id, 450_000)
    before = dict(store.list_markers(video_id)[0])
    response = client.patch(f"/api/markers/{marker_id}/importance", json={"importance": importance})
    assert response.status_code == 200
    saved = response.json()
    assert saved["importance"] == importance
    assert {key: value for key, value in saved.items() if key != "importance"} == {
        key: value for key, value in before.items() if key != "importance"
    }
    reopened = VideoStore(store.path)
    assert reopened.list_markers(video_id)[0]["importance"] == importance
    assert reopened.get_segments(video_id)[0].text == "saved subtitle"
    with TestClient(create_app(reopened)) as restarted:
        assert restarted.get(f"/api/videos/{video_id}/markers").json()[0]["importance"] == importance


@pytest.mark.parametrize("body", [
    {}, {"importance": None}, {"importance": "red"}, {"importance": 1}, {"importance": True},
    {"importance": "important", "start_ms": 0}, {"importance": "critical", "review_status": "excluded"},
])
def test_invalid_values_and_extra_fields_do_not_change_marker(saved_marker, body):
    store, video_id, marker_id, client = saved_marker
    store.set_marker_importance(marker_id, "critical")
    before = dict(store.list_markers(video_id)[0])
    assert client.patch(f"/api/markers/{marker_id}/importance", json=body).status_code == 422
    assert dict(store.list_markers(video_id)[0]) == before


def test_missing_marker_returns_404(saved_marker):
    _, _, _, client = saved_marker
    assert client.patch("/api/markers/99999/importance", json={"importance": "important"}).status_code == 404


def test_migration_defaults_existing_markers_without_changing_user_edits(saved_marker):
    store, video_id, marker_id, _ = saved_marker
    store.set_marker_jump_time(marker_id, 450_000)
    store.set_marker_review_status(marker_id, "mastered")
    with store.connect() as connection:
        connection.execute("ALTER TABLE video_markers DROP COLUMN importance")
    migrated = VideoStore(store.path)
    marker = migrated.list_markers(video_id)[0]
    assert marker["importance"] == "standard"
    assert marker["user_start_ms"] == 450_000
    assert marker["review_status"] == "mastered"
    migrated.set_marker_importance(marker_id, "critical")
    assert VideoStore(store.path).list_markers(video_id)[0]["importance"] == "critical"


def test_legacy_table_recovery_preserves_importance_and_user_time(saved_marker):
    store, video_id, marker_id, _ = saved_marker
    store.set_marker_importance(marker_id, "critical")
    store.set_marker_jump_time(marker_id, 450_000)
    with store.connect() as connection:
        connection.execute("ALTER TABLE video_markers RENAME TO video_markers_legacy")
    marker = VideoStore(store.path).list_markers(video_id)[0]
    assert marker["id"] == marker_id
    assert marker["importance"] == "critical"
    assert marker["user_start_ms"] == 450_000


def test_removal_restore_and_time_update_preserve_importance(saved_marker):
    store, video_id, marker_id, client = saved_marker
    client.patch(f"/api/markers/{marker_id}/importance", json={"importance": "important"})
    client.patch(f"/api/markers/{marker_id}", json={"review_status": "excluded"})
    assert store.list_excluded_markers(video_id)[0]["importance"] == "important"
    client.patch(f"/api/markers/{marker_id}/jump-time", json={"user_start_ms": 0})
    restored = client.patch(f"/api/markers/{marker_id}", json={"review_status": "active"})
    assert restored.json()["importance"] == "important"
    assert restored.json()["user_start_ms"] == 0


def test_reanalysis_retains_tagged_markers_and_new_markers_default_to_standard(saved_marker):
    store, video_id, marker_id, _ = saved_marker
    store.set_marker_importance(marker_id, "important")
    results = [{
        "start_ms": 521_000, "end_ms": 530_000, "title": "Topic",
        "reason": "updated note", "source_quote": "updated quote", "signal_type": "explicit",
    }]
    store.publish_exam_results(video_id, "medium_recall", results, "first", "run-1")
    assert store.list_markers(video_id)[0]["id"] == marker_id
    assert store.list_markers(video_id)[0]["importance"] == "important"
    results[0].update(start_ms=522_000, end_ms=531_000, title="Other topic")
    store.publish_exam_results(video_id, "medium_recall", results, "second", "run-2")
    markers = store.list_markers(video_id)
    assert len(markers) == 2
    assert next(row for row in markers if row["id"] != marker_id)["importance"] == "standard"
    store.delete_markers_for_mode(video_id, "medium_recall")
    assert [row["id"] for row in store.list_markers(video_id)] == [marker_id]


def test_reanalysis_rechecks_tags_saved_after_snapshot(saved_marker, monkeypatch):
    store, video_id, marker_id, _ = saved_marker
    connect = store.connect

    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def fetchall(self):
            rows = self.cursor.fetchall()
            store.set_marker_importance(marker_id, "critical")
            return rows

    class Connection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, *args):
            cursor = self.connection.execute(sql, *args)
            if sql == "SELECT * FROM video_markers WHERE video_id = ? AND analysis_mode = ?":
                return Cursor(cursor)
            return cursor

    @contextmanager
    def interleaved():
        with connect() as connection:
            yield Connection(connection)

    monkeypatch.setattr(store, "connect", interleaved)
    store.publish_exam_results(video_id, "medium_recall", [], "signature", "run")
    assert store.list_markers(video_id)[0]["importance"] == "critical"


def test_video_lists_count_all_modes_but_not_removed_markers(saved_marker):
    store, video_id, _, client = saved_marker
    blue = store.add_marker(video_id, 1000, 2000, "Blue", "", "", "", .9, "high_recall")
    red = store.add_marker(video_id, 3000, 4000, "Red", "", "", "", .9, "low_recall")
    excluded = store.add_marker(video_id, 5000, 6000, "Removed", "", "", "", .9, "medium_recall")
    store.set_marker_importance(blue, "important")
    store.set_marker_review_status(blue, "mastered")
    store.set_marker_importance(red, "critical")
    store.set_marker_importance(excluded, "critical")
    store.set_marker_review_status(excluded, "excluded")
    empty_id = store.create_video("empty", "empty.mp4", "empty.mp4", "empty.wav", 1000)
    expected = {"standard": 1, "important": 1, "critical": 1}
    assert store.marker_importance_counts() == {video_id: expected}
    assert store.marker_importance_counts(empty_id) == {}
    videos = client.get("/api/videos").json()
    assert next(row for row in videos if row["id"] == video_id)["marker_importance_counts"] == expected
    assert next(row for row in videos if row["id"] == empty_id)["marker_importance_counts"] == {
        "standard": 0, "important": 0, "critical": 0,
    }
    assert client.get(f"/api/videos/{video_id}").json()["marker_importance_counts"] == expected
    root = client.get("/api/library").json()
    assert next(item["video"] for item in root["items"] if item.get("video", {}).get("id") == video_id)["marker_importance_counts"] == expected
    folder_id = store.create_folder("Course")
    store.set_video_folder(video_id, folder_id)
    assert client.get(f"/api/library?folder_id={folder_id}").json()["items"][0]["video"]["marker_importance_counts"] == expected
    assert client.get(f"/api/folders/{folder_id}/videos").json()[0]["marker_importance_counts"] == expected


def test_counts_update_after_color_change_removal_and_restore(saved_marker):
    _, video_id, marker_id, client = saved_marker
    url = f"/api/videos/{video_id}"
    assert client.get(url).json()["marker_importance_counts"] == {"standard": 1, "important": 0, "critical": 0}
    client.patch(f"/api/markers/{marker_id}/importance", json={"importance": "critical"})
    assert client.get(url).json()["marker_importance_counts"] == {"standard": 0, "important": 0, "critical": 1}
    client.patch(f"/api/markers/{marker_id}", json={"review_status": "excluded"})
    assert client.get(url).json()["marker_importance_counts"] == {"standard": 0, "important": 0, "critical": 0}
    client.patch(f"/api/markers/{marker_id}", json={"review_status": "active"})
    assert client.get(url).json()["marker_importance_counts"] == {"standard": 0, "important": 0, "critical": 1}


@pytest.mark.parametrize("url", ["/api/videos", "/api/library"])
def test_video_lists_aggregate_once_instead_of_querying_each_video(saved_marker, monkeypatch, url):
    store, _, _, client = saved_marker
    for index in range(4):
        store.create_video(f"empty-{index}", "empty.mp4", "empty.mp4", "empty.wav", 1000)
    aggregate = store.marker_importance_counts
    calls = []

    def count(video_id=None):
        calls.append(video_id)
        return aggregate(video_id)

    monkeypatch.setattr(store, "marker_importance_counts", count)
    assert client.get(url).status_code == 200
    assert calls == [None]
