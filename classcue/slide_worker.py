"""Offline PPT worker. Only the parent publishes database rows."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
from queue import Queue
import sys
from threading import Event, Lock, Thread
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--site-packages", required=True)
    parser.add_argument("--probe", action="store_true")
    options = parser.parse_args()
    sys.path.insert(0, options.site_packages)
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    from classcue.slide_runtime import PREFIX
    from classcue.slide_cuda import CudaDifference
    from classcue.slide_extractor import SlideRoi, extract_slides
    from classcue.processing_control import ProcessingCancelled
    import cv2
    import torch

    cv2.setNumThreads(1)
    torch.set_num_threads(2)
    output_lock = Lock()

    def emit(value):
        with output_lock:
            print(PREFIX + json.dumps(value, ensure_ascii=False, default=str), flush=True)

    try:
        compare = CudaDifference()
        emit({"event": "ready", "gpu_name": torch.cuda.get_device_name(0)})
    except Exception:
        emit({"event": "error", "code": "cuda", "message": "PPT CUDA 初始化失败，请检查驱动和显存"})
        return
    if options.probe:
        import numpy as np
        from classcue.slide_extractor import _feature
        pixels = np.full((180, 320, 3), 245, np.uint8)
        feature = _feature(pixels, SlideRoi(0, 0, 320, 180), (), 320)
        compare(feature, feature)
        emit({"event": "ready", "gpu_name": torch.cuda.get_device_name(0)})
        return
    commands = Queue(maxsize=1)
    stopped = Event()
    active = Event()

    def heartbeat():
        while True:
            if active.wait(timeout=5):
                emit({"event": "heartbeat"})
                time.sleep(5)

    Thread(target=heartbeat, name="slide-worker-heartbeat", daemon=True).start()

    def read():
        try:
            for line in sys.stdin:
                if len(line) > 65536:
                    break
                value = json.loads(line)
                if value.get("action") == "cancel":
                    stopped.set()
                elif value.get("action") == "extract":
                    commands.put(value)
        finally:
            stopped.set()
            commands.put(None)

    Thread(target=read, name="slide-worker-control", daemon=True).start()
    while True:
        command = commands.get()
        if command is None:
            return
        # Cancellation may arrive immediately after the extract command.
        try:
            active.set()
            result = extract_slides(
                Path(command["source"]), Path(command["staging"]), command["source_hash"],
                roi=SlideRoi(**command["roi"]),
                ignore_regions=tuple(SlideRoi(**r) for r in command["ignore_regions"]),
                should_stop=stopped.is_set,
                progress=lambda value, message, stage=None: emit(
                    {"event": "progress", "value": value, "message": message, "stage": stage}),
                compare=compare, prefetch=True,
            )
            result.backend = "cuda"
            emit({"event": "result", "result": asdict(result)})
        except ProcessingCancelled:
            emit({"event": "error", "code": "cancelled", "message": "已停止 PPT 提取"})
        except ValueError as error:
            emit({"event": "error", "code": "source", "message": str(error)})
        except Exception:
            emit({"event": "error", "code": "cuda", "message": "PPT CUDA 处理失败，请重试或使用 CPU"})
            return
        finally:
            active.clear()
            compare.clear_frames()


if __name__ == "__main__":
    main()
