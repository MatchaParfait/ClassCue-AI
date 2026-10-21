"""Disposable API for desktop video library workflow verification."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path.cwd()))

import api.main as api_main
import uvicorn
import video_pipeline
from video_store import VideoStore

root = Path(sys.argv[1])
source = root / "downloads" / "lesson.mp4"
source.parent.mkdir(parents=True, exist_ok=True)
source.write_bytes(b"disposable video fixture")
video_pipeline.video_duration_ms = lambda _: 1000
api_main.model_is_downloaded = lambda _: False
application = api_main.create_app(VideoStore(root / "workspace" / "project.db"))

uvicorn.run(application, host="127.0.0.1", port=int(sys.argv[2]), log_level="warning")
