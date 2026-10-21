"""Run optional CPU FireRedVAD in its own dependency environment."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from asr_storage import storage_root


ROOT = Path(__file__).resolve().parent


def detect_speech(audio_path, backend: str, *, model_path=None, python_path=None, should_stop=None):
    if backend != "firered":
        raise ValueError("Unknown VAD backend")
    runtime = storage_root(ROOT) / ".runtime" / "firered-asr2"
    interpreter = Path(python_path) if python_path else runtime / (
        "Scripts/python.exe" if os.name == "nt" else "bin/python"
    )
    if not interpreter.is_file():
        raise RuntimeError(f"{backend} VAD runtime is not ready: {interpreter}")
    with tempfile.TemporaryDirectory(prefix=f"{backend}-vad-") as temporary:
        output = Path(temporary) / "speech.json"
        command = [
            str(interpreter), str(ROOT / "scripts" / f"{backend}_vad_worker.py"),
            "--audio", str(Path(audio_path).resolve()), "--output", str(output),
        ]
        if not model_path:
            from asr_models import component_paths
            model_path = component_paths("fireredasr2-aed")["vad"] / "VAD"
        command.extend(["--model-path", str(model_path)])
        started = time.perf_counter()
        env = os.environ.copy()
        env.update({"PYTHONUTF8": "1", "CUDA_VISIBLE_DEVICES": ""})
        from qwen_process import run_process
        lines = []
        code = run_process(command, env=env, cwd=ROOT, should_stop=should_stop,
                           on_line=lines.append, timeout=7200)
        result = subprocess.CompletedProcess(command, code, stdout="".join(lines), stderr="")
        if result.returncode:
            # Keep diagnostics local: model download errors can contain credential URLs.
            log_dir = ROOT / ".runtime" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log = log_dir / f"{backend}-vad-error.log"
            log.write_text(result.stderr + result.stdout, encoding="utf-8")
            raise RuntimeError(
                f"{backend} VAD failed (exit {result.returncode}); see {log}. "
                "Check model access and runtime preparation."
            )
        payload = json.loads(output.read_text(encoding="utf-8"))
        if payload.get("backend") != backend or not isinstance(payload.get("speech_intervals"), list):
            raise ValueError("Unexpected VAD worker response")
        payload["worker_wall_seconds"] = time.perf_counter() - started
        return payload
