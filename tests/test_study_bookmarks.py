from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from api.main import create_app
from classcue.study_bookmarks import StudyBookmarkStore
from classcue.video_store import VideoStore


@pytest.fixture
def library(tmp_path):
    store = VideoStore(tmp_path / "project.db")
    parent = store.create_folder("Course")
    child = store.create_folder("Week", parent)
    other = store.create_folder("Other")
    source = tmp_path / "lesson.mp4"
    source.write_bytes(b"synthetic media")
    video = store.create_video("synthetic", source.name, str(source), "", 60_000)
    store.set_video_folder(video, child)
    client = TestClient(create_app(store))
    identity = client.get("/api/library/status").json()["library_id"]
    return store, client, identity, parent, child, other, video


def test_bookmarks_and_recent_playback_follow_nested_folder_moves(library):
    store, client, identity, parent, child, other, video = library
    repo = StudyBookmarkStore(store)
    assert client.get(f"/api/videos/{video}/study-progress", params={"library_id": identity}).json() is None
    saved = client.put(f"/api/videos/{video}/study-progress", json={
        "library_id": identity, "position_ms": 23_180, "duration_ms": 60_000, "observed_at": 100,
    })
    assert saved.status_code == 200
    assert saved.json()["position_ms"] == 23_180
    first = client.post(f"/api/videos/{video}/bookmarks", json={
        "library_id": identity, "position_ms": 22_000, "note": "  再看一遍  ",
    })
    assert first.status_code == 201
    second = repo.create(video, None, "整个视频复习")
    navigation = client.get("/api/study-navigation").json()
    assert navigation["library_id"] == identity
    folders = {item["folder_id"]: item for item in navigation["folders"]}
    assert folders[parent]["bookmark_count"] == folders[child]["bookmark_count"] == 2
    assert folders[other]["latest"] is None
    assert folders[parent]["latest"]["video_id"] == video
    assert [folder["id"] for folder in folders[parent]["latest"]["path"]] == [parent, child]
    assert len(client.get("/api/study-bookmarks", params={"folder_id": parent, "library_id": identity}).json()) == 2
    edited = client.patch(f"/api/study-bookmarks/{first.json()['id']}", json={"library_id": identity, "note": "下次看"})
    assert edited.status_code == 200
    assert edited.json()["note"] == "下次看"
    store.update_folder(child, "Renamed")
    store.set_folder_parent(child, other)
    navigation = repo.navigation()
    folders = {item["folder_id"]: item for item in navigation["folders"]}
    assert folders[parent]["bookmark_count"] == 0
    assert folders[parent]["latest"] is None
    assert folders[other]["bookmark_count"] == 2
    assert folders[other]["latest"]["path"][-1]["name"] == "Renamed"
    assert repo.bookmarks(parent) == []
    assert len(repo.bookmarks(other)) == 2
    assert client.request("DELETE", f"/api/study-bookmarks/{second['id']}", json={"library_id": identity}).status_code == 200
    assert len(repo.bookmarks(video_id=video)) == 1


def test_progress_order_completion_and_restart(library):
    store, client, identity, parent, child, other, video = library
    repo = StudyBookmarkStore(store)
    repo.save_progress(video, 60_000, 60_000, 200)
    repo.save_progress(video, 10_000, 60_000, 100)
    reopened = StudyBookmarkStore(VideoStore(store.path))
    assert reopened.progress(video)["position_ms"] == 60_000
    assert reopened.navigation()["latest"]["completed"] is True
    repo.save_progress(video, 5_000, 60_000, 300)
    assert repo.navigation()["latest"]["position_ms"] == 5_000
    assert repo.navigation()["latest"]["completed"] is False
    assert repo.save_progress(video, 100_000, 60_000, 400)["position_ms"] == 60_000


def test_delete_undo_and_redo_restore_study_records(library, tmp_path):
    store, client, identity, parent, child, other, video = library
    repo = StudyBookmarkStore(store)
    repo.create(video, 1000, "reminder")
    repo.save_progress(video, 2000, 60_000, 200)
    store.trash_folder(parent, tmp_path / "trash")
    assert repo.navigation()["latest"] is None
    assert repo.bookmarks() == []
    store.undo_latest_library_edit(tmp_path / "trash")
    assert repo.progress(video)["position_ms"] == 2000
    assert repo.bookmarks(parent)[0]["note"] == "reminder"
    store.undo_latest_library_edit(tmp_path / "trash", redo=True)
    assert repo.bookmarks() == []
    store.undo_latest_library_edit(tmp_path / "trash")
    assert repo.bookmarks(parent)[0]["position_ms"] == 1000


def test_validation_missing_records_and_library_scope(library):
    store, client, identity, parent, child, other, video = library
    for payload in [
        {"position_ms": -1}, {"position_ms": True}, {"position_ms": 60_001},
        {"note": "a" * 2001}, {"unknown": "ignored"},
    ]:
        assert client.post(f"/api/videos/{video}/bookmarks", json={"library_id": identity, **payload}).status_code in (400, 422)
    assert client.post(f"/api/videos/{video}/bookmarks", json={"library_id": "wrong"}).status_code == 409
    assert client.post("/api/videos/99999/bookmarks", json={"library_id": identity}).status_code == 404
    assert client.get("/api/study-bookmarks", params={"library_id": identity, "folder_id": 99999}).status_code == 404
    assert client.patch("/api/study-bookmarks/99999", json={"library_id": identity, "note": ""}).status_code == 404
    assert client.put(f"/api/videos/{video}/study-progress", json={
        "library_id": identity, "position_ms": -1, "duration_ms": 0, "observed_at": 100,
    }).status_code == 422


def test_latest_is_per_folder_and_bookmarks_survive_newer_playback(library):
    store, client, identity, parent, child, other, video = library
    second = store.create_video("second", "second.mp4", "second.mp4", "", 60_000)
    store.set_video_folder(second, other)
    repo = StudyBookmarkStore(store)
    repo.create(video, 1000, "keep this")
    repo.save_progress(video, 5000, 60_000, 100)
    repo.save_progress(second, 6000, 60_000, 200)
    navigation = repo.navigation()
    assert navigation["latest"]["video_id"] == second
    folders = {item["folder_id"]: item for item in navigation["folders"]}
    assert folders[parent]["latest"]["video_id"] == video
    assert folders[parent]["bookmark_count"] == 1
    assert folders[other]["bookmark_count"] == 0
