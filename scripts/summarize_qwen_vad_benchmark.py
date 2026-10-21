"""Validate benchmark artifacts and write a concise comparison report."""

import argparse
import json
from pathlib import Path
import statistics
import wave


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    root = args.report.resolve()
    boundaries = json.loads((root / "boundary_timings.json").read_text(encoding="utf-8"))
    first = json.loads((root / "qwen_timings.json").read_text(encoding="utf-8"))
    second = json.loads((root / "repeat" / "qwen_timings.json").read_text(encoding="utf-8"))
    with wave.open(str(root / "timeline.wav")) as source:
        full_frames = source.getnframes()
        rate = source.getframerate()
    validations = []
    for mode in ("energy", "firered"):
        full_plan = json.loads((root / f"{mode}_full_plan.json").read_text(encoding="utf-8"))["spans"]
        assert full_plan[0]["start_sample"] == 0
        assert full_plan[-1]["end_sample"] == full_frames
        assert all(a["end_sample"] == b["start_sample"] for a, b in zip(full_plan, full_plan[1:]))
        assert sum(s["end_sample"] - s["start_sample"] for s in full_plan) == full_frames
        for directory in (root, root / "repeat"):
            payload = json.loads((directory / f"qwen-{mode}.json").read_text(encoding="utf-8"))
            segments = payload["segments"]
            assert [s["id"] for s in segments] == list(range(1, len(segments) + 1))
            assert all(0 <= s["start_ms"] < s["end_ms"] <= payload["duration_ms"] for s in segments)
            assert all(a["start_ms"] <= b["start_ms"] for a, b in zip(segments, segments[1:]))
            lines = (directory / f"qwen-{mode}.txt").read_text(encoding="utf-8").splitlines()
            blocks = (directory / f"qwen-{mode}.srt").read_text(encoding="utf-8").strip().split("\n\n")
            assert len(lines) == len(blocks) == len(segments)
        validations.append({"mode": mode, "full_samples_preserved": True, "transcripts_valid": True})
    lines = [
        "# Qwen VAD Performance Comparison", "",
        f"- Source: `{boundaries['video']}`",
        f"- Full audio: {full_frames / rate:.3f} seconds.",
        f"- Qwen excerpt: source {boundaries['sample_start_seconds']:.0f} to "
        f"{boundaries['sample_start_seconds'] + boundaries['sample_audio_seconds']:.0f} seconds "
        f"({boundaries['sample_audio_seconds']:.0f} seconds total).",
        "- Approximately 90-second chunks, BS=2, SDPA, FP16 GPU ASR, FP32 CPU aligner.",
        "- Qwen order: energy then FireRed; second run reverses the order.",
        "- Warmup/model load/extraction/VAD are excluded from Qwen excerpt timings.", "",
        "## Full-Audio Boundary Preparation", "",
        "| Mode | VAD load | VAD inference | Worker wall time | Boundary planning | Chunks | Energy fallbacks |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for entry in boundaries["modes"]:
        assert entry["status"] == "ready"
        lines.append(
            f"| {entry['mode']} | {entry['vad_load_seconds']:.3f}s | "
            f"{entry['vad_inference_seconds']:.3f}s | {entry['vad_wall_seconds']:.3f}s | "
            f"{entry['planning_seconds']:.3f}s | {entry['full_chunk_count']} | {entry['fallback_count']} |"
        )
    lines.extend([
        "", "## Six-Minute Qwen Excerpt", "",
        "| Mode | Run 1 | Run 2 | Mean | Allocator reserved peak | Sampled device free minimum |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    means = {}
    for mode in ("energy", "firered"):
        a = next(e for e in first["modes"] if e["mode"] == mode)
        b = next(e for e in second["modes"] if e["mode"] == mode)
        assert a["status"] == b["status"] == "completed"
        means[mode] = statistics.mean([a["wall_seconds"], b["wall_seconds"]])
        lines.append(
            f"| {mode} | {a['wall_seconds']:.3f}s | {b['wall_seconds']:.3f}s | "
            f"{means[mode]:.3f}s | {max(a['peak_cuda_reserved_mib'], b['peak_cuda_reserved_mib']):.0f} MiB | "
            f"{min(a['sampled_device_min_free_mib'], b['sampled_device_min_free_mib']):.0f} MiB |"
        )
    lines.extend([
        "", "## Interpretation", "",
        "- FireRed adds full-file CPU detection and one isolated worker startup; it does not use GPU memory.",
        "- The Qwen speed difference changes sign between runs. Two runs do not establish a speed benefit or penalty.",
        "- Timings above are measured data, not a full-file end-to-end ASR result or an accuracy ranking.",
        "- Energy planning uses cumulative sliding sums, not the slower NumPy convolution implementation.",
        "- Every audio sample is retained; boundary timing and output segment counts differ between modes.",
        "- Full-file sample coverage and both runs' TXT/SRT/JSON structures passed validation.",
        "- Pyannote was deferred because official model access requires authorization.", "",
        "## Manual Boundary Review", "",
        "- Open `review/index.html`: energy and FireRed cuts are paired by boundary index.",
        "- Each clip includes four seconds before and after the cut. The red line is the cut.",
        "- Mark pause / inside speech / uncertain. Browser-local choices can be exported as CSV.",
        "- These are full-file cuts; excerpt transcript timestamps start at zero (add 600s for source time).",
        "- To assess recognition near a cut, compare the two excerpt transcripts in addition to listening.",
    ])
    (root / "comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (root / "validation.json").write_text(json.dumps(validations, indent=2), encoding="utf-8")
    print(json.dumps({"means_seconds": means, "validation": validations}, indent=2))


if __name__ == "__main__":
    main()
