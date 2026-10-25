"""Recycle undo must keep slide images readable under the active slide root."""

import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

import api.main as api_main
from classcue.video_store import VideoStore


@pytest.fixture
def library(tmp_path):
    videos = tmp_path / "library" / "videos"
    videos.mkdir(parents=True)
    store = VideoStore(tmp_path / "library" / "project.db")
    return SimpleNamespace(
        store=store, videos=videos, trash=videos / ".recycle_bin",
        slides=tmp_path / "library" / "slides",
    )


def add_video(library, name="lesson.mp4", content=b"video fixture"):
    video_file = library.videos / name
    video_file.write_bytes(content)
    video = library.store.create_video(
        VideoStore.content_hash(content), name, str(video_file), "", 10000)
    return video, video_file


def slide_row(relative, digest, start_ms, end_ms, representative_ms):
    return {
        "start_ms": start_ms, "end_ms": end_ms, "representative_ms": representative_ms,
        "image_path": relative, "image_hash": digest, "duration_ms": end_ms - start_ms,
        "retention_class": "primary", "confidence": 1.0,
        "roi_json": "{}", "extraction_version": "synthetic",
    }


def add_slide_image(slide_root, relative, content):
    image = slide_root / relative
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(content)
    return image


def synthetic_slide(store, video_id, slide_root, content=b"slide fixture"):
    digest = VideoStore.content_hash(content)
    relative = f"{digest}/generation-1/state.jpg"
    image = add_slide_image(slide_root, relative, content)
    store.replace_video_slides(video_id, [slide_row(relative, digest, 0, 5000, 1000)])
    return relative, image


def active_payload(store):
    with store.connect() as connection:
        row = connection.execute(
            "SELECT payload_json FROM recycle_bin WHERE undone_at IS NULL").fetchone()
    return json.loads(row["payload_json"])


def rewrite_active_payload(store, transform):
    with store.connect() as connection:
        row = connection.execute(
            "SELECT operation_id, payload_json FROM recycle_bin WHERE undone_at IS NULL").fetchone()
        payload = transform(json.loads(row["payload_json"]))
        connection.execute(
            "UPDATE recycle_bin SET payload_json = ? WHERE operation_id = ?",
            (json.dumps(payload, ensure_ascii=False), row["operation_id"]),
        )


def test_undo_after_media_rebind_restores_slide_under_current_slide_root(library, tmp_path):
    store = library.store
    video, video_file = add_video(library)
    relative, original = synthetic_slide(store, video, library.slides)

    store.trash_video(video, library.trash, library.slides)
    assert not original.exists()

    current_root = tmp_path / "media" / "slides"
    assert store.undo_latest_recycle(library.trash, current_root)["videos"] == 1

    rows = store.list_slides(video, include_auxiliary=True)
    assert [row["image_path"] for row in rows] == [relative]
    assert (current_root / relative).read_bytes() == b"slide fixture"
    assert not original.exists()
    assert video_file.read_bytes() == b"video fixture"


def test_undo_with_unchanged_slide_root_restores_in_place(library):
    store = library.store
    video, _ = add_video(library)
    relative, original = synthetic_slide(store, video, library.slides)

    store.trash_video(video, library.trash, library.slides)
    assert store.undo_latest_recycle(library.trash, library.slides)["videos"] == 1

    assert original.read_bytes() == b"slide fixture"
    assert [row["image_path"] for row in store.list_slides(video)] == [relative]


def test_undo_derives_relative_path_for_legacy_payload(library, tmp_path):
    store = library.store
    video, _ = add_video(library)
    relative, _ = synthetic_slide(store, video, library.slides)
    store.trash_video(video, library.trash, library.slides)
    # Payloads written before this fix carry absolute originals only.
    rewrite_active_payload(store, lambda payload: {
        **payload,
        "files": [
            {key: value for key, value in item.items() if key != "relative"}
            for item in payload["files"]
        ],
    })

    current_root = tmp_path / "media" / "slides"
    assert store.undo_latest_recycle(library.trash, current_root)["videos"] == 1

    assert (current_root / relative).read_bytes() == b"slide fixture"
    assert [row["image_path"] for row in store.list_slides(video)] == [relative]


