import sqlite3

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from classcue.video_store import VideoStore


@pytest.fixture
def library(tmp_path):
    store = VideoStore(tmp_path / "project.db")
    folder = store.create_folder("Chapter")
    videos = []
    for index in range(3):
        path = tmp_path / f"lesson-{index}.mp4"
        path.write_bytes(f"synthetic-{index}".encode())
        videos.append(store.create_video(str(index), path.name, str(path), "", 1000))
    store.reorder_library(None, [("folder", folder), *(("video", video) for video in videos)])
    with TestClient(create_app(store)) as client:
        yield store, client, folder, videos, tmp_path / "trash"


def positions(store):
    with store.connect() as connection:
        return [
            tuple(row) for row in connection.execute(
                "SELECT 'folder', id, parent_id, library_order FROM video_folders "
                "UNION ALL SELECT 'video', id, library_folder_id, library_order FROM videos "
                "ORDER BY 1, 2"
            )
        ]


def undo(client, kind):
    assert client.get("/api/library/undo/latest").json()["kind"] == kind
    response = client.post("/api/library/undo")
    assert response.status_code == 200, response.text
    assert response.json()["kind"] == kind
    return response.json()


def test_mixed_move_order_and_delete_undo_in_persisted_reverse_order(library):
    store, client, folder, videos, _ = library
    original = positions(store)
    assert client.post(f"/api/videos/{videos[0]}/folder", json={"folder_id": folder}).status_code == 200
    moved = positions(store)
    assert client.post("/api/library/reorder", json={
        "parent_id": None,
        "ordered_items": [{"type": "video", "id": videos[2]}, {"type": "video", "id": videos[1]},
                          {"type": "folder", "id": folder}],
    }).status_code == 200
    reordered = positions(store)
    assert client.delete(f"/api/videos/{videos[2]}").status_code == 200
    assert VideoStore(store.path).latest_library_undo()["kind"] == "video"
    undo(client, "video")
    assert positions(store) == reordered
    undo(client, "reorder")
    assert positions(store) == moved
    result = undo(client, "move_video")
    assert result["parent_id"] is None
    assert positions(store) == original
    assert client.get("/api/library/undo/latest").json() is None
    assert client.post("/api/library/undo").status_code == 409


def test_folder_subtree_move_restores_parent_and_original_position(library):
    store, client, folder, videos, _ = library
    destination = store.create_folder("Destination")
    child = store.create_folder("Child", folder)
    leaf = store.create_folder("Leaf", child)
    store.set_video_folder(videos[0], leaf)
    original = positions(store)
    assert client.post(f"/api/folders/{child}/parent", json={"parent_id": destination}).status_code == 200
    result = undo(client, "move_folder")
    assert result["parent_id"] == folder
    assert positions(store) == original
    assert store.get_video(videos[0])["library_folder_id"] == leaf


def test_failed_and_noop_moves_do_not_consume_history(library):
    store, client, folder, videos, _ = library
    assert client.post(f"/api/videos/{videos[0]}/folder", json={"folder_id": None}).status_code == 200
    assert client.post(f"/api/folders/{folder}/parent", json={"parent_id": None}).status_code == 200
    assert client.get("/api/library/undo/latest").json() is None
    assert client.post(f"/api/videos/{videos[0]}/folder", json={"folder_id": folder}).status_code == 200
    latest = store.latest_library_undo()
    before = positions(store)
    assert client.post(f"/api/folders/{folder}/parent", json={"parent_id": folder}).status_code == 400
    assert client.post("/api/library/reorder", json={
        "ordered_items": [{"type": "video", "id": videos[0]}],
    }).status_code == 400
    assert store.latest_library_undo() == latest
    assert positions(store) == before
    undo(client, "move_video")


def test_missing_parent_keeps_history_for_retry(library):
    store, client, folder, videos, _ = library
    store.set_video_folder(videos[0], folder)
    with store.connect() as connection:
        original_folder = dict(connection.execute("SELECT * FROM video_folders WHERE id = ?", (folder,)).fetchone())
    assert client.post(f"/api/videos/{videos[0]}/folder", json={"folder_id": None}).status_code == 200
    store.delete_folder(folder)
    before = positions(store)
    latest = store.latest_library_undo()
    response = client.post("/api/library/undo")
    assert response.status_code == 409
    assert "原文件夹已不存在" in response.json()["detail"]
    assert positions(store) == before
    assert store.latest_library_undo() == latest
    with store.connect() as connection:
        store._insert_rows(connection, "video_folders", [original_folder])
    undo(client, "move_video")
    assert store.get_video(videos[0])["library_folder_id"] == folder


def test_new_untracked_position_is_not_overwritten(library):
    store, client, folder, videos, _ = library
    assert client.post(f"/api/videos/{videos[0]}/folder", json={"folder_id": folder}).status_code == 200
    store.set_video_folder(videos[0], None)
    before = positions(store)
    latest = store.latest_library_undo()
    assert client.post("/api/library/undo").status_code == 409
    assert positions(store) == before
    assert store.latest_library_undo() == latest


