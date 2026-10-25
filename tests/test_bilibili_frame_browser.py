"""Exercise the real Edge seek/capture code using intercepted synthetic media only."""

import importlib.util
import io
import re
import socketserver
import threading

import pytest
from PIL import Image

from classcue.bilibili import BilibiliError
from classcue.bilibili_frames import EdgeFrameSession as BrowserFrameReader
from tests.bilibili_visual_fakes import synthetic_frame


class Repo:
    def control(self, *_):
        return None

    def set_control(self, *_):
        raise AssertionError("Synthetic playback must not trigger risk controls")


@pytest.fixture
def websocket_sink():
    connections = []

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            connections.append(True)

    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_address[1], connections
        finally:
            server.shutdown()
            thread.join(timeout=1)


@pytest.mark.parametrize("with_websocket", [False, True])
def test_real_edge_captures_a_seeked_synthetic_frame_without_external_requests(tmp_path, websocket_sink,
                                                                             with_websocket):
    if importlib.util.find_spec("playwright") is None:
        pytest.skip("Playwright is not installed")
    import av
    import numpy as np

    path = tmp_path / "synthetic.mp4"
    with av.open(str(path), "w") as output:
        stream = output.add_stream("libx264", rate=10)
        stream.width, stream.height, stream.pix_fmt = 960, 540, "yuv420p"
        for index in range(40):
            with Image.open(io.BytesIO(synthetic_frame(index * 100))) as image:
                frame = av.VideoFrame.from_ndarray(np.asarray(image.convert("RGB")), format="rgb24")
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)
    media = path.read_bytes()
    html = """<!doctype html><html><head><style>body{margin:0}.bpx-player-video-wrap{width:960px}
    video{width:960px;height:540px}</style></head><body>
    <div class="bpx-player-video-wrap"><video muted preload="auto" src="/synthetic.mp4"></video></div>
    <script>window.__INITIAL_STATE__={cid:123,videoData:{bvid:"BV1xx411c7mD",cid:123,pages:[{cid:123}]}};</script>
    </body></html>"""
    port, connections = websocket_sink
    if with_websocket:
        html = html.replace("</body>", f"""<script>
        window.socketMessages=0;
        const socket=new WebSocket("wss://127.0.0.1:{port}/synthetic");
        socket.onopen=()=>{{socket.send("synthetic");window.socketOpened=true;}};
        socket.onmessage=()=>window.socketMessages++;
        </script></body>""")
    with BrowserFrameReader(Repo()) as reader:
        try:
            reader.start()
        except BilibiliError as error:
            pytest.skip(str(error))
        calls = []

        def intercept(route):
            calls.append(route.request.url)
            if "/video/BV1xx411c7mD/" in route.request.url:
                route.fulfill(status=200, content_type="text/html", body=html)
            elif route.request.url == "https://www.bilibili.com/synthetic.mp4":
                requested = route.request.headers.get("range", "")
                match = re.fullmatch(r"bytes=(\d+)-(\d*)", requested)
                if match:
                    start, end = int(match[1]), min(int(match[2]) if match[2] else len(media) - 1, len(media) - 1)
                    route.fulfill(status=206, content_type="video/mp4", body=media[start:end + 1],
                                  headers={"Content-Range": f"bytes {start}-{end}/{len(media)}", "Accept-Ranges": "bytes"})
                else:
                    route.fulfill(status=200, content_type="video/mp4", body=media, headers={"Accept-Ranges": "bytes"})
            else:
                route.abort()

        reader.context.route("**/*", intercept)
        unit = {"id": "synthetic-unit", "bvid": "BV1xx411c7mD", "cid": 123, "page": 1}
        result = reader.capture(unit, 2000)
        assert abs(result["captured_ms"] - 2000) <= 150
        assert result["time_source"] in {"decoded_frame", "player_current_time"}
        with Image.open(io.BytesIO(result["data"])) as image:
            assert image.width >= 960 and image.height >= 540
        assert calls and all(url.startswith("https://www.bilibili.com/") for url in calls)
        if with_websocket:
            reader.page.wait_for_function("window.socketOpened === true", timeout=2000)
            assert reader.page.evaluate("window.socketMessages") == 0
            assert not connections
