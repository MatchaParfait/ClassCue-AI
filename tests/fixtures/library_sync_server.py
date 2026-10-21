"""Disposable library state transitions for desktop polling verification."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path.cwd()))

import api.main as api_main
from core import TranscriptSegment
import uvicorn
from video_store import VideoStore

root = Path(sys.argv[1])
store = VideoStore(root / "workspace" / "project.db")
folder_id = store.create_folder("测试课程")
for index in (1, 2):
    source = root / f"lesson-{index}.mp4"
    source.write_bytes(f"fixture video {index}".encode())
    video_id = store.create_video(
        f"fixture-{index}", source.name, str(source), str(root / f"audio-{index}.wav"), 1000,
    )
    store.set_status(video_id, "processing")
    if index == 2:
        store.set_video_folder(video_id, folder_id)

application = api_main.create_app(store)


@application.post("/__qa__/complete/{video_id}")
def complete(video_id: int):
    store.mark_transcribed(video_id, [TranscriptSegment(1, 0, 1000, "已完成字幕")])
    store.add_marker(video_id, 0, 1000, "测试考点", "", "", "", .9, "medium_recall")
    store.set_status(video_id, "review_ready")
    return {"status": "review_ready"}


uvicorn.run(application, host="127.0.0.1", port=int(sys.argv[2]), log_level="warning")
