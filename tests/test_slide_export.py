from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfReader
import pytest

from api.main import create_app
import api.main as api_main
from classcue.slide_export import write_slide_pdf
from classcue.video_store import VideoStore


@pytest.fixture
def export_data(tmp_path, monkeypatch):
    root = tmp_path / "slides"
    root.mkdir()
    for name, size, color in (("early", (640, 360), "red"), ("complete", (800, 600), "blue")):
        Image.new("RGB", size, color).save(root / f"{name}.jpg", quality=95)
    store = VideoStore(tmp_path / "library.db")
    video_id = store.create_video("a" * 64, "lesson.mp4", "lesson.mp4", "audio.wav", 20_000)
    rows = [
        {
            "start_ms": 0, "end_ms": 6000, "representative_ms": 5000,
            "image_path": "early.jpg", "image_hash": "early", "duration_ms": 6000,
            "retention_class": "animation", "confidence": 0.65, "roi_json": "{}",
            "extraction_version": "test", "is_llm_preferred": False,
        },
        {
            "start_ms": 6000, "end_ms": 20000, "representative_ms": 19000,
            "image_path": "complete.jpg", "image_hash": "complete", "duration_ms": 14000,
            "retention_class": "primary", "confidence": 0.9, "roi_json": "{}",
            "extraction_version": "test", "is_llm_preferred": True,
        },
    ]
    store.replace_video_slides(video_id, rows)
    monkeypatch.setattr(api_main.asr_storage, "slide_directory", lambda *args: root)
    with TestClient(create_app(store)) as client:
        yield client, store, root, video_id


def test_pdf_defaults_to_preferred_and_can_export_full_timeline(export_data):
    client, _, root, video_id = export_data
    response = client.get(f"/api/videos/{video_id}/slides/export")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["cache-control"] == "no-store"
    assert "filename*=UTF-8''" in response.headers["content-disposition"]
    pdf = PdfReader(BytesIO(response.content))
    assert len(pdf.pages) == 1
    page = pdf.pages[0]
    assert "00:00:06 - 00:00:20" in page.extract_text()
    assert "@ 00:00:19" in page.extract_text()
    assert float(page.mediabox.width) == 960
    assert float(page.mediabox.height) == 748
    resource = next(iter(page["/Resources"]["/XObject"].values())).get_object()
    assert "/DCTDecode" in resource["/Filter"]
    assert resource.get_data() == (root / "complete.jpg").read_bytes()

    detailed = client.get(f"/api/videos/{video_id}/slides/export?mode=timeline_states")
    pages = PdfReader(BytesIO(detailed.content)).pages
    assert len(pages) == 2
    assert "00:00:00 - 00:00:06" in pages[0].extract_text()
    assert float(pages[0].mediabox.height) == 568
    assert "00:00:06 - 00:00:20" in pages[1].extract_text()


def test_export_rejects_missing_invalid_or_foreign_resources(export_data, tmp_path):
    client, store, root, video_id = export_data
    assert client.get(f"/api/videos/{video_id}/slides/export?mode=invalid").status_code == 422
    assert client.get("/api/videos/99999/slides/export").status_code == 404
    empty_id = store.create_video("b" * 64, "empty.mp4", "empty.mp4", "audio.wav", 1000)
    assert client.get(f"/api/videos/{empty_id}/slides/export").status_code == 409
    with store.connect() as connection:
        connection.execute("UPDATE video_slides SET is_llm_preferred = 0 WHERE video_id = ?", (video_id,))
    assert client.get(f"/api/videos/{video_id}/slides/export").status_code == 409
    assert client.get(f"/api/videos/{video_id}/slides/export?mode=timeline_states").status_code == 200
    with store.connect() as connection:
        connection.execute("UPDATE video_slides SET is_llm_preferred = 1 WHERE video_id = ?", (video_id,))
    (root / "early.jpg").unlink()
    assert client.get(f"/api/videos/{video_id}/slides/export").status_code == 409
    outside = tmp_path / "outside.jpg"
    Image.new("RGB", (20, 20)).save(outside)
    with store.connect() as connection:
        connection.execute(
            "UPDATE video_slides SET image_path = ? WHERE video_id = ?", (str(outside), video_id),
        )
    assert client.get(f"/api/videos/{video_id}/slides/export").status_code == 409
    with store.connect() as connection:
        connection.execute("UPDATE video_slides SET image_path = '../outside.jpg' WHERE video_id = ?", (video_id,))
    assert client.get(f"/api/videos/{video_id}/slides/export").status_code == 409


def test_export_retains_repeated_time_instances_and_rejects_corrupt_images(tmp_path):
    path = tmp_path / "repeat.jpg"
    Image.new("RGB", (320, 180), "green").save(path)
    slides = [
        dict(image_path=path.name, start_ms=0, end_ms=5000, representative_ms=4000),
        dict(image_path=path.name, start_ms=8000, end_ms=10000, representative_ms=9000),
    ]
    output = BytesIO()
    write_slide_pdf(slides, tmp_path, output, title="Synthetic lesson")
    pages = PdfReader(BytesIO(output.getvalue())).pages
    assert len(pages) == 2
    assert "00:00:08 - 00:00:10" in pages[1].extract_text()
    path.write_bytes(b"not an image")
    with pytest.raises(OSError):
        write_slide_pdf(slides, tmp_path, BytesIO(), title="Synthetic lesson")
    with pytest.raises(ValueError):
        write_slide_pdf([], tmp_path, BytesIO(), title="Synthetic lesson")
