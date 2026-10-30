from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
import api.main as api_main
import classcue.video_pipeline as video_pipeline
from classcue.core import TranscriptSegment
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


@pytest.fixture
def secondary_upload_library(secondary_library, tmp_path, monkeypatch):
    monkeypatch.setattr(api_main, "DIRECTORY_PROJECT_ROOT", tmp_path / "application")
    monkeypatch.setattr(video_pipeline, "video_duration_ms", lambda _: 1000)
    _, store, ids = secondary_library
    with store.connect() as connection:
        for video_id in ids:
            row = store.get_video(video_id)
            connection.execute("UPDATE videos SET content_hash = ? WHERE id = ?",
                               (store.file_hash(Path(row["video_path"])), video_id))
    return secondary_library


def upload_secondary(client, primary, content=b"synthetic secondary upload"):
    return client.post("/api/videos/upload", params={"filename": "secondary.mp4", "secondary_for": primary},
                       content=content)


def test_secondary_upload_stays_out_of_library_counts_search_and_folder_batches(secondary_upload_library):
    client, store, ids = secondary_upload_library
    folder = store.create_folder("Synthetic course")
    store.set_video_folder(ids[0], folder)
    response = upload_secondary(client, ids[0])
    assert response.status_code == 200, response.text
    secondary = response.json()["video"]["id"]
    assert response.json()["secondary_view"]["video_id"] == secondary
    assert store.get_video(secondary)["is_secondary"] == 1
    assert {row["id"] for row in store.list_videos()} == set(ids)
    assert {row["id"] for row in client.get("/api/videos").json()} == set(ids)
    assert client.get(f"/api/videos/{secondary}/stream").content == b"synthetic secondary upload"
    store.set_video_folder(secondary, folder)
    assert store.get_folder(folder)["video_count"] == 1
    assert [row["id"] for row in store.list_folder_videos(folder, recursive=True)] == [ids[0]]
    listing = client.get("/api/library", params={"folder_id": folder}).json()["items"]
    assert [item["video"]["id"] for item in listing] == [ids[0]]
    assert client.post(f"/api/folders/{folder}/process").json()["video_ids"] == [ids[0]]
    reordered = client.post("/api/library/reorder", json={
        "parent_id": folder, "ordered_items": [{"type": "video", "id": ids[0]}],
        "previous_items": [{"type": "video", "id": ids[0]}],
    })
    assert reordered.status_code == 200, reordered.text
    assert not store.has_transcript(secondary)
    assert not store.list_recent_jobs()
    store.mark_transcribed(secondary, [TranscriptSegment(1, 0, 1000, "secondary-only-search")])
    assert client.get("/api/library/search", params={"q": "secondary-only-search"}).json()["total"] == 0


def test_secondary_upload_reuses_binding_and_stays_hidden_after_unlink_and_restart(secondary_upload_library):
    client, store, ids = secondary_upload_library
    first = upload_secondary(client, ids[0]).json()
    second = upload_secondary(client, ids[0]).json()
    assert first["secondary_view"] == second["secondary_view"]
    assert second["existed"]
    assert len(store.list_videos(include_secondary=True)) == 4
    client.delete(f"/api/videos/{ids[0]}/secondary-view")
    reopened = VideoStore(store.path)
    assert {row["id"] for row in reopened.list_videos()} == set(ids)
    assert reopened.get_video(first["video"]["id"])["is_secondary"] == 1
    assert Path(reopened.get_video(first["video"]["id"])["video_path"]).is_file()


def test_secondary_upload_preserves_existing_primary_and_explicit_normal_import_promotes(secondary_upload_library):
    client, store, ids = secondary_upload_library
    normal = Path(store.get_video(ids[1])["video_path"]).read_bytes()
    linked = upload_secondary(client, ids[0], normal)
    assert linked.status_code == 200, linked.text
    assert linked.json()["secondary_view"]["video_id"] == ids[1]
    assert store.get_video(ids[1])["is_secondary"] == 0
    hidden = upload_secondary(client, ids[0]).json()["video"]["id"]
    promoted = client.post("/api/videos/upload", params={"filename": "secondary.mp4"},
                           content=b"synthetic secondary upload")
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["video"]["id"] == hidden
    assert store.get_video(hidden)["is_secondary"] == 0
    assert hidden in {row["id"] for row in VideoStore(store.path).list_videos()}
    assert store.get_secondary_view(ids[0])["video_id"] == hidden


def test_secondary_upload_rejects_main_recording_and_missing_parent_without_registration(secondary_upload_library):
    client, store, ids = secondary_upload_library
    before = dict(store.get_video(ids[0]))
    content = Path(before["video_path"]).read_bytes()
    assert upload_secondary(client, ids[0], content).status_code == 400
    assert dict(store.get_video(ids[0])) == before
    assert upload_secondary(client, 999).status_code == 404
    assert len(store.list_videos(include_secondary=True)) == 3
    assert store.get_secondary_view(ids[0]) is None


@pytest.mark.parametrize("independent_study", [False, True])
def test_legacy_secondary_migration_hides_unused_uploads_and_keeps_study_data(secondary_library, independent_study):
    _, store, ids = secondary_library
    store.set_secondary_view(ids[0], ids[1], 123)
    if independent_study:
        store.mark_transcribed(ids[1], [TranscriptSegment(1, 0, 1000, "Independent study")])
    with store.connect() as connection:
        connection.execute("ALTER TABLE videos DROP COLUMN is_secondary")
    for _ in range(2):
        store = VideoStore(store.path)
        assert bool(store.get_video(ids[1])["is_secondary"]) is not independent_study
        assert (ids[1] in {row["id"] for row in store.list_videos()}) is independent_study
        assert store.get_secondary_view(ids[0])["offset_ms"] == 123
        assert Path(store.get_video(ids[1])["video_path"]).is_file()
