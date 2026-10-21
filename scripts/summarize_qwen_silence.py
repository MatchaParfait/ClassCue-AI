"""Validate original-time transcripts and summarize the silence-skipping trial."""

import argparse
from difflib import SequenceMatcher
import json
import math
from pathlib import Path
import unicodedata


def normalized(text):
    return "".join(c for c in text if unicodedata.category(c)[0] in {"L", "N"})


def summarize(report):
    root = Path(report).resolve()
    plan = json.loads((root / "inference_plan.json").read_text(encoding="utf-8"))
    timings = json.loads((root / "qwen_timings.json").read_text(encoding="utf-8"))
    modes = {e["mode"]: e for e in timings["modes"]}
    rate = plan["sample_rate"]
    validation, payloads = [], {}
    for mode in ("no-skip", "skip"):
        result = modes[mode]
        if result["status"] != "completed":
            raise ValueError(f"{mode} did not complete: {result.get('error')}")
        payload = json.loads((root / f"qwen-{mode}.json").read_text(encoding="utf-8"))
        payloads[mode] = payload
        segments = payload["segments"]
        if [s["id"] for s in segments] != list(range(1, len(segments) + 1)):
            raise ValueError("Invalid segment ids")
        if not all(0 <= s["start_ms"] < s["end_ms"] <= payload["duration_ms"] for s in segments):
            raise ValueError("Transcript is outside original audio")
        if not all(a["start_ms"] <= b["start_ms"] for a, b in zip(segments, segments[1:])):
            raise ValueError("Transcript is out of order")
        if not all(
            0 <= w["start"] <= w["end"] <= payload["duration_ms"] / 1000 + .001
            for s in segments for w in s.get("words", [])
        ):
            raise ValueError("Invalid restored word timestamps")
        silence = plan["sample_silence_plans"][mode]
        if silence["retained_samples"] + silence["removed_samples"] != silence["total_samples"]:
            raise ValueError("Incomplete source partition")
        if sum(c["input_samples"] for c in silence["chunks"]) != silence["retained_samples"]:
            raise ValueError("Incomplete retained partition")
        positions = []
        for chunk in silence["chunks"]:
            compact_cursor = 0
            for item in chunk["slices"]:
                if item["compact_start_sample"] != compact_cursor:
                    raise ValueError("Discontinuous compact map")
                if (item["source_end_sample"] - item["source_start_sample"]
                        != item["compact_end_sample"] - item["compact_start_sample"]):
                    raise ValueError("Map changes sample length")
                compact_cursor = item["compact_end_sample"]
                positions.append((item["source_start_sample"], item["source_end_sample"]))
            if compact_cursor != chunk["input_samples"]:
                raise ValueError("Compact map does not cover input")
        positions.extend((r["start_sample"], r["end_sample"]) for r in silence["deleted_regions"])
        cursor = 0
        for a, b in sorted(positions):
            if a != cursor:
                raise ValueError("Source partition contains gaps or overlaps")
            cursor = b
        if cursor != silence["total_samples"]:
            raise ValueError("Source partition has incomplete tail")
        validation.append({
            "mode": mode, "original_time_transcripts_valid": True,
            "source_sample_partition_valid": True, "segment_count": len(segments),
            "word_crossing_join_warnings": len(result["mapping_warnings"]),
        })

    differences = []
    for index, span in enumerate(plan["modes"][0]["spans"]):
        left, right = span["start_sample"] / rate, span["end_sample"] / rate
        texts = {
            mode: "".join(s["text"] for s in payload["segments"]
                          if left <= s["start_ms"] / 1000 < right)
            for mode, payload in payloads.items()
        }
        a, b = normalized(texts["no-skip"]), normalized(texts["skip"])
        changes = [
            {"operation": op, "baseline": a[i:j], "filtered": b[k:l]}
            for op, i, j, k, l in SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
            if op != "equal"
        ]
        differences.append({
            "source_chunk_index": index, "source_start_seconds": left + plan["sample_start_seconds"],
            "source_end_seconds": right + plan["sample_start_seconds"], **texts,
            "normalized_text_equal": a == b, "changes": changes,
        })
    (root / "transcript_differences.json").write_text(
        json.dumps(differences, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    (root / "validation.json").write_text(json.dumps(validation, indent=2), encoding="utf-8")
    baseline, filtered = modes["no-skip"], modes["skip"]
    reduction = 1 - filtered["wall_seconds"] / baseline["wall_seconds"]
    silence = plan["sample_silence_plans"]["skip"]
    parameters = silence["parameters"]
    lines = [
        "# Qwen Silence-Skipping Comparison", "",
        f"- Video: `{plan['video']}`",
        f"- Source audio SHA256: `{plan['source_audio_sha256']}`",
        f"- Full source duration: {plan['full_audio_seconds']:.3f} s.",
        f"- Measured range: source {plan['sample_start_seconds']:.3f} to "
        f"{plan['sample_start_seconds'] + plan['sample_audio_seconds']:.3f} s.",
        f"- Full-source trial: {plan['sample_is_full_source']}.",
        "- Both conditions use identical FireRed + energy-fallback external cuts.",
        f"- Target {plan['chunk_seconds']:g} s, BS={plan['batch_size']}; GPU FP16 SDPA ASR, CPU FP32 alignment.",
        f"- Speech padding: {parameters['padding_seconds']:g} s per side; "
        f"minimum skipped remainder: {parameters['min_removed_seconds']:g} s.",
        f"- Skipped: {len(silence['deleted_regions'])} regions, {silence['removed_samples'] / rate:.3f} s "
        f"({silence['removed_samples'] / silence['total_samples']:.2%}).",
        f"- Shared VAD worker: {plan['vad_wall_seconds']:.3f} s "
        f"(load {plan['vad_load_seconds']:.3f} s, inference {plan['vad_inference_seconds']:.3f} s).",
        f"- Shared plan preparation: {plan['preparation_seconds']:.3f} s.",
        f"- Qwen model loading: {timings['load_seconds']:.3f} s, excluded from inference measurements.",
        "- Extraction, model load and warmup are excluded; input compaction and timestamp restoration are included.",
        f"- Run order: {' then '.join(e['mode'] for e in timings['modes'])}.", "",
        "## Measured Inference", "",
        "| Condition | ASR input | Wall time | ASR stage | Alignment stage | RTF | Reserved GPU peak | Sampled free min |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for mode in ("no-skip", "skip"):
        e = modes[mode]
        free = f"{e['sampled_device_min_free_mib']:.0f} MiB" if e["sampled_device_min_free_mib"] is not None else "N/A"
        lines.append(
            f"| {mode} | {e['asr_input_seconds']:.3f}s | {e['wall_seconds']:.3f}s | "
            f"{e['asr_seconds']:.3f}s | {e['align_seconds']:.3f}s | {e['rtf']:.4f} | "
            f"{e['peak_cuda_reserved_mib']:.0f} MiB | {free} |"
        )
    lines.extend([
        "", "## Interpretation", "",
        f"- Measured warm inference time reduction: {reduction:.2%} "
        f"({baseline['wall_seconds'] - filtered['wall_seconds']:.3f}s). Negative means slower.",
        "- This is one ordered pair on one source, not a universal speedup or an accuracy result.",
        "- Both paths already need FireRed for external cuts, so skipping does not add a second VAD pass.",
        "- Source audio is unmodified. Subtitles retain original-source time.",
        f"- Normalized transcript differences: {sum(not e['normalized_text_equal'] for e in differences)} "
        f"of {len(differences)} external chunks; these are changes, not measured errors.",
        "- Inspect `transcript_differences.json` and `review/index.html` before enabling bulk jobs.",
        "- Baseline transcript words in skipped regions can be real speech, hallucinations, or alignment errors.",
        "- Splice-crossing word warnings are audit hints; a single interval spanning a splice is ambiguous.",
        "- Valid source sample partitions and restored subtitle/word ranges passed automatic validation.",
        "- Extraction reuse does not measure source video decoding cost.", "",
        "## Manual Review", "",
        "- `review/deleted_regions.csv`: exact original sample/time intervals.",
        "- `review/index.html`: original/context audio, exact removed audio, local joined context and both transcripts.",
        "- Mark no speech / speech removed / damaged word edge / uncertain; export selections and notes as CSV.",
    ])
    memory_path = root / "cpu_memory_samples.json"
    if memory_path.is_file():
        measurements = json.loads(memory_path.read_text(encoding="utf-8"))
        samples = measurements["samples"]
        peak_working = max(s.get("process_peak_working_mib", 0) for s in samples)
        peak_private = max(s.get("process_private_commit_mib", 0) for s in samples)
        min_available = min(s.get("system_available_physical_mib", float("inf")) for s in samples)
        min_commit = min(s.get("system_available_commit_mib", float("inf")) for s in samples)
        baseline_available = measurements["baseline"].get("system_available_physical_mib", 0)
        physical_increase = max(0, baseline_available - min_available)
        ram_reserve = math.ceil((max(peak_working, physical_increase) + 3072) / 1024)
        commit_reserve = math.ceil((peak_private + 4096) / 1024)
        summary = {
            "cpu_threads": timings.get("cpu_threads"), "peak_working_mib": peak_working,
            "peak_private_commit_mib": peak_private,
            "min_system_available_physical_mib": min_available,
            "min_system_available_commit_mib": min_commit,
            "system_physical_increase_mib": physical_increase,
            "recommended_free_physical_before_launch_gib": ram_reserve,
            "recommended_free_commit_before_launch_gib": commit_reserve,
            "safety_floor_physical_gib": 3, "safety_floor_commit_gib": 4,
            "gpu_headroom_target_mib": 1024,
            "scope": "observed run; RAM includes loading, commit can include Windows CUDA driver allocations",
        }
        (root / "memory_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        lines.extend([
            "", "## RAM And Commit Stability", "",
            f"- CPU threads: {timings.get('cpu_threads')}; oneDNN primitive cache disabled.",
            f"- Process peak resident working set (Windows native peak): {peak_working / 1024:.2f} GiB.",
            f"- Sampled process peak private commit: {peak_private / 1024:.2f} GiB.",
            f"- Minimum system free physical RAM: {min_available / 1024:.2f} GiB.",
            f"- Minimum system free commit: {min_commit / 1024:.2f} GiB.",
            f"- Suggested free physical RAM before launch: at least {ram_reserve} GiB "
            "(observed peak/increment plus 3 GiB system margin).",
            f"- Suggested free commit capacity before launch: at least {commit_reserve} GiB "
            "(sampled private peak plus 4 GiB margin).",
            "- RAM, commit and GPU VRAM are distinct; Windows CUDA driver allocations can contribute to commit.",
            "- Sampled private/commit peaks are approximate. Native peak working set includes model loading.",
            "- The monitor stops an isolated worker after two consecutive unsafe samples "
            "(private >12 GiB, physical free <1.5 GiB, or commit free <3 GiB).",
        ])
        for mode in ("no-skip", "skip"):
            batches = modes[mode].get("batch_memory", [])
            lines.append(
                f"- {mode} post-batch private commit (GiB): "
                + ", ".join(f"{s['process_private_commit_mib'] / 1024:.2f}" for s in batches)
            )
    repeat_path = root / "repeat" / "qwen_timings.json"
    if repeat_path.is_file():
        repeated = json.loads(repeat_path.read_text(encoding="utf-8"))
        repeated_plan = json.loads((root / "repeat" / "inference_plan.json").read_text(encoding="utf-8"))
        if (repeated_plan["source_audio_sha256"] != plan["source_audio_sha256"]
                or repeated_plan["sample_start_seconds"] != plan["sample_start_seconds"]
                or repeated_plan["sample_audio_seconds"] != plan["sample_audio_seconds"]):
            raise ValueError("Repeat does not use the identical source range")
        second = {e["mode"]: e for e in repeated["modes"]}
        if not all(second[mode]["status"] == "completed" for mode in ("no-skip", "skip")):
            raise ValueError("Repeat trial is incomplete")
        means = {
            mode: (modes[mode]["wall_seconds"] + second[mode]["wall_seconds"]) / 2
            for mode in ("no-skip", "skip")
        }
        result = {
            "run1_order": [e["mode"] for e in timings["modes"]],
            "run2_order": [e["mode"] for e in repeated["modes"]],
            "mean_seconds": means, "mean_time_reduction_fraction": 1 - means["skip"] / means["no-skip"],
        }
        first_memory = json.loads((root / "memory_summary.json").read_text(encoding="utf-8"))
        second_memory = json.loads((root / "repeat" / "memory_summary.json").read_text(encoding="utf-8"))
        result["recommended_free_physical_before_launch_gib"] = max(
            first_memory["recommended_free_physical_before_launch_gib"],
            second_memory["recommended_free_physical_before_launch_gib"],
        )
        result["recommended_free_commit_before_launch_gib"] = max(
            first_memory["recommended_free_commit_before_launch_gib"],
            second_memory["recommended_free_commit_before_launch_gib"],
        )
        (root / "two_round_summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        lines.extend([
            "", "## Reversed-Order Repeat", "",
            "| Condition | Run 1 | Run 2 | Mean |", "|---|---:|---:|---:|",
        ])
        for mode in ("no-skip", "skip"):
            lines.append(f"| {mode} | {modes[mode]['wall_seconds']:.3f}s | "
                         f"{second[mode]['wall_seconds']:.3f}s | {means[mode]:.3f}s |")
        lines.append(f"- Mean time reduction: {result['mean_time_reduction_fraction']:.2%}; "
                     "two trials on this excerpt, not an accuracy or full-source speed result.")
    (root / "comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({
        "time_reduction_fraction": reduction, "validation": validation,
        "removed_seconds": silence["removed_samples"] / rate,
    }, ensure_ascii=True, indent=2))
    return validation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    summarize(args.report)


if __name__ == "__main__":
    main()
