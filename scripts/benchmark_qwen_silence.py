"""Compare Qwen with/without silence compaction using identical VAD boundaries."""

import argparse
from dataclasses import asdict
import hashlib
import gc
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from media import extract_audio
from qwen_boundaries import plan_spans
from qwen_runtime import runtime_python
from qwen_silence import build_silence_plan
from qwen_vad import detect_speech


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--audio", type=Path, help="Reuse a previously extracted source WAV")
    parser.add_argument("--sample-start", type=float, default=0)
    parser.add_argument("--sample-seconds", type=float)
    parser.add_argument("--chunk-seconds", type=float, default=90)
    parser.add_argument("--batch-size", type=int, default=2)
    parser.add_argument("--speech-padding-seconds", type=float, default=.3)
    parser.add_argument("--min-removed-seconds", type=float, default=1.5)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--reverse-order", action="store_true")
    parser.add_argument("--cpu-threads", type=int, default=4)
    parser.add_argument("--staged", action="store_true", help="Run ASR and alignment in separate sequential processes")
    parser.add_argument("--condition", choices=["both", "skip", "no-skip"], default="both")
    args = parser.parse_args()
    if args.sample_start < 0 or args.batch_size < 1 or (
        args.sample_seconds is not None and args.sample_seconds <= 0
    ):
        parser.error("Invalid sample range or batch size")
    return run_trial(args)


