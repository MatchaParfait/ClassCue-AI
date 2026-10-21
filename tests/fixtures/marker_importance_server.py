"""Disposable review data for importance label verification."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path.cwd()))

import api.main as api_main
from classcue.core import TranscriptSegment
import uvicorn
from classcue.video_store import VideoStore


root = Path(sys.argv[1])
store = VideoStore(root / "workspace" / "project.db")
if not store.list_videos():
    source = root / "workspace" / "videos" / "lesson.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"disposable video fixture")
    video_id = store.create_video("fixture", source.name, str(source), "lesson.wav", 600_000)
    store.mark_transcribed(video_id, [TranscriptSegment(1, 0, 1000, "请掌握这些知识点")])
    for index, title in enumerate(["分治算法中 k、m、n 和 fn 的含义", "递归式的含义", "等比数列求和公式"]):
        marker_id = store.add_marker(
            video_id, 70_000 + index * 100_000, 90_000 + index * 100_000,
            title, "教师明确要求理解这些符号的含义，并用于分析时间复杂度。",
            "理解这个知识点，有助于分析算法的思路和时间复杂度。", "AI-test", .9, "medium_recall",
        )
        if index == 0:
            store.set_marker_jump_time(marker_id, 60_000)

application = api_main.create_app(store)


@application.get("/__qa__/state")
def state():
    return [dict(row) for row in store.list_markers(1)]


uvicorn.run(application, host="127.0.0.1", port=int(sys.argv[2]), log_level="warning")
