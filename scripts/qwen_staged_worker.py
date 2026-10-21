"""Run either GPU ASR or CPU alignment, never retaining both model processes."""

import argparse
import gc
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event, Thread
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stage", choices=["asr", "align"], required=True)
    parser.add_argument("--cpu-threads", type=int, default=4)
    parser.add_argument("--reverse-order", action="store_true")
    args = parser.parse_args()
    os.environ.update({
        "QWEN_CPU_THREADS": str(args.cpu_threads), "OMP_NUM_THREADS": str(args.cpu_threads),
        "MKL_NUM_THREADS": str(args.cpu_threads), "OPENBLAS_NUM_THREADS": str(args.cpu_threads),
        "ONEDNN_PRIMITIVE_CACHE_CAPACITY": "0",
    })
    if args.stage == "align":
        os.environ["CUDA_VISIBLE_DEVICES"] = ""
    from qwen_memory import memory_snapshot, ensure_memory_headroom
    args.output.mkdir(parents=True, exist_ok=True)
    baseline, snapshots, stop = memory_snapshot(), [], Event()
    memory_started = time.perf_counter()

    def monitor():
        breaches = 0
        while not stop.is_set():
            values = memory_snapshot()
            row = {"elapsed_seconds": time.perf_counter() - memory_started, **values}
            if args.stage == "asr":
                try:
                    smi = subprocess.run(
                        ["nvidia-smi", "--query-gpu=memory.used,memory.free", "--format=csv,noheader,nounits"],
                        capture_output=True, text=True, timeout=5,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    )
                    used, free = [float(s.strip()) for s in smi.stdout.splitlines()[0].split(",")]
                    row.update(device_used_mib=used, device_free_mib=free)
                except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
                    pass
            snapshots.append(row)
            unsafe = (
                values.get("process_private_commit_mib", 0) > 12288
                or values.get("system_available_physical_mib", float("inf")) < 1536
                or values.get("system_available_commit_mib", float("inf")) < 3072
                or row.get("device_free_mib", float("inf")) < 768
            )
            breaches = breaches + 1 if unsafe else 0
            if breaches >= 2:
                (args.output / f"memory_abort_{args.stage}.json").write_text(
                    json.dumps({"snapshot": values, "samples": snapshots}, indent=2), encoding="utf-8",
                )
                print(f"{args.stage}: memory safety limit; aborting isolated worker", flush=True)
                os._exit(2)
            stop.wait(1)

    thread = Thread(target=monitor, daemon=True)
    thread.start()
    try:
        import soundfile as sf
        import torch
        from qwen_asr import Qwen3ForcedAligner
        from asr_models import component_paths
        from qwen_transcriber import load_qwen_model, aligned_segments, _checkpoint_reader
        from qwen_silence import compact_audio, restore_segments
        from scripts.qwen_chunked import _offset_segment
        from scripts.compare_asr import _write_result
        if args.stage == "asr" and not torch.cuda.is_available():
            raise RuntimeError("Staged GPU ASR requires an available CUDA device")
        torch.set_num_threads(args.cpu_threads)
        torch.set_num_interop_threads(1)
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        audio, rate = sf.read(plan["sample_audio"], dtype="float32")
        paths = component_paths("qwen3-asr-1.7b")
        ensure_memory_headroom()
        begin = time.perf_counter()
        if args.stage == "asr":
            model = load_qwen_model(str(paths["recognizer"]), str(paths["aligner"]), load_aligner=False)
            model.max_inference_batch_size = plan["batch_size"]
        else:
            with _checkpoint_reader():
                model = Qwen3ForcedAligner.from_pretrained(
                    str(paths["aligner"]), dtype=torch.float32, device_map="cpu",
                    attn_implementation="sdpa", local_files_only=True,
                )
        load_seconds = time.perf_counter() - begin
        print(f"{args.stage} model ready: {load_seconds:.2f}s, CPU={args.cpu_threads}", flush=True)
        if args.stage == "asr":
            model.transcribe(audio=(audio[:10 * rate], rate), language="Chinese", return_time_stamps=False)
        else:
            model.align(audio=(audio[:10 * rate], rate), text="你好", language="Chinese")
        gc.collect()
        if args.stage == "asr":
            torch.cuda.empty_cache()
        summary = {
            "stage": args.stage, "load_seconds": load_seconds, "cpu_threads": args.cpu_threads,
            "baseline_memory": baseline, "modes": [], "scope": "staged, excludes model load/warmup",
            "gpu": torch.cuda.get_device_name(0) if args.stage == "asr" else "CPU",
            "torch": torch.__version__, "batch_size": plan["batch_size"],
        }
        entries = list(reversed(plan["modes"])) if args.reverse_order else plan["modes"]
        for entry in entries:
            mode = entry["mode"]
            chunks = [c for c in entry["chunks"] if c["input_samples"]]
            texts_by_index = {}
            if args.stage == "align":
                texts_by_index = {
                    t["source_chunk_index"]: t["text"]
                    for t in json.loads((args.output / f"asr-{mode}.json").read_text(encoding="utf-8"))
                }
            sample_start = len(snapshots)
            if args.stage == "asr":
                torch.cuda.reset_peak_memory_stats()
            begin = time.perf_counter()
            batches, batch_memory, texts, segments, warnings = [], [], [], [], []
            stage_seconds = 0
            for index in range(0, len(chunks), plan["batch_size"]):
                ensure_memory_headroom()
                group = chunks[index:index + plan["batch_size"]]
                waveforms = [(compact_audio(audio, c), rate) for c in group]
                if args.stage == "asr":
                    torch.cuda.synchronize()
                    start = time.perf_counter()
                    outputs = model.transcribe(audio=waveforms, language="Chinese", return_time_stamps=False)
                    torch.cuda.synchronize()
                    stage_seconds += time.perf_counter() - start
                    if len(outputs) != len(group):
                        raise ValueError("Unexpected ASR result count")
                    texts.extend({
                        "source_chunk_index": chunk["source_chunk_index"], "text": output.text,
                    } for chunk, output in zip(group, outputs))
                else:
                    nonempty = [(c, w) for c, w in zip(group, waveforms) if texts_by_index[c["source_chunk_index"]].strip()]
                    start = time.perf_counter()
                    outputs = model.align(
                        audio=[w for c, w in nonempty],
                        text=[texts_by_index[c["source_chunk_index"]] for c, w in nonempty],
                        language="Chinese",
                    ) if nonempty else []
                    stage_seconds += time.perf_counter() - start
                    if len(outputs) != len(nonempty):
                        raise ValueError("Unexpected alignment result count")
                    for (chunk, waveform), output in zip(nonempty, outputs):
                        cues = aligned_segments(
                            texts_by_index[chunk["source_chunk_index"]], list(output.items),
                            0, chunk["input_samples"] / rate,
                        )
                        for cue in restore_segments(cues, chunk, rate, len(audio), warnings=warnings):
                            segments.append(_offset_segment(cue, 0, len(segments) + 1))
                del outputs, waveforms, group
                gc.collect()
                if args.stage == "asr":
                    torch.cuda.empty_cache()
                batches.append(time.perf_counter() - begin - sum(batches))
                batch_memory.append(memory_snapshot())
                (args.output / f"memory_progress_{args.stage}.json").write_text(json.dumps({
                    "stage": args.stage, "mode": mode, "completed_batches": len(batches),
                    "total_batches": (len(chunks) + plan["batch_size"] - 1) // plan["batch_size"],
                    "batch_memory": batch_memory,
                }, indent=2), encoding="utf-8")
                print(f"{args.stage} {mode} batch {len(batches)}: {batches[-1]:.2f}s; "
                      f"RAM={batch_memory[-1].get('process_working_mib',0):.0f}MiB; "
                      f"private={batch_memory[-1].get('process_private_commit_mib',0):.0f}MiB", flush=True)
            elapsed = time.perf_counter() - begin
            sampled = snapshots[sample_start:]
            metrics = {
                "mode": mode, "status": "completed", "error": None, "wall_seconds": elapsed,
                "stage_seconds": stage_seconds, "batch_seconds": batches, "batch_memory": batch_memory,
                "audio_seconds": len(audio) / rate, "chunk_count": len(entry["spans"]),
                "inference_chunk_count": len(chunks),
                "asr_input_seconds": sum(c["input_samples"] / rate for c in chunks),
                "segment_count": len(segments), "mapping_warnings": warnings,
                "peak_cuda_allocated_mib": torch.cuda.max_memory_allocated() / 2**20 if args.stage == "asr" else 0,
                "peak_cuda_reserved_mib": torch.cuda.max_memory_reserved() / 2**20 if args.stage == "asr" else 0,
                "sampled_device_peak_used_mib": max((s.get("device_used_mib",0) for s in sampled), default=0),
                "sampled_device_min_free_mib": min((s["device_free_mib"] for s in sampled if "device_free_mib" in s), default=None),
            }
            for key in ("process_working_mib", "process_private_commit_mib"):
                metrics[f"peak_{key}"] = max((s.get(key, 0) for s in sampled), default=0)
            for key in ("system_available_physical_mib", "system_available_commit_mib"):
                metrics[f"min_{key}"] = min((s.get(key, float("inf")) for s in sampled), default=0)
            if args.stage == "asr":
                (args.output / f"asr-{mode}.json").write_text(
                    json.dumps(texts, ensure_ascii=False, indent=2), encoding="utf-8",
                )
            else:
                _write_result(args.output, f"qwen-{mode}", f"Qwen staged ({mode})",
                              segments, round(len(audio) * 1000 / rate))
            summary["modes"].append(metrics)
            (args.output / f"{args.stage}_timings.json").write_text(
                json.dumps(summary, indent=2), encoding="utf-8",
            )
    finally:
        stop.set()
        thread.join(timeout=6)
        (args.output / f"cpu_memory_samples_{args.stage}.json").write_text(json.dumps({
            "baseline": baseline, "samples": snapshots,
        }, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
