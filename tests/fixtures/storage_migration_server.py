"""Disposable real API/filesystem fixture; never reads or moves the user's library."""
from pathlib import Path
import json
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import api.main as api_main
import classcue.storage_migration as storage_migration
from classcue.video_store import VideoStore


def application(directory):
    directory = Path(directory).resolve()
    project = directory / "application"
    data = directory / "library"
    assets = directory / "assets"
    config = project / "data" / "asr_storage.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"storage_root": str(assets)}), encoding="utf-8")
    audio = data / "temporary_audio" / "lesson.wav"
    model = assets / "models" / "asr" / "fixture" / "weights.bin"
    environment = assets / ".runtime" / "qwen-asr" / "ready.json"
    for path, content in ((audio, b"audio" * 200000), (model, b"weights" * 1000), (environment, b"preserve environment")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    blocked = directory / "blocked-media" / "videos" / "fixture" / "lesson.mp4"
    blocked.parent.mkdir(parents=True)
    blocked.write_bytes(b"do not overwrite")
    store = VideoStore(data / "project.db")
    video = data / "videos" / "fixture" / "lesson.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"fixture video")
    store.create_video("fixture", video.name, str(video), str(audio), 1000)
    api_main.DIRECTORY_PROJECT_ROOT = project
    api_main.ACTIVE_ASR_STORAGE_ROOT = assets
    api_main.ACTIVE_LOG_DIR = project / ".runtime" / "logs"
    # Slow small fixture transfers just enough to inspect actual progress and refresh recovery.
    storage_migration.CHUNK = 32768
    original = storage_migration.migrate
    def with_visible_progress(*args, report=lambda _detail: None, **kwargs):
        def emit(detail):
            report(detail)
            if detail["phase"] == "copying":
                time.sleep(.035)
        return original(*args, report=emit, **kwargs)
    storage_migration.migrate = with_visible_progress
    return api_main.create_app(store)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(application(sys.argv[1]), host="127.0.0.1", port=int(sys.argv[2]), log_level="warning")
