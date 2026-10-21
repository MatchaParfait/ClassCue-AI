"""JSONL Paraformer worker with native timestamps and fixed GPU hardware routing."""

from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import asdict
import faulthandler
import argparse
import json
import os
from pathlib import Path
import sys
from time import perf_counter
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", choices=("cpu", "gpu"), default="cpu")
    device = parser.parse_args().device
    if device == "cpu":
        os.environ["CUDA_VISIBLE_DEVICES"] = ""
    faulthandler.enable()
    channel = sys.stdout

    def emit(value):
        channel.write(json.dumps(value, ensure_ascii=True) + "\n")
        channel.flush()

    model, key = None, None
    for line in sys.stdin:
        audit = {}
        try:
            request = json.loads(line)
            with redirect_stdout(sys.stderr):
                from classcue.paraformer_transcriber import (
                    is_cuda_oom, load_paraformer_model,
                    release_cuda_memory, transcribe_paraformer,
                )
                from classcue.paraformer_routing import select_batch_size
                from classcue.processing_control import ResourcePaused
                from classcue.asr_cpu import cpu_thread_policy

                policy = cpu_thread_policy(device, request.get("cpu_threads"))
                audit.update(policy)

                total_mib = None
                if device == "gpu":
                    import torch

                    if not torch.cuda.is_available():
                        raise RuntimeError("Paraformer GPU requires available CUDA")
                    total_mib = torch.cuda.get_device_properties(0).total_memory / 2**20
                batch_size = select_batch_size(
                    device, request.get("batch_size"), gpu_total_mib=total_mib,
                )
                audit.update(
                    gpu_total_memory_mib=total_mib,
                    batch_size_source="hardware" if request.get("batch_size") is None else "explicit",
                )

                requested = (request["recognizer"], request["vad"],
                             policy["cpu_threads"], device)
                load_seconds = 0.0
                if model is None or key != requested:
                    if model is not None:
                        model, key = None, None
                        if device == "gpu":
                            release_cuda_memory()
                    emit({"event": "progress", "fraction": .01,
                          "message": f"Loading Paraformer {device.upper()}"})
                    started = perf_counter()
                    for attempt in range(2):
                        try:
                            model = load_paraformer_model(
                                *requested[:2], cpu_threads=requested[2], device=device,
                            )
                            break
                        except Exception as exc:
                            if device != "gpu" or not is_cuda_oom(exc):
                                raise
                            exc.__traceback__ = None
                            release_cuda_memory()
                            if attempt:
                                raise ResourcePaused(
                                    "Paraformer GPU 模型加载仍显存不足，任务已暂停；请释放显存后继续。",
                                    failure={"stage": "load", "attempts": 2},
                                ) from None
                            emit({"event": "progress", "fraction": .01,
                                  "message": "CUDA OOM: retrying model load once"})
                    key = requested
                    load_seconds = perf_counter() - started
                if request.get("raw_output_path"):
                    audit["raw_output_path"] = request["raw_output_path"]
                segments, duration_ms = transcribe_paraformer(
                    model, request["audio_path"],
                    progress=lambda fraction, message: emit({
                        "event": "progress", "fraction": fraction, "message": message,
                    }),
                    batch_size=batch_size,
                    temporary_directory=request.get("temporary_directory"), audit=audit,
                )
                audit["stage_seconds"]["load"] = load_seconds
            emit({"event": "result", "segments": [asdict(segment) for segment in segments],
                  "duration_ms": duration_ms, "audit": audit})
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            from classcue.processing_control import ResourcePaused

            if isinstance(exc, ResourcePaused):
                # Release the resident model before the paused worker waits for another request.
                model, key = None, None
                release_cuda_memory()
                emit({"event": "resource_paused", "message": str(exc),
                      "failure": exc.failure, "audit": audit})
            else:
                emit({"event": "error", "message": f"{type(exc).__name__}: {exc}", "audit": audit})


if __name__ == "__main__":
    main()
