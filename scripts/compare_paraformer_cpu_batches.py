"""Interleave warm CPU ASR trials with identical VAD spans and native timestamps."""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import statistics
import sys
from threading import Event, Thread
import time
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def trial_order(batch_sizes, repeats):
    if repeats < 1 or not batch_sizes or any(size < 1 for size in batch_sizes):
        raise ValueError("Positive batch sizes and repetitions are required")
    if len(set(batch_sizes)) != len(batch_sizes):
        raise ValueError("Batch sizes must be distinct")
    return [size for index in range(repeats)
            for size in (batch_sizes if index % 2 == 0 else list(reversed(batch_sizes)))]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path)
    parser.add_argument("--recognizer", type=Path, required=True)
    parser.add_argument("--vad", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--batch-sizes", type=int, nargs="+", default=[1, 4])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--batch-order", choices=("length", "time"), default="time")
    args = parser.parse_args()
    if args.threads < 1:
        parser.error("CPU thread count must be positive")
    try:
        order = trial_order(args.batch_sizes, args.repeats)
    except ValueError as exc:
        parser.error(str(exc))
    os.environ.update({
        "CUDA_VISIBLE_DEVICES": "", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        "MODELSCOPE_OFFLINE": "1", "OMP_NUM_THREADS": str(args.threads),
        "MKL_NUM_THREADS": str(args.threads), "OPENBLAS_NUM_THREADS": str(args.threads),
        "OMP_WAIT_POLICY": "PASSIVE",
    })
    import numpy as np
    import torch
    from paraformer_transcriber import (
        detect_speech, load_paraformer_model, observe_encoder_batches,
        ordered_interval_indices, timestamp_segments,
    )
    from qwen_memory import ensure_memory_headroom, memory_snapshot
    from scripts.benchmark_paraformer_cpu import cpu_percent, save_json, summarize_resources

    if torch.version.cuda is not None or torch.cuda.is_available():
        raise RuntimeError("A CPU-only torch build is required")
    args.output.mkdir(parents=True, exist_ok=True)
    if (args.output / "result.json").exists():
        raise FileExistsError("Refusing to overwrite the batch comparison")
    model = load_paraformer_model(args.recognizer, args.vad, cpu_threads=args.threads)
    spans = detect_speech(model.vad, args.audio)
    input_order = ordered_interval_indices(spans, args.batch_order)
    ordered_spans = [spans[index] for index in input_order]
    with wave.open(str(args.audio), "rb") as source:
        duration = source.getnframes() / source.getframerate()
        audios = []
        for start, end in ordered_spans:
            source.setpos(start * 16)
            pcm = source.readframes((end - start) * 16)
            audios.append(np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768)
    # Warm both batch shapes before collecting the interleaved measurements.
    for bs in args.batch_sizes:
        model.recognizer.generate(input=audios[:bs], batch_size=bs, fs=16000,
                                  output_timestamp=True, pred_timestamp=True)
    logical = os.cpu_count() or 1
    trials = []
    result = {
        "status": "running", "audio": str(args.audio.resolve()), "audio_seconds": duration,
        "cpu_threads": args.threads, "logical_cpus": logical, "torch": torch.__version__,
        "cuda_available": False, "backend": "funasr-pytorch-cpu",
        "speech_intervals_ms": spans, "trials": trials,
        "batch_order": args.batch_order, "input_order": input_order,
        "scope": "Warm ASR + native timestamp validation; excludes load, decode and VAD.",
        "order": order,
    }
    save_json(args.output / "result.json", result)
    try:
        for index, bs in enumerate(result["order"]):
            samples, errors, stop = [], [], Event()

            def sample():
                previous_wall, previous_cpu = time.perf_counter(), time.process_time()
                try:
                    with (args.output / f"trial-{index + 1}-bs{bs}-samples.jsonl").open(
                            "w", encoding="utf-8") as output:
                        while not stop.wait(.5):
                            wall, cpu = time.perf_counter(), time.process_time()
                            values = memory_snapshot()
                            values.update({
                                "sample_seconds": wall - previous_wall,
                                "process_cpu_percent_all_logical_cores": cpu_percent(
                                    cpu - previous_cpu, wall - previous_wall, logical),
                            })
                            samples.append(values)
                            output.write(json.dumps(values) + "\n")
                            previous_wall, previous_cpu = wall, cpu
                except Exception as exc:
                    errors.append(str(exc))

            thread = Thread(target=sample, daemon=True)
            thread.start()
            started, cpu_started = time.perf_counter(), time.process_time()
            batch_sizes, encoder_sizes, count = [], [], 0
            texts = [""] * len(spans)
            try:
                for position in range(0, len(audios), bs):
                    ensure_memory_headroom(physical_mib=1024, commit_mib=2048)
                    group = audios[position:position + bs]
                    with observe_encoder_batches(model.recognizer) as observed:
                        outputs = model.recognizer.generate(
                            input=group, batch_size=bs, fs=16000,
                            output_timestamp=True, pred_timestamp=True)
                    if observed != [len(group)] or len(outputs) != len(group):
                        raise RuntimeError("Requested true encoder batch was not respected")
                    batch_sizes.append(len(group))
                    encoder_sizes.extend(observed)
                    for original_index, span, output in zip(
                        input_order[position:position + bs],
                        ordered_spans[position:position + bs], outputs,
                    ):
                        segments = timestamp_segments(output, span[0], span[1] - span[0])
                        texts[original_index] = output.get("text", "")
                        count += sum(len(segment.words) for segment in segments)
            finally:
                elapsed, cpu_elapsed = time.perf_counter() - started, time.process_time() - cpu_started
                stop.set()
                thread.join(timeout=5)
            if errors:
                raise RuntimeError(errors[0])
            trial = {
                "index": index + 1, "batch_size": bs, "wall_seconds": elapsed,
                "cpu_seconds": cpu_elapsed, "rtf": elapsed / duration, "word_count": count,
                "batch_sizes": batch_sizes, "encoder_batch_sizes": encoder_sizes,
                "mean_cpu_percent_all_logical_cores": cpu_percent(cpu_elapsed, elapsed, logical),
                "resources": summarize_resources(samples), "raw_texts": texts,
            }
            trials.append(trial)
            save_json(args.output / "result.json", result)
            print(f"Warm trial {index + 1} BS{bs}: {elapsed:.3f}s; words={count}", flush=True)
        summary = {}
        for bs in args.batch_sizes:
            values = [trial for trial in trials if trial["batch_size"] == bs]
            summary[str(bs)] = {
                "median_seconds": statistics.median(trial["wall_seconds"] for trial in values),
                "min_seconds": min(trial["wall_seconds"] for trial in values),
                "max_seconds": max(trial["wall_seconds"] for trial in values),
                "peak_sampled_ram_mib": max(
                    trial["resources"]["peak_process_working_mib"] for trial in values),
                "median_cpu_percent": statistics.median(
                    trial["mean_cpu_percent_all_logical_cores"] for trial in values),
                "token_counts": dict(Counter(trial["word_count"] for trial in values)),
            }
        result.update(status="completed", summary=summary)
        print(json.dumps(summary, indent=2), flush=True)
    except Exception as exc:
        result.update(status="failed", error=str(exc))
        raise
    finally:
        save_json(args.output / "result.json", result)


if __name__ == "__main__":
    main()
