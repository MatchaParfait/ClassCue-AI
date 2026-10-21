"""Disposable media and database for PC workbench interaction tests."""

from pathlib import Path
from fractions import Fraction
import shutil
import os
import sys

sys.path.insert(0, str(Path.cwd()))

import av
import numpy as np
import uvicorn
from api.main import create_app
from core import TranscriptSegment
from video_store import VideoStore


root = Path(sys.argv[1])
workspace = root / "workspace"
store = VideoStore(workspace / "project.db")
source = workspace / "lesson.mp4"
if not source.exists():
    with av.open(str(source), "w") as container:
        stream = container.add_stream("libx264", rate=25)
        stream.width, stream.height, stream.pix_fmt = 640, 360, "yuv420p"
        audio = container.add_stream("aac", rate=48_000)
        audio.layout = "stereo"
        for number in range(250):
            pixels = np.full((360, 640, 3), (228, 235, 243), dtype=np.uint8)
            pixels[36:324, 44:596] = (47, 76, 93)
            for line in range(6):
                pixels[65 + line * 38:70 + line * 38, 80:440 - line * 24] = (220, 233, 226)
            pixels[280:300, 470:560] = (65 + number % 100, 160, 105)
            for packet in stream.encode(av.VideoFrame.from_ndarray(pixels, format="rgb24")):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
        for offset in range(0, 480_000, 4096):
            times = np.arange(offset, min(offset + 4096, 480_000)) / 48_000
            samples = np.stack([.2 * np.sin(2 * np.pi * 440 * times), np.zeros_like(times)]).astype(np.float32)
            frame = av.AudioFrame.from_ndarray(samples, format="fltp", layout="stereo")
            frame.sample_rate, frame.pts, frame.time_base = 48_000, offset, Fraction(1, 48_000)
            for packet in audio.encode(frame):
                container.mux(packet)
        for packet in audio.encode():
            container.mux(packet)

if not store.list_videos():
    folder = store.create_folder("算法课程")
    for index in range(24):
        name = ["分治算法与时间复杂度", "ff8081819fef905101a0adaa76d80768", "递归与递推关系", "等比数列求和公式"][index % 4]
        if index == 10:
            name = "算法设计与分析_第十二讲_分治算法的递归式及复杂度证明_2026_Classroom_Lecture_Recording_完整版"
        path = workspace / "videos" / f"{name}-{index + 1}.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, path)
        with path.open("ab") as handle:
            handle.write(f"fixture-{index}".encode("ascii"))
        stat = path.stat()
        video_id = store.create_video(store.file_hash(path), path.name, str(path), "lesson.wav", 10_000,
                                      stat.st_size, stat.st_mtime_ns)
        if index >= 12:
            store.set_video_folder(video_id, folder)
        if index in (0, 5, 12):
            segments = [TranscriptSegment(1, 0, 10_000, "请掌握分治算法和时间复杂度。")]
            if os.environ.get("SEARCH_QA"):
                segments = [TranscriptSegment(number + 1, number * 300, number * 300 + 300,
                    f"第{number + 1}处，请掌握分治算法和时间复杂度。") for number in range(25)]
            store.mark_transcribed(video_id, segments)
            for point in range(3):
                store.add_marker(
                    video_id, 1_000 + point * 2_000, 2_000 + point * 2_000,
                    ["分治算法的基本思路", "递归式中的参数", "时间复杂度分析"][point],
                    "教师明确强调理解这个知识点。", "掌握这些符号的含义，理解分解、递归求解及合并三个步骤，并用递归式分析时间复杂度。" * 4, "AI-test", .9, "medium_recall",
                )
            store.set_status(video_id, "review_ready")
        elif index == 2:
            store.set_status(video_id, "failed")
    for name in ("pure", "mixed"):
        directory = root / name
        directory.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, directory / "import.mp4")
    (root / "mixed" / "notes.txt").write_text("disposable non-video fixture", encoding="ascii")
    shutil.copyfile(source, root / "import.mp4")

application = create_app(store)
uvicorn.run(application, host="127.0.0.1", port=int(sys.argv[2]), log_level="warning")
