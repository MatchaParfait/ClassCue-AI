"""Compare raw streaming FSMN endpoints with whole-file offline inference."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import statistics
import sys
import time
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def statistics_for(spans):
    lengths = [(end - start) / 1000 for start, end in spans]
    gaps = [(right[0] - left[1]) / 1000 for left, right in zip(spans, spans[1:])]
    return {
        "segments": len(spans), "speech_seconds": sum(lengths),
        "median_seconds": statistics.median(lengths) if lengths else None,
        "mean_seconds": statistics.mean(lengths) if lengths else None,
        "shorter_than_1s": sum(value < 1 for value in lengths),
        "shorter_than_2s": sum(value < 2 for value in lengths),
        "max_seconds": max(lengths, default=0),
        "median_gap_seconds": statistics.median(gaps) if gaps else None,
    }


def run_mode(vad_path, audio_path, name, options, stream, *, model_conf=None):
    import numpy as np
    from funasr import AutoModel

    vad = AutoModel(model=str(vad_path), device="cpu", ngpu=0, ncpu=4, hub="ms",
                    disable_update=True, check_latest=False, disable_pbar=True,
                    trust_remote_code=False, **(model_conf or {}))
    opts = vad.model.vad_opts
    before = {key: getattr(opts, key) for key in
              ("max_end_silence_time", "max_single_segment_time", "speech_noise_thres")}
    started = time.perf_counter()
    events, spans, cache_evidence = [], [], {}
    if stream:
        cache = {}
        pending = None
        first_stats = None
        identity_changes = 0
        with wave.open(str(audio_path), "rb") as source:
            total = source.getnframes()
            read = 0
            while read < total:
                pcm = source.readframes(min(9600, total - read))
                read += len(pcm) // 2
                audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
                outputs = vad.generate(input=audio, cache=cache, is_final=read == total,
                                       chunk_size=600, is_streaming_input=True,
                                       batch_size=1, fs=16000, **options)
                stats = cache.get("stats")
                if read < total:
                    if first_stats is None:
                        first_stats = stats
                    if stats is not first_stats:
                        identity_changes += 1
                values = outputs[0].get("value", [])
                events.append({"read_ms": read / 16, "is_final": read == total, "value": values})
                for start, end in values:
                    if start != -1:
                        if pending is not None:
                            raise ValueError("Unpaired duplicate start event")
                        pending = start
                    if end != -1:
                        if pending is None:
                            raise ValueError("Unpaired end event")
                        if end > pending:
                            spans.append([int(pending), int(end)])
                        pending = None
            if pending is not None:
                raise ValueError("Final interval did not close")
        cache_evidence = {"calls": len(events), "premature_stats_reinitializations": identity_changes,
                          "final_calls": sum(event["is_final"] for event in events)}
    else:
        outputs = vad.generate(input=str(audio_path), chunk_size=60000,
                               is_streaming_input=False, is_final=True,
                               batch_size=1, fs=16000, **options)
        spans = outputs[0]["value"]
        events = outputs
    elapsed = time.perf_counter() - started
    after = {key: getattr(opts, key) for key in before}
    return {
        "mode": name, "stream": stream, "options": options, "model_conf": model_conf,
        "wall_seconds": elapsed,
        "effective_model_options_before": before, "effective_model_options_after": after,
        "statistics": statistics_for(spans), "spans_ms": spans,
        "cache_evidence": cache_evidence, "events": events,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeline", type=Path, required=True)
    parser.add_argument("--sample", type=Path, required=True)
    parser.add_argument("--vad", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.environ.update({
        "CUDA_VISIBLE_DEVICES": "", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        "MODELSCOPE_OFFLINE": "1", "OMP_NUM_THREADS": "4", "MKL_NUM_THREADS": "4",
        "OPENBLAS_NUM_THREADS": "4", "PYTHONUTF8": "1",
    })
    import torch
    from funasr.models.fsmn_vad_streaming import model as fsmn_module
    from scripts.benchmark_paraformer_cpu import prepare_sample, save_json

    torch.set_num_threads(4)
    torch.set_num_interop_threads(1)
    args.output.mkdir(parents=True, exist_ok=True)
    if (args.output / "result.json").exists():
        raise FileExistsError("Refusing to overwrite diagnostic evidence")
    first_sample = args.output / "first300.wav"
    prepare_sample(args.timeline, first_sample, start_seconds=0, duration_seconds=300)
    source = Path(fsmn_module.__file__)
    result = {
        "torch": torch.__version__, "torch_cuda": torch.version.cuda,
        "funasr": importlib.metadata.version("funasr"),
        "fsmn_source": str(source),
        "fsmn_source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "cases": {},
    }
    modes = [
        ("stream_fixed800", {"max_end_silence_time": 800, "max_single_segment_time": 30000}, True),
        ("offline_fixed800", {"max_end_silence_time": 800, "max_single_segment_time": 30000}, False),
        ("offline_default", {}, False),
        ("stream_fixed2000", {"max_end_silence_time": 2000}, True),
    ]
    for label, path in (("first300", first_sample), ("600_to_900", args.sample)):
        entries = []
        result["cases"][label] = entries
        for name, options, stream in modes:
            value = run_mode(args.vad, path, name, options, stream)
            entries.append(value)
            save_json(args.output / "result.json", result)
            print(label, name, json.dumps(value["statistics"]), flush=True)
        print("fixed stream equals offline:", entries[0]["spans_ms"] == entries[1]["spans_ms"],
              flush=True)
    full = run_mode(args.vad, args.timeline, modes[0][0], modes[0][1], True)
    result["full_stream_fixed800"] = full
    baseline = json.loads((args.timeline.parent / "result.json").read_text(encoding="utf-8"))
    result["matches_original_full_run"] = full["spans_ms"] == baseline["audit"]["speech_intervals_ms"]
    save_json(args.output / "result.json", result)
    print("full", json.dumps(full["statistics"]), flush=True)
    print("matches_original_full_run:", result["matches_original_full_run"], flush=True)


if __name__ == "__main__":
    main()
