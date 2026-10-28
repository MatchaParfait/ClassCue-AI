from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from classcue.video_store import VideoStore


@pytest.fixture
def secondary_library(tmp_path):
    store = VideoStore(tmp_path / "project.db")
    ids = []
    for index, duration in enumerate((60000, 15000, 120000)):
        source = tmp_path / f"camera-{index}.mp4"
        source.write_bytes(f"synthetic camera {index}".encode())
        ids.append(store.create_video(
            f"hash-{index}", source.name, str(source), str(source.with_suffix(".wav")), duration,
        ))
    app = create_app(store)
    with TestClient(app) as client:
        yield client, store, ids


@pytest.mark.parametrize("secondary_index", [1, 2])
def test_secondary_view_accepts_any_duration_and_persists_offset(secondary_library, secondary_index):
    client, store, ids = secondary_library
    endpoint = f"/api/videos/{ids[0]}/secondary-view"
    assert client.get(endpoint).json() is None
    result = client.put(endpoint, json={"secondary_video_id": ids[secondary_index], "offset_ms": 0})
    assert result.status_code == 200, result.text
    assert result.json()["duration_ms"] == (15000 if secondary_index == 1 else 120000)
    for offset in (-90000, 1234, 240000):
        result = client.patch(endpoint, json={"offset_ms": offset})
        assert result.status_code == 200
        assert result.json()["offset_ms"] == offset
        reopened = VideoStore(store.path)
        assert reopened.get_secondary_view(ids[0])["offset_ms"] == offset
    assert not store.list_recent_jobs()
    assert not store.has_transcript(ids[secondary_index])
    assert client.get(f"/api/videos/{ids[secondary_index]}/stream", headers={"Range": "bytes=0-8"}).status_code == 206


def test_secondary_view_rejects_self_missing_and_invalid_offsets(secondary_library):
    client, store, ids = secondary_library
    endpoint = f"/api/videos/{ids[0]}/secondary-view"
    assert client.put(endpoint, json={"secondary_video_id": ids[0], "offset_ms": 0}).status_code == 400
    assert client.put(endpoint, json={"secondary_video_id": 999, "offset_ms": 0}).status_code == 404
    assert client.patch(endpoint, json={"offset_ms": 0}).status_code == 404
    for invalid in (True, 1.5, "1000", 2**63):
        assert client.patch(endpoint, json={"offset_ms": invalid}).status_code == 422
    Path(store.get_video(ids[1])["video_path"]).unlink()
    assert client.put(endpoint, json={"secondary_video_id": ids[1], "offset_ms": 0}).status_code == 404
    assert client.get("/api/videos/999/secondary-view").status_code == 404


def test_remove_secondary_view_preserves_both_recordings_and_resets_new_offset(secondary_library):
    client, store, ids = secondary_library
    endpoint = f"/api/videos/{ids[0]}/secondary-view"
    client.put(endpoint, json={"secondary_video_id": ids[1], "offset_ms": -5000})
    assert client.delete(endpoint).status_code == 200
    assert client.delete(endpoint).status_code == 200
    assert client.get(endpoint).json() is None
    for video_id in ids[:2]:
        assert Path(store.get_video(video_id)["video_path"]).is_file()
    assert client.put(endpoint, json={"secondary_video_id": ids[2], "offset_ms": 0}).json()["offset_ms"] == 0


@pytest.mark.parametrize("deleted_index", [0, 1])
def test_recycle_and_undo_restore_secondary_association(secondary_library, tmp_path, deleted_index):
    client, store, ids = secondary_library
    endpoint = f"/api/videos/{ids[0]}/secondary-view"
    client.put(endpoint, json={"secondary_video_id": ids[1], "offset_ms": 1234})
    store.trash_video(ids[deleted_index], tmp_path / "videos" / ".recycle_bin")
    assert store.get_secondary_view(ids[0]) is None
    store.undo_latest_recycle(tmp_path / "videos" / ".recycle_bin")
    assert client.get(endpoint).json()["offset_ms"] == 1234
