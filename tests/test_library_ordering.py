from contextlib import closing
import sqlite3

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from api.main import create_app
from classcue.video_store import VideoStore


def create_video(store, label):
    return store.create_video(label, f"{label}.mp4", f"{label}.mp4", f"{label}.wav", 1000)


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("first_kind", ["video", "folder"])
def test_saved_mixed_api_order_survives_repeated_initialization(tmp_path, nested, first_kind):
    path = tmp_path / "videos.db"
    store = VideoStore(path)
    parent = store.create_folder("parent") if nested else None
    first_folder = store.create_folder("first folder", parent)
    second_folder = store.create_folder("second folder", parent)
    first_video = create_video(store, "first")
    second_video = create_video(store, "second")
    store.set_video_folder(first_video, parent)
    store.set_video_folder(second_video, parent)
    if first_kind == "video":
        expected = [
            ("video", second_video), ("folder", second_folder),
            ("video", first_video), ("folder", first_folder),
        ]
    else:
        expected = [
            ("folder", second_folder), ("video", second_video),
            ("folder", first_folder), ("video", first_video),
        ]
    params = {} if parent is None else {"folder_id": parent}
    with TestClient(create_app(store)) as client:
        response = client.post("/api/library/reorder", json={
            "parent_id": parent,
            "ordered_items": [{"type": kind, "id": item_id} for kind, item_id in expected],
        })
        assert response.status_code == 200
        before = client.get("/api/library", params=params).json()
    for _ in range(3):
        store = VideoStore(path)
        with TestClient(create_app(store)) as client:
            response = client.get("/api/library", params=params)
            assert response.status_code == 200
            assert response.json() == before
        rows = [(item["type"], item[item["type"]]) for item in response.json()["items"]]
        assert [(kind, row["id"]) for kind, row in rows] == expected
        assert [row["library_order"] for _kind, row in rows] == list(range(4))


@pytest.mark.parametrize("already_has_order", [False, True])
def test_old_library_schema_initializes_only_new_order_column(tmp_path, already_has_order):
    path = tmp_path / "legacy.db"
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            connection.execute("""
                CREATE TABLE videos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content_hash TEXT NOT NULL UNIQUE,
                    filename TEXT NOT NULL,
                    video_path TEXT NOT NULL,
                    audio_path TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'uploaded',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            connection.executemany(
                "INSERT INTO videos (content_hash,filename,video_path,audio_path) VALUES (?,?,?,?)",
                [("first", "first.mp4", "first.mp4", "first.wav"),
                 ("second", "second.mp4", "second.mp4", "second.wav")],
            )
            if already_has_order:
                connection.execute("ALTER TABLE videos ADD COLUMN library_order INTEGER NOT NULL DEFAULT 0")
                connection.execute("UPDATE videos SET library_order=9 WHERE id=1")
    for _ in range(3):
        store = VideoStore(path)
        rows = store.list_folder_videos(None)
        expected = [(2, 0), (1, 9)] if already_has_order else [(1, 1), (2, 2)]
        assert [(row["id"], row["library_order"]) for row in rows] == expected
        assert all(row["library_folder_id"] is None for row in rows)


def test_multiple_zero_positions_are_not_reinterpreted_on_startup(tmp_path):
    path = tmp_path / "videos.db"
    store = VideoStore(path)
    ids = [create_video(store, label) for label in ("second", "first")]
    assert all(store.get_video(video_id)["library_order"] == 0 for video_id in ids)
    for _ in range(3):
        store = VideoStore(path)
        assert all(store.get_video(video_id)["library_order"] == 0 for video_id in ids)
        assert [row["id"] for row in store.list_folder_videos(None)] == ids[::-1]
