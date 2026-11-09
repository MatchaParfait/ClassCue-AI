"""Slow image reads must not block task controls or return changed evidence."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

from PIL import Image
import pytest

import api.main as api_main
from classcue.asr_storage import slide_directory
from classcue.course_connection import connection_token
from classcue.course_tools import CourseTools
from tests.test_course_tools import fixture, video
from tests.test_course_retrieval import retrieval, seed_slides


def image_request(f, kind):
    video_id = video(f, "合成读取")
    root = slide_directory(api_main.DIRECTORY_PROJECT_ROOT, f.app.state.jobs.provider.data_root())
    slides = seed_slides(f.store, root, video_id)
    args = {"video_id": video_id}
    args.update({"start_seconds": 1} if kind == "frame" else {"slide_id": slides[0]["id"]})
    return video_id, root / "synthetic.jpg", slides[0]["id"], {
        "library_id": f.app.state.jobs.provider.library_id(), "arguments": args,
    }


def post_image(f, kind, payload):
    name = "read_video_frame" if kind == "frame" else "read_slide"
    return f.client.post(f"/api/course-tools/{name}", json=payload,
                         headers={"Authorization": f"Bearer {connection_token()}"})


@pytest.mark.parametrize("kind", ["frame", "slide"])
def test_slow_image_read_keeps_task_cancellation_responsive(retrieval, monkeypatch, kind):
    f = retrieval
    _, image, _, payload = image_request(f, kind)
    entered, release = Event(), Event()
    original = CourseTools._image

    def blocked(path):
        entered.set()
        assert release.wait(5)
        return original(path)

    if kind == "frame":
        def decode(*_args):
            entered.set()
            assert release.wait(5)
            return image
        monkeypatch.setattr(api_main, "video_preview_frame", decode)
    else:
        monkeypatch.setattr(CourseTools, "_image", staticmethod(blocked))
    f.store.create_job("paused-for-cancel", "course_transcribe")
    f.store.update_job("paused-for-cancel", status="resource_paused")
    with ThreadPoolExecutor(max_workers=2) as pool:
        picture = pool.submit(post_image, f, kind, payload)
        try:
            assert entered.wait(3)
            cancellation = pool.submit(f.client.post, "/api/jobs/paused-for-cancel/cancel")
            result = cancellation.result(timeout=2)
            assert result.status_code == 200
            assert result.json()["status"] == "interrupted"
        finally:
            release.set()
        assert picture.result(timeout=3).status_code == 200


@pytest.mark.parametrize("change", ["frame_file", "frame_path", "slide_file", "slide_metadata", "library"])
def test_image_read_rejects_evidence_changed_while_encoding(retrieval, monkeypatch, change):
    f = retrieval
    kind = "frame" if change.startswith("frame") else "slide"
    video_id, image, slide_id, payload = image_request(f, kind)
    entered, release = Event(), Event()
    original = CourseTools._image

    def blocked(path):
        result = original(path)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(CourseTools, "_image", staticmethod(blocked))
    monkeypatch.setattr(api_main, "video_preview_frame", lambda *_args: image)
    with ThreadPoolExecutor(max_workers=1) as pool:
        picture = pool.submit(post_image, f, kind, payload)
        try:
            assert entered.wait(3)
            with f.app.state.jobs._lock:
                if change == "frame_file":
                    Path(f.store.get_video(video_id)["video_path"]).write_bytes(b"new source bytes")
                elif change == "frame_path":
                    with f.store.connect() as db:
                        db.execute("UPDATE videos SET video_path=? WHERE id=?", ("different.mp4", video_id))
                elif change == "slide_file":
                    Image.new("RGB", (20, 20), (220, 10, 10)).save(image)
                elif change == "slide_metadata":
                    with f.store.connect() as db:
                        db.execute("UPDATE video_slides SET image_hash=? WHERE id=?", ("b" * 64, slide_id))
                else:
                    monkeypatch.setattr(f.app.state.jobs.provider, "library_id", lambda: "different-library")
        finally:
            release.set()
        response = picture.result(timeout=3)
        assert response.status_code == 409
        assert "image" not in response.json()
        assert "变化" in response.json()["detail"]
