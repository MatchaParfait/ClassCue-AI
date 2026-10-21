"""JSONL worker for the isolated official Nano timestamp runtime."""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", choices=("cpu", "gpu"), default="cpu")
    device = parser.parse_args().device
    if device == "cpu":
        os.environ["CUDA_VISIBLE_DEVICES"] = ""
    channel = sys.stdout

    def emit(event):
        channel.write(json.dumps(event, ensure_ascii=True) + "\n")
        channel.flush()

    model, key = None, None
    for line in sys.stdin:
        audit = {}
        try:
            request = json.loads(line)
            with redirect_stdout(sys.stderr):
                from nano_transcriber import (
                    DEFAULT_BATCH_SIZE, load_nano_model, transcribe_nano,
                    is_cuda_oom, release_cuda_memory,
                )
                from processing_control import ResourcePaused
                from asr_cpu import cpu_thread_policy

                policy = cpu_thread_policy(device, request.get("cpu_threads"))
                audit.update(policy)

                requested = (request["recognizer"], request["vad"], policy["cpu_threads"])
                if model is None or key != requested:
                    model, key = None, None
                    if device == "gpu":
                        release_cuda_memory()
                    emit({"event": "progress", "fraction": .01, "message": f"Loading Nano {device.upper()}"})
                    for attempt in range(2):
                        try:
                            model = load_nano_model(*requested[:2], device=device, cpu_threads=requested[2])
                            break
                        except Exception as exc:
                            if device != "gpu" or not is_cuda_oom(exc):
                                raise
                            exc.__traceback__ = None
                            release_cuda_memory()
                            if attempt:
                                raise ResourcePaused("Nano model load exhausted GPU memory; release memory and resume.",
                                                     failure={"stage": "load", "attempts": 2}) from None
                    key = requested
                if request.get("raw_output_path"):
                    audit["raw_output_path"] = request["raw_output_path"]
                segments, duration = transcribe_nano(
                    model, request["audio_path"],
                    progress=lambda f, m: emit({"event": "progress", "fraction": f, "message": m}),
                    batch_size=request.get("batch_size", DEFAULT_BATCH_SIZE),
                    temporary_directory=request.get("temporary_directory"), audit=audit,
                )
            emit({"event": "result", "segments": [asdict(cue) for cue in segments],
                  "duration_ms": duration, "audit": audit})
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            from processing_control import ResourcePaused

            if isinstance(exc, ResourcePaused):
                model, key = None, None
                release_cuda_memory()
                emit({"event": "resource_paused", "message": str(exc), "failure": exc.failure, "audit": audit})
            else:
                emit({"event": "error", "message": f"{type(exc).__name__}: {exc}", "audit": audit})


if __name__ == "__main__":
    main()
