"""Serial production-path FireRed routing and completed-cache acceptance run."""

import argparse
from dataclasses import asdict
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    from firered_resilience import read_json, resource_profile
    from qwen_resilience import atomic_json
    from transcriber import transcribe_audio

    args.output.mkdir(parents=True, exist_ok=True)
    if (args.output / "acceptance.json").exists():
        raise FileExistsError("Acceptance report already exists; use another output directory")
    cache = args.output / "acceptance-workspace" / "temporary_audio" / "firered_jobs"
    events = []
    profile = resource_profile(refresh=True)
    print(f"Native profile: {profile}", flush=True)

    def progress(fraction, message):
        print(f"{fraction:.3f} {message}", flush=True)

    started = time.perf_counter()
    from firered_resilience import resource_events
    with resource_events(events.append):
        segments, duration = transcribe_audio(
            args.video, model_id="fireredasr2-aed", cache_directory=cache, progress=progress,
        )
    first_seconds = time.perf_counter() - started
    state_path = next(path for path in cache.glob("*/state.json")
                      if read_json(path).get("status") == "completed")
    before = read_json(state_path)
    cached_started = time.perf_counter()
    cached, cached_duration = transcribe_audio(
        args.video, model_id="fireredasr2-aed", cache_directory=cache, progress=progress,
    )
    cached_seconds = time.perf_counter() - cached_started
    after = read_json(state_path)
    if segments != cached or duration != cached_duration or len(before["attempts"]) != len(after["attempts"]):
        raise AssertionError("Cached rerun changed subtitles or launched another model attempt")
    attempts = before["attempts"]
    metric_rows = [attempt.get("metrics", {}) for attempt in attempts]
    samples = [sample for row in metric_rows for sample in row.get("samples", [])]

    def peak(name):
        values = [sample[name] for sample in samples if name in sample]
        return max(values) if values else None

    total_aed = sum(row.get("aed_seconds", 0) for row in metric_rows)
    sampled_gpu = sum("device_used_mib" in sample for sample in samples)
    summary = {
        "video": str(args.video.resolve()), "hardware": profile,
        "audio_seconds": duration / 1000, "subtitles": len(segments),
        "initial_batch_size": before["initial_batch_size"],
        "final_batch_size": before["current_batch_size"],
        "recoveries": before["recoveries"], "new_model_attempts": len(attempts),
        "first_wall_seconds": first_seconds, "cached_wall_seconds": cached_seconds,
        "cached_new_attempts": len(after["attempts"]) - len(attempts),
        "cached_subtitles_identical": True, "total_aed_seconds": total_aed,
        "source_throughput": (duration / 1000 / first_seconds),
        "peak_cuda_allocated_mib": max((row.get("peak_cuda_allocated_mib", 0) for row in metric_rows), default=0),
        "peak_cuda_reserved_mib": max((row.get("peak_cuda_reserved_mib", 0) for row in metric_rows), default=0),
        "whole_gpu_sampled_peak_mib": peak("device_used_mib"),
        "gpu_sample_coverage": sampled_gpu / len(samples) if samples else 0,
        "process_peak_working_mib": peak("process_peak_working_mib"),
        "process_peak_private_commit_mib": peak("process_peak_private_commit_mib"),
        "state_path": str(state_path), "metrics": metric_rows,
        "memory_guard_enabled": False,
    }
    atomic_json(args.output / "acceptance.json", summary)
    atomic_json(args.output / "subtitles.json", [asdict(item) for item in segments])
    print({key: value for key, value in summary.items() if key != "metrics"}, flush=True)


if __name__ == "__main__":
    main()
