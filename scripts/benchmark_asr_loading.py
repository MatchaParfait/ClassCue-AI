"""Compare old/new checkpoint loaders in fresh processes, without saved settings."""

import argparse
from contextlib import nullcontext
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import random
import sys
from threading import Event, Thread
from time import perf_counter
import wave


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", choices=("dolphin", "paraformer"), required=True)
    parser.add_argument("--device", choices=("cpu", "gpu"), default="cpu")
    parser.add_argument("--variant", choices=("original", "optimized"), required=True)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    from paraformer_transcriber import runtime_environment

    os.environ.update(runtime_environment(args.device, 8))
    import numpy as np
    import torch
    from asr_models import SETTINGS_PATH, component_paths
    import asr_checkpoint
    from qwen_memory import memory_snapshot
    from scripts.benchmark_paraformer_cpu import validate_output

    settings = SETTINGS_PATH.read_bytes() if SETTINGS_PATH.exists() else None
    random.seed(2026)
    np.random.seed(2026)
    torch.manual_seed(2026)

    if args.model == "dolphin":
        import dolphin_checkpoint
        import dolphin_transcriber as implementation
        if args.variant == "original":
            from dolphin import load_model

            dolphin_checkpoint.load_verified_dolphin_checkpoint = (
                lambda path, *, device: load_model("small.cn", model_dir=path, device=device)
            )
        loader = implementation.load_dolphin_model
        transcribe = implementation.transcribe_dolphin
        paths = component_paths("dolphin-small-cn")
    else:
        import paraformer_transcriber as implementation
        loader = implementation.load_paraformer_model
        transcribe = implementation.transcribe_paraformer
        paths = component_paths("paraformer-large-cpu")

    if args.variant == "original":
        asr_checkpoint.reuse_funasr_checkpoint_tensors = lambda _: nullcontext()

    before = memory_snapshot()
    samples, errors = [], []
    stop = Event()

    def sample():
        try:
            while not stop.wait(.05):
                samples.append(memory_snapshot())
        except Exception as exc:
            errors.append(str(exc))

    thread = Thread(target=sample, daemon=True)
    thread.start()
    started = perf_counter()
    try:
        model = loader(paths["recognizer"], paths["vad"], device=args.device, cpu_threads=8)
        implementation.synchronize_device(model)
        load_seconds = perf_counter() - started
        after = memory_snapshot()
        samples.append(after)
    finally:
        stop.set()
        thread.join()
    if errors:
        raise RuntimeError(errors)

    digest = hashlib.sha256()
    modules = (model.recognizer if args.model == "dolphin" else model.recognizer.model,
               model.vad.model)
    parameter_count = 0
    for index, module in enumerate(modules):
        parameter_count += sum(parameter.numel() for parameter in module.parameters())
        for name, tensor in sorted(module.state_dict().items()):
            tensor = tensor.detach().cpu().contiguous()
            digest.update(f"{index}:{name}:{tensor.dtype}:{tuple(tensor.shape)}".encode())
            digest.update(tensor.numpy().tobytes())

    # Use the same offline excerpt and RNG seed for text/timestamp equivalence.
    excerpt = args.output / "sample.wav"
    with wave.open(str(args.audio), "rb") as source, wave.open(str(excerpt), "wb") as target:
        target.setparams(source.getparams())
        target.writeframes(source.readframes(min(source.getnframes(), 15 * source.getframerate())))
    random.seed(2026)
    np.random.seed(2026)
    torch.manual_seed(2026)
    audit = {}
    cues, duration = transcribe(
        model, excerpt, batch_size=8, temporary_directory=args.output / "audio", audit=audit,
    )
    validation = validate_output(cues, duration, audit)
    if not cues or any(not cue.words for cue in cues):
        raise AssertionError("Expected native timestamps")
    if (SETTINGS_PATH.read_bytes() if SETTINGS_PATH.exists() else None) != settings:
        raise AssertionError("Saved model selection changed")
    result = {
        "model": args.model, "device": args.device, "variant": args.variant,
        "load_seconds": load_seconds, "before": before, "after_load": after,
        "load_sampled_peak_private_commit_mib": max(
            sample["process_private_commit_mib"] for sample in samples),
        "load_process_peak_working_mib": after["process_peak_working_mib"],
        "load_process_peak_private_commit_mib": after["process_peak_private_commit_mib"],
        "minimum_system_available_commit_mib": min(
            sample["system_available_commit_mib"] for sample in samples),
        "state_sha256": digest.hexdigest(), "parameter_count": parameter_count,
        "duration_ms": duration, "validation": validation,
        "segments": [asdict(cue) for cue in cues], "audit": audit,
        "saved_selection_unchanged": True,
    }
    (args.output / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(json.dumps({key: value for key, value in result.items()
                      if key not in ("before", "after_load", "segments", "audit")},
                     ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
