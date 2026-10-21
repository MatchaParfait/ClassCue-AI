from concurrent.futures import ThreadPoolExecutor
import hashlib
from io import BytesIO
import json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

av = pytest.importorskip("av")
np = pytest.importorskip("numpy")

from api.main import create_app
from classcue.video_store import VideoStore
from classcue.video_thumbnail import ThumbnailUnavailable, cached_video_thumbnail, video_thumbnail


def make_video(path, frames=30, target_color=(200, 30, 50), width=640, height=360, target_pixels=None):
    with av.open(str(path), "w") as container:
        stream = container.add_stream("ffv1", rate=25)
        stream.width, stream.height, stream.pix_fmt = width, height, "bgr0"
        for index in range(frames):
            color = target_color if index == 23 else (30, 180, 50)
            pixels = np.empty((height, width, 3), dtype=np.uint8)
            pixels[:] = color
            if index == 23 and target_pixels is not None:
                pixels = target_pixels
            for packet in stream.encode(av.VideoFrame.from_ndarray(pixels, format="rgb24")):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)


def decoded_image(data):
    with av.open(BytesIO(data)) as image:
        return next(image.decode(video=0)).to_ndarray(format="rgb24")


def test_exact_frame_24_native_and_cached_without_touching_source(tmp_path, monkeypatch):
    import classcue.video_thumbnail as thumbnails

    source = tmp_path / "source.mkv"
    make_video(source)
    before = source.read_bytes(), source.stat().st_mtime_ns
    encode = Mock(wraps=thumbnails._encode_frame)
    monkeypatch.setattr(thumbnails, "_encode_frame", encode)
    with ThreadPoolExecutor(max_workers=4) as pool:
        outputs = list(pool.map(lambda _: video_thumbnail(source, tmp_path / "cache"), range(4)))
    assert len(set(outputs)) == 1
    assert encode.call_count == 1
    pixels = decoded_image(outputs[0].read_bytes())
    assert pixels.shape == (360, 640, 3)
    np.testing.assert_allclose(pixels[90, 160], [200, 30, 50], atol=3)
    assert video_thumbnail(source, tmp_path / "cache") == outputs[0]
    assert encode.call_count == 1
    assert (source.read_bytes(), source.stat().st_mtime_ns) == before
    assert not list((tmp_path / "cache").glob("*.tmp"))


def test_cache_only_lookup_never_decodes_or_creates_missing_cache(tmp_path, monkeypatch):
    import classcue.video_thumbnail as thumbnails
    source = tmp_path / "source.mkv"
    make_video(source)
    cache = tmp_path / "cache"
    assert cached_video_thumbnail(source, cache) is None
    assert not cache.exists()
    existing = video_thumbnail(source, cache)
    monkeypatch.setattr(thumbnails, "_encode_frame", Mock(side_effect=AssertionError("must not decode")))
    assert cached_video_thumbnail(source, cache) == existing
    monkeypatch.setattr(thumbnails, "THUMBNAIL_VERSION", "future")
    assert cached_video_thumbnail(source, cache) is None
    thumbnails._encode_frame.assert_not_called()


def test_source_change_invalidates_cache_and_rename_still_works(tmp_path):
    source = tmp_path / "source.mkv"
    make_video(source)
    old = video_thumbnail(source, tmp_path / "cache")
    make_video(source, target_color=(20, 60, 220))
    changed = video_thumbnail(source, tmp_path / "cache")
    assert old != changed
    np.testing.assert_allclose(decoded_image(changed.read_bytes())[90, 160], [20, 60, 220], atol=3)
    renamed = source.rename(tmp_path / "renamed.mkv")
    np.testing.assert_array_equal(decoded_image(video_thumbnail(renamed, tmp_path / "cache").read_bytes()),
                                  decoded_image(changed.read_bytes()))


@pytest.mark.parametrize("frames", [1, 23])
def test_short_video_does_not_use_a_different_frame(tmp_path, frames):
    source = tmp_path / "short.mkv"
    make_video(source, frames=frames)
    with pytest.raises(ThumbnailUnavailable, match="24"):
        video_thumbnail(source, tmp_path / "cache")
    assert not list((tmp_path / "cache").glob("*.jpg"))
    assert not list((tmp_path / "cache").glob("*.tmp"))


@pytest.mark.parametrize("width,height", [(1920, 1080), (180, 360), (641, 359), (160, 90)])
def test_native_resolution_without_resizing_or_cropping(tmp_path, width, height):
    source = tmp_path / "native.mkv"
    make_video(source, frames=24, width=width, height=height)
    pixels = decoded_image(video_thumbnail(source, tmp_path / "cache").read_bytes())
    assert pixels.shape == (height, width, 3)


