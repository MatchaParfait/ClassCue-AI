from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from api.main import create_app
from classcue.video_store import VideoStore


def test_library_folder_api_contract(tmp_path: Path):
    source = tmp_path / "lesson.mp4"
    source.write_bytes(b"video")
    store = VideoStore(tmp_path / "video.db")
    video_id = store.create_video(
        "library-api-video", source.name, str(source), str(tmp_path / "lesson.wav"), 1000
    )
    client = TestClient(create_app(store))

    created = client.post("/api/folders", json={"name": "第一章"})
    assert created.status_code == 201
    folder_id = created.json()["id"]

    moved = client.post(
        f"/api/videos/{video_id}/folder", json={"folder_id": folder_id}
    )
    assert moved.status_code == 200
    assert moved.json()["folder_id"] == folder_id

    listing = client.get("/api/library", params={"folder_id": folder_id})
    assert listing.status_code == 200
    assert listing.json()["items"][0]["video"]["id"] == video_id

    reordered = client.post(
        "/api/library/reorder",
        json={
            "parent_id": folder_id,
            "ordered_items": [{"type": "video", "id": video_id}],
        },
    )
    assert reordered.status_code == 200

    reserved = client.post(f"/api/folders/{folder_id}/process")
    assert reserved.status_code == 501
    assert reserved.json()["status"] == "not_implemented"


def test_folder_moves_preserve_subtree_files_and_persist(tmp_path: Path):
    store = VideoStore(tmp_path / "video.db")
    first = store.create_folder("First")
    second = store.create_folder("Second")
    child = store.create_folder("Child", first)
    leaf = store.create_folder("Leaf", child)
    source = tmp_path / "lesson.mp4"
    source.write_bytes(b"fixture")
    video = store.create_video("hash", source.name, str(source), "", 1000)
    store.set_video_folder(video, leaf)
    store.reorder_library(second, [("folder", store.create_folder("Existing", second))])
    with TestClient(create_app(store)) as client:
        for destination in (second, None, first):
            moved = client.post(f"/api/folders/{child}/parent", json={"parent_id": destination})
            assert moved.status_code == 200
            assert moved.json()["parent_id"] == destination
            items = client.get("/api/library", params={} if destination is None else {"folder_id": destination}).json()["items"]
            assert items[-1]["folder"]["id"] == child
            assert moved.json()["video_count"] == 1
            assert store.get_folder(leaf)["parent_id"] == child
            assert store.get_video(video)["library_folder_id"] == leaf
            assert source.read_bytes() == b"fixture"
            reopened = VideoStore(tmp_path / "video.db")
            assert reopened.get_folder(child)["parent_id"] == destination


@pytest.mark.parametrize("target", ["self", "child", "descendant", "missing"])
def test_folder_move_rejects_cycles_and_missing_target_without_changes(tmp_path: Path, target: str):
    store = VideoStore(tmp_path / "video.db")
    parent = store.create_folder("Parent")
    child = store.create_folder("Child", parent)
    leaf = store.create_folder("Leaf", child)
    destination = {"self": parent, "child": child, "descendant": leaf, "missing": 99999}[target]
    before = [dict(store.get_folder(item)) for item in (parent, child, leaf)]
    with TestClient(create_app(store)) as client:
        response = client.post(f"/api/folders/{parent}/parent", json={"parent_id": destination})
        assert response.status_code == (404 if target == "missing" else 400)
        assert [dict(store.get_folder(item)) for item in (parent, child, leaf)] == before
        assert client.post("/api/folders/99999/parent", json={"parent_id": None}).status_code == 404


def test_folder_move_to_same_parent_keeps_order_and_name(tmp_path: Path):
    store = VideoStore(tmp_path / "video.db")
    parent = store.create_folder("Parent")
    child = store.create_folder("Repeated name", parent)
    other = store.create_folder("Repeated name")
    with TestClient(create_app(store)) as client:
        before = dict(store.get_folder(child))
        assert client.post(f"/api/folders/{child}/parent", json={"parent_id": parent}).status_code == 200
        assert dict(store.get_folder(child)) == before
        assert client.post(f"/api/folders/{other}/parent", json={"parent_id": parent}).status_code == 200
        assert [row["id"] for row in store.list_folders(parent)] == [child, other]
