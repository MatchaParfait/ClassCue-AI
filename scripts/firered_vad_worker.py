"""Isolated CPU FireRedVAD CLI; writes seconds-based intervals as JSON."""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> None:
    from classcue.qwen_vad_firered import DEFAULT_MODEL_PATH, detect_firered

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-path", type=Path, default=DEFAULT_MODEL_PATH)
    parser.add_argument("--min-silence-ms", type=int, default=300)
    parser.add_argument("--speech-pad-ms", type=int, default=0)
    parser.add_argument("--cpu-threads", type=int, default=2)
    parser.add_argument("--inference-chunk-seconds", type=float, default=60)
    parser.add_argument("--context-seconds", type=float, default=2)
    args = parser.parse_args()
    with redirect_stdout(sys.stderr):
        result = detect_firered(
            args.audio,
            args.model_path,
            min_silence_ms=args.min_silence_ms,
            speech_pad_ms=args.speech_pad_ms,
            cpu_threads=args.cpu_threads,
            inference_chunk_seconds=args.inference_chunk_seconds,
            context_seconds=args.context_seconds,
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=True, indent=2), encoding="utf-8")
    print(json.dumps({
        "backend": result["backend"],
        "intervals": len(result["speech_intervals"]),
        "load_seconds": result["load_seconds"],
        "inference_seconds": result["inference_seconds"],
        "output": str(args.output.resolve()),
    }, ensure_ascii=True))


if __name__ == "__main__":
    main()
