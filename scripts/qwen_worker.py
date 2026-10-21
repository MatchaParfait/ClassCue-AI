"""Line-delimited JSON worker; heavy Qwen imports stay out of Streamlit."""

from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import asdict
import faulthandler
import json
from pathlib import Path
import sys
import traceback


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> None:
    faulthandler.enable()
    channel = sys.stdout

    def emit(value: dict) -> None:
        channel.write(json.dumps(value, ensure_ascii=True) + "\n")
        channel.flush()

    model = None
    paths = None
    for line in sys.stdin:
        try:
            request = json.loads(line)
            with redirect_stdout(sys.stderr):
                from qwen_transcriber import (
                    load_qwen_model, transcribe_qwen, transcribe_qwen_batch,
                )

                requested_paths = (request["recognizer"], request["aligner"])
                if model is None or paths != requested_paths:
                    emit({"event": "progress", "fraction": 0.01, "message": "加载 Qwen ASR 与时间对齐模型"})
                    model = load_qwen_model(*requested_paths)
                    paths = requested_paths
                progress = lambda fraction, message: emit({
                        "event": "progress", "fraction": fraction, "message": message,
                    })
                if "audio_paths" in request:
                    results = transcribe_qwen_batch(
                        model, request["audio_paths"], request.get("batch_size", 2), progress,
                    )
                    response = {"event": "result", "results": [
                        {"segments": [asdict(s) for s in segments], "duration_ms": duration}
                        for segments, duration in results
                    ]}
                else:
                    audit = {}
                    segments, duration = transcribe_qwen(
                        model, request["audio_path"], progress=progress,
                        silence_report=audit, **request.get("silence_options", {}),
                    )
                    if request.get("silence_audit_output") and audit:
                        audit_path = Path(request["silence_audit_output"])
                        audit_path.parent.mkdir(parents=True, exist_ok=True)
                        audit_path.write_text(
                            json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8",
                        )
                    response = {"event": "result", "segments": [asdict(s) for s in segments],
                                "duration_ms": duration}
            emit(response)
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            emit({"event": "error", "message": f"{type(exc).__name__}: {exc}"})


if __name__ == "__main__":
    main()