def test_undo_conflict_in_current_slide_root_keeps_file_and_recycle(library, tmp_path):
    store = library.store
    video, video_file = add_video(library)
    relative, _ = synthetic_slide(store, video, library.slides)
    store.trash_video(video, library.trash, library.slides)
    current_root = tmp_path / "media" / "slides"
    replacement = add_slide_image(current_root, relative, b"new generation")

    with pytest.raises(ValueError, match="原位置已有同名文件"):
        store.undo_latest_recycle(library.trash, current_root)

    assert replacement.read_bytes() == b"new generation"
    assert store.get_video(video) is None
    assert not video_file.exists()
    slide_record = next(
        item for item in active_payload(store)["files"] if item.get("role") == "slide")
    assert Path(slide_record["trash"]).read_bytes() == b"slide fixture"
    assert store.latest_recycle() is not None

    replacement.unlink()
    store.undo_latest_recycle(library.trash, current_root)
    assert (current_root / relative).read_bytes() == b"slide fixture"
    assert store.get_video(video) is not None


def test_folder_undo_after_media_rebind_restores_video_and_slides(library, tmp_path):
    store = library.store
    folder = store.create_folder("课程")
    video, _ = add_video(library)
    store.set_video_folder(video, folder)
    relative, _ = synthetic_slide(store, video, library.slides)

    store.trash_folder(folder, library.trash, library.slides)
    assert store.get_folder(folder) is None and store.get_video(video) is None

    current_root = tmp_path / "media" / "slides"
    undo = store.undo_latest_recycle(library.trash, current_root)
    assert undo["folders"] == 1 and undo["videos"] == 1
    assert store.get_video(video)["library_folder_id"] == folder
    assert (current_root / relative).read_bytes() == b"slide fixture"


def test_shared_slide_image_between_states_is_restored_once(library, tmp_path):
    store = library.store
    video, _ = add_video(library)
    digest = VideoStore.content_hash(b"shared fixture")
    relative = f"{digest}/generation-1/state.jpg"
    add_slide_image(library.slides, relative, b"shared fixture")
    store.replace_video_slides(video, [
        slide_row(relative, digest, 0, 5000, 1000),
        slide_row(relative, digest, 5000, 10000, 6000),
    ])
    store.trash_video(video, library.trash, library.slides)
    records = [
        item for item in active_payload(store)["files"]
        if item.get("role") == "slide" and item.get("moved")]
    assert len(records) == 1

    current_root = tmp_path / "media" / "slides"
    store.undo_latest_recycle(library.trash, current_root)

    assert (current_root / relative).read_bytes() == b"shared fixture"
    assert len(store.list_slides(video, include_auxiliary=True)) == 2


def test_undo_without_slide_root_restores_recorded_originals(library):
    store = library.store
    video, _ = add_video(library)
    relative, original = synthetic_slide(store, video, library.slides)

    store.trash_video(video, library.trash, library.slides)
    assert store.undo_latest_recycle(library.trash)["videos"] == 1

    assert original.read_bytes() == b"slide fixture"
    assert [row["image_path"] for row in store.list_slides(video)] == [relative]


def test_api_undo_after_media_first_bind_serves_slide_from_new_root(tmp_path, monkeypatch):
    monkeypatch.setattr(api_main, "DIRECTORY_PROJECT_ROOT", tmp_path / "application")
    library_videos = tmp_path / "library" / "videos"
    library_videos.mkdir(parents=True)
    store = VideoStore(tmp_path / "library" / "project.db")
    video_file = library_videos / "lesson.mp4"
    video_file.write_bytes(b"video fixture")
    video = store.create_video(VideoStore.content_hash(b"video fixture"), "lesson.mp4",
                               str(video_file), "", 10000)
    relative, _ = synthetic_slide(store, video, tmp_path / "library" / "slides")

    application = api_main.create_app(store)
    with TestClient(application) as client:
        deleted = client.delete(f"/api/videos/{video}")
        assert deleted.status_code == 200, deleted.text

        # A confirmed first bind of the media root leaves this configuration.
        configuration = tmp_path / "application" / "data" / "asr_storage.json"
        configuration.parent.mkdir(parents=True)
        configuration.write_text(
            json.dumps({"media_storage_root": str(tmp_path / "media")}), encoding="utf-8")

        undone = client.post("/api/recycle-bin/undo")
        assert undone.status_code == 200, undone.text

        slide_id = store.list_slides(video, include_auxiliary=True)[0]["id"]
        image = client.get(f"/api/videos/{video}/slides/{slide_id}/image")
        assert image.status_code == 200, image.text
        assert image.content == b"slide fixture"
