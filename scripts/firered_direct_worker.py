"""Resident direct FireRed worker; production GPU jobs use firered_worker.py."""

from contextlib import redirect_stdout
from dataclasses import asdict
import argparse
import faulthandler
import json
import os
from pathlib import Path
import sys
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", choices=("gpu", "cpu"), default="cpu")
    device = parser.parse_args().device
    if device == "cpu":
        os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
    faulthandler.enable()
    channel = sys.stdout

    def emit(value):
        channel.write(json.dumps(value, ensure_ascii=True) + "\n")
        channel.flush()

    model, model_path = None, None
    for line in sys.stdin:
        try:
            request = json.loads(line)
            with redirect_stdout(sys.stderr):
                from asr_cpu import cpu_thread_policy
                from firered_transcriber import load_firered_model, transcribe_firered

                policy = cpu_thread_policy(device)
                if request.get("device", "gpu") != device:
                    raise ValueError("FireRed request device does not match worker device")
                requested_path = (request["recognizer"], request["vad"], policy["cpu_threads"])
                if model is None or model_path != requested_path:
                    if device == "cpu":
                        from paraformer_transcriber import runtime_environment

                        os.environ.update(runtime_environment(device, policy["cpu_threads"]))
                        os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
                        import torch

                        torch.set_num_threads(policy["cpu_threads"])
                        try:
                            torch.set_num_interop_threads(1)
                        except RuntimeError:
                            pass
                    emit({"event": "progress", "fraction": .01, "message": "Loading FireRedVAD + AED"})
                    model = load_firered_model(
                        *requested_path[:2], use_gpu=device == "gpu",
                        batch_size=3 if device == "cpu" else 1,
                    )
                    model_path = requested_path
                segments, duration = transcribe_firered(
                    model, request["audio_path"],
                    progress=lambda fraction, message: emit({
                        "event": "progress", "fraction": fraction, "message": message,
                    }),
                    temporary_directory=request["temporary_directory"],
                )
            emit({"event": "result", "segments": [asdict(segment) for segment in segments],
                  "duration_ms": duration, "audit": policy})
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            emit({"event": "error", "message": f"{type(exc).__name__}: {exc}"})


if __name__ == "__main__":
    main()
