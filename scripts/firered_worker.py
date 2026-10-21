"""One owned FireRed attempt with durable native results and typed failures."""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import faulthandler
import json
import os
import subprocess
from pathlib import Path
import sys
from threading import Event, Lock, Thread
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def cuda_hardware(torch):
    available = bool(torch.cuda.is_available())
    value = {"cuda_available": available, "gpu_total_mib": None, "device": "cuda:0"}
    if available:
        properties = torch.cuda.get_device_properties(0)
        value.update(gpu_total_mib=properties.total_memory / 2**20,
                     gpu_name=properties.name,
                     dtype="bfloat16" if torch.cuda.is_bf16_supported() else "float16")
        if getattr(properties, "uuid", None):
            value["gpu_uuid"] = str(properties.uuid)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--request", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    faulthandler.enable()
    channel, channel_lock = sys.stdout, Lock()

    def emit(value):
        with channel_lock:
            channel.write(json.dumps(value, ensure_ascii=True, allow_nan=False) + "\n")
            channel.flush()

    if args.probe:
        try:
            with redirect_stdout(sys.stderr):
                import torch
                hardware = cuda_hardware(torch)
            emit({"event": "hardware", "hardware": hardware})
            return 0
        except Exception as error:
            from qwen_resilience import classify_error
            emit({"event": "error", "kind": classify_error(error),
                  "message": f"{type(error).__name__}: {error}"})
            return 1
    if args.request is None or args.output is None:
        parser.error("--request and --output are required for inference")
    from firered_resilience import FireRedCheckpoints, select_batch_size
    from qwen_resilience import atomic_json, classify_error, read_json
    from qwen_memory import memory_snapshot

    request = read_json(args.request)
    manifest, attempt_id = request["manifest"], request["attempt_id"]
    started, phase, torch = time.perf_counter(), "load", None
    stop = Event()
    metrics = {"attempt_id": attempt_id, "batch_size": request["batch_size"],
               "process_id": os.getpid(), "device": "cuda:0", "memory_guard_enabled": False,
               "batches": [], "samples": []}
    phase_started = time.perf_counter()
    gpu_id = [os.environ.get("CUDA_VISIBLE_DEVICES", "0").split(",")[0]]

    def snapshot():
        try:
            return memory_snapshot()
        except OSError:
            return {}

    def monitor():
        warned = False
        while not stop.is_set():
            sample = {"elapsed_seconds": time.perf_counter() - started, **snapshot()}
            try:
                result = subprocess.run(
                    ["nvidia-smi", "--query-gpu=memory.used,memory.free,memory.total",
                     "--format=csv,noheader,nounits", f"--id={gpu_id[0]}"],
                    capture_output=True, text=True, timeout=3,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                if result.returncode == 0:
                    used, free, total = map(float, result.stdout.strip().split(","))
                    sample.update(device_used_mib=used, device_free_mib=free, device_total_mib=total)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
            metrics["samples"].append(sample)
            if not warned and (
                sample.get("system_available_physical_mib", float("inf")) < 1024
                or sample.get("system_available_commit_mib", float("inf")) < 1024
                or sample.get("device_free_mib", float("inf")) < 512
            ):
                warned = True
                emit({"event": "resource_warning",
                      "message": "资源余量较低，继续执行；仅真实分配失败后恢复或暂停。"})
            stop.wait(1)

    monitor_thread = Thread(target=monitor, daemon=True)
    monitor_thread.start()

    def set_phase(value):
        nonlocal phase, phase_started
        now = time.perf_counter()
        seconds = metrics.setdefault("phase_seconds", {})
        seconds[phase] = seconds.get(phase, 0) + now - phase_started
        phase_started = now
        phase = value

    try:
        with redirect_stdout(sys.stderr):
            import torch
            from firered_transcriber import load_firered_model, transcribe_firered
            from transcript_events import worker_transcript_callback
            hardware = cuda_hardware(torch)
            if not hardware["cuda_available"]:
                raise RuntimeError("FireRed 需要可用 CUDA；不会自动切换 CPU")
            configured_dtype = manifest.get("identity", {}).get("aed", {}).get("dtype")
            if configured_dtype and configured_dtype != hardware["dtype"]:
                raise RuntimeError("FireRed CUDA precision changed since preparation; restart the task")
            batch = min(request["batch_size"], select_batch_size(hardware["gpu_total_mib"]))
            uuid = hardware.get("gpu_uuid")
            if uuid:
                gpu_id[0] = uuid if uuid.startswith(("GPU-", "MIG-")) else f"GPU-{uuid}"
            metrics.update(batch_size=batch, hardware=hardware, dtype=hardware["dtype"])
            emit({"event": "resource", "resource": {"current_batch_size": batch}})
            emit({"event": "progress", "fraction": .04, "message": f"加载 FireRedVAD + AED，BS{batch}"})
            load_started = time.perf_counter()
            system = load_firered_model(manifest["paths"]["recognizer"], manifest["paths"]["vad"], batch)
            torch.cuda.synchronize()
            metrics["load_seconds"] = time.perf_counter() - load_started
            metrics["load_cuda_allocated_mib"] = torch.cuda.memory_allocated() / 2**20
            metrics["load_cuda_reserved_mib"] = torch.cuda.memory_reserved() / 2**20
            checkpoints = FireRedCheckpoints(args.output, manifest["job_id"], manifest["total_samples"])
            pipeline_started = time.perf_counter()
            transcribe_firered(
                system, manifest["timeline"], timeline_path=manifest["timeline"],
                checkpoints=checkpoints, metrics=metrics, on_phase=set_phase,
                progress=lambda fraction, message: emit({
                    "event": "progress", "fraction": fraction, "message": message,
                }),
                on_resource=lambda value: emit({"event": "resource", "resource": value}),
                **({"on_transcript": worker_transcript_callback(request, emit)}
                   if request.get("stream_transcript") else {}),
            )
            torch.cuda.synchronize()
            metrics["pipeline_seconds"] = time.perf_counter() - pipeline_started
        return 0
    except Exception as error:
        kind = classify_error(error, torch)
        if phase == "load" and kind == "inference":
            kind = "environment"
        failure = {"kind": kind, "phase": phase, "message": str(error),
                   "error_type": type(error).__name__, "traceback": traceback.format_exc(),
                   "memory": snapshot()}
        atomic_json(args.output / f"failure-{attempt_id}.json", failure)
        traceback.print_exc(file=sys.stderr)
        return 1
    finally:
        set_phase("finished")
        stop.set()
        monitor_thread.join(timeout=4)
        metrics.update(elapsed_seconds=time.perf_counter() - started, final_memory=snapshot())
        metrics["aed_seconds"] = sum(metrics["phase_seconds"].get(value, 0)
                                     for value in ("inference", "alignment"))
        metrics["new_speech_seconds"] = sum(batch["speech_seconds"] for batch in metrics["batches"])
        metrics["speech_throughput"] = (metrics["new_speech_seconds"] / metrics["aed_seconds"]
                                       if metrics["aed_seconds"] else None)
        if torch is not None:
            try:
                metrics.update(peak_cuda_allocated_mib=torch.cuda.max_memory_allocated() / 2**20,
                               peak_cuda_reserved_mib=torch.cuda.max_memory_reserved() / 2**20)
            except Exception:
                pass
        atomic_json(args.output / f"metrics-{attempt_id}.json", metrics)


if __name__ == "__main__":
    raise SystemExit(main())
