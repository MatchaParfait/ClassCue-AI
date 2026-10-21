"""Measure official Nano/Dolphin CTC inference, real batches and resource use."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import importlib.metadata
from importlib import import_module
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
from threading import Event, Thread
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.benchmark_paraformer_cpu import save_json, parse_gpu_sample, summarize_gpu, validate_output
from scripts.benchmark_firered_cpu import cpu_percent, summarize_resources, system_cpu_times


def write_transcript(directory, segments, duration_ms, *, engine="nano"):
    model_id = "dolphin-small-cn" if engine == "dolphin" else "fun-asr-nano-2512"
    save_json(directory / f"{engine}.json", {"model_id": model_id, "duration_ms": duration_ms,
                                       "segments": [asdict(segment) for segment in segments]})

    def stamp(ms):
        seconds, millis = divmod(ms, 1000)
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"

    subtitles = [f"{i}\n{stamp(c.start_ms)} --> {stamp(c.end_ms)}\n{c.text}"
                 for i, c in enumerate(segments, 1)]
    (directory / f"{engine}.srt").write_text("\n\n".join(subtitles) + "\n", encoding="utf-8")
    (directory / f"{engine}.txt").write_text("\n".join(c.text for c in segments) + "\n", encoding="utf-8")


def run(args):
    from paraformer_transcriber import runtime_environment

    os.environ.update(runtime_environment(args.device, args.threads))
    from qwen_memory import memory_snapshot, ensure_memory_headroom
    from asr_models import component_paths

    model_id = "dolphin-small-cn" if args.engine == "dolphin" else "fun-asr-nano-2512"
    paths = component_paths(model_id)
    args.output.mkdir(parents=True, exist_ok=True)
    if (args.output / "result.json").exists():
        raise FileExistsError("Refusing to overwrite an existing benchmark")
    started, phase = time.perf_counter(), "imports"
    audit, timings, samples, gpu_samples, errors = {}, {}, [], [], []
    stop = Event()
    gpu_process = gpu_thread = None
    torch = None
    result = {"status": "running", "model_id": model_id,
              "started_at_utc": datetime.now(timezone.utc).isoformat(), "pid": os.getpid(),
              "video": str(args.video), "source_bytes": args.video.stat().st_size,
              "batch_size": args.batch_size, "device": args.device, "cpu_threads": args.threads,
              "audit": audit, "timings": timings, "cuda_memory": {}, "sampling_seconds": .5,
              "baseline_memory": memory_snapshot()}
    save_json(args.output / "result.json", result)

    def sample():
        wall, cpu, system = time.perf_counter(), time.process_time(), system_cpu_times()
        try:
            with (args.output / "resource_samples.jsonl").open("w", encoding="utf-8") as log:
                while not stop.wait(.5):
                    now, current_cpu, current_system = time.perf_counter(), time.process_time(), system_cpu_times()
                    value = {**memory_snapshot(), "elapsed_seconds": now - started,
                             "phase": phase, "sample_seconds": now - wall,
                             "process_cpu_percent_all_logical_cores": cpu_percent(current_cpu - cpu, now - wall, os.cpu_count() or 1)}
                    if system and current_system:
                        total = current_system[1] - system[1]
                        value["system_cpu_percent"] = 100 * (1 - (current_system[0] - system[0]) / total) if total else 0
                    samples.append(value)
                    log.write(json.dumps(value) + "\n")
                    log.flush()
                    wall, cpu, system = now, current_cpu, current_system
        except Exception:
            errors.append(traceback.format_exc())

    thread = Thread(target=sample, daemon=True)
    thread.start()
    try:
        if args.device == "gpu":
            if os.environ.get("CUDA_VISIBLE_DEVICES") not in (None, "0"):
                raise ValueError("GPU telemetry requires unmapped CUDA device 0")
            gpu_process = subprocess.Popen(
                ["nvidia-smi", "-i", "0", "--query-gpu=memory.used,memory.free,utilization.gpu,temperature.gpu,power.draw",
                 "--format=csv,noheader,nounits", "-lms", "500"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)

            def sample_gpu():
                try:
                    with (args.output / "gpu_samples.jsonl").open("w", encoding="utf-8") as log:
                        for line in gpu_process.stdout:
                            value = {**parse_gpu_sample(line), "elapsed_seconds": time.perf_counter() - started, "phase": phase}
                            gpu_samples.append(value)
                            log.write(json.dumps(value) + "\n")
                            log.flush()
                except Exception:
                    errors.append(traceback.format_exc())

            gpu_thread = Thread(target=sample_gpu, daemon=True)
            gpu_thread.start()
        ensure_memory_headroom(physical_mib=1024, commit_mib=2048)
        import torch
        import psutil
        nano = import_module(f"{args.engine}_transcriber")

        timings["imports"] = time.perf_counter() - started
        processor = platform.processor()
        if os.name == "nt":
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
                processor = winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
        result["hardware"] = {"processor": processor, "physical_cpus": psutil.cpu_count(logical=False),
                              "logical_cpus": os.cpu_count(), "python": sys.version,
                              "torch": torch.__version__, "torch_cuda": torch.version.cuda,
                              "cuda_available": torch.cuda.is_available(),
                              "versions": {name: importlib.metadata.version(name) for name in (
                                  ("funasr", "dataoceanai-dolphin", "torchaudio") if args.engine == "dolphin"
                                  else ("funasr", "transformers", "torchaudio"))}}
        if args.device == "cpu" and (torch.version.cuda is not None or torch.cuda.is_available()):
            raise ValueError("CPU benchmark requires a CPU-only wheel and hidden CUDA")
        if args.device == "gpu":
            result["hardware"].update(gpu_name=torch.cuda.get_device_name(0), gpu_total_memory_mib=torch.cuda.get_device_properties(0).total_memory / 2**20)
            torch.cuda.reset_peak_memory_stats()
        phase = "load"
        begin = time.perf_counter()
        model = getattr(nano, f"load_{args.engine}_model")(
            paths["recognizer"], paths["vad"], device=args.device, cpu_threads=args.threads)
        if args.engine == "dolphin":
            if args.dolphin_decoder:
                model.decoder_mode = args.dolphin_decoder
            model.profile_decode = args.dolphin_profile
        nano.synchronize_device(model)
        timings["load"] = time.perf_counter() - begin
        recognizer_module = model.recognizer if args.engine == "dolphin" else model.recognizer.model
        result["component_devices"] = {name: sorted({str(p.device) for p in component.parameters()})
                                       for name, component in (("asr", recognizer_module), ("vad", model.vad.model))}
        result["parameter_count"] = sum(p.numel() for p in recognizer_module.parameters())
        result["parameter_dtypes"] = dict(Counter(str(p.dtype) for p in recognizer_module.parameters()))
        result["actual_cpu_threads"] = torch.get_num_threads()
        if args.device == "gpu":
            result["cuda_memory"].update(load_peak_allocated_mib=torch.cuda.max_memory_allocated() / 2**20,
                                         load_peak_reserved_mib=torch.cuda.max_memory_reserved() / 2**20)
            torch.cuda.reset_peak_memory_stats()
        generate_owner = nano if args.engine == "dolphin" else model.recognizer
        generate_name = "generate_batch" if args.engine == "dolphin" else "generate"
        original_decode, original_vad = nano.stream_decode_audio_to_wav, nano.detect_speech
        original_generate = getattr(generate_owner, generate_name)
        if args.seed is not None:
            torch.manual_seed(args.seed)
            result["seed"] = args.seed

        def decode(*a, **kw):
            nonlocal phase
            phase = "audio"
            return original_decode(*a, **kw)

        def vad(*a, **kw):
            nonlocal phase
            phase = "vad"
            return original_vad(*a, **kw)

        def generate(*a, **kw):
            nonlocal phase
            phase = "asr"
            ensure_memory_headroom(physical_mib=1024, commit_mib=2048)
            value = original_generate(*a, **kw)
            nano.synchronize_device(model)
            phase = "validation"
            return value

        def progress(fraction, message):
            save_json(args.output / "progress.json", {"fraction": fraction, "message": message,
                      "elapsed_seconds": time.perf_counter() - started, "stage_seconds": audit.get("stage_seconds", {})})
            save_json(args.output / "result.json", result)
            print(f"{fraction:.1%} {message}", flush=True)

        nano.stream_decode_audio_to_wav, nano.detect_speech = decode, vad
        setattr(generate_owner, generate_name, generate)
        audit["raw_output_path"] = str(args.output / "raw_outputs.jsonl")
        begin, cpu = time.perf_counter(), time.process_time()
        try:
            segments, duration_ms = getattr(nano, f"transcribe_{args.engine}")(
                model, args.video, progress, batch_size=args.batch_size,
                temporary_directory=args.output, audit=audit)
        finally:
            timings["pipeline"] = time.perf_counter() - begin
            timings["pipeline_cpu_seconds"] = time.process_time() - cpu
            nano.stream_decode_audio_to_wav, nano.detect_speech = original_decode, original_vad
            setattr(generate_owner, generate_name, original_generate)
        phase = "save"
        result.update(validate_output(segments, duration_ms, audit))
        write_transcript(args.output, segments, duration_ms, engine=args.engine)
        result.update(status="completed", duration_seconds=duration_ms / 1000,
                      pipeline_rtf=timings["pipeline"] / (duration_ms / 1000))
        phase = "hash"
        with args.video.open("rb") as source:
            result["source_sha256"] = hashlib.file_digest(source, "sha256").hexdigest()
    except Exception as exc:
        result.update(status="failed", error=str(exc), traceback=traceback.format_exc())
        print(result["traceback"], flush=True)
    finally:
        stop.set()
        thread.join(timeout=5)
        if gpu_process is not None:
            gpu_process.terminate()
            gpu_process.wait(timeout=10)
            gpu_thread.join(timeout=5)
        if torch is not None and args.device == "gpu" and torch.cuda.is_initialized():
            result["cuda_memory"].update(pipeline_peak_allocated_mib=torch.cuda.max_memory_allocated() / 2**20,
                                         pipeline_peak_reserved_mib=torch.cuda.max_memory_reserved() / 2**20)
        if timings.get("pipeline"):
            result["mean_pipeline_cpu_percent_all_logical_cores"] = cpu_percent(timings["pipeline_cpu_seconds"], timings["pipeline"], os.cpu_count() or 1)
        result["process_wall_seconds"] = time.perf_counter() - started
        result["final_memory"] = memory_snapshot()
        result["resources"] = summarize_resources(samples)
        phases = sorted({s["phase"] for s in samples + gpu_samples})
        result["resources_by_phase"] = {p: summarize_resources([s for s in samples if s["phase"] == p]) for p in phases}
        result["gpu_resources"] = summarize_gpu(gpu_samples)
        result["gpu_by_phase"] = {p: summarize_gpu([s for s in gpu_samples if s["phase"] == p]) for p in phases}
        result["sampling_errors"] = errors
        if errors or (args.device == "gpu" and not gpu_samples):
            result["status"] = "failed"
        save_json(args.output / "result.json", result)
        print(json.dumps({"status": result["status"], "timings": timings, "stage_seconds": audit.get("stage_seconds")}), flush=True)
    return int(result["status"] != "completed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=6)
    parser.add_argument("--device", choices=("cpu", "gpu"), default="gpu")
    parser.add_argument("--threads", type=int, default=16)
    parser.add_argument("--engine", choices=("nano", "dolphin"), default="nano")
    parser.add_argument("--dolphin-decoder", choices=("official", "host-search"))
    parser.add_argument("--dolphin-profile", action="store_true")
    parser.add_argument("--seed", type=int)
    args = parser.parse_args()
    args.video, args.output = args.video.resolve(), args.output.resolve()
    if not args.video.is_file() or args.batch_size < 1 or args.threads < 1:
        parser.error("A local audio/video file and positive batch/thread counts are required")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