def test_preserves_single_pixel_color_detail(tmp_path):
    source = tmp_path / "detail.mkv"
    original = np.zeros((360, 640, 3), dtype=np.uint8)
    original[:, ::2] = (220, 40, 40)
    original[:, 1::2] = (40, 40, 220)
    make_video(source, frames=24, target_pixels=original)
    pixels = decoded_image(video_thumbnail(source, tmp_path / "cache").read_bytes())
    assert np.abs(pixels.astype(float) - original).mean() < 4


def test_legacy_thumbnail_is_not_reused_and_version_changes_invalidate_cache(tmp_path, monkeypatch):
    import classcue.video_thumbnail as thumbnails

    source = tmp_path / "source.mkv"
    make_video(source)
    cache = tmp_path / "cache"
    cache.mkdir()
    old_key = hashlib.sha256(json.dumps([24, *thumbnails._source_identity(source)]).encode("utf-8")).hexdigest()
    old = cache / f"{old_key}.jpg"
    old.write_bytes(b"legacy low-resolution cache")
    current = video_thumbnail(source, cache)
    assert current != old
    assert decoded_image(current.read_bytes()).shape == (360, 640, 3)
    assert old.read_bytes() == b"legacy low-resolution cache"
    monkeypatch.setattr(thumbnails, "THUMBNAIL_VERSION", "future-version")
    assert video_thumbnail(source, cache) != current


def test_endpoint_uses_configured_cache_and_survives_new_app(tmp_path, monkeypatch):
    import api.main as api_main
    import classcue.asr_storage as asr_storage

    project = tmp_path / "application"
    workspace = tmp_path / "workspace"
    cache = tmp_path / "cache"
    source = tmp_path / "video.mkv"
    make_video(source)
    monkeypatch.setattr(api_main, "DIRECTORY_PROJECT_ROOT", project)
    asr_storage.save_directories(project, workspace, asset_root=str(tmp_path / "assets"),
                                audio_cache_dir=str(cache), log_dir=str(tmp_path / "logs"))
    store = VideoStore(workspace / "project.db")
    video_id = store.create_video("hash", source.name, str(source), "video.wav", 1200)
    with TestClient(create_app(store)) as client:
        response = client.get(f"/api/videos/{video_id}/thumbnail")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"
        assert response.headers["cache-control"] == "private, no-cache"
        assert decoded_image(response.content).shape == (360, 640, 3)
        np.testing.assert_allclose(decoded_image(response.content)[90, 160], [200, 30, 50], atol=3)
        assert client.get("/api/videos/999/thumbnail").status_code == 404
    assert len(list((cache / "thumbnails").glob("*.jpg"))) == 1
    assert not (workspace / "temporary_audio").exists()
    with TestClient(create_app(VideoStore(store.path))) as client:
        assert client.get(f"/api/videos/{video_id}/thumbnail").content == response.content
        renamed = source.rename(tmp_path / "renamed.mkv")
        with store.connect() as connection:
            connection.execute("UPDATE videos SET video_path = ? WHERE id = ?", (str(renamed), video_id))
        assert client.get(f"/api/videos/{video_id}/thumbnail").status_code == 200
        renamed.unlink()
        assert client.get(f"/api/videos/{video_id}/thumbnail").status_code == 404


@pytest.mark.parametrize("kind", ["corrupt", "short", "audio_only"])
def test_endpoint_falls_back_for_unreadable_sources(tmp_path, monkeypatch, kind):
    import api.main as api_main

    monkeypatch.setattr(api_main, "DIRECTORY_PROJECT_ROOT", tmp_path / "application")
    source = tmp_path / "source.mkv"
    if kind == "short":
        make_video(source, frames=23)
    elif kind == "audio_only":
        source = tmp_path / "audio.wav"
        import wave
        with wave.open(str(source), "wb") as audio:
            audio.setparams((1, 2, 16000, 0, "NONE", ""))
            audio.writeframes(b"\x00\x00" * 16000)
    else:
        source.write_bytes(b"not video")
    store = VideoStore(tmp_path / "project.db")
    video_id = store.create_video("hash", source.name, str(source), "video.wav", 1000)
    with TestClient(create_app(store)) as client:
        assert client.get(f"/api/videos/{video_id}/thumbnail").status_code == 404
    assert not list(tmp_path.rglob("*.jpg"))
