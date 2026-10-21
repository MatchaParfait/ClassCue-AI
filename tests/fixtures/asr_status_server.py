"""Disposable HTTP fixture for desktop ASR status interaction checks."""

import json
from pathlib import Path
import sys
from threading import RLock
import time

sys.path.insert(0, str(Path.cwd()))

import api.main as api_main
import uvicorn
from classcue.video_store import VideoStore


calls = []
failed = set()
lock = RLock()


def downloaded(model_id):
    with lock:
        calls.append({"kind": "files", "model_id": model_id})
    if model_id == "dolphin-small-cn":
        time.sleep(2)
    if model_id in failed:
        raise OSError("Fixture disk unavailable")
    return True


def size(kind, result):
    def inspect(*_args, **_kwargs):
        with lock:
            calls.append({"kind": kind})
        time.sleep(.1)
        return result
    return inspect


api_main.model_is_downloaded = downloaded
api_main.cache_size = size("model-size", 1024 ** 3)
for name in ("runtime_is_ready", "firered_runtime_is_ready", "paraformer_runtime_is_ready",
             "nano_runtime_is_ready", "dolphin_runtime_is_ready"):
    setattr(api_main, name, lambda *_args: True)
for name in ("runtime_size", "firered_runtime_size", "paraformer_runtime_size",
             "nano_runtime_size", "dolphin_runtime_size"):
    setattr(api_main, name, size(name, 2 * 1024 ** 3))
for name in ("paraformer_cuda_status", "nano_cuda_status", "dolphin_cuda_status"):
    setattr(api_main, name, lambda **_kwargs: {
        "gpu_available": True, "gpu_runtime_ready": True,
        "gpu_prepare_available": True, "gpu_name": "Fixture GPU",
    })

application = api_main.create_app(VideoStore(Path(sys.argv[1]) / "project.db"))


@application.get("/__qa__/calls")
def get_calls():
    with lock:
        return list(calls)


@application.post("/__qa__/failure/{model_id}")
def set_failure(model_id: str, value: bool):
    if value:
        failed.add(model_id)
    else:
        failed.discard(model_id)
    return {"ok": True}


uvicorn.run(application, host="127.0.0.1", port=int(sys.argv[2]), log_level="warning")