def run_trial(args):
    import numpy as np

    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    timeline = args.audio.resolve() if args.audio else out / "timeline.wav"
    started = time.perf_counter()
    if not args.audio:
        extract_audio(args.video, timeline)
    extraction_seconds = time.perf_counter() - started if not args.audio else 0
    with wave.open(str(timeline), "rb") as source:
        if (source.getframerate(), source.getnchannels(), source.getsampwidth()) != (16000, 1, 2):
            raise ValueError("Expected 16kHz mono PCM16 source WAV")
        rate, total = source.getframerate(), source.getnframes()
        raw = source.readframes(total)
    with timeline.open("rb") as source:
        audio_hash = hashlib.file_digest(source, "sha256").hexdigest()
    audio = np.frombuffer(raw, dtype="<i2").astype("float32")
    audio /= 32768
    print("Running full-source CPU FireRedVAD once for both conditions", flush=True)
    vad = detect_speech(timeline, "firered")
    started = time.perf_counter()
    full_spans = plan_spans(audio, rate, args.chunk_seconds, "firered", vad["speech_intervals"])
    full_silence = build_silence_plan(
        full_spans, total, rate, vad["speech_intervals"], enabled=True,
        padding_seconds=args.speech_padding_seconds, min_removed_seconds=args.min_removed_seconds,
    )
    preparation_seconds = time.perf_counter() - started
    (out / "silence_full_plan.json").write_text(json.dumps({
        "vad": vad, "spans": [asdict(s) for s in full_spans], "silence": full_silence,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    start = round(args.sample_start * rate)
    end = total if args.sample_seconds is None else min(total, start + round(args.sample_seconds * rate))
    if start >= end:
        raise ValueError("Sample starts beyond the source")
    if start == 0 and end == total:
        sample_path = timeline
    else:
        sample_path = out / "sample.wav"
        with wave.open(str(sample_path), "wb") as sample:
            sample.setparams((1, 2, rate, 0, "NONE", "not compressed"))
            sample.writeframes(raw[start * 2:end * 2])
    cropped = [
        [max(a, start / rate) - start / rate, min(b, end / rate) - start / rate]
        for a, b in vad["speech_intervals"] if b > start / rate and a < end / rate
    ]
    spans = plan_spans(audio[start:end], rate, args.chunk_seconds, "firered", cropped)
    modes = []
    sample_plans = {}
    for name, enabled in (("no-skip", False), ("skip", True)):
        if args.condition not in ("both", name):
            continue
        silence = build_silence_plan(
            spans, end - start, rate, cropped, enabled=enabled,
            padding_seconds=args.speech_padding_seconds, min_removed_seconds=args.min_removed_seconds,
        )
        sample_plans[name] = silence
        modes.append({
            "mode": name, "skip_non_speech": enabled,
            "spans": [asdict(s) for s in spans], "chunks": silence["chunks"],
        })
    plan = {
        "video": str(args.video.resolve()), "source_audio": str(timeline),
        "source_audio_sha256": audio_hash, "source_audio_reused": bool(args.audio),
        "full_audio_seconds": total / rate, "sample_audio": str(sample_path),
        "sample_start_seconds": start / rate, "sample_audio_seconds": (end - start) / rate,
        "sample_is_full_source": start == 0 and end == total,
        "sample_rate": rate, "chunk_seconds": args.chunk_seconds, "batch_size": args.batch_size,
        "extraction_seconds": extraction_seconds,
        "vad_wall_seconds": vad["worker_wall_seconds"],
        "vad_load_seconds": vad["load_seconds"], "vad_inference_seconds": vad["inference_seconds"],
        "preparation_seconds": preparation_seconds,
        "silence_parameters": full_silence["parameters"],
        "sample_silence_plans": sample_plans, "modes": modes,
        "scope": "same FireRed+energy boundaries, with/without skipping; original-time subtitles",
    }
    plan_path = out / "inference_plan.json"
    if hasattr(args, "audio_profile"):
        plan["routing"] = {
            "audio_profile": args.audio_profile,
            "skip_non_speech": args.condition == "skip",
        }
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "source_seconds": total / rate, "full_removed_seconds": full_silence["removed_samples"] / rate,
        "deleted_regions": len(full_silence["deleted_regions"]),
        "sample_seconds": (end - start) / rate,
        "sample_removed_seconds": sample_plans.get("skip", sample_plans.get("no-skip"))["removed_samples"] / rate,
    }), flush=True)
    if args.prepare_only:
        return 0
    if args.staged:
        from asr_models import component_paths
        from qwen_resilience import (
            QwenOptions, atomic_json, digest, file_digest, model_identity, read_json, run_prepared,
        )
        from scripts.compare_asr import _write_result
        from processing_control import ResourcePaused
        paths = component_paths("qwen3-asr-1.7b")
        entries = list(reversed(modes)) if args.reverse_order else modes
        del raw, audio
        gc.collect()
        executions = []
        for entry in entries:
            options = QwenOptions(
                mode="manual", batch_size=args.batch_size, chunk_seconds=int(args.chunk_seconds),
                align_batch_size=args.batch_size, cpu_threads=args.cpu_threads,
                skip_non_speech=entry["skip_non_speech"], auto_recovery=False,
            )
            identity = {
                "version": 1, "options": asdict(options),
                "audio_sha256": file_digest(sample_path),
                "model": model_identity(paths), "chunks": entry["chunks"],
            }
            directory = out / "jobs" / digest(identity)
            directory.mkdir(parents=True, exist_ok=True)
            manifest = {
                "job_id": digest(identity), "identity": identity, "timeline": str(sample_path),
                "total_samples": end - start, "rate": rate, "chunks": entry["chunks"],
                "paths": {key: str(value) for key, value in paths.items()},
                "vad": {"speech_intervals": cropped}, "preparation_seconds": preparation_seconds,
            }
            atomic_json(directory / "manifest.json", manifest)
            try:
                from filelock import FileLock
                with FileLock(str(directory / "run.lock"), timeout=0):
                    segments, duration = run_prepared(
                        directory, manifest, options,
                        progress=lambda fraction, message: print(f"{fraction:.1%} {message}", flush=True),
                    )
            except ResourcePaused as error:
                print(str(error), flush=True)
                return 2
            _write_result(out, f"qwen-{entry['mode']}", "Qwen staged GPU", segments, duration)
            executions.append({"mode": entry["mode"], **read_json(directory / "state.json"),
                               "cache_directory": str(directory)})
        atomic_json(out / "execution.json", {
            "modes": executions, "stages_never_overlap": True,
            "scope": "shared resilient GPU ASR then GPU alignment",
        })
        return 0
    env = os.environ.copy()
    env.update({"PYTHONUTF8": "1", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"})
    del raw, audio
    gc.collect()
    with (out / "inference.log").open("w", encoding="utf-8") as log:
        for stage in (("asr", "align") if args.staged else (None,)):
            script = "qwen_staged_worker.py" if stage else "qwen_benchmark_worker.py"
            command = [
                str(runtime_python()), "-u", str(ROOT / "scripts" / script),
                "--plan", str(plan_path), "--output", str(out), "--cpu-threads", str(args.cpu_threads),
            ]
            if stage:
                command.extend(["--stage", stage])
            if args.reverse_order:
                command.append("--reverse-order")
            process = subprocess.Popen(
                command, env=env, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            for line in process.stdout:
                log.write(line)
                log.flush()
                print(line.rstrip(), flush=True)
            if process.wait():
                return process.returncode
    if args.staged:
        merge_staged_results(out)
    return 0


def merge_staged_results(out):
    asr = json.loads((out / "asr_timings.json").read_text(encoding="utf-8"))
    align = json.loads((out / "align_timings.json").read_text(encoding="utf-8"))
    results = []
    for first in asr["modes"]:
        second = next(e for e in align["modes"] if e["mode"] == first["mode"])
        item = {
            **first, "wall_seconds": first["wall_seconds"] + second["wall_seconds"],
            "asr_seconds": first["stage_seconds"], "align_seconds": second["stage_seconds"],
            "segment_count": second["segment_count"], "mapping_warnings": second["mapping_warnings"],
            "batch_memory": first["batch_memory"] + second["batch_memory"],
            "asr_batch_memory": first["batch_memory"], "align_batch_memory": second["batch_memory"],
        }
        item["rtf"] = item["wall_seconds"] / first["audio_seconds"]
        for key in ("peak_process_working_mib", "peak_process_private_commit_mib"):
            item[key] = max(first[key], second[key])
        for key in ("min_system_available_physical_mib", "min_system_available_commit_mib"):
            item[key] = min(first[key], second[key])
        results.append(item)
    summary = {
        **asr, "stage": "staged", "load_seconds": asr["load_seconds"] + align["load_seconds"],
        "asr_load_seconds": asr["load_seconds"], "align_load_seconds": align["load_seconds"],
        "scope": "ASR then CPU alignment in non-overlapping processes, load/warmup excluded",
        "modes": results,
    }
    (out / "qwen_timings.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    plan_path = out / "inference_plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8")) if plan_path.is_file() else {}
    for result in results:
        path = out / f"qwen-{result['mode']}.json"
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload.update({
                "timestamp_axis": "original_sample",
                "sample_start_in_source_seconds": plan.get("sample_start_seconds", 0),
                "skip_non_speech": result["mode"] == "skip", "staged": True,
                "asr_input_seconds": result.get("asr_input_seconds"),
            })
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "mapping_warnings.json").write_text(json.dumps({
        result["mode"]: result["mapping_warnings"] for result in results
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    first = json.loads((out / "cpu_memory_samples_asr.json").read_text(encoding="utf-8"))
    second = json.loads((out / "cpu_memory_samples_align.json").read_text(encoding="utf-8"))
    offset = first["samples"][-1]["elapsed_seconds"] if first["samples"] else 0
    samples = [{**s, "phase": "asr"} for s in first["samples"]] + [
        {**s, "phase": "align", "elapsed_seconds": s["elapsed_seconds"] + offset}
        for s in second["samples"]
    ]
    (out / "cpu_memory_samples.json").write_text(json.dumps({
        "baseline": first["baseline"], "samples": samples, "stages_never_overlap": True,
    }, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
