import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from api.main import create_app
from core import TranscriptSegment
import video_store
from video_store import VideoStore


@pytest.fixture
def library(tmp_path, monkeypatch):
    store = VideoStore(tmp_path / "videos.db")
    connections = []
    queries = []
    base = video_store.ClosingConnection

    class TrackedConnection(base):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.closed = False
            self.set_trace_callback(
                lambda sql: queries.append(sql) if sql.lstrip().upper().startswith(("SELECT", "WITH")) else None
            )
            connections.append(self)

        def close(self):
            super().close()
            self.closed = True

    monkeypatch.setattr(video_store, "ClosingConnection", TrackedConnection)
    client = TestClient(create_app(store))
    return store, client, connections, queries


def add_video(store, label):
    return store.create_video(label, f"{label}.mp4", f"{label}.mp4", f"{label}.wav", 1000)


@pytest.mark.parametrize("count", [1, 20])
def test_video_list_uses_bounded_queries_including_task_locks_and_closes_connections(library, count):
    store, client, connections, queries = library
    video_ids = [add_video(store, f"video{index}") for index in range(count)]
    store.mark_transcribed(video_ids[0], [TranscriptSegment(1, 0, 1000, "test")])
    connections.clear()
    queries.clear()

    response = client.get("/api/videos")

    assert response.status_code == 200
    rows = response.json()
    assert [row["id"] for row in rows] == video_ids[::-1]
    assert sum(row["has_transcript"] for row in rows) == 1
    assert len(connections) == len(queries) == 4
    assert all(connection.closed for connection in connections)


@pytest.mark.parametrize("count", [1, 20])
def test_mixed_library_uses_bounded_queries_regardless_of_item_count(library, count):
    store, client, connections, queries = library
    folders = []
    for index in range(count):
        add_video(store, f"root{index}")
        folder_id = store.create_folder(f"folder{index}")
        folders.append(folder_id)
        store.set_video_folder(add_video(store, f"child{index}"), folder_id)
    connections.clear()
    queries.clear()

    response = client.get("/api/library")

    assert response.status_code == 200
    items = response.json()["items"]
    folder_rows = [item["folder"] for item in items if item["type"] == "folder"]
    assert {row["id"] for row in folder_rows} == set(folders)
    assert all(row["video_count"] == 1 for row in folder_rows)
    assert len(items) == count * 2
    assert len(connections) == len(queries) == 5
    assert all(connection.closed for connection in connections)


def test_folder_counts_include_descendants_and_refresh_after_move(library):
    store, client, connections, queries = library
    parent = store.create_folder("parent")
    nested = store.create_folder("nested", parent)
    empty = store.create_folder("empty", parent)
    direct_video = add_video(store, "direct")
    nested_video = add_video(store, "nested")
    store.set_video_folder(direct_video, parent)
    store.set_video_folder(nested_video, nested)
    connections.clear()
    queries.clear()

    response = client.get(f"/api/library?folder_id={parent}")

    assert response.status_code == 200
    body = response.json()
    assert body["folder"]["video_count"] == 2
    counts = {item["folder"]["id"]: item["folder"]["video_count"]
              for item in body["items"] if item["type"] == "folder"}
    assert counts == {nested: 1, empty: 0}
    assert len(connections) == len(queries) == 6
    assert all(connection.closed for connection in connections)
    store.set_video_folder(nested_video, parent)
    assert store.get_folder(parent)["video_count"] == 2
    assert store.get_folder(nested)["video_count"] == 0
    assert {row["id"]: row["video_count"] for row in store.list_folders(parent)} == {
        nested: 0, empty: 0,
    }


def test_transcript_flag_is_live_not_cached(library):
    store, client, _connections, _queries = library
    video_id = add_video(store, "video")
    assert client.get("/api/videos").json()[0]["has_transcript"] is False
    store.mark_transcribed(video_id, [TranscriptSegment(1, 0, 1000, "test")])
    store.set_status(video_id, "failed")
    assert client.get("/api/videos").json()[0]["has_transcript"] is True
    assert store.list_folder_videos(None)[0]["has_transcript"] == 1
    assert store.get_video(video_id)["has_transcript"] == 1
    with store.connect() as connection:
        connection.execute("DELETE FROM video_transcript_segments WHERE video_id = ?", (video_id,))
    assert client.get("/api/videos").json()[0]["has_transcript"] is False


def test_api_direct_connection_closes_on_success_and_missing_marker(library):
    store, client, connections, _queries = library
    video_id = add_video(store, "video")
    marker_id = store.add_marker(video_id, 0, 1000, "test", "", "test", "test", .9)
    connections.clear()
    response = client.patch(f"/api/markers/{marker_id}", json={"review_status": "mastered"})
    assert response.status_code == 200
    assert connections and all(connection.closed for connection in connections)
    connections.clear()
    response = client.patch("/api/markers/999999", json={"review_status": "mastered"})
    assert response.status_code == 404
    assert connections and all(connection.closed for connection in connections)