def test_undo_rejects_new_ancestry_cycle_without_losing_record(library):
    store, client, folder, _, _ = library
    child = store.create_folder("Child", folder)
    assert client.post(f"/api/folders/{child}/parent", json={"parent_id": None}).status_code == 200
    store.set_folder_parent(folder, child)
    before = positions(store)
    latest = store.latest_library_undo()
    assert client.post("/api/library/undo").status_code == 409
    assert positions(store) == before
    assert store.latest_library_undo() == latest


def test_reorder_undo_is_atomic_and_retryable_on_database_failure(library):
    store, client, folder, videos, trash = library
    original = positions(store)
    store.reorder_library(None, [("video", videos[2])], record_undo=True)
    reordered = positions(store)
    latest = store.latest_library_undo()
    with store.connect() as connection:
        connection.execute(
            f"CREATE TRIGGER deny_undo BEFORE UPDATE OF library_order ON videos "
            f"WHEN OLD.id = {videos[1]} BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.undo_latest_library_edit(trash)
    assert positions(store) == reordered
    assert store.latest_library_undo() == latest
    with store.connect() as connection:
        connection.execute("DROP TRIGGER deny_undo")
    undo(client, "reorder")
    assert positions(store) == original


def test_legacy_recycle_records_migrate_before_new_layout_edits(library):
    store, _, folder, videos, trash = library
    recycled = store.trash_video(videos[0], trash)
    with store.connect() as connection:
        connection.execute("DROP TABLE library_undo_history")
    reopened = VideoStore(store.path)
    assert reopened.latest_library_undo()["operation_id"] == recycled["operation_id"]
    reopened.set_video_folder(videos[1], folder, record_undo=True)
    assert reopened.undo_latest_library_edit(trash)["kind"] == "move_video"
    assert reopened.undo_latest_library_edit(trash)["kind"] == "video"
    assert reopened.get_video(videos[0]) is not None
    assert VideoStore(store.path).latest_library_undo() is None


def test_direct_recycle_undo_updates_shared_history(library):
    store, _, folder, videos, trash = library
    store.set_video_folder(videos[0], folder, record_undo=True)
    store.trash_folder(folder, trash)
    assert store.undo_latest_recycle(trash)["kind"] == "folder"
    assert store.latest_library_undo()["kind"] == "move_video"
    store.undo_latest_library_edit(trash)
    assert store.get_video(videos[0])["library_folder_id"] is None


def test_new_siblings_are_kept_when_undoing_an_earlier_reorder(library):
    store, client, _, videos, _ = library
    original = positions(store)
    store.reorder_library(None, [("video", videos[2])], record_undo=True)
    added = store.create_folder("New folder")
    undo(client, "reorder")
    assert [row for row in positions(store) if row[:2] != ("folder", added)] == original
    assert store.get_folder(added) is not None


@pytest.mark.parametrize("kind", ["move", "reorder"])
def test_undo_preserves_the_visible_order_even_when_server_positions_are_tied(library, kind):
    store, client, folder, videos, _ = library
    with store.connect() as connection:
        connection.execute("UPDATE videos SET library_order = 0")
    original = positions(store)
    visible = [{"type": "video", "id": videos[1]}, {"type": "folder", "id": folder},
               {"type": "video", "id": videos[2]}, {"type": "video", "id": videos[0]}]
    if kind == "move":
        response = client.post(f"/api/videos/{videos[0]}/folder",
                               json={"folder_id": folder, "previous_order": visible})
    else:
        response = client.post("/api/library/reorder", json={
            "ordered_items": [{"type": "video", "id": videos[2]}], "previous_order": visible,
        })
    assert response.status_code == 200
    reopened = VideoStore(store.path)
    restored = reopened.undo_latest_library_edit(store.path.parent / "trash")
    assert restored["ordered_items"] == visible
    assert positions(store) == original


@pytest.mark.parametrize("previous,status", [
    ([{"type": "video", "id": 99999}], 400),
    ([{"type": "video", "id": 1}, {"type": "video", "id": 1}], 400),
    ([{"type": "unknown", "id": 1}], 422),
    ([{"type": "video", "id": "1"}], 422),
])
def test_invalid_previous_order_does_not_change_positions_or_history(library, previous, status):
    store, client, folder, videos, _ = library
    original = positions(store)
    response = client.post(f"/api/videos/{videos[0]}/folder",
                           json={"folder_id": folder, "previous_order": previous})
    assert response.status_code == status
    assert positions(store) == original
    assert store.latest_library_undo() is None


def test_visible_reorder_is_undoable_even_if_the_server_already_has_the_requested_positions(library):
    store, client, folder, videos, _ = library
    original = positions(store)
    visible = [{"type": "video", "id": video} for video in reversed(videos)] + [{"type": "folder", "id": folder}]
    saved = [{"type": "folder", "id": folder}] + [{"type": "video", "id": video} for video in videos]
    assert client.post("/api/library/reorder", json={"ordered_items": saved, "previous_order": visible}).status_code == 200
    assert positions(store) == original
    assert undo(client, "reorder")["ordered_items"] == visible
    assert positions(store) == original
