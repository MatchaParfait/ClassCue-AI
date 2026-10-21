"""One isolated Qwen stage; validated chunks are committed before progress."""

import argparse
from contextlib import redirect_stdout
from dataclasses import asdict
import gc
import json
import os
from pathlib import Path
import sys
from threading import Event, Thread
import time
import traceback
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    from classcue.qwen_resilience import (
        Checkpoints, QwenOptions, atomic_json, classify_error, read_audio, read_json, validate_segments,
    )
    from classcue.qwen_memory import memory_snapshot

    request = read_json(args.request)
    manifest, stage = request["manifest"], request["stage"]
    options = QwenOptions(**manifest["identity"]["options"])
    os.environ.update(QWEN_CPU_THREADS=str(options.cpu_threads), OMP_NUM_THREADS=str(options.cpu_threads),
                      MKL_NUM_THREADS=str(options.cpu_threads), ONEDNN_PRIMITIVE_CACHE_CAPACITY="0")
    attempt_id = request["attempt_id"]
    started, phase, torch = time.perf_counter(), "load", None
    samples, batches, stop = [], [], Event()
    pressure_warned = Event()
    channel = sys.stdout

    def emit(fraction, message):
        channel.write(json.dumps({"event": "progress", "fraction": fraction, "message": message},
                                 ensure_ascii=True) + "\n")
        channel.flush()

    def snapshot():
        try:
            return memory_snapshot()
        except OSError:
            return {}

    def monitor():
        while not stop.is_set():
            from classcue.qwen_resilience import gpu_free_mib
            row = {"elapsed_seconds": time.perf_counter() - started, **snapshot(),
                   "device_free_mib": gpu_free_mib()}
            samples.append(row)
            if (not pressure_warned.is_set() and (
                row.get("system_available_physical_mib", float("inf")) < 1024
                or row.get("system_available_commit_mib", float("inf")) < 1024
                or (row["device_free_mib"] is not None and row["device_free_mib"] < 512)
            )):
                pressure_warned.set()
                channel.write(json.dumps({
                    "event": "resource_warning",
                    "message": "资源余量较低，继续执行；仅在真实分配失败后按设置降级。",
                }, ensure_ascii=True) + "\n")
                channel.flush()
            stop.wait(1)

    monitor_thread = Thread(target=monitor, daemon=True)
    monitor_thread.start()
    metrics = {"stage": stage, "attempt_id": attempt_id, "batches": batches,
               "memory_guard_enabled": False, "batch_size": request["batch_size"],
               "process_id": os.getpid(), "device": "cuda:0", "dtype": "float16"}
    alignment_context = None
    alignment_diagnostics = []

    def record_alignment(event):
        alignment_diagnostics.append({**alignment_context, **event})
        atomic_json(args.output / f"alignment-diagnostics-{attempt_id}.json", alignment_diagnostics)

    try:
        with redirect_stdout(sys.stderr):
            import torch
            from qwen_asr import Qwen3ForcedAligner
            from classcue.qwen_transcriber import (
                load_qwen_model, _checkpoint_reader, recover_aligned_segments,
            )
            from classcue.qwen_loading import model_loading
            from classcue.qwen_silence import restore_segments

            if not torch.cuda.is_available():
                from classcue.qwen_resilience import QwenRunError
                raise QwenRunError("Qwen GPU 模式需要可用的 CUDA；请检查驱动或选择其他模型")
            torch.set_num_threads(options.cpu_threads)
            paths = manifest["paths"]
            emit(.06 if stage == "asr" else .8, f"加载 GPU {stage} 模型")
            load_started = time.perf_counter()
            with model_loading(request["load_profile"]) as loading:
                if stage == "asr":
                    model = load_qwen_model(paths["recognizer"], paths["aligner"], load_aligner=False)
                    model.max_inference_batch_size = request["batch_size"]
                else:
                    with _checkpoint_reader():
                        model = Qwen3ForcedAligner.from_pretrained(
                            paths["aligner"], dtype=torch.float16, device_map="cuda:0",
                            attn_implementation="sdpa", local_files_only=True,
                        )
            torch.cuda.synchronize()
            metrics.update(load_seconds=time.perf_counter() - load_started, loading=loading,
                           gpu=torch.cuda.get_device_name(0),
                           load_cuda_allocated_mib=torch.cuda.memory_allocated() / 2**20,
                           load_cuda_reserved_mib=torch.cuda.memory_reserved() / 2**20)
            phase = "inference"
            checkpoints = Checkpoints(args.output, manifest["job_id"])
            chunks = request["chunks"]
            pending = [c for c in chunks if (
                checkpoints.load("asr", c) is None if stage == "asr"
                else checkpoints.load("align", c, checkpoints.load("asr", c)) is None
            )]
            metrics["startup_to_ready_seconds"] = time.perf_counter() - started
            torch.cuda.reset_peak_memory_stats()
            with wave.open(manifest["timeline"], "rb") as source:
                for index in range(0, len(pending), request["batch_size"]):
                    group = pending[index:index + request["batch_size"]]
                    waveforms = [(read_audio(source, chunk), manifest["rate"]) for chunk in group]
                    begin = time.perf_counter()
                    if stage == "asr":
                        nonempty = [(c, w) for c, w in zip(group, waveforms) if c["input_samples"]]
                        outputs = model.transcribe(
                            audio=[w for c, w in nonempty], language="Chinese", return_time_stamps=False,
                        ) if nonempty else []
                        torch.cuda.synchronize()
                        if len(outputs) != len(nonempty):
                            raise ValueError("Unexpected ASR output count")
                        result = {c["source_chunk_index"]: output.text
                                  for (c, _), output in zip(nonempty, outputs)}
                        phase = "checkpoint"
                        for chunk in group:
                            checkpoints.save("asr", chunk, result.get(chunk["source_chunk_index"], ""))
                        if request.get("stream_preview"):
                            from classcue.qwen_preview import chunk_preview
                            preview = [segment for chunk in group if (segment := chunk_preview(
                                chunk, result.get(chunk["source_chunk_index"], ""), manifest["rate"],
                            )) is not None]
                            if preview:
                                channel.write(json.dumps({"event": "transcript_preview",
                                    "segments": [asdict(segment) for segment in preview]}, ensure_ascii=True) + "\n")
                                channel.flush()
                    else:
                        texts = {c["source_chunk_index"]: checkpoints.load("asr", c) for c in group}
                        if any(text is None for text in texts.values()):
                            raise ValueError("Alignment requires complete ASR checkpoints")
                        nonempty = [(c, w) for c, w in zip(group, waveforms)
                                    if texts[c["source_chunk_index"]].strip()]
                        outputs = model.align(
                            audio=[w for c, w in nonempty],
                            text=[texts[c["source_chunk_index"]] for c, w in nonempty], language="Chinese",
                        ) if nonempty else []
                        torch.cuda.synchronize()
                        if len(outputs) != len(nonempty):
                            raise ValueError("Unexpected alignment output count")
                        result = {}
                        for (chunk, waveform), output in zip(nonempty, outputs):
                            alignment_context = {
                                "source_chunk_index": chunk["source_chunk_index"],
                                "start_sample": chunk["start_sample"], "end_sample": chunk["end_sample"],
                                "input_samples": chunk["input_samples"],
                            }
                            text = texts[chunk["source_chunk_index"]]
                            cues = recover_aligned_segments(
                                model, text, list(output.items), waveform,
                                allow_recovery=options.auto_recovery, diagnostic=record_alignment,
                            )
                            warnings = []
                            restored = restore_segments(cues, chunk, manifest["rate"],
                                                        manifest["total_samples"], warnings=warnings)
                            validate_segments(restored, chunk["end_sample"] / manifest["rate"] * 1000,
                                              chunk["start_sample"] / manifest["rate"] * 1000)
                            result[chunk["source_chunk_index"]] = [asdict(s) for s in restored]
                            phase = "checkpoint"
                            checkpoints.save("align", chunk, result[chunk["source_chunk_index"]], text)
                            phase = "inference"
                            alignment_context = None
                        phase = "checkpoint"
                        for chunk in group:
                            if chunk["source_chunk_index"] not in result:
                                checkpoints.save("align", chunk, [], texts[chunk["source_chunk_index"]])
                    batches.append({"seconds": time.perf_counter() - begin, "memory": snapshot()})
                    phase = "inference"
                    fraction = (index + len(group)) / max(1, len(pending))
                    emit((.08 + .7 * fraction) if stage == "asr" else (.8 + .19 * fraction),
                         f"{stage} 已保存 {index + len(group)}/{len(pending)} 个待处理分块")
                    del outputs, waveforms, group, nonempty, result
                    gc.collect()
                    torch.cuda.empty_cache()
            metrics.update(peak_cuda_allocated_mib=torch.cuda.max_memory_allocated() / 2**20,
                           peak_cuda_reserved_mib=torch.cuda.max_memory_reserved() / 2**20)
        return 0
    except Exception as error:
        kind = classify_error(error, torch)
        if phase == "load" and kind == "inference":
            kind = "environment"
        failure = {"kind": kind, "phase": phase, "stage": stage, "message": str(error),
                   "error_type": type(error).__name__, "traceback": traceback.format_exc(),
                   "memory": snapshot()}
        if alignment_context is not None:
            failure["alignment_chunk"] = alignment_context
        if hasattr(error, "details"):
            failure["alignment_error"] = error.details
        atomic_json(args.output / f"failure-{attempt_id}.json", failure)
        traceback.print_exc(file=sys.stderr)
        return 1
    finally:
        stop.set()
        monitor_thread.join(timeout=3)
        metrics.update(elapsed_seconds=time.perf_counter() - started, samples=samples)
        atomic_json(args.output / f"metrics-{attempt_id}.json", metrics)


if __name__ == "__main__":
    raise SystemExit(main())
