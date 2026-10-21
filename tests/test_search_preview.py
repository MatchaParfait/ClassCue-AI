from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from classcue.video_store import VideoStore
from classcue.video_thumbnail import video_preview_frame
from tests.test_video_thumbnail import make_video, decoded_image, np


def test_preview_seeks_to_requested_frame_and_shares_cache(tmp_path, monkeypatch):
    import classcue.video_thumbnail as thumbnails

    source = tmp_path / "lesson.mkv"
    make_video(source)
    before = source.read_bytes(), source.stat().st_mtime_ns
    encode = Mock(wraps=thumbnails._encode_preview)
    monkeypatch.setattr(thumbnails, "_encode_preview", encode)
    with ThreadPoolExecutor(max_workers=4) as pool:
        previews = list(pool.map(lambda _: video_preview_frame(source, tmp_path / "cache", 920), range(4)))
    assert len(set(previews)) == 1
    assert encode.call_count == 1
    np.testing.assert_allclose(decoded_image(previews[0].read_bytes())[90, 160], [200, 30, 50], atol=3)
    other = video_preview_frame(source, tmp_path / "cache", 400)
    assert other != previews[0]
    np.testing.assert_allclose(decoded_image(other.read_bytes())[90, 160], [30, 180, 50], atol=3)
    assert (source.read_bytes(), source.stat().st_mtime_ns) == before
    assert not list((tmp_path / "cache").glob("*.tmp"))


def test_preview_keeps_aspect_ratio_limits_size_and_invalidates_changed_source(tmp_path):
    source = tmp_path / "large.mkv"
    make_video(source, width=1920, height=1080, frames=24)
    previous = video_preview_frame(source, tmp_path / "cache", 920)
    assert decoded_image(previous.read_bytes()).shape == (540, 960, 3)
    make_video(source, width=1920, height=1080, frames=24, target_color=(20, 60, 220))
    current = video_preview_frame(source, tmp_path / "cache", 920)
    assert current != previous
    np.testing.assert_allclose(decoded_image(current.read_bytes())[90, 160], [20, 60, 220], atol=3)


def test_endpoint_validates_workspace_time_and_reuses_configured_cache(tmp_path, monkeypatch):
    import api.main as api_main
    import classcue.asr_storage as asr_storage

    project, workspace, cache = tmp_path / "application", tmp_path / "workspace", tmp_path / "cache"
    monkeypatch.setattr(api_main, "DIRECTORY_PROJECT_ROOT", project)
    asr_storage.save_directories(project, workspace, asset_root=str(tmp_path / "assets"),
                                audio_cache_dir=str(cache), log_dir=str(tmp_path / "logs"))
    source = tmp_path / "lesson.mkv"
    make_video(source)
    store = VideoStore(workspace / "project.db")
    video_id = store.create_video("hash", source.name, str(source), "lesson.wav", 1200)
    with TestClient(create_app(store)) as client:
        scope = client.get("/api/library/status").json()["library_id"]
        params = {"library_id": scope, "time_ms": 920}
        response = client.get(f"/api/videos/{video_id}/preview-frame", params=params)
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"
        assert response.headers["cache-control"] == "private, no-cache"
        np.testing.assert_allclose(decoded_image(response.content)[90, 160], [200, 30, 50], atol=3)
        assert client.get(f"/api/videos/{video_id}/preview-frame", params=params).content == response.content
        assert client.get(f"/api/videos/{video_id}/preview-frame", params={**params, "library_id": "other"}).status_code == 409
        assert client.get(f"/api/videos/{video_id}/preview-frame", params={**params, "time_ms": -1}).status_code == 422
        assert client.get(f"/api/videos/{video_id}/preview-frame", params={**params, "time_ms": "bad"}).status_code == 422
        assert client.get(f"/api/videos/{video_id}/preview-frame").status_code == 422
        assert client.get("/api/videos/999/preview-frame", params=params).status_code == 404
        last = client.get(f"/api/videos/{video_id}/preview-frame", params={**params, "time_ms": 999999})
        assert last.status_code == 200
        np.testing.assert_allclose(decoded_image(last.content)[90, 160], [30, 180, 50], atol=3)
        source.unlink()
        assert client.get(f"/api/videos/{video_id}/preview-frame", params=params).status_code == 404
    assert len(list((cache / "search-previews").glob("*.jpg"))) == 2


@pytest.mark.parametrize("kind", ["corrupt", "audio_only"])
def test_preview_failure_is_a_non_mutating_404(tmp_path, monkeypatch, kind):
    import api.main as api_main
    import wave

    monkeypatch.setattr(api_main, "DIRECTORY_PROJECT_ROOT", tmp_path / "application")
    source = tmp_path / "source.wav"
    if kind == "audio_only":
        with wave.open(str(source), "wb") as audio:
            audio.setparams((1, 2, 16000, 0, "NONE", "")); audio.writeframes(b"\0\0" * 16000)
    else:
        source.write_bytes(b"broken")
    store = VideoStore(tmp_path / "video.db")
    video_id = store.create_video("hash", source.name, str(source), "audio.wav", 1000)
    with TestClient(create_app(store)) as client:
        scope = client.get("/api/library/status").json()["library_id"]
        response = client.get(f"/api/videos/{video_id}/preview-frame", params={"library_id": scope, "time_ms": 0})
        assert response.status_code == 404
        assert store.get_video(video_id)["status"] == "uploaded"
    assert not list(tmp_path.rglob("*.jpg"))
