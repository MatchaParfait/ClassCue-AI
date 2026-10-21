"""Disposable real video frames for desktop thumbnail verification."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path.cwd()))

import av
import numpy as np
import uvicorn
from api.main import create_app
from classcue.video_store import VideoStore


root = Path(sys.argv[1]) / "workspace"
store = VideoStore(root / "project.db")
if not store.list_videos():
    for index, dimensions in enumerate([(640, 360), (180, 360)]):
        width, height = dimensions
        source = root / f"ff8081819fef905101a0adaa76d80768-{index + 1}.mkv"
        with av.open(str(source), "w") as container:
            stream = container.add_stream("ffv1", rate=25)
            stream.width, stream.height, stream.pix_fmt = width, height, "bgr0"
            for number in range(30):
                pixels = np.empty((height, width, 3), dtype=np.uint8)
                pixels[:] = (200, 30, 50) if number == 23 else (30, 180, 50)
                pixels[:height // 4, :width // 4] = (235, 220, 100)
                for packet in stream.encode(av.VideoFrame.from_ndarray(pixels, format="rgb24")):
                    container.mux(packet)
            for packet in stream.encode():
                container.mux(packet)
        video_id = store.create_video(f"fixture-{index}", source.name, str(source), "lesson.wav", 1200)
        if index == 0:
            store.set_status(video_id, "review_ready")
    bad_source = root / "unreadable.mp4"
    bad_source.write_bytes(b"disposable corrupt fixture")
    store.create_video("corrupt", bad_source.name, str(bad_source), "lesson.wav", 1200)

uvicorn.run(create_app(store), host="127.0.0.1", port=int(sys.argv[2]), log_level="warning")
