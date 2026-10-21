"""Benchmark the full FireRed pipeline without CUDA or NVIDIA monitoring."""

from __future__ import annotations

import argparse
import csv
import ctypes
from ctypes import wintypes
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import statistics
import sys
from threading import Event, Thread
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def cpu_percent(cpu_delta, wall_delta, logical_cpus):
    return 100 * cpu_delta / wall_delta / logical_cpus if wall_delta > 0 else 0.0


def system_cpu_times():
    if os.name != "nt":
        return None
    idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
    function = ctypes.WinDLL("kernel32", use_last_error=True).GetSystemTimes
    function.argtypes = [ctypes.POINTER(wintypes.FILETIME)] * 3
    if not function(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        raise ctypes.WinError(ctypes.get_last_error())

    def ticks(value):
        return value.dwLowDateTime + (value.dwHighDateTime << 32)

    return ticks(idle), ticks(kernel) + ticks(user)


def summarize_resources(samples):
    result = {"samples": len(samples)}
    for key in ("process_working_mib", "process_private_commit_mib"):
        result["peak_" + key] = max((s[key] for s in samples if key in s), default=None)
    for key in ("system_available_physical_mib", "system_available_commit_mib"):
        result["min_" + key] = min((s[key] for s in samples if key in s), default=None)
    for key in ("process_cpu_percent_all_logical_cores", "system_cpu_percent"):
        values = [s[key] for s in samples if s.get(key) is not None]
        weights = [s["sample_seconds"] for s in samples if s.get(key) is not None]
        result["mean_" + key] = (
            sum(v * w for v, w in zip(values, weights)) / sum(weights) if weights else None
        )
        result["peak_" + key] = max(values, default=None)
    return result


def save_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def mark_stopped(directory):
    from qwen_memory import memory_snapshot

    path = directory / "result.json"
    result = json.loads(path.read_text(encoding="utf-8"))
    if result["status"] != "running":
        raise ValueError("Only a running trial can be marked stopped")
    samples = [
        json.loads(line) for line in (directory / "resource_samples.jsonl")
        .read_text(encoding="utf-8").splitlines()
    ]
    progress = json.loads((directory / "progress.json").read_text(encoding="utf-8"))
    result.update({
        "status": "stopped", "error": "Stopped at user request: CPU inference too slow.",
        "stopped_at_utc": datetime.now(timezone.utc).isoformat(),
        "process_wall_seconds": samples[-1]["elapsed_seconds"] if samples else progress["elapsed_seconds"],
        "timings": progress["timings"],
        "timing_scope": "Partial run; elapsed time ends at last resource sample, not full pipeline.",
        "resources": summarize_resources(samples),
        "resources_by_phase": {
            name: summarize_resources([s for s in samples if s["phase"] == name])
            for name in ("imports", "load", "audio", "vad", "aed")
        },
        "final_memory": {},
        "system_memory_after_stop": memory_snapshot(),
    })
    save_json(path, result)


def write_report(directory):
    result = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    fields = [
        "elapsed_seconds", "phase", "sample_seconds",
        "process_cpu_percent_all_logical_cores", "system_cpu_percent",
        "process_working_mib", "process_private_commit_mib",
        "process_peak_working_mib", "process_peak_private_commit_mib",
        "system_available_physical_mib", "system_available_commit_mib",
    ]
    with (directory / "resource_samples.jsonl").open(encoding="utf-8") as source:
        with (directory / "resources.csv").open("w", newline="", encoding="utf-8-sig") as output:
            writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for line in source:
                writer.writerow(json.loads(line))

    def number(value, suffix=""):
        return "-" if value is None else f"{value:.2f}{suffix}"

    hardware, timings = result.get("hardware", {}), result.get("timings", {})
    resource, final = result.get("resources", {}), result.get("final_memory", {})
    mean_pipeline_cpu = result.get("mean_pipeline_cpu_percent_all_logical_cores")
    if mean_pipeline_cpu is None and timings.get("pipeline_seconds") and hardware.get("logical_cpus"):
        mean_pipeline_cpu = cpu_percent(
            timings["pipeline_cpu_seconds"], timings["pipeline_seconds"], hardware["logical_cpus"],
        )
    lines = [
        "# FireRed CPU Benchmark", "",
        f"- Status: {result['status']}; source: `{result['video']}`.",
        f"- Started (UTC): {result['started_at_utc']}.",
        f"- BS: {result['batch_size']}; device: CPU; dtype: {result.get('model_dtype', '-')}.",
        f"- PyTorch threads: {hardware.get('cpu_threads', '-')}; "
        f"logical CPUs: {hardware.get('logical_cpus', '-')}.",
        "- CUDA hidden; AED/VAD explicitly use CPU; software audio decode; no NVIDIA monitoring.",
        "- Word timestamps and strict alignment validation remain enabled; no human accuracy labels.", "",
        "## Timing", "",
        f"- Audio duration: {number(result.get('duration_seconds'), ' s')}.",
        f"- Model load: {number(timings.get('model_load_seconds'), ' s')}.",
        f"- Pipeline: {number(timings.get('pipeline_seconds'), ' s')} "
        "(audio preparation + VAD + ASR + timestamp validation; excludes model load).",
        f"- Process wall: {number(result.get('process_wall_seconds'), ' s')}.",
        f"- Pipeline RTF: {number(result.get('pipeline_rtf'))}.",
        f"- Completed batches: {len(result.get('batches', []))}; "
        f"VAD segments: {result.get('vad_segment_count', '-')}.",
        f"- Subtitles: {result.get('subtitle_count', '-')}; "
        f"timestamped tokens: {result.get('word_count', '-')}.", "",
        "## Resources", "",
        f"- Mean pipeline process CPU: "
        f"{number(mean_pipeline_cpu, '%')}.",
        f"- Peak sampled process CPU: "
        f"{number(resource.get('peak_process_cpu_percent_all_logical_cores'), '%')}.",
        f"- Mean/peak sampled whole-system CPU: "
        f"{number(resource.get('mean_system_cpu_percent'), '%')} / "
        f"{number(resource.get('peak_system_cpu_percent'), '%')}.",
        f"- OS-recorded peak process working set: "
        f"{number(final.get('process_peak_working_mib'), ' MiB')}.",
        f"- OS-recorded peak process private commit: "
        f"{number(final.get('process_peak_private_commit_mib'), ' MiB')}.",
        f"- Minimum sampled system free physical/commit: "
        f"{number(resource.get('min_system_available_physical_mib'), ' MiB')} / "
        f"{number(resource.get('min_system_available_commit_mib'), ' MiB')}.", "",
        "Process CPU is normalized across all logical CPUs, not percent of one core.",
        "Whole-system CPU includes other programs and can differ from Task Manager's utility metric.",
        "Working set is resident RAM; private commit is not physical RAM. Samples are every 0.5s.", "",
        "| Phase | Mean process CPU % | Peak process CPU % | Peak RAM MiB | Peak private MiB |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, values in result.get("resources_by_phase", {}).items():
        lines.append(f"| {name} | "
                     f"{number(values.get('mean_process_cpu_percent_all_logical_cores'))} | "
                     f"{number(values.get('peak_process_cpu_percent_all_logical_cores'))} | "
                     f"{number(values.get('peak_process_working_mib'))} | "
                     f"{number(values.get('peak_process_private_commit_mib'))} |")
    if result.get("error"):
        lines.extend(["", "## Failure", "", result["error"]])
    lines.extend([
        "", "## Files", "",
        "- resources.csv: CPU and memory samples.",
        "- resource_samples.jsonl: original incremental samples.",
        "- result.json: exact configuration, batch timing and resource summaries.",
        "- raw_outputs.jsonl: original per-utterance text and timestamps.",
        "- firered.json / firered.srt / firered.txt: validated full-video output, if completed.",
        "- worker.log: official pipeline log.",
    ])
    (directory / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(args):
    # Set this before importing torch, including indirect framework imports.
    os.environ.update({
        "CUDA_VISIBLE_DEVICES": "-1", "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
    })
    from qwen_memory import ensure_memory_headroom, memory_snapshot

    args.output.mkdir(parents=True, exist_ok=True)
    result_path = args.output / "result.json"
    if result_path.exists():
        raise FileExistsError(f"Refusing to overwrite an existing CPU trial: {args.output}")
    started = time.perf_counter()
    phase = "imports"
    logical_cpus = os.cpu_count() or 1
    stop, unsafe = Event(), Event()
    samples, sampling_errors, batches = [], [], []
    timings = {}
    result = {
        "status": "running", "device": "cpu", "batch_size": args.batch_size,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "video": str(args.video), "source_bytes": args.video.stat().st_size,
        "model_root": str(args.model_root), "pid": os.getpid(),
        "cuda_visible_devices": os.environ["CUDA_VISIBLE_DEVICES"],
        "gpu_monitoring": False, "sampling_seconds": 0.5,
        "cpu_percent_definition": "Process CPU seconds / wall seconds / logical CPUs * 100.",
        "measurement_scope": "Full video, software decode, VAD, AED, strict word timestamps.",
        "baseline_memory": memory_snapshot(),
        "memory_guard": {"minimum_physical_mib": 1024, "minimum_commit_mib": 2048},
        "batches": batches,
    }
    save_json(result_path, result)

    def sample():
        try:
            previous_wall, previous_cpu = time.perf_counter(), time.process_time()
            previous_system = system_cpu_times()
            breaches = 0
            with (args.output / "resource_samples.jsonl").open("w", encoding="utf-8") as output:
                while not stop.wait(0.5):
                    now, cpu = time.perf_counter(), time.process_time()
                    system = system_cpu_times()
                    values = memory_snapshot()
                    values.update({
                        "elapsed_seconds": now - started, "phase": phase,
                        "sample_seconds": now - previous_wall,
                        "process_cpu_percent_all_logical_cores": cpu_percent(
                            cpu - previous_cpu, now - previous_wall, logical_cpus,
                        ),
                    })
                    if system is not None and previous_system is not None:
                        idle_delta = system[0] - previous_system[0]
                        total_delta = system[1] - previous_system[1]
                        values["system_cpu_percent"] = (
                            100 * (1 - idle_delta / total_delta) if total_delta else 0.0
                        )
                    samples.append(values)
                    output.write(json.dumps(values) + "\n")
                    output.flush()
                    low = (values.get("system_available_physical_mib", float("inf")) < 1024
                           or values.get("system_available_commit_mib", float("inf")) < 2048)
                    breaches = breaches + 1 if low else 0
                    if breaches >= 4:
                        unsafe.set()
                    elif not low:
                        unsafe.clear()
                    previous_wall, previous_cpu, previous_system = now, cpu, system
        except Exception:
            sampling_errors.append(traceback.format_exc())
            unsafe.set()

    thread = Thread(target=sample, daemon=True)
    thread.start()
    try:
        ensure_memory_headroom()
        import torch
        import firered_transcriber
        from scripts.compare_asr import _write_result

        if args.threads:
            torch.set_num_threads(args.threads)
        result["hardware"] = {
            "torch": torch.__version__, "logical_cpus": logical_cpus,
            "cpu_threads": torch.get_num_threads(),
            "interop_threads": torch.get_num_interop_threads(),
        }
        ensure_memory_headroom()
        phase = "load"
        begin = time.perf_counter()
        system = firered_transcriber.load_firered_model(
            str(args.model_root / "recognizer"), str(args.model_root / "vad" / "VAD"),
            use_gpu=False, batch_size=args.batch_size,
        )
        timings["model_load_seconds"] = time.perf_counter() - begin
        if any(p.device.type != "cpu" for p in system.asr.model.parameters()):
            raise RuntimeError("AED parameters are not exclusively on CPU")
        if system.asr.config.use_gpu or system.vad.config.use_gpu:
            raise RuntimeError("CPU-only configuration was not respected")
        result["configuration"] = asdict(system.config)
        result["model_dtype"] = str(next(system.asr.model.parameters()).dtype)
        save_json(result_path, result)
        print(f"CPU BS={args.batch_size} model ready: {timings['model_load_seconds']:.2f}s; "
              f"threads={torch.get_num_threads()}; dtype={result['model_dtype']}", flush=True)

        original_stream = firered_transcriber.stream_decode_audio_to_wav
        original_vad, original_asr = system.vad.detect, system.asr.transcribe

        def stream(*a, **kw):
            nonlocal phase
            phase = "audio"
            begin = time.perf_counter()
            duration = original_stream(*a, **kw)
            timings["audio_preparation_seconds"] = time.perf_counter() - begin
            result["duration_seconds"] = duration / 1000
            save_json(result_path, result)
            return duration

        def vad(*a, **kw):
            nonlocal phase
            phase = "vad"
            begin = time.perf_counter()
            detection, probabilities = original_vad(*a, **kw)
            timings["vad_seconds"] = time.perf_counter() - begin
            spans = detection["timestamps"]
            save_json(args.output / "vad_segments.json", spans)
            result["vad_segment_count"] = len(spans)
            result["speech_seconds"] = sum(end - start for start, end in spans)
            save_json(result_path, result)
            return detection, probabilities

        def asr(ids, waves):
            nonlocal phase
            phase = "aed"
            if sampling_errors:
                raise RuntimeError(sampling_errors[0])
            if unsafe.is_set():
                ensure_memory_headroom(physical_mib=1024, commit_mib=2048)
            begin, cpu_begin = time.perf_counter(), time.process_time()
            outputs = original_asr(ids, waves)
            elapsed = time.perf_counter() - begin
            timings["aed_seconds"] = timings.get("aed_seconds", 0) + elapsed
            batches.append({
                "index": len(batches) + 1, "uttids": ids,
                "input_seconds": [len(audio) / rate for rate, audio in waves],
                "wall_seconds": elapsed, "cpu_seconds": time.process_time() - cpu_begin,
                "output_count": len(outputs),
            })
            with (args.output / "raw_outputs.jsonl").open("a", encoding="utf-8") as output:
                for value in outputs:
                    output.write(json.dumps(value, ensure_ascii=False) + "\n")
            return outputs

        firered_transcriber.stream_decode_audio_to_wav = stream
        system.vad.detect, system.asr.transcribe = vad, asr

        def progress(fraction, message):
            current = {
                "fraction": fraction, "message": message, "phase": phase,
                "elapsed_seconds": time.perf_counter() - started,
                "completed_batches": len(batches), "timings": timings,
                "memory": memory_snapshot(), "recent_cpu": samples[-1] if samples else None,
            }
            save_json(args.output / "progress.json", current)
            save_json(result_path, result)
            print(f"CPU BS={args.batch_size} {fraction:.1%}: {message}", flush=True)

        phase = "pipeline"
        begin, cpu_begin = time.perf_counter(), time.process_time()
        try:
            segments, duration_ms = firered_transcriber.transcribe_firered(
                system, args.video, progress, temporary_directory=args.output,
            )
        finally:
            timings["pipeline_seconds"] = time.perf_counter() - begin
            timings["pipeline_cpu_seconds"] = time.process_time() - cpu_begin
            firered_transcriber.stream_decode_audio_to_wav = original_stream
            system.vad.detect, system.asr.transcribe = original_vad, original_asr
        phase = "save"
        _write_result(args.output, "firered", "FireRed CPU", segments, duration_ms)
        result.update({
            "status": "completed", "duration_seconds": duration_ms / 1000,
            "subtitle_count": len(segments), "word_count": sum(len(s.words) for s in segments),
            "pipeline_rtf": timings["pipeline_seconds"] / (duration_ms / 1000),
            "mean_pipeline_cpu_percent_all_logical_cores": cpu_percent(
                timings["pipeline_cpu_seconds"], timings["pipeline_seconds"], logical_cpus,
            ),
        })
    except BaseException as exc:
        result.update(status="failed", error=str(exc), traceback=traceback.format_exc())
        print(result["traceback"], flush=True)
    finally:
        stop.set()
        thread.join(timeout=5)
        result["timings"] = timings
        result["sampling_errors"] = sampling_errors
        if sampling_errors:
            result["status"] = "failed"
        result["process_wall_seconds"] = time.perf_counter() - started
        if timings.get("pipeline_seconds"):
            result["mean_pipeline_cpu_percent_all_logical_cores"] = cpu_percent(
                timings["pipeline_cpu_seconds"], timings["pipeline_seconds"], logical_cpus,
            )
        result["final_memory"] = memory_snapshot()
        result["resources"] = summarize_resources(samples)
        result["resources_by_phase"] = {
            name: summarize_resources([s for s in samples if s["phase"] == name])
            for name in ("imports", "load", "audio", "vad", "aed", "save")
        }
        save_json(result_path, result)
        print(f"CPU BS={args.batch_size} {result['status']}: {timings}", flush=True)
    return 0 if result["status"] == "completed" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--model-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=3)
    parser.add_argument("--threads", type=int)
    parser.add_argument("--summarize-only", action="store_true")
    parser.add_argument("--mark-stopped", action="store_true",
                        help="Finalize a partial trial after its process has been stopped externally.")
    args = parser.parse_args()
    args.video, args.model_root, args.output = (
        p.resolve() for p in (args.video, args.model_root, args.output)
    )
    if not args.video.is_file() or args.batch_size < 1 or (args.threads is not None and args.threads < 1):
        parser.error("A local video, positive batch size and positive thread count are required")
    if args.summarize_only:
        if args.mark_stopped:
            mark_stopped(args.output)
        write_report(args.output)
        return 0
    for path in ("recognizer/model.pth.tar", "vad/VAD/model.pth.tar"):
        if not (args.model_root / path).is_file():
            parser.error(f"Missing local model component: {path}")
    code = run(args)
    write_report(args.output)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
