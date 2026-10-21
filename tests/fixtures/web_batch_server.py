"""Disposable API with simulated ASR and AI for desktop batch verification."""

from pathlib import Path
from types import SimpleNamespace
import os
import sys
import time

sys.path.insert(0, str(Path.cwd()))

import api.main as api_main
from ai_provider import ProviderProfile
from api_failure import ApiUnavailable
from core import TranscriptSegment
import video_pipeline
from video_store import VideoStore
import uvicorn


root = Path(sys.argv[1])
store = VideoStore(root / "workspace" / "project.db")
paths = {}
for name in ["pure", "mixed", "nested", "one/course", "two/course"]:
    directory = root / "downloads" / name
    directory.mkdir(parents=True)
    for index in range(3 if name == "pure" else 1):
        (directory / f"lesson-{index}.mp4").write_bytes(f"{name}-{index}".encode())
    paths[name] = str(directory)
(Path(paths["mixed"]) / "notes.txt").write_text("fixture")
(Path(paths["nested"]) / "child").mkdir()
(Path(paths["nested"]) / "child" / "lesson").mkdir()
(Path(paths["nested"]) / "empty").mkdir()
(Path(paths["nested"]) / "child" / "chapter.mp4").write_bytes(b"nested-chapter")
(Path(paths["nested"]) / "child" / "lesson" / "section.mp4").write_bytes(b"nested-section")
calls = {"asr": 0, "ai": 0, "online": False}

profile = ProviderProfile(base_url="https://unused.example/v1", model="fixture", max_retries=0)
api_main.selected_model_id = lambda: "whisper-turbo"
api_main.require_selectable_model = lambda _: None
api_main.load_provider_settings = lambda: SimpleNamespace(active_profile=profile)
api_main.resolve_interface_key = lambda *_: "fixture-key"
api_main.interface_provider = lambda _: "openai_compatible"
api_main.model_is_downloaded = lambda *_: True
api_main.cache_size = lambda *_: 0
video_pipeline.video_duration_ms = lambda _: 1000


def transcribe(*_args, **_kwargs):
    calls["asr"] += 1
    time.sleep(.15)
    return [TranscriptSegment(1, 0, 1000, "会考，请掌握")], 1000


video_pipeline._transcribe_video_with_fallback = transcribe


class Judge:
    def analyze_video(self, current_store, video_id, **_kwargs):
        calls["ai"] += 1
        if not calls["online"]:
            raise ApiUnavailable("测试 API 暂不可用", status_code=503)
        current_store.set_analysis_run(video_id, "medium_recall", "completed", 0)
        return "completed", 0

    def close(self):
        pass


api_main.create_judge = lambda *_args, **_kwargs: Judge()
application = api_main.create_app(store)


@application.get("/__qa__/state")
def state():
    return {
        "paths": paths, "calls": calls,
        "folders": [dict(row) for row in store.list_folders()],
        "videos": [dict(row) for row in store.list_videos()],
    }


@application.post("/__qa__/repair")
def repair():
    calls["online"] = True
    return calls


@application.post("/__qa__/seed-markers")
def seed_markers():
    for video in store.list_videos():
        for index, level in enumerate(("standard", "important", "critical")):
            marker = store.add_marker(video["id"], index * 200, index * 200 + 100,
                                      "Fixture point", "", "", str(index), .9, "medium_recall")
            store.set_marker_importance(marker, level)
    return {"ok": True}


if frontend_project := os.environ.get("QA_FRONTEND_PROJECT_ROOT"):
    from frontend_host import mount_frontend
    mount_frontend(application, Path(frontend_project))

uvicorn.run(application, host="127.0.0.1", port=int(sys.argv[2]), log_level="warning")
