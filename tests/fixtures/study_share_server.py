"""Isolated sender/receiver fixtures for sharing browser tests and local preview."""

from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import av
import numpy as np
import uvicorn

import api.main as api_main
from classcue.core import TranscriptSegment
from classcue.study_share import export_package
from classcue.video_store import VideoStore


root = Path(sys.argv[1]).resolve()
root.mkdir(parents=True, exist_ok=True)
api_main.DIRECTORY_PROJECT_ROOT = root / "application"
source = root / "synthetic.mp4"
if not source.exists():
    with av.open(str(source), "w") as container:
        stream = container.add_stream("libx264", rate=15)
        stream.width, stream.height, stream.pix_fmt = 640, 360, "yuv420p"
        for number in range(120):
            pixels = np.full((360, 640, 3), (32, 64, 49), dtype=np.uint8)
            for line in range(5):
                pixels[64 + line * 44:70 + line * 44, 64:520 - line * 35] = (224, 234, 213)
            pixels[296:308, 64:64 + number * 4] = (248, 217, 74)
            for packet in stream.encode(av.VideoFrame.from_ndarray(pixels, format="rgb24")):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)


def register(store, path):
    stat = path.stat()
    return store.create_video(store.file_hash(path), path.name, str(path), "unused.wav", 8000,
                              stat.st_size, stat.st_mtime_ns)


sender = VideoStore(root / "sender.sqlite")
if not sender.list_videos():
    sid = register(sender, source)
    sender.mark_transcribed(sid, [
        TranscriptSegment(1, 0, 2500, "分治算法的基本步骤是分解、递归求解和合并。"),
        TranscriptSegment(2, 2500, 5000, "考试要掌握递归式和时间复杂度分析。"),
        TranscriptSegment(3, 5000, 8000, "归并排序的时间复杂度是 n log n。"),
    ], "fixture-only")
    sender.set_analysis_run(sid, "medium_recall", "completed", 2)
    sender.set_analysis_run(sid, "low_recall", "completed", 0)
    mid = sender.add_marker(sid, 0, 2500, "分治算法的三个步骤", "分解、递归求解、合并。",
                           "分治算法的基本步骤是分解、递归求解和合并。", "重点", 1., "medium_recall")
    sender.set_marker_importance(mid, "critical")
    sender.set_marker_jump_time(mid, 400)
    sender.add_marker(sid, 2500, 5000, "递归式与时间复杂度", "掌握归并排序的复杂度。",
                      "考试要掌握递归式和时间复杂度分析。", "考试", 1., "medium_recall")
    scheme = sender.save_recall_scheme({"name": "老师的自定义方案", "base_mode": "high_recall", "rules": [], "notes": ""})
    sender.set_analysis_run(sid, scheme["id"], "completed", 0)
    sender.update_video_display_name(sid, "分享者的算法课")
    sender.set_status(sid, "review_ready")
(root / "lesson.studyshare").write_bytes(export_package(sender, sender.list_videos()[0]["id"]))

receiver = VideoStore(root / "receiver.sqlite")
if not receiver.list_videos():
    local = root / "本地算法课_尚未处理.mp4"
    shutil.copyfile(source, local)
    register(receiver, local)
    for index in range(1, 4):
        other = root / f"其他课程_{index}.mp4"
        other.write_bytes(source.read_bytes() + f"fixture-{index}".encode())
        register(receiver, other)


def no_processing(*args, **kwargs):
    raise AssertionError("ASR and AI are forbidden in the sharing fixture")


api_main.process_video_once = no_processing
api_main.process_saved_analysis_once = no_processing
api_main.prepare_video_for_analysis = no_processing
application = api_main.create_app(receiver)
uvicorn.run(application, host="127.0.0.1", port=int(sys.argv[2]), log_level="warning")
