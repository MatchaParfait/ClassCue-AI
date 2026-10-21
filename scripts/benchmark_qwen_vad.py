"""Compare full-file boundary planning and Qwen inference on a common excerpt."""

import argparse
from dataclasses import asdict
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
from qwen_vad import detect_speech


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sample-start", type=float, default=600)
    parser.add_argument("--sample-seconds", type=float, default=360)
    parser.add_argument("--chunk-seconds", type=float, default=90)
    parser.add_argument("--batch-size", type=int, default=2)
    parser.add_argument("--modes", nargs="+", choices=["energy", "firered"],
                        default=["energy", "firered"])
    parser.add_argument("--boundaries-only", action="store_true")
    args = parser.parse_args()
    if args.sample_start < 0 or args.sample_seconds <= 0 or args.batch_size < 1:
        parser.error("Invalid sample range or batch size")
    import numpy as np
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    timeline = out / "timeline.wav"
    begin = time.perf_counter()
    extract_audio(args.video, timeline)
    extraction_seconds = time.perf_counter() - begin
    with wave.open(str(timeline), "rb") as source:
        rate = source.getframerate()
        total = source.getnframes()
        raw = source.readframes(total)
    audio = np.frombuffer(raw, dtype="<i2").astype("float32") / 32768
    start = round(args.sample_start * rate)
    end = min(total, start + round(args.sample_seconds * rate))
    if end <= start:
        raise ValueError("Sample starts beyond the source")
    sample_path = out / "sample.wav"
    with wave.open(str(sample_path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(raw[start * 2:end * 2])
    summary = {
        "video": str(args.video.resolve()), "full_audio_seconds": total / rate,
        "sample_start_seconds": start / rate, "sample_audio_seconds": (end - start) / rate,
        "chunk_seconds": args.chunk_seconds, "batch_size": args.batch_size,
        "extraction_seconds": extraction_seconds, "modes": [],
        "sample_audio": str(sample_path), "scope": "CPU VAD full file, Qwen excerpt only",
    }
    for mode in args.modes:
        print(f"Boundary benchmark: {mode}", flush=True)
        try:
            vad = None
            begin = time.perf_counter()
            if mode != "energy":
                vad = detect_speech(timeline, mode)
            vad_wall = time.perf_counter() - begin if vad else 0
            intervals = vad["speech_intervals"] if vad else None
            begin = time.perf_counter()
            full_spans = plan_spans(audio, rate, args.chunk_seconds, mode, intervals)
            planning_seconds = time.perf_counter() - begin
            (out / f"{mode}_full_plan.json").write_text(json.dumps({
                "vad": vad, "spans": [asdict(s) for s in full_spans],
                "planning_seconds": planning_seconds,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            cropped = None if intervals is None else [
                [max(a, start / rate) - start / rate, min(b, end / rate) - start / rate]
                for a, b in intervals if b > start / rate and a < end / rate
            ]
            sample_spans = plan_spans(
                audio[start:end], rate, args.chunk_seconds, mode, cropped,
            )
            summary["modes"].append({
                "mode": mode, "status": "ready", "vad_wall_seconds": vad_wall,
                "vad_load_seconds": vad.get("load_seconds") if vad else 0,
                "vad_inference_seconds": vad.get("inference_seconds") if vad else 0,
                "planning_seconds": planning_seconds,
                "full_chunk_count": len(full_spans),
                "fallback_count": sum(s.reason == "energy-fallback" for s in full_spans),
                "spans": [asdict(s) for s in sample_spans],
            })
        except Exception as exc:
            summary["modes"].append({"mode": mode, "status": "unavailable", "error": str(exc)})
            print(f"{mode}: unavailable: {exc}", flush=True)
        (out / "boundary_timings.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    if args.boundaries_only:
        return 0
    ready = dict(summary)
    ready["modes"] = [entry for entry in summary["modes"] if entry["status"] == "ready"]
    plan_path = out / "inference_plan.json"
    plan_path.write_text(json.dumps(ready, ensure_ascii=False, indent=2), encoding="utf-8")
    env = os.environ.copy()
    env.update({"PYTHONUTF8": "1", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"})
    command = [str(runtime_python()), "-u", str(ROOT / "scripts" / "qwen_benchmark_worker.py"),
               "--plan", str(plan_path), "--output", str(out)]
    result = subprocess.run(
        command, env=env, cwd=ROOT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
