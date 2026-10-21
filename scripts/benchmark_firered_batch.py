"""Benchmark the production FireRed pipeline in isolated, sequential BS trials."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
from threading import Event, Thread
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def reusable_trial(directory, video):
    saved = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    if (saved.get("status") != "completed"
            or Path(saved.get("video", "")).resolve() != video.resolve()
            or saved.get("source_bytes") != video.stat().st_size):
        raise FileExistsError(f"Existing trial is incomplete or has a different source: {directory}")
    required = ("raw_outputs.json", "vad_segments.json", "resource_samples.json", "firered.json")
    if any(not (directory / filename).is_file() for filename in required):
        raise FileExistsError(f"Existing trial artifacts are incomplete: {directory}")
    return saved


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def edit_distance(first, second):
    if len(first) < len(second):
        first, second = second, first
    row = list(range(len(second) + 1))
    for i, a in enumerate(first, 1):
        previous, row = row, [i]
        for j, b in enumerate(second, 1):
            row.append(min(row[-1] + 1, previous[j] + 1, previous[j - 1] + (a != b)))
    return row[-1]


def compare_outputs(reference, candidate):
    first = {item["uttid"]: item for item in reference}
    second = {item["uttid"]: item for item in candidate}
    changed = []
    timestamp_deltas = []
    mismatched_timestamps = []
    edits = reference_chars = 0
    for key in sorted(first.keys() | second.keys()):
        a, b = first.get(key, {}), second.get(key, {})
        old, new = a.get("text", ""), b.get("text", "")
        reference_chars += len(old)
        if old != new:
            distance = edit_distance(old, new)
            edits += distance
            changed.append({"uttid": key, "reference": old, "candidate": new, "edits": distance})
        else:
            old_times, new_times = a.get("timestamp", []), b.get("timestamp", [])
            if len(old_times) == len(new_times):
                for old_word, new_word in zip(old_times, new_times):
                    if old_word[0] == new_word[0]:
                        timestamp_deltas.extend([
                            abs(old_word[1] - new_word[1]), abs(old_word[2] - new_word[2]),
                        ])
                    else:
                        mismatched_timestamps.append(key)
                        break
            else:
                mismatched_timestamps.append(key)
    return {
        "reference_utterances": len(first), "candidate_utterances": len(second),
        "missing_utterance_ids": sorted(first.keys() - second.keys()),
        "extra_utterance_ids": sorted(second.keys() - first.keys()),
        "changed_text_utterances": len(changed), "character_edits": edits,
        "relative_character_edit_rate": edits / max(1, reference_chars),
        "not_accuracy_score": "Reference is BS=1 output, not human ground truth.",
        "max_matching_token_timestamp_delta_seconds": max(timestamp_deltas, default=None),
        "p95_matching_token_timestamp_delta_seconds": percentile(timestamp_deltas, 0.95),
        "timestamp_shape_mismatch_utterance_ids": mismatched_timestamps,
        "changed_text": changed,
    }


def check_memory_headroom(args, *, initial=False):
    if args.disable_memory_guard:
        return
    from qwen_memory import ensure_memory_headroom

    if initial:
        ensure_memory_headroom()
    else:
        ensure_memory_headroom(
            physical_mib=args.minimum_free_physical_mib,
            commit_mib=args.minimum_free_commit_mib,
        )


def worker(args):
    import faulthandler
    from qwen_memory import memory_snapshot

    faulthandler.enable()
    directory = args.output / f"bs{args.worker}"
    directory.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    phase = "imports"
    memory_samples, gpu_samples = [], []
    stop, unsafe = Event(), Event()
    result = {
        "batch_size": args.worker, "status": "running",
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "video": str(args.video), "source_bytes": args.video.stat().st_size,
        "sampling": {"ram_seconds": 0.25, "gpu_seconds": 1},
        "memory_guard": {
            "enabled": not args.disable_memory_guard,
            "initial_physical_mib": 2048, "initial_commit_mib": 4096,
            "minimum_free_physical_mib": args.minimum_free_physical_mib,
            "minimum_free_commit_mib": args.minimum_free_commit_mib,
            "sustained_seconds": 2,
        },
        "measurement_scope": "Fresh process; model load separate; full production video pipeline.",
        "baseline_memory": memory_snapshot(), "batches": [],
    }
    save_json(directory / "result.json", result)

    def sample_memory():
        breaches = 0
        while not stop.is_set():
            snapshot = memory_snapshot()
            memory_samples.append({
                "elapsed_seconds": time.perf_counter() - started, "phase": phase, **snapshot,
            })
            low = not args.disable_memory_guard and (
                snapshot.get("system_available_physical_mib", float("inf")) < args.minimum_free_physical_mib
                or snapshot.get("system_available_commit_mib", float("inf")) < args.minimum_free_commit_mib
            )
            breaches = breaches + 1 if low else 0
            if breaches >= 8:
                unsafe.set()
            elif not low:
                unsafe.clear()
            stop.wait(0.25)

    memory_thread = Thread(target=sample_memory, daemon=True)
    memory_thread.start()
    query = "memory.used,memory.free,utilization.gpu,temperature.gpu,power.draw,clocks.sm"
    gpu_process = subprocess.Popen(
        ["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits", "-lms", "1000"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )

    def sample_gpu():
        for line in gpu_process.stdout:
            values = line.strip().split(",")
            if len(values) != 6:
                continue
            parsed = []
            for value in values:
                try:
                    parsed.append(float(value.strip()))
                except ValueError:
                    parsed.append(None)
            gpu_samples.append({
                "elapsed_seconds": time.perf_counter() - started, "phase": phase,
                **dict(zip(("used_mib", "free_mib", "utilization_percent", "temperature_c",
                            "power_w", "sm_clock_mhz"), parsed)),
            })

    gpu_thread = Thread(target=sample_gpu, daemon=True)
    gpu_thread.start()
    torch = None
    timings = {}
    raw_outputs, vad_segments = [], []
    try:
        import torch
        import firered_transcriber
        from asr_models import component_paths
        from scripts.compare_asr import _write_result

        if args.disable_memory_guard:
            print(f"BS={args.worker} experimental memory guard DISABLED", flush=True)
        check_memory_headroom(args, initial=True)
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA is unavailable; refusing to compare CPU fallback with GPU trials.")
        result["hardware"] = {
            "gpu": torch.cuda.get_device_name(0),
            "gpu_total_mib": torch.cuda.get_device_properties(0).total_memory / 2**20,
            "torch": torch.__version__, "torch_cuda": torch.version.cuda,
            "cpu_threads": torch.get_num_threads(), "logical_cpus": os.cpu_count(),
        }

        def synchronize():
            torch.cuda.synchronize()

        phase = "load"
        begin = time.perf_counter()
        paths = component_paths("fireredasr2-aed")
        system = firered_transcriber.load_firered_model(
            str(paths["recognizer"]), str(paths["vad"] / "VAD"),
        )
        synchronize()
        timings["model_load_seconds"] = time.perf_counter() - begin
        result["load_cuda_peak_allocated_mib"] = torch.cuda.max_memory_allocated() / 2**20
        result["load_cuda_peak_reserved_mib"] = torch.cuda.max_memory_reserved() / 2**20
        system.config.asr_batch_size = args.worker
        result["configuration"] = asdict(system.config)
        result["model_dtype"] = str(next(system.asr.model.parameters()).dtype)
        original_stream = firered_transcriber.stream_decode_audio_to_wav
        original_vad, original_asr = system.vad.detect, system.asr.transcribe

        def stream(*a, **kw):
            nonlocal phase
            phase = "audio"
            begin = time.perf_counter()
            duration = original_stream(*a, **kw)
            timings["audio_preparation_seconds"] = time.perf_counter() - begin
            with Path(a[1]).open("rb") as source:
                result["wav_sha256"] = hashlib.file_digest(source, "sha256").hexdigest()
            result["wav_bytes"] = Path(a[1]).stat().st_size
            return duration

        def vad(*a, **kw):
            nonlocal phase
            phase = "vad"
            synchronize()
            begin = time.perf_counter()
            detection, probabilities = original_vad(*a, **kw)
            synchronize()
            timings["vad_seconds"] = time.perf_counter() - begin
            vad_segments.extend(detection["timestamps"])
            result["pre_aed_cuda_peak_allocated_mib"] = torch.cuda.max_memory_allocated() / 2**20
            result["pre_aed_cuda_peak_reserved_mib"] = torch.cuda.max_memory_reserved() / 2**20
            save_json(directory / "vad_segments.json", vad_segments)
            result["vad_segment_count"] = len(vad_segments)
            result["speech_seconds"] = sum(end - start for start, end in vad_segments)
            torch.cuda.reset_peak_memory_stats()
            return detection, probabilities

        def asr(ids, waves):
            nonlocal phase
            phase = "aed"
            if unsafe.is_set():
                check_memory_headroom(args)
            synchronize()
            begin = time.perf_counter()
            cpu_begin = time.process_time()
            outputs = original_asr(ids, waves)
            synchronize()
            elapsed = time.perf_counter() - begin
            timings["aed_seconds"] = timings.get("aed_seconds", 0) + elapsed
            raw_outputs.extend(outputs)
            durations = [len(samples) / rate for rate, samples in waves]
            result["batches"].append({
                "index": len(result["batches"]) + 1, "uttids": ids,
                "input_seconds": durations, "batch_wall_seconds": elapsed,
                "batch_cpu_seconds": time.process_time() - cpu_begin,
                "cuda_allocated_mib": torch.cuda.memory_allocated() / 2**20,
                "cuda_reserved_mib": torch.cuda.memory_reserved() / 2**20,
                "output_count": len(outputs),
            })
            return outputs

        firered_transcriber.stream_decode_audio_to_wav = stream
        system.vad.detect, system.asr.transcribe = vad, asr
        last_report = 0.0

        def progress(fraction, message):
            nonlocal last_report
            now = time.perf_counter()
            if now - last_report >= 15 or fraction >= 1:
                last_report = now
                save_json(directory / "progress.json", {
                    "batch_size": args.worker, "fraction": fraction, "message": message,
                    "elapsed_seconds": now - started, "timings": timings,
                    "completed_batches": len(result["batches"]),
                    "memory": memory_snapshot(),
                })
                print(f"BS={args.worker} {fraction:.1%}: {message}", flush=True)

        print(f"BS={args.worker} model ready: {timings['model_load_seconds']:.2f}s", flush=True)
        phase = "pipeline"
        synchronize()
        begin = time.perf_counter()
        cpu_begin = time.process_time()
        torch.cuda.reset_peak_memory_stats()
        segments, duration_ms = firered_transcriber.transcribe_firered(
            system, args.video, progress, temporary_directory=directory,
        )
        synchronize()
        timings["pipeline_seconds"] = time.perf_counter() - begin
        timings["pipeline_cpu_seconds"] = time.process_time() - cpu_begin
        result["aed_cuda_peak_allocated_mib"] = torch.cuda.max_memory_allocated() / 2**20
        result["aed_cuda_peak_reserved_mib"] = torch.cuda.max_memory_reserved() / 2**20
        phase = "save"
        _write_result(directory, "firered", f"FireRed BS={args.worker}", segments, duration_ms)
        result.update(status="completed", duration_seconds=duration_ms / 1000,
                      subtitle_count=len(segments), word_count=sum(len(s.words) for s in segments))
        result["pipeline_rtf"] = timings["pipeline_seconds"] / result["duration_seconds"]
        result["pipeline_realtime_multiple"] = 1 / result["pipeline_rtf"]
        result["aed_speech_realtime_multiple"] = result["speech_seconds"] / timings["aed_seconds"]
        durations = [d for batch in result["batches"] for d in batch["input_seconds"]]
        result["input_segment_seconds"] = {
            "min": min(durations), "median": statistics.median(durations),
            "p95": percentile(durations, 0.95), "max": max(durations),
            "sum": sum(durations),
        }
        seconds = [batch["batch_wall_seconds"] for batch in result["batches"]]
        result["batch_seconds"] = {
            "median": statistics.median(seconds), "p95": percentile(seconds, 0.95),
            "max": max(seconds),
        }
    except BaseException as exc:
        result.update(status="failed", error=str(exc), traceback=traceback.format_exc())
        print(result["traceback"], flush=True)
    finally:
        if phase == "aed" and torch is not None and torch.cuda.is_available():
            result["aed_cuda_peak_allocated_mib"] = torch.cuda.max_memory_allocated() / 2**20
            result["aed_cuda_peak_reserved_mib"] = torch.cuda.max_memory_reserved() / 2**20
        phase = "done"
        result["timings"] = timings
        result["process_wall_seconds"] = time.perf_counter() - started
        result["final_memory"] = memory_snapshot()
        result["peak_process_working_mib"] = result["final_memory"].get("process_peak_working_mib")
        result["peak_process_private_commit_mib"] = result["final_memory"].get(
            "process_peak_private_commit_mib"
        )
        if torch is not None and torch.cuda.is_available():
            result["final_cuda_allocated_mib"] = torch.cuda.memory_allocated() / 2**20
            result["final_cuda_reserved_mib"] = torch.cuda.memory_reserved() / 2**20
        stop.set()
        memory_thread.join(timeout=2)
        gpu_process.terminate()
        gpu_process.wait(timeout=10)
        gpu_thread.join(timeout=2)
        gpu_process.stdout.close()
        result["resources_by_phase"] = {}
        for label in ("load", "audio", "vad", "aed"):
            ram = [s for s in memory_samples if s["phase"] == label]
            gpu = [s for s in gpu_samples if s["phase"] == label]
            data = {"ram_samples": len(ram), "gpu_samples": len(gpu)}
            for key in ("process_working_mib", "process_private_commit_mib"):
                data["peak_" + key] = max((s[key] for s in ram), default=None)
            for key in ("system_available_physical_mib", "system_available_commit_mib"):
                data["min_" + key] = min((s[key] for s in ram), default=None)
            for key in ("used_mib", "temperature_c", "power_w"):
                values = [s[key] for s in gpu if s[key] is not None]
                data["peak_device_" + key] = max(values, default=None)
            for key in ("utilization_percent", "power_w", "sm_clock_mhz"):
                values = [s[key] for s in gpu if s[key] is not None]
                data["mean_device_" + key] = statistics.mean(values) if values else None
            result["resources_by_phase"][label] = data
        save_json(directory / "resource_samples.json", {"ram": memory_samples, "gpu": gpu_samples})
        save_json(directory / "raw_outputs.json", raw_outputs)
        save_json(directory / "result.json", result)
        print(f"BS={args.worker} {result['status']}: {timings}", flush=True)
    return 0 if result["status"] == "completed" else 1


def summarize(args):
    trials = [
        json.loads((args.output / f"bs{bs}" / "result.json").read_text(encoding="utf-8"))
        for bs in args.batch_sizes if (args.output / f"bs{bs}" / "result.json").is_file()
    ]
    reference_path = args.output / "bs1" / "raw_outputs.json"
    reference = json.loads(reference_path.read_text(encoding="utf-8")) if reference_path.exists() else []
    reference_trial = next((trial for trial in trials if trial["batch_size"] == 1), {})
    lines = [
        "# FireRed Batch Size Benchmark", "", f"Source: `{args.video}`", "",
        "Fresh process per trial. Production VAD, beam=3, GPU half precision, timestamps enabled.",
        "Pipeline timing excludes model load, includes audio preparation/VAD/ASR/subtitle validation.",
        "CUDA peaks are process allocator measurements. Device samples include desktop/other apps.",
        "No manual transcripts: output comparison measures consistency, not recognition accuracy.", "",
        "| BS | Status | Load s | Audio s | VAD s | AED s | Pipeline s | Audio throughput x | AED CUDA allocated/reserved MiB | AED RAM MiB |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for trial in trials:
        bs = trial["batch_size"]
        if trial["status"] == "completed" and reference_trial.get("status") == "completed":
            outputs = json.loads((args.output / f"bs{bs}" / "raw_outputs.json").read_text(encoding="utf-8"))
            comparison = compare_outputs(reference, outputs)
            comparison["wav_identical"] = trial["wav_sha256"] == reference_trial["wav_sha256"]
            comparison["vad_identical"] = (
                json.loads((args.output / f"bs{bs}" / "vad_segments.json").read_text())
                == json.loads((args.output / "bs1" / "vad_segments.json").read_text())
            )
            trial["comparison_to_bs1"] = comparison
            save_json(args.output / f"bs{bs}" / "comparison.json", comparison)
            durations = [d for batch in trial["batches"] for d in batch["input_seconds"]]
            trial["derived_metrics"] = {
                "padding_duration_ratio": sum(
                    max(batch["input_seconds"]) * len(batch["input_seconds"])
                    for batch in trial["batches"]
                ) / sum(durations),
                "pipeline_speedup_over_bs1": (
                    reference_trial["timings"]["pipeline_seconds"] / trial["timings"]["pipeline_seconds"]
                ),
                "aed_speedup_over_bs1": (
                    reference_trial["timings"]["aed_seconds"] / trial["timings"]["aed_seconds"]
                ),
                "pipeline_time_reduction_percent": (
                    1 - trial["timings"]["pipeline_seconds"] / reference_trial["timings"]["pipeline_seconds"]
                ) * 100,
                "mean_pipeline_cpu_percent_all_logical_cores": (
                    trial["timings"]["pipeline_cpu_seconds"] / trial["timings"]["pipeline_seconds"]
                    / trial["hardware"]["logical_cpus"] * 100
                ),
            }
            trial["empty_text_utterances"] = sum(not item["text"].strip() for item in outputs)
            trial["returned_utterance_count"] = len(outputs)
        timing = trial.get("timings", {})
        def number(value):
            return "-" if value is None else f"{value:.2f}"
        values = [number(timing.get(key)) for key in (
            "model_load_seconds", "audio_preparation_seconds", "vad_seconds",
            "aed_seconds", "pipeline_seconds",
        )]
        allocated = number(trial.get("aed_cuda_peak_allocated_mib"))
        reserved = number(trial.get("aed_cuda_peak_reserved_mib"))
        ram = trial.get("resources_by_phase", {}).get("aed", {}).get("peak_process_working_mib")
        lines.append(f"| {bs} | {trial['status']} | {' | '.join(values)} | "
                     f"{number(trial.get('pipeline_realtime_multiple'))} | {allocated}/{reserved} | {number(ram)} |")
        if trial.get("error"):
            lines.extend(["", f"BS={bs} failure: {trial['error']}", ""])
    completed = [trial for trial in trials if trial["status"] == "completed"]
    if completed:
        first = completed[0]
        hardware = first["hardware"]
        lengths = first["input_segment_seconds"]
        lines.extend([
            "", "## Input And Configuration", "",
            f"- GPU: {hardware['gpu']} ({hardware['gpu_total_mib']:.1f} MiB).",
            f"- PyTorch: {hardware['torch']}; CUDA runtime: {hardware['torch_cuda']}.",
            f"- Model dtype: {first['model_dtype']}; PyTorch CPU threads: {hardware['cpu_threads']}.",
            f"- Source duration: {first['duration_seconds']:.3f}s; detected speech: {first['speech_seconds']:.3f}s.",
            f"- VAD segments: {first['vad_segment_count']}.",
            f"- Input lengths: min {lengths['min']:.2f}s, median {lengths['median']:.2f}s, "
            f"P95 {lengths['p95']:.2f}s, max {lengths['max']:.2f}s.",
            f"- Prepared PCM16 WAV: {first['wav_bytes']} bytes; SHA-256 `{first['wav_sha256']}`.",
            "- Settings: official VAD defaults, original segment order, beam=3, timestamps enabled, LID/punctuation disabled.",
            "- Experimental memory guard by trial: " + "; ".join(
                f"BS{trial['batch_size']} "
                + ("enabled" if trial.get("memory_guard", {}).get("enabled", True) else "disabled")
                for trial in trials
            ) + ".",
            "", "## Detailed Resources", "",
            "RAM working set is resident process memory. Private commit is not physical RAM.",
            "CUDA reserved includes allocator cache; it is not the same as allocated memory.",
            "Windows WDDM device memory/utilization includes other desktop applications.",
            "",
            "| BS | Process wall s | Whole-process peak RAM/private MiB | AED peak RAM/private MiB | "
            "AED device peak MiB | AED mean GPU % | Mean/peak power W | Peak temp C | Mean SM MHz |",
            "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ])
        for trial in completed:
            resource = trial["resources_by_phase"]["aed"]
            lines.append(
                f"| {trial['batch_size']} | {trial['process_wall_seconds']:.2f} | "
                f"{trial['peak_process_working_mib']:.1f}/{trial['peak_process_private_commit_mib']:.1f} | "
                f"{resource['peak_process_working_mib']:.1f}/{resource['peak_process_private_commit_mib']:.1f} | "
                f"{resource['peak_device_used_mib']:.0f} | {resource['mean_device_utilization_percent']:.1f} | "
                f"{resource['mean_device_power_w']:.1f}/{resource['peak_device_power_w']:.1f} | "
                f"{resource['peak_device_temperature_c']:.0f} | {resource['mean_device_sm_clock_mhz']:.0f} |"
            )
        lines.extend([
            "", "## Throughput And Batch Latency", "",
            "Audio throughput is full source duration / pipeline wall time; detected-speech throughput uses AED time only.",
            "Padding ratio is sum(batch maximum input duration * batch count) / actual input duration, "
            "not a measured compute multiplier.",
            "",
            "| BS | Pipeline speedup vs BS1 | Time saved % | Speech throughput x | Batch count | "
            f"Batch median/P95/max s | Padding ratio | Mean CPU % ({hardware['logical_cpus']} logical cores) |",
            "|---:|---:|---:|---:|---:|---:|---:|---:|",
        ])
        for trial in completed:
            metric, latency = trial["derived_metrics"], trial["batch_seconds"]
            lines.append(
                f"| {trial['batch_size']} | {metric['pipeline_speedup_over_bs1']:.3f} | "
                f"{metric['pipeline_time_reduction_percent']:.2f} | "
                f"{trial['aed_speech_realtime_multiple']:.2f} | {len(trial['batches'])} | "
                f"{latency['median']:.3f}/{latency['p95']:.3f}/{latency['max']:.3f} | "
                f"{metric['padding_duration_ratio']:.3f} | "
                f"{metric['mean_pipeline_cpu_percent_all_logical_cores']:.1f} |"
            )
        lines.extend([
            "", "## Output Consistency", "",
            "All nonempty outputs pass the production strict word/timestamp validation.",
            "Character edit rate is relative to BS1, NOT an accuracy/error rate against human transcription.",
            "Timestamp deltas below compare matching tokens only in utterances with identical text.",
            "",
            "| BS | WAV/VAD identical | Missing IDs | Changed-text utterances | Character edits/rate | "
            "Timestamp P95/max ms | Timestamp shape mismatches |",
            "|---:|---|---:|---:|---:|---:|---:|",
        ])
        for trial in completed:
            comp = trial["comparison_to_bs1"]
            p95_delta = comp["p95_matching_token_timestamp_delta_seconds"]
            max_delta = comp["max_matching_token_timestamp_delta_seconds"]
            p95_ms = "-" if p95_delta is None else f"{p95_delta * 1000:.1f}"
            max_ms = "-" if max_delta is None else f"{max_delta * 1000:.1f}"
            lines.append(
                f"| {trial['batch_size']} | {comp['wav_identical']}/{comp['vad_identical']} | "
                f"{len(comp['missing_utterance_ids'])} | {comp['changed_text_utterances']} | "
                f"{comp['character_edits']}/{comp['relative_character_edit_rate']:.2%} | "
                f"{p95_ms}/{max_ms} | "
                f"{len(comp['timestamp_shape_mismatch_utterance_ids'])} |"
            )
        for trial in completed:
            lines.append(
                f"\nBS={trial['batch_size']}: {len(trial['batches'])} batches, "
                f"{trial['returned_utterance_count']} "
                f"returned utterances, {trial['empty_text_utterances']} empty-text outputs, "
                f"{trial['subtitle_count']} subtitles, {trial['word_count']} timestamped tokens."
            )
        fastest = min(completed, key=lambda trial: trial["timings"]["pipeline_seconds"])
        fastest_temperature = fastest["resources_by_phase"]["aed"]["peak_device_temperature_c"]
        lines.extend([
            "", "## Interpretation And Limits", "",
            f"- One complete run per BS, table order {'/'.join(str(bs) for bs in args.batch_sizes)}; "
            "reused runs retain their original timestamps; no statistical confidence interval or accuracy ranking.",
            "- No application ASR settings were changed. Different background load/cache/thermal conditions can affect results.",
            "- Model-load RAM peaks and system headroom are recorded separately in result.json; "
            "some load-time commit/RAM minima are low and transient.",
            "- When enabled, the experimental guard checks initial physical/commit headroom (2048/4096 MiB), "
            "then flags sustained low headroom for ~2s and rechecks before the next batch. "
            "Default minimum physical/commit headroom is 1024/2048 MiB; overrides are stored in each result.json.",
            "- No GPU memory cap, no precision reduction, no timestamp disabling, no automatic smaller-BS retry.",
            f"- BS{fastest['batch_size']} is fastest in this trial, reaches {fastest_temperature:.0f}C, "
            f"and has a {fastest['batch_seconds']['max']:.2f}s slowest batch. "
            "Thermal throttling was not directly measured.",
            "- Review changed-text utterances against the source before claiming that larger batches preserve accuracy.",
            "", "## Files", "",
            "- summary.json: combined measurements, derived metrics, and output comparison.",
            "- bs*/result.json: exact config, timings, batch inputs/latencies, and phase resource summaries.",
            "- bs*/resource_samples.json: RAM at 0.25s, device GPU data at 1s.",
            "- bs*/firered.json / firered.srt / firered.txt: validated full-video output.",
            "- bs*/raw_outputs.json / comparison.json: per-utterance text, timestamps, and differences.",
            "- bs*/worker.log: complete official pipeline messages and errors.",
        ])
    chronological = sorted(trials, key=lambda trial: trial.get("started_at_utc", ""))
    summary = {"video": str(args.video), "created_at_utc": datetime.now(timezone.utc).isoformat(),
               "table_order": args.batch_sizes,
               "trial_order": [trial["batch_size"] for trial in chronological], "trials": trials}
    save_json(args.output / "summary.json", summary)
    (args.output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--batch-sizes", nargs="+", type=int, default=[1, 2, 4])
    parser.add_argument("--worker", type=int)
    parser.add_argument("--summarize-only", action="store_true")
    parser.add_argument("--reuse-completed", action="store_true",
                        help="Keep matching completed trials and run only missing batch sizes.")
    parser.add_argument("--minimum-free-physical-mib", type=float, default=1024)
    parser.add_argument("--minimum-free-commit-mib", type=float, default=2048)
    parser.add_argument("--disable-memory-guard", action="store_true",
                        help="Disable startup and sustained low-memory guards for this experiment only.")
    args = parser.parse_args()
    args.video, args.output = args.video.resolve(), args.output.resolve()
    if (not args.video.is_file() or any(bs < 1 for bs in args.batch_sizes)
            or len(set(args.batch_sizes)) != len(args.batch_sizes)
            or (not args.worker and 1 not in args.batch_sizes)
            or args.minimum_free_physical_mib <= 0 or args.minimum_free_commit_mib <= 0):
        parser.error("A local video and unique positive batch sizes including the BS1 reference are required.")
    args.output.mkdir(parents=True, exist_ok=True)
    if args.worker:
        return worker(args)
    if not args.summarize_only:
        from firered_runtime import runtime_python, runtime_is_ready
        if not runtime_is_ready():
            parser.error("FireRed runtime is not ready.")
        env = os.environ.copy()
        env.update({"PYTHONUTF8": "1", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"})
        for bs in args.batch_sizes:
            trial = args.output / f"bs{bs}"
            trial.mkdir(parents=True, exist_ok=True)
            if (trial / "result.json").exists():
                if args.reuse_completed:
                    reusable_trial(trial, args.video)
                    print(f"BS={bs} reuse completed trial (no new inference)", flush=True)
                    continue
                raise FileExistsError(f"Refusing to overwrite an existing trial: {trial}")
            command = [str(runtime_python()), "-u", str(Path(__file__).resolve()),
                       str(args.video), "--output", str(args.output), "--worker", str(bs),
                       "--minimum-free-physical-mib", str(args.minimum_free_physical_mib),
                       "--minimum-free-commit-mib", str(args.minimum_free_commit_mib)]
            if args.disable_memory_guard:
                command.append("--disable-memory-guard")
            with (trial / "worker.log").open("w", encoding="utf-8") as log:
                process = subprocess.Popen(
                    command, env=env, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                try:
                    for line in process.stdout:
                        log.write(line)
                        log.flush()
                        if line.startswith(("BS=", "Traceback", "RuntimeError", "ValueError")):
                            print(line.rstrip(), flush=True)
                    code = process.wait()
                except BaseException:
                    process.terminate()
                    process.wait(timeout=30)
                    raise
                finally:
                    process.stdout.close()
            if not (trial / "result.json").exists():
                save_json(trial / "result.json", {
                    "batch_size": bs, "status": "failed", "error": f"Worker exited with code {code}",
                })
            elif code:
                saved = json.loads((trial / "result.json").read_text(encoding="utf-8"))
                if saved["status"] == "running":
                    saved.update(status="interrupted", error=f"Worker exited with code {code}")
                    save_json(trial / "result.json", saved)
            time.sleep(10)
    summarize(args)
    results = json.loads((args.output / "summary.json").read_text(encoding="utf-8"))
    return 0 if all(t["status"] == "completed" for t in results["trials"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
