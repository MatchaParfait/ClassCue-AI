"""Offline ASR lifecycle acceptance; the monitoring interpreter needs optional psutil."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event, Thread
import time
import traceback
import wave


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def prepare_excerpt(video, target, seconds=120, start_seconds=0):
    import av

    count, limit, skip = 0, round(seconds * 16000), round(start_seconds * 16000)
    with av.open(str(video)) as container, wave.open(str(target), "wb") as output:
        stream = container.streams.audio[0]
        output.setparams((1, 2, 16000, 0, "NONE", ""))
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)

        def write(frames):
            nonlocal count, skip
            for frame in frames:
                samples = frame.to_ndarray().reshape(-1)
                omitted = min(skip, len(samples))
                samples, skip = samples[omitted:], skip - omitted
                samples = samples[:limit - count]
                output.writeframes(samples.astype("<i2", copy=False).tobytes())
                count += len(samples)

        for frame in container.decode(stream):
            write(resampler.resample(frame))
            if count >= limit:
                break
        if count < limit:
            write(resampler.resample(None))
    if count != limit:
        raise ValueError("Local video is shorter than the requested excerpt")
    return {"start_seconds": start_seconds, "duration_seconds": count / 16000, "samples": count,
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}


def process_snapshot(root_pid, known):
    import psutil

    processes = []
    try:
        root = psutil.Process(root_pid)
        candidates = [root, *root.children(recursive=True)]
    except psutil.Error:
        candidates = []
    for process in candidates:
        try:
            identity = (process.pid, process.create_time())
            known.add(identity)
            memory = process.memory_info()
            processes.append({
                "pid": process.pid, "ppid": process.ppid(), "created": identity[1],
                "rss_mib": memory.rss / 2**20,
                "private_mib": getattr(memory, "private", None) / 2**20
                if getattr(memory, "private", None) is not None else None,
            })
        except psutil.Error:
            continue
    return {"processes": processes, "tree_rss_mib": sum(p["rss_mib"] for p in processes),
            "tree_private_mib": sum(p["private_mib"] for p in processes
                                    if p["private_mib"] is not None)}


def surviving_processes(known):
    import psutil

    alive = []
    for pid, created in sorted(known):
        try:
            process = psutil.Process(pid)
            if process.create_time() == created and process.is_running():
                alive.append(pid)
        except psutil.Error:
            pass
    return alive


def gpu_snapshot(owned_pids):
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    result = {}
    try:
        device = subprocess.run(
            ["nvidia-smi", "-i", "0", "--query-gpu=memory.used,memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3, creationflags=flags,
        )
        if device.returncode == 0:
            values = device.stdout.strip().split(",")
            result["device_used_mib"], result["device_free_mib"] = map(float, values)
        applications = subprocess.run(
            ["nvidia-smi", "-i", "0", "--query-compute-apps=pid,used_gpu_memory",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3, creationflags=flags,
        )
        owned, unavailable = [], False
        if applications.returncode == 0:
            for line in applications.stdout.splitlines():
                parts = line.split(",")
                if len(parts) != 2 or not parts[0].strip().isdigit():
                    continue
                pid = int(parts[0])
                if pid in owned_pids:
                    try:
                        memory = float(parts[1].strip())
                    except ValueError:
                        memory, unavailable = None, True
                    owned.append({"pid": pid, "used_mib": memory})
            result["owned_processes"] = owned
            result["owned_used_mib"] = None if unavailable else sum(p["used_mib"] for p in owned)
        else:
            result["owned_used_mib"] = None
        result["device_memory_is_shared_with_other_applications"] = True
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        result["sampling_error"] = f"{type(error).__name__}: {error}"
    return result


def worker(args):
    from asr_models import SETTINGS_PATH, component_paths
    from qwen_resilience import atomic_json
    from qwen_memory import memory_snapshot

    result = {"engine": args.engine, "status": "running", "pid": os.getpid(),
              "before": memory_snapshot()}
    settings = SETTINGS_PATH.read_bytes() if SETTINGS_PATH.exists() else None
    print(json.dumps({"event": "worker_pid", "pid": os.getpid()}), flush=True)
    started = time.monotonic()
    try:
        def progress(fraction, message):
            print(json.dumps({"event": "progress", "fraction": fraction, "message": message}),
                  flush=True)

        if args.engine == "firered":
            from firered_resilience import run_firered

            paths = component_paths("fireredasr2-aed")
            segments, duration = run_firered(
                args.audio, args.output / "cache",
                {"recognizer": str(paths["recognizer"]), "vad": str(paths["vad"] / "VAD")},
                progress=progress, should_stop=lambda: False,
            )
        else:
            from transcriber import DolphinWorker

            child = DolphinWorker(device="cpu")
            try:
                segments, duration = child.transcribe(
                    args.audio, component_paths("dolphin-small-cn"), progress, lambda: False)
            finally:
                child.close()
        from qwen_resilience import validate_segments

        validate_segments(segments, duration)
        if not segments:
            raise ValueError("Expected speech subtitles in the fixed excerpt")
        result.update(status="completed", duration_ms=duration,
                      segments=[asdict(segment) for segment in segments])
    except Exception as error:
        result.update(status="failed", error_type=type(error).__name__, error=str(error),
                      traceback=traceback.format_exc())
    finally:
        result.update(wall_seconds=time.monotonic() - started, after=memory_snapshot(),
                      saved_settings_unchanged=(
                          SETTINGS_PATH.read_bytes() if SETTINGS_PATH.exists() else None) == settings)
        if not result["saved_settings_unchanged"]:
            result["status"] = "failed"
        atomic_json(args.output / "worker-result.json", result)
    return 0 if result["status"] == "completed" else 1


def trial(args, audio, directory, engine):
    from qwen_process import run_process
    from qwen_resilience import atomic_json, read_json

    directory.mkdir(parents=True, exist_ok=False)
    samples, known, errors = [], set(), []
    stop, identified = Event(), Event()
    worker_pid = [None]
    started = time.monotonic()
    before_gpu = gpu_snapshot(set())

    def monitor():
        last_gpu = float("-inf")
        try:
            while not stop.wait(.25):
                if not identified.is_set():
                    continue
                sample = {"elapsed_seconds": time.monotonic() - started,
                          **process_snapshot(worker_pid[0], known)}
                if time.monotonic() - last_gpu >= 1:
                    sample["gpu"] = gpu_snapshot({pid for pid, _created in known})
                    last_gpu = time.monotonic()
                samples.append(sample)
        except Exception as error:
            errors.append(f"{type(error).__name__}: {error}")

    monitor_thread = Thread(target=monitor, daemon=True)
    monitor_thread.start()
    code, launch_error = None, None
    env = {**os.environ, "PYTHONUTF8": "1", "HF_HUB_OFFLINE": "1",
           "TRANSFORMERS_OFFLINE": "1", "MODELSCOPE_OFFLINE": "1",
           "HF_HUB_DISABLE_TELEMETRY": "1", "PIP_NO_INDEX": "1"}
    command = [sys.executable, "-u", str(Path(__file__).resolve()), "--worker",
               "--engine", engine, "--audio", str(audio), "--output", str(directory)]
    with (directory / "worker.log").open("w", encoding="utf-8") as log:
        def line_received(line):
            log.write(line)
            log.flush()
            try:
                event = json.loads(line)
            except ValueError:
                return
            if isinstance(event, dict) and event.get("event") == "worker_pid":
                worker_pid[0] = event["pid"]
                process_snapshot(worker_pid[0], known)
                identified.set()

        try:
            code = run_process(command, cwd=ROOT, env=env, on_line=line_received, timeout=args.timeout)
        except Exception as error:
            launch_error = f"{type(error).__name__}: {error}"
        finally:
            stop.set()
            monitor_thread.join(timeout=8)
    deadline = time.monotonic() + 5
    alive = surviving_processes(known)
    while alive and time.monotonic() < deadline:
        time.sleep(.1)
        alive = surviving_processes(known)
    result = read_json(directory / "worker-result.json", {})
    result.update(
        returncode=code, launch_error=launch_error, monitoring_errors=errors,
        before_gpu=before_gpu, after_cleanup_gpu=gpu_snapshot({pid for pid, _created in known}),
        worker_pid_observed=identified.is_set(), observed_process_count=len(known),
        observed_process_identities=[{"pid": pid, "created": created}
                                     for pid, created in sorted(known)],
        surviving_owned_pids=alive, resource_samples=samples,
        peak_tree_rss_mib=max((s["tree_rss_mib"] for s in samples), default=None),
        peak_tree_private_mib=max((s["tree_private_mib"] for s in samples), default=None),
        peak_device_gpu_used_mib=max(
            (s["gpu"]["device_used_mib"] for s in samples
             if s.get("gpu", {}).get("device_used_mib") is not None), default=None),
        owned_gpu_memory_supported=any(
            s.get("gpu", {}).get("owned_processes")
            and s["gpu"].get("owned_used_mib") is not None for s in samples),
    )
    result["lifecycle_passed"] = (
        code == 0 and result.get("status") == "completed" and bool(samples)
        and not errors and identified.is_set() and not alive
        and result.get("saved_settings_unchanged") is True)
    atomic_json(directory / "acceptance.json", result)
    print(json.dumps({"trial": directory.name, "engine": engine,
                      "status": result.get("status"), "lifecycle_passed": result["lifecycle_passed"],
                      "peak_tree_rss_mib": result["peak_tree_rss_mib"],
                      "surviving_owned_pids": alive}), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path)
    parser.add_argument("--audio", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--start-seconds", type=float, default=600)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--engine", choices=("firered", "dolphin"), default="firered")
    args = parser.parse_args()
    args.output = args.output.resolve()
    if args.worker:
        return worker(args)
    if (args.rounds < 1 or args.timeout <= 0 or args.start_seconds < 0
            or args.video is None or not args.video.is_file()):
        parser.error("A local video, positive rounds and positive timeout are required")
    args.output.mkdir(parents=True, exist_ok=False)
    from qwen_resilience import atomic_json
    from qwen_memory import ensure_memory_headroom, memory_snapshot

    audio = args.output / "excerpt-120s.wav"
    prepared = prepare_excerpt(args.video, audio, start_seconds=args.start_seconds)
    engine, trials, failures = "firered", [], []
    for index in range(args.rounds):
        baseline = memory_snapshot()
        try:
            ensure_memory_headroom(baseline)
        except RuntimeError as error:
            trials.append({"status": "blocked", "engine": engine, "lifecycle_passed": False,
                           "before_round": baseline, "error": str(error)})
            break
        result = trial(args, audio, args.output / f"round-{index + 1}-{engine}", engine)
        result["before_round"] = baseline
        if result.get("status") != "completed" and engine == "firered":
            failures.append(result)
            engine = "dolphin"
            try:
                ensure_memory_headroom()
            except RuntimeError as error:
                trials.append({"status": "blocked", "engine": engine, "lifecycle_passed": False,
                               "error": str(error)})
                break
            result = trial(args, audio, args.output / f"round-{index + 1}-{engine}", engine)
        trials.append(result)
        if not result["lifecycle_passed"]:
            break
    summary = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_video": str(args.video.resolve()), "excerpt": prepared,
        "requested_rounds": args.rounds, "trials": trials, "firered_failures": failures,
        "passed": len(trials) == args.rounds and all(r["lifecycle_passed"] for r in trials),
        "repeated_structured_outputs_equal": bool(trials) and all(
            r.get("engine") == trials[0].get("engine")
            and r.get("segments") == trials[0].get("segments") for r in trials),
        "sampling_interval_seconds": .25,
        "limitations": [
            "RSS and private commit are sampled; sub-interval transient peaks may be missed.",
            "Only descendants of the created worker are inspected; no existing service is controlled.",
            "GPU device memory includes other apps; owned process VRAM may be unavailable under WDDM.",
            "Each round starts fresh; this is lifecycle acceptance, not an accuracy or speedup benchmark.",
        ],
    }
    atomic_json(args.output / "summary.json", summary)
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
