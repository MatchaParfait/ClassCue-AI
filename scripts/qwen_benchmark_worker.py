"""Measure Qwen ASR/alignment separately on prepared, identical audio ranges."""

import argparse
import gc
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event, Thread
import time
import faulthandler

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reverse-order", action="store_true")
    parser.add_argument("--cpu-threads", type=int, default=4)
    parser.add_argument("--max-private-mib", type=float, default=12288)
    args = parser.parse_args()
    os.environ.update({
        "QWEN_CPU_THREADS": str(args.cpu_threads),
        "OMP_NUM_THREADS": str(args.cpu_threads), "MKL_NUM_THREADS": str(args.cpu_threads),
        "OPENBLAS_NUM_THREADS": str(args.cpu_threads),
        "ONEDNN_PRIMITIVE_CACHE_CAPACITY": "0",
    })
    faulthandler.enable()
    from qwen_memory import memory_snapshot, ensure_memory_headroom
    args.output.mkdir(parents=True, exist_ok=True)
    cpu_samples, cpu_stop = [], Event()
    memory_started = time.perf_counter()
    baseline_memory = memory_snapshot()
    def sample_cpu():
        breaches = 0
        while not cpu_stop.is_set():
            snapshot = memory_snapshot()
            cpu_samples.append({"elapsed_seconds": time.perf_counter() - memory_started, **snapshot})
            unsafe = (
                snapshot.get("process_private_commit_mib", 0) > args.max_private_mib
                or snapshot.get("system_available_physical_mib", float("inf")) < 1536
                or snapshot.get("system_available_commit_mib", float("inf")) < 3072
            )
            breaches = breaches + 1 if unsafe else 0
            if breaches >= 2:
                (args.output / "memory_abort.json").write_text(json.dumps({
                    "reason": "memory_safety_limit", "snapshot": snapshot, "samples": cpu_samples,
                }, indent=2), encoding="utf-8")
                print("Memory safety limit reached; stopping this isolated Qwen worker", flush=True)
                os._exit(2)
            cpu_stop.wait(1)
    cpu_thread = Thread(target=sample_cpu, daemon=True)
    cpu_thread.start()
    import numpy as np
    import soundfile as sf
    import torch
    from asr_models import component_paths
    from qwen_transcriber import aligned_segments, load_qwen_model
    from scripts.compare_asr import _write_result
    from scripts.qwen_chunked import _offset_segment
    from qwen_silence import compact_audio, restore_segments

    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    audio, rate = sf.read(plan["sample_audio"], dtype="float32")
    paths = component_paths("qwen3-asr-1.7b")
    started = time.perf_counter()
    model = load_qwen_model(str(paths["recognizer"]), str(paths["aligner"]))
    model.max_inference_batch_size = plan["batch_size"]
    load_seconds = time.perf_counter() - started
    print(f"Model ready ({load_seconds:.2f}s), BS={plan['batch_size']}", flush=True)
    model.transcribe(audio=(audio[:rate * 10], rate), language="Chinese", return_time_stamps=True)

    original_asr = model._infer_asr
    original_align = model.forced_aligner.align
    stages = {"asr_seconds": 0.0, "align_seconds": 0.0}

    def asr(*a, **kw):
        torch.cuda.synchronize()
        begin = time.perf_counter()
        try:
            return original_asr(*a, **kw)
        finally:
            torch.cuda.synchronize()
            stages["asr_seconds"] += time.perf_counter() - begin

    def align(*a, **kw):
        begin = time.perf_counter()
        try:
            return original_align(*a, **kw)
        finally:
            stages["align_seconds"] += time.perf_counter() - begin

    model._infer_asr = asr
    model.forced_aligner.align = align
    summary = {
        "load_seconds": load_seconds,
        "gpu": torch.cuda.get_device_name(0),
        "torch": torch.__version__,
        "batch_size": plan["batch_size"], "modes": [],
        "cpu_threads": torch.get_num_threads(),
        "baseline_memory": baseline_memory,
        "scope": "warm Qwen, includes ASR + CPU alignment; excludes extraction/VAD/load",
    }
    args.output.mkdir(parents=True, exist_ok=True)
    entries = list(reversed(plan["modes"])) if args.reverse_order else plan["modes"]
    for entry in entries:
        mode = entry["mode"]
        print(f"BEGIN {mode}", flush=True)
        stages.update(asr_seconds=0.0, align_seconds=0.0)
        gc.collect()
        torch.cuda.empty_cache()
        torch.cuda.reset_peak_memory_stats()
        snapshots, stop = [], Event()

        def monitor():
            while not stop.is_set():
                try:
                    result = subprocess.run(
                        ["nvidia-smi", "--query-gpu=memory.used,memory.free,utilization.gpu",
                         "--format=csv,noheader,nounits"],
                        capture_output=True, text=True, timeout=5,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    )
                    values = [float(x.strip()) for x in result.stdout.splitlines()[0].split(",")]
                    snapshots.append(values)
                except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
                    pass
                stop.wait(1)

        thread = Thread(target=monitor, daemon=True)
        thread.start()
        started = time.perf_counter()
        segments, batches, mapping_warnings, batch_memory = [], [], [], []
        sample_start_index = len(cpu_samples)
        status, error = "completed", None
        try:
            spans = [s for s in entry.get("chunks", entry["spans"]) if s.get("input_samples", 1)]
            for index in range(0, len(spans), plan["batch_size"]):
                ensure_memory_headroom()
                group = spans[index:index + plan["batch_size"]]
                waveforms = [
                    (compact_audio(audio, s) if "slices" in s else audio[s["start_sample"]:s["end_sample"]], rate)
                    for s in group
                ]
                batch_start = time.perf_counter()
                outputs = model.transcribe(
                    audio=waveforms, language="Chinese", return_time_stamps=True,
                )
                if len(outputs) != len(group):
                    raise ValueError("Unexpected batch output count")
                for span, output in zip(group, outputs):
                    duration = span.get("input_samples", span["end_sample"] - span["start_sample"]) / rate
                    items = list(output.time_stamps.items) if output.time_stamps is not None else []
                    cues = aligned_segments(output.text, items, 0, duration, 1)
                    if "slices" in span:
                        cues = restore_segments(
                            cues, span, rate, len(audio), warnings=mapping_warnings,
                        )
                        offset = 0
                    else:
                        offset = round(span["start_sample"] * 1000 / rate)
                    for cue in cues:
                        segments.append(_offset_segment(cue, offset, len(segments) + 1))
                batches.append(time.perf_counter() - batch_start)
                print(f"{mode}: batch {index // plan['batch_size'] + 1}, {batches[-1]:.2f}s", flush=True)
                # Periodic allocator cleanup keeps the CPU aligner from competing
                # with retained GPU workspace under Windows shared-memory pressure.
                gc.collect()
                torch.cuda.empty_cache()
                batch_memory.append(memory_snapshot())
                (args.output / "memory_progress.json").write_text(json.dumps({
                    "mode": mode, "completed_batches": len(batches),
                    "total_batches": (len(spans) + plan["batch_size"] - 1) // plan["batch_size"],
                    "batch_memory": batch_memory, "current": batch_memory[-1],
                }, indent=2), encoding="utf-8")
        except Exception as exc:
            status, error = "failed", repr(exc)
            print(f"{mode} failed: {error}", flush=True)
        finally:
            stop.set()
            thread.join(timeout=6)
        elapsed = time.perf_counter() - started
        result = {
            "mode": mode, "status": status, "error": error,
            "wall_seconds": elapsed, **stages, "batch_seconds": batches,
            "audio_seconds": len(audio) / rate,
            "rtf": elapsed / (len(audio) / rate),
            "peak_cuda_allocated_mib": torch.cuda.max_memory_allocated() / 2**20,
            "peak_cuda_reserved_mib": torch.cuda.max_memory_reserved() / 2**20,
            "sampled_device_peak_used_mib": max((s[0] for s in snapshots), default=None),
            "sampled_device_min_free_mib": min((s[1] for s in snapshots), default=None),
            "sampled_gpu_utilization_mean": float(np.mean([s[2] for s in snapshots])) if snapshots else None,
            "segment_count": len(segments), "chunk_count": len(entry["spans"]),
            "inference_chunk_count": len(spans),
            "asr_input_seconds": sum(
                s.get("input_samples", s["end_sample"] - s["start_sample"]) / rate for s in spans
            ),
            "mapping_warnings": mapping_warnings,
            "batch_memory": batch_memory,
        }
        sampled = cpu_samples[sample_start_index:]
        for key in ("process_working_mib", "process_private_commit_mib"):
            result[f"peak_{key}"] = max((s.get(key, 0) for s in sampled), default=0)
        for key in ("system_available_physical_mib", "system_available_commit_mib"):
            result[f"min_{key}"] = min((s.get(key, float("inf")) for s in sampled), default=0)
        if status == "completed":
            _write_result(args.output, f"qwen-{mode}", f"Qwen ({mode})", segments, round(len(audio) / rate * 1000))
            payload_path = args.output / f"qwen-{mode}.json"
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
            payload.update({
                "timestamp_axis": "original_sample",
                "sample_start_in_source_seconds": plan.get("sample_start_seconds", 0),
                "skip_non_speech": entry.get("skip_non_speech", False),
                "asr_input_seconds": result["asr_input_seconds"],
            })
            payload_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        summary["modes"].append(result)
        (args.output / "qwen_timings.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        print(json.dumps(result), flush=True)
    cpu_stop.set()
    cpu_thread.join(timeout=2)
    (args.output / "cpu_memory_samples.json").write_text(json.dumps({
        "baseline": baseline_memory, "samples": cpu_samples,
    }, indent=2), encoding="utf-8")
    return 0 if all(r["status"] == "completed" for r in summary["modes"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
