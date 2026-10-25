"""Synthetic frame storage and browser boundaries, without Bilibili traffic."""

import io
from types import SimpleNamespace

import pytest
from PIL import Image

from classcue.bilibili import BilibiliError
from classcue.bilibili_frames import EdgeFrameSession as BrowserFrameReader, FrameStore, browser_url_allowed
from classcue.bilibili_store import BilibiliStore
from classcue.core import TranscriptSegment
from classcue.video_store import VideoStore
from tests.bilibili_visual_fakes import synthetic_frame


BV = "BV1xx411c7mD"


@pytest.fixture
def frames(tmp_path, monkeypatch):
    monkeypatch.setattr("classcue.bilibili_frames.library_scope", lambda _project, database: database.parent / "cache")
    store = VideoStore(tmp_path / "library.db")
    video = store.create_video("synthetic", "lesson.mp4", "lesson.mp4", "lesson.wav", 10000)
    store.mark_transcribed(video, [TranscriptSegment(1, 0, 5000, "合成课堂材料")])
    marker = store.add_marker(video, 0, 5000, "合成考点", "", "", "重点", .8)
    repo = BilibiliStore(store)
    run = repo.create_run(repo.snapshot(marker), {"model": "synthetic"})
    unit = {"id": BV + ":123", "bvid": BV, "cid": 123, "page": 1, "duration_ms": 10000}
    run["candidates"] = [{"units": [unit]}]
    repo.save_run(run)
    return FrameStore(store), run, unit, marker


def test_frame_is_run_owned_persistent_and_hash_checked(frames):
    store, run, unit, marker = frames
    record = store.add(run, unit, 2000, 2050, synthetic_frame(), "decoded_frame")
    assert store.get(marker, run["id"], record["id"]) == record
    assert FrameStore(VideoStore(store.store.path)).image(record).sha256 == record["image_hash"]
    assert store.add(run, unit, 2000, 2050, synthetic_frame(), "decoded_frame")["id"] == record["id"]
    with pytest.raises(KeyError):
        store.get(marker + 1, run["id"], record["id"])
    with pytest.raises(KeyError):
        store.get(marker, "another-run", record["id"])
    with pytest.raises(ValueError, match="不一致"):
        store.add(run, {**unit, "cid": 999}, 1000, 1000, synthetic_frame(), "decoded_frame")
    store.path(record["id"]).write_bytes(synthetic_frame(77))
    with pytest.raises(ValueError, match="已变化"):
        store.image(record)


@pytest.mark.parametrize("requested,captured,clock", [
    (-1, 0, "decoded_frame"), (1000, 4000, "decoded_frame"), (1000, 10000, "decoded_frame"),
    (1000, 1000, "model_time"), (1.5, 1000, "decoded_frame"),
])
def test_invalid_or_model_generated_timestamps_are_rejected(frames, requested, captured, clock):
    store, run, unit, _ = frames
    with pytest.raises(ValueError):
        store.add(run, unit, requested, captured, synthetic_frame(), clock)


@pytest.mark.parametrize("size,color", [((320, 180), "red"), ((960, 540), "black"), ((960, 540), "white")])
def test_blank_or_too_small_frames_are_not_evidence(frames, size, color):
    store, run, unit, _ = frames
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    with pytest.raises(ValueError):
        store.add(run, unit, 1000, 1000, output.getvalue(), "decoded_frame")


def test_cascading_deletion_queues_and_removes_image_files(frames):
    store, run, unit, marker = frames
    record = store.add(run, unit, 1000, 1000, synthetic_frame(), "decoded_frame")
    path = store.path(record["id"])
    with store.store.connect() as connection:
        connection.execute("DELETE FROM video_markers WHERE id=?", (marker,))
        assert connection.execute("SELECT count(*) FROM bili_frames").fetchone()[0] == 0
    assert path.exists()
    store.collect()
    assert not path.exists()
    for value in ["../not-a-frame", "a" * 33, ""]:
        with pytest.raises(ValueError):
            store.path(value)


@pytest.mark.parametrize("url,allowed", [
    ("https://www.bilibili.com/video/" + BV, True),
    ("https://xy.bilivideo.com/media.m4s", True),
    ("https://i0.hdslb.com/asset.js", True),
    ("http://www.bilibili.com/", False), ("https://bilibili.com.attacker.test/", False),
    ("https://www.bilibili.com@attacker.test/", False), ("file:///private", False),
    ("https://localhost/video", False), ("https://www.bilibili.com:444/", False),
])
def test_browser_url_boundary(url, allowed):
    assert browser_url_allowed(url) is allowed


class Route:
    def __init__(self, url, method="GET"):
        self.request = SimpleNamespace(url=url, method=method, headers={"cookie": "synthetic-session"})
        self.action, self.headers = None, None

    def abort(self):
        self.action = "abort"

    def continue_(self, *, headers):
        self.action, self.headers = "continue", headers


def test_browser_blocks_writes_and_does_not_forward_cookie_to_cdn(frames):
    store, *_ = frames
    reader = BrowserFrameReader(BilibiliStore(store.store))
    cdn = Route("https://xy.bilivideo.com/media.m4s")
    reader.route(cdn)
    assert cdn.action == "continue" and "cookie" not in cdn.headers
    api = Route("https://api.bilibili.com/x/player/wbi/v2")
    reader.route(api)
    assert api.headers["cookie"] == "synthetic-session"
    for route in [Route("https://api.bilibili.com/action", "POST"), Route("https://attacker.test/")]:
        reader.route(route)
        assert route.action == "abort"


def test_browser_risk_and_streaming_budgets_propagate_without_credentials(frames):
    store, *_ = frames
    repo = BilibiliStore(store.store)
    reader = BrowserFrameReader(repo)
    reader.response(SimpleNamespace(status=412, url="https://api.bilibili.com/x/player/wbi/v2", headers={}))
    assert repo.control("platform_pause")["reason"] == "browser_http_412"
    with pytest.raises(BilibiliError, match="风控"):
        reader.checkpoint()


def test_websocket_sink_never_connects_forwards_or_waits_for_close(frames):
    store, *_ = frames
    reader = BrowserFrameReader(BilibiliStore(store.store))
    handlers = []

    class Socket:
        def on_message(self, handler):
            handlers.append(handler)

        def close(self):
            raise AssertionError("Do not synchronously close from a route callback")

        def connect_to_server(self):
            raise AssertionError("No WebSocket server connection is allowed")

        def send(self, _):
            raise AssertionError("Do not forward or fabricate WebSocket messages")

    reader.block_web_socket(Socket())
    assert len(handlers) == 1
    assert handlers[0]("synthetic-message") is None
