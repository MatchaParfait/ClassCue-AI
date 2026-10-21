"""Measure the actual CPU/CUDA Paraformer pipeline, including native timestamps."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import csv
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import sys
import subprocess
from threading import Event, Thread
import time
import traceback
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.benchmark_firered_cpu import (
    cpu_percent, summarize_resources, system_cpu_times,
)


def parse_gpu_sample(line):
    fields = ("device_used_mib", "device_free_mib", "device_utilization_percent",
              "temperature_c", "power_w")
    parts = line.strip().split(",")
    if len(parts) != len(fields):
        raise ValueError("Unexpected NVIDIA telemetry columns")
    values = {}
    for field, value in zip(fields, parts):
        try:
            values[field] = float(value.strip())
        except ValueError:
            values[field] = None
    return values


def summarize_gpu(samples):
    values = [s["device_utilization_percent"] for s in samples
              if s.get("device_utilization_percent") is not None]
    memory = [s["device_used_mib"] for s in samples if s.get("device_used_mib") is not None]
    return {
        "sample_count": len(samples),
        "mean_device_utilization_percent": sum(values) / len(values) if values else None,
        "peak_device_utilization_percent": max(values) if values else None,
        "peak_device_used_mib": max(memory) if memory else None,
    }


def save_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    # Windows readers can briefly hold the destination without delete-sharing.
    for attempt in range(10):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(.05 * (attempt + 1))


def prepare_sample(source, target, *, start_seconds=600, duration_seconds=300):
    with wave.open(str(source), "rb") as audio:
        rate = audio.getframerate()
        start, count = round(start_seconds * rate), round(duration_seconds * rate)
        if (start < 0 or count < 1 or start + count > audio.getnframes()
                or audio.getnchannels() != 1 or audio.getsampwidth() != 2):
            raise ValueError("Sample must be within a mono PCM16 timeline")
        audio.setpos(start)
        with wave.open(str(target), "wb") as output:
            output.setparams(audio.getparams())
            output.writeframes(audio.readframes(count))


def write_transcript(directory, segments, duration_ms):
    payload = {"model_id": "paraformer-large-cpu", "duration_ms": duration_ms,
               "segments": [asdict(segment) for segment in segments]}
    save_json(directory / "paraformer.json", payload)

    def stamp(milliseconds):
        hours, rest = divmod(milliseconds, 3_600_000)
        minutes, rest = divmod(rest, 60_000)
        seconds, millis = divmod(rest, 1000)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"

    subtitles, text = [], []
    for index, segment in enumerate(segments, 1):
        subtitles.append(
            f"{index}\n{stamp(segment.start_ms)} --> {stamp(segment.end_ms)}\n{segment.text}"
        )
        text.append(f"[{stamp(segment.start_ms)} --> {stamp(segment.end_ms)}] {segment.text}")
    (directory / "paraformer.srt").write_text("\n\n".join(subtitles) + "\n", encoding="utf-8")
    (directory / "paraformer.txt").write_text("\n".join(text) + "\n", encoding="utf-8")


def validate_output(segments, duration_ms, audit):
    batch_sizes = audit["batch_sizes"]
    expected = audit["batch_size"]
    targets = [batch.get("target_batch_size", expected) for batch in audit.get("batches", [])]
    if not targets:
        targets = [expected] * len(batch_sizes)
    if len(targets) != len(batch_sizes) or any(
        not 1 <= target <= expected for target in targets
    ):
        raise ValueError("Invalid ASR target batch size")
    if any(size != target for size, target in zip(batch_sizes[:-1], targets[:-1])):
        raise ValueError("A nonfinal ASR batch did not contain the requested utterance count")
    if batch_sizes and not 1 <= batch_sizes[-1] <= targets[-1]:
        raise ValueError("Invalid final ASR batch size")
    if "input_order" in audit:
        order = audit["input_order"]
        if sorted(order) != list(range(len(audit["speech_intervals_ms"]))):
            raise ValueError("ASR input order omitted or duplicated a VAD interval")
        observed = [index for batch in audit["batches"] for index in batch["original_indices"]]
        if observed != order:
            raise ValueError("Executed ASR batches do not cover the planned interval order")
    last_start = -1
    words = 0
    for index, segment in enumerate(segments, 1):
        if segment.id != index:
            raise ValueError("Restored subtitles do not have sequential identifiers")
        if (segment.start_ms < last_start or segment.start_ms < 0
                or segment.end_ms <= segment.start_ms or segment.end_ms > duration_ms + 1):
            raise ValueError("Invalid subtitle order or original-video timestamp")
        if not segment.words:
            raise ValueError("A text subtitle has no word timestamps")
        for word in segment.words:
            if (word["start"] * 1000 < segment.start_ms - 2
                    or word["end"] * 1000 > segment.end_ms + 2):
                raise ValueError("A word timestamp falls outside its subtitle")
        last_start = segment.start_ms
        words += len(segment.words)
    if audit["speech_intervals_ms"] and not segments:
        raise ValueError("Speech was detected but the whole recording produced no subtitles")
    return {"subtitle_count": len(segments), "word_count": words,
            "first_subtitle_ms": segments[0].start_ms if segments else None,
            "last_subtitle_ms": segments[-1].end_ms if segments else None}


def write_report(directory, result, samples):
    fields = ["elapsed_seconds", "phase", "sample_seconds",
              "process_cpu_percent_all_logical_cores", "system_cpu_percent",
              "process_working_mib", "process_private_commit_mib",
              "process_peak_working_mib", "process_peak_private_commit_mib",
              "system_available_physical_mib", "system_available_commit_mib"]
    with (directory / "resources.csv").open("w", newline="", encoding="utf-8-sig") as output:
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(samples)

    def number(value, unit=""):
        return "-" if value is None else f"{value:.3f}{unit}"

    audit, hardware = result["audit"], result.get("hardware", {})
    timings, memory = result["timings"], result["final_memory"]
    resources = result["resources"]
    lines = [
        f"# Paraformer {result.get('device', 'cpu').upper()} Benchmark", "",
        f"- Status: {result['status']}.",
        f"- Source: `{result['video']}`.",
        f"- Started (UTC): {result['started_at_utc']}.",
        f"- Backend: `{audit.get('backend', 'funasr-pytorch-cpu')}`; dtype: FP32.",
        "- Checkpoint: official BiCifParaformer/CifPredictorV3 large-vad-punc ASR weights.",
        "- Loaded components: ASR + FSMN-VAD only; no punctuation, speakers or aligner.",
        f"- Checked component devices: {result.get('component_devices', {})}.",
        f"- VAD invocation: {audit.get('vad_mode', 'original streaming implementation')}; "
        f"endpoint silence: {audit.get('vad_end_silence_ms', 800)} ms.",
        f"- VAD requested batch size: {audit.get('vad_batch_size', 1)}; "
        f"input files: {audit.get('vad_input_count', 1)}.",
        f"- Observed VAD encoder batch distribution (size: calls): "
        f"{dict(Counter(audit.get('vad_encoder_batch_sizes', [])))}.",
        f"- ASR batch size: {result['batch_size']} actual VAD utterances, not batch seconds.",
        f"- Final effective ASR batch size: {audit.get('effective_batch_size', result['batch_size'])}; "
        f"CUDA OOM recovery attempts: {len(audit.get('oom_recovery', []))}.",
        f"- ASR batch order: {audit.get('batch_order', 'time')}; "
        "original intervals and subtitle timeline retained.",
        f"- PyTorch CPU threads: {result['cpu_threads']}; "
        f"host logical CPUs: {hardware.get('logical_cpus', '-')}.",
        f"- CPU: {hardware.get('processor', '-')}.",
        f"- Torch build: {hardware.get('torch', '-')}; CUDA build: {hardware.get('torch_cuda')}; "
        f"CUDA available: {hardware.get('cuda_available')}.",
        (f"- GPU: {hardware.get('gpu_name')}; ASR parameters checked on CUDA; "
         f"VAD parameters checked on {audit.get('vad_device', 'cuda:0')}."
         if result.get("device") == "gpu"
         else "- CUDA hidden; all model parameters checked on CPU."),
        "- Software-only audio decode; this host is not an actual ultrabook benchmark.",
        "- Native BiCIF timestamps retained and strictly validated; no manual accuracy labels.", "",
        "## Timing", "",
        f"- Audio duration: {number(result.get('duration_seconds'), ' s')}.",
        f"- Imports: {number(timings.get('imports'), ' s')}.",
        f"- Model load: {number(timings.get('load'), ' s')}.",
        f"- Pipeline excluding load: {number(timings.get('pipeline'), ' s')}.",
        f"- Process wall: {number(result['process_wall_seconds'], ' s')}.",
        f"- Pipeline real-time factor: {number(result.get('pipeline_rtf'))}.",
    ]
    for name, seconds in audit.get("stage_seconds", {}).items():
        lines.append(f"- {name}: {number(seconds, ' s')}.")
    lines += [
        f"- VAD segments: {len(audit.get('speech_intervals_ms', []))}; "
        f"speech: {number(audit.get('speech_seconds'), ' s')}.",
        f"- Actual batch distribution (size: count): {dict(Counter(audit.get('batch_sizes', [])))}.",
        f"- Estimated padded input length: "
        f"{number(audit.get('estimated_padded_seconds'), ' equivalent seconds')}; "
        "not measured FLOPs or an ASR speedup estimate.",
        f"- Subtitles: {result.get('subtitle_count', '-')}; "
        f"timestamped tokens: {result.get('word_count', '-')}.", "",
        "## Resources", "",
        f"- Mean pipeline process CPU: "
        f"{number(result.get('mean_pipeline_cpu_percent_all_logical_cores'), '%')}.",
        f"- Peak sampled process CPU: "
        f"{number(resources.get('peak_process_cpu_percent_all_logical_cores'), '%')}.",
        f"- Mean/peak sampled whole-system CPU: "
        f"{number(resources.get('mean_system_cpu_percent'), '%')} / "
        f"{number(resources.get('peak_system_cpu_percent'), '%')}.",
        f"- OS-recorded peak process working set: "
        f"{number(memory.get('process_peak_working_mib'), ' MiB')}.",
        f"- OS-recorded peak process private commit: "
        f"{number(memory.get('process_peak_private_commit_mib'), ' MiB')}.",
        f"- Minimum sampled system free physical RAM: "
        f"{number(resources.get('min_system_available_physical_mib'), ' MiB')}.", "",
        "CPU percent is CPU seconds / elapsed seconds / host logical CPUs * 100.",
        "Private commit includes nonresident allocations; it is not resident RAM.",
        "Samples are every 0.5 seconds and include model-load transient peaks.", "",
        "| Phase | Mean CPU % | Peak CPU % | Peak RAM MiB | Peak private MiB |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, values in result["resources_by_phase"].items():
        lines.append(
            f"| {name} | {number(values.get('mean_process_cpu_percent_all_logical_cores'))} | "
            f"{number(values.get('peak_process_cpu_percent_all_logical_cores'))} | "
            f"{number(values.get('peak_process_working_mib'))} | "
            f"{number(values.get('peak_process_private_commit_mib'))} |"
        )
    if result.get("error"):
        lines += ["", "## Failure", "", result["error"]]
    if result.get("device") == "gpu":
        lines += [
            "", "## GPU", "",
            "- NVIDIA telemetry is device-wide, including desktop and other applications.",
            "- PyTorch allocated/reserved peaks cover this process's tensor allocator only;",
            "  they exclude CUDA context/driver allocations and are not total process VRAM.",
            "- CUDA synchronized at timed inference boundaries; FP32, no autocast.",
            f"- GPU sampling errors: {result.get('gpu_sampling_errors', [])}.",
            f"- CUDA memory peaks (MiB): {result.get('cuda_memory', {})}.", "",
            "| Phase | Samples | Mean GPU % | Peak GPU % | Peak device VRAM MiB |",
            "|---|---:|---:|---:|---:|",
        ]
        for name, values in result.get("gpu_by_phase", {}).items():
            lines.append(
                f"| {name} | {values['sample_count']} | "
                f"{number(values['mean_device_utilization_percent'])} | "
                f"{number(values['peak_device_utilization_percent'])} | "
                f"{number(values['peak_device_used_mib'])} |"
            )
    lines += ["", "## Artifacts", "",
              "- result.json: configuration, timing, CPU/RAM summaries and batching audit.",
              "- resources.csv / resource_samples.jsonl: incremental resource samples.",
              "- gpu_samples.jsonl (GPU mode): incremental device-wide NVIDIA telemetry.",
              "- raw_outputs.jsonl: official model outputs with each absolute VAD span.",
              "- paraformer.json / paraformer.srt / paraformer.txt: validated full output.",
              "- worker.log: model and pipeline log."]
    (directory / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(args):
    environment = {
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        "MODELSCOPE_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
        "OMP_NUM_THREADS": str(args.threads), "MKL_NUM_THREADS": str(args.threads),
        "OPENBLAS_NUM_THREADS": str(args.threads), "OMP_WAIT_POLICY": "PASSIVE",
    }
    if args.device == "cpu":
        environment["CUDA_VISIBLE_DEVICES"] = ""
    os.environ.update(environment)
    from qwen_memory import ensure_memory_headroom, memory_snapshot

    args.output.mkdir(parents=True, exist_ok=True)
    if (args.output / "result.json").exists():
        raise FileExistsError(f"Refusing to overwrite an existing trial: {args.output}")
    started, phase = time.perf_counter(), "imports"
    audit, timings, samples, sampling_errors = {}, {}, [], []
    gpu_samples, gpu_sampling_errors = [], []
    gpu_process = gpu_thread = None
    torch = None
    stop, unsafe = Event(), Event()
    logical_cpus = os.cpu_count() or 1
    result = {
        "status": "running", "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "video": str(args.video), "source_bytes": args.video.stat().st_size,
        "recognizer": str(args.recognizer), "vad": str(args.vad),
        "batch_size": args.batch_size, "cpu_threads": args.threads,
        "device": args.device, "vad_device": args.vad_device, "cuda_memory": {},
        "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
        "pid": os.getpid(), "sampling_seconds": 0.5,
        "baseline_memory": memory_snapshot(), "audit": audit, "timings": timings,
        "memory_guard": {"physical_mib": 1024, "commit_mib": 2048},
    }
    save_json(args.output / "result.json", result)

    def sample():
        previous_wall, previous_cpu = time.perf_counter(), time.process_time()
        previous_system = system_cpu_times()
        breaches = 0
        try:
            with (args.output / "resource_samples.jsonl").open("w", encoding="utf-8") as output:
                while not stop.wait(.5):
                    now, cpu = time.perf_counter(), time.process_time()
                    system = system_cpu_times()
                    values = memory_snapshot()
                    values.update({
                        "elapsed_seconds": now - started, "phase": phase,
                        "sample_seconds": now - previous_wall,
                        "process_cpu_percent_all_logical_cores": cpu_percent(
                            cpu - previous_cpu, now - previous_wall, logical_cpus),
                    })
                    if system is not None and previous_system is not None:
                        total = system[1] - previous_system[1]
                        values["system_cpu_percent"] = (
                            100 * (1 - (system[0] - previous_system[0]) / total) if total else 0.0)
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
    pipeline_start = pipeline_cpu = None
    try:
        if args.device == "gpu":
            gpu_process = subprocess.Popen(
                ["nvidia-smi", "-i", "0",
                 "--query-gpu=memory.used,memory.free,utilization.gpu,temperature.gpu,power.draw",
                 "--format=csv,noheader,nounits", "-lms", "500"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )

            def sample_gpu():
                try:
                    with (args.output / "gpu_samples.jsonl").open("w", encoding="utf-8") as output:
                        for line in gpu_process.stdout:
                            value = {"elapsed_seconds": time.perf_counter() - started,
                                     "phase": phase, **parse_gpu_sample(line)}
                            gpu_samples.append(value)
                            output.write(json.dumps(value) + "\n")
                            output.flush()
                except Exception:
                    gpu_sampling_errors.append(traceback.format_exc())

            gpu_thread = Thread(target=sample_gpu, daemon=True)
            gpu_thread.start()
        ensure_memory_headroom(physical_mib=1024, commit_mib=2048)
        import torch
        import psutil
        import paraformer_transcriber

        timings["imports"] = time.perf_counter() - started
        processor = platform.processor()
        if os.name == "nt":
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
                processor = winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
        result["hardware"] = {
            "processor": processor, "physical_cpus": psutil.cpu_count(logical=False),
            "logical_cpus": logical_cpus, "python": sys.version,
            "torch": torch.__version__, "torch_cuda": torch.version.cuda,
            "cuda_available": torch.cuda.is_available(), "platform": platform.platform(),
            "versions": {name: importlib.metadata.version(name) for name in
                         ("funasr", "torchaudio", "transformers", "numpy", "av")},
        }
        if args.device == "cpu" and (torch.version.cuda is not None or torch.cuda.is_available()):
            raise RuntimeError("Benchmark requires a CPU-only PyTorch build with no CUDA")
        if args.device == "gpu":
            if not torch.cuda.is_available():
                raise RuntimeError("CUDA is unavailable; GPU benchmark cannot fall back to CPU")
            if os.environ.get("CUDA_VISIBLE_DEVICES") not in (None, "0"):
                raise RuntimeError("GPU telemetry requires CUDA device 0 without remapping")
            result["hardware"].update(
                gpu_name=torch.cuda.get_device_name(0),
                matmul_allow_tf32=torch.backends.cuda.matmul.allow_tf32,
                cudnn_allow_tf32=torch.backends.cudnn.allow_tf32,
            )
            torch.cuda.reset_peak_memory_stats()
        phase = "load"
        begin = time.perf_counter()
        model = paraformer_transcriber.load_paraformer_model(
            args.recognizer, args.vad, cpu_threads=args.threads, device=args.device,
            vad_device=args.vad_device)
        paraformer_transcriber.synchronize_device(model)
        timings["load"] = time.perf_counter() - begin
        result["model_dtype"] = str(next(model.recognizer.model.parameters()).dtype)
        result["component_devices"] = {
            name: sorted({str(parameter.device) for parameter in component.model.parameters()})
            for name, component in (("asr", model.recognizer), ("vad", model.vad))
        }
        result["hardware"]["interop_threads"] = torch.get_num_interop_threads()
        result["hardware"]["actual_torch_threads"] = torch.get_num_threads()
        if torch.get_num_threads() != args.threads or model.cpu_threads != args.threads:
            raise RuntimeError("Requested CPU thread count was not respected")
        if args.device == "gpu":
            result["cuda_memory"]["load_peak_allocated_mib"] = torch.cuda.max_memory_allocated() / 2**20
            result["cuda_memory"]["load_peak_reserved_mib"] = torch.cuda.max_memory_reserved() / 2**20
            torch.cuda.reset_peak_memory_stats()
        print(f"{args.device.upper()} model ready in {timings['load']:.3f}s; BS={args.batch_size}; "
              f"threads={torch.get_num_threads()}", flush=True)

        original_stream = paraformer_transcriber.stream_decode_audio_to_wav
        original_vad = paraformer_transcriber.detect_speech
        original_generate = model.recognizer.generate
        asr_started = False

        def stream(*a, **kw):
            nonlocal phase
            phase = "audio"
            duration_ms = original_stream(*a, **kw)
            result["duration_seconds"] = duration_ms / 1000
            return duration_ms

        def vad(*a, **kw):
            nonlocal phase
            phase = "vad"
            value = original_vad(*a, **kw)
            if args.device == "gpu":
                torch.cuda.synchronize()
                result["cuda_memory"]["vad_peak_allocated_mib"] = torch.cuda.max_memory_allocated() / 2**20
                result["cuda_memory"]["vad_peak_reserved_mib"] = torch.cuda.max_memory_reserved() / 2**20
            return value

        def generate(*a, **kw):
            nonlocal phase, asr_started
            phase = "asr"
            if sampling_errors:
                raise RuntimeError(sampling_errors[0])
            if unsafe.is_set():
                ensure_memory_headroom(physical_mib=1024, commit_mib=2048)
            if args.device == "gpu":
                torch.cuda.synchronize()
                if not asr_started:
                    torch.cuda.reset_peak_memory_stats()
                    asr_started = True
            outputs = original_generate(*a, **kw)
            if args.device == "gpu":
                torch.cuda.synchronize()
                result["cuda_memory"]["asr_peak_allocated_mib"] = torch.cuda.max_memory_allocated() / 2**20
                result["cuda_memory"]["asr_peak_reserved_mib"] = torch.cuda.max_memory_reserved() / 2**20
            phase = "validation"
            return outputs

        def progress(fraction, message):
            current = {"fraction": fraction, "message": message, "phase": phase,
                       "elapsed_seconds": time.perf_counter() - started,
                       "batches_completed": len(audit.get("batches", [])),
                       "stage_seconds": audit.get("stage_seconds", {}),
                       "memory": memory_snapshot(), "recent_cpu": samples[-1] if samples else None}
            save_json(args.output / "progress.json", current)
            save_json(args.output / "result.json", result)
            print(f"{args.device.upper()} BS={args.batch_size} {fraction:.1%}: {message}", flush=True)

        paraformer_transcriber.stream_decode_audio_to_wav = stream
        paraformer_transcriber.detect_speech = vad
        model.recognizer.generate = generate
        audit["raw_output_path"] = str(args.output / "raw_outputs.jsonl")
        pipeline_start, pipeline_cpu = time.perf_counter(), time.process_time()
        try:
            segments, duration_ms = paraformer_transcriber.transcribe_paraformer(
                model, args.video, progress, batch_size=args.batch_size,
                temporary_directory=args.output, audit=audit, batch_order=args.batch_order)
        finally:
            timings["pipeline"] = time.perf_counter() - pipeline_start
            timings["pipeline_cpu_seconds"] = time.process_time() - pipeline_cpu
            paraformer_transcriber.stream_decode_audio_to_wav = original_stream
            paraformer_transcriber.detect_speech = original_vad
            model.recognizer.generate = original_generate
            if args.device == "gpu":
                for kind in ("allocated", "reserved"):
                    current = getattr(torch.cuda, f"max_memory_{kind}")() / 2**20
                    previous = result["cuda_memory"].get(f"vad_peak_{kind}_mib", 0)
                    result["cuda_memory"][f"pipeline_peak_{kind}_mib"] = max(previous, current)
        phase = "save"
        result.update(validate_output(segments, duration_ms, audit))
        write_transcript(args.output, segments, duration_ms)
        result.update({"status": "completed", "duration_seconds": duration_ms / 1000,
                       "pipeline_rtf": timings["pipeline"] / (duration_ms / 1000)})
        phase = "hash"
        with args.video.open("rb") as source:
            result["source_sha256"] = hashlib.file_digest(source, "sha256").hexdigest()
    except BaseException as exc:
        result.update(status="failed", error=str(exc), traceback=traceback.format_exc())
        print(result["traceback"], flush=True)
    finally:
        stop.set()
        thread.join(timeout=5)
        if gpu_process is not None:
            if gpu_process.poll() is None:
                gpu_process.terminate()
            gpu_process.wait(timeout=10)
        if gpu_thread is not None:
            gpu_thread.join(timeout=5)
        result["gpu_sampling_errors"] = gpu_sampling_errors
        result["gpu_resources"] = summarize_gpu(gpu_samples)
        result["gpu_by_phase"] = {
            name: summarize_gpu([s for s in gpu_samples if s["phase"] == name])
            for name in ("imports", "load", "audio", "vad", "asr", "validation", "save", "hash")
        }
        if args.device == "gpu" and (gpu_sampling_errors or not gpu_samples):
            result["status"] = "failed"
        if timings.get("pipeline"):
            result["mean_pipeline_cpu_percent_all_logical_cores"] = cpu_percent(
                timings["pipeline_cpu_seconds"], timings["pipeline"], logical_cpus)
        result["sampling_errors"] = sampling_errors
        if sampling_errors:
            result["status"] = "failed"
        result["process_wall_seconds"] = time.perf_counter() - started
        result["final_memory"] = memory_snapshot()
        result["resources"] = summarize_resources(samples)
        result["resources_by_phase"] = {
            name: summarize_resources([s for s in samples if s["phase"] == name])
            for name in ("imports", "load", "audio", "vad", "asr", "validation", "save", "hash")
        }
        save_json(args.output / "result.json", result)
        write_report(args.output, result, samples)
        print(f"{args.device.upper()} BS={args.batch_size} {result['status']}: {timings}", flush=True)
    return 0 if result["status"] == "completed" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--recognizer", type=Path, required=True)
    parser.add_argument("--vad", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--threads", type=int, default=16)
    parser.add_argument("--device", choices=("cpu", "gpu"), default="cpu")
    parser.add_argument("--vad-device", choices=("cpu", "gpu"), default="cpu",
                        help="CPU by default; gpu is retained for controlled comparisons")
    parser.add_argument("--batch-order", choices=("length", "time"), default="length")
    args = parser.parse_args()
    args.video, args.recognizer, args.vad, args.output = (
        path.resolve() for path in (args.video, args.recognizer, args.vad, args.output))
    if not args.video.is_file() or args.batch_size < 1 or args.threads < 1:
        parser.error("A local video and positive batch size / CPU threads are required")
    if args.device == "cpu" and args.vad_device != "cpu":
        parser.error("CPU ASR requires CPU VAD")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
