"""Prepare FireRedASR2 dependencies in an isolated environment."""

from __future__ import annotations

from preparation_progress import run_logged

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from asr_models import Progress, ROOT, selected_firered_device, require_firered_gpu
from asr_storage import log_directory, active_storage_root as storage_root


ENVIRONMENT = storage_root(ROOT) / ".runtime" / "firered-asr2"
RUNTIME_VERSION = "firered-asr2-4e7d9aa-torch-2.10.0-cu128-fbank-1.22.3-v3-vad"


def runtime_python() -> Path:
    return ENVIRONMENT / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def runtime_is_ready() -> bool:
    try:
        version = json.loads((ENVIRONMENT / "ready.json").read_text(encoding="utf-8"))["version"]
        return version == RUNTIME_VERSION and runtime_python().is_file()
    except (OSError, ValueError, KeyError, TypeError):
        return False


def runtime_size() -> int:
    if not ENVIRONMENT.exists():
        return 0
    return sum(file.stat().st_size for file in ENVIRONMENT.rglob("*")
               if file.is_file() and not file.is_symlink())


def clear_runtime() -> int:
    from cleanup_progress import cleanup_directories
    root = storage_root(ROOT) / ".runtime"
    return cleanup_directories(root, [(ENVIRONMENT, "FireRed GPU 环境")],
                               scope="FireRed 运行环境", locks=[root / "firered-setup.lock"])


def _has_nvidia() -> bool:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


def install_runtime(progress: Progress | None = None) -> None:
    from filelock import FileLock, Timeout

    require_firered_gpu(selected_firered_device())
    ENVIRONMENT.parent.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(str(ENVIRONMENT.parent / "firered-setup.lock"), timeout=0):
            if runtime_is_ready():
                marker = json.loads((ENVIRONMENT / "ready.json").read_text(encoding="utf-8"))
                needs_cuda = (
                    selected_firered_device() == "gpu"
                    and marker.get("gpu_wheel") is False
                    and _has_nvidia()
                )
                if not needs_cuda:
                    return
            _install_runtime(progress)
    except Timeout as exc:
        raise RuntimeError("FireRedASR2 运行环境正在安装，请等待安装完成。") from exc


def _install_runtime(progress: Progress | None) -> None:
    target = ENVIRONMENT.resolve()
    root = (storage_root(ROOT) / ".runtime").resolve()
    if target == root or not target.is_relative_to(root) or target != ENVIRONMENT.absolute():
        raise ValueError("FireRed environment path is not an application-owned directory")
    log_path = log_directory(ROOT) / "firered-install.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    bundled_uv = ROOT / ".runtime" / "bin" / ("uv.exe" if os.name == "nt" else "uv")
    uv = str(bundled_uv) if bundled_uv.is_file() else shutil.which("uv")
    env = os.environ.copy()
    env["UV_CACHE_DIR"] = str(storage_root(ROOT) / ".runtime" / "cache")
    env["UV_PYTHON_INSTALL_DIR"] = str(ROOT / ".runtime" / "python")
    env["UV_HTTP_TIMEOUT"] = "300"
    env["PYTHONUTF8"] = "1"
    cpu_selected = selected_firered_device() == "cpu"
    if cpu_selected:
        env["CUDA_VISIBLE_DEVICES"] = "-1"
    gpu = not cpu_selected and _has_nvidia()
    index = f"https://download.pytorch.org/whl/{'cu128' if gpu else 'cpu'}"
    if uv:
        commands = [[uv, "venv", "--python", "3.12", str(ENVIRONMENT)]]
        pip = [uv, "pip", "install", "--python", str(runtime_python())]
    else:
        if not (3, 11) <= sys.version_info[:2] <= (3, 12):
            raise RuntimeError("安装 FireRedASR2 需要 uv 或 Python 3.11/3.12；请使用一键启动入口。")
        commands = [[sys.executable, "-m", "venv", str(ENVIRONMENT)]]
        pip = [str(runtime_python()), "-m", "pip", "install"]
    if runtime_python().is_file():
        commands.clear()
    commands += [
        pip + ["torch==2.10.0", "torchaudio==2.10.0", "--index-url", index],
        pip + ["-r", str(ROOT / "scripts" / "firered_requirements.txt")],
        [str(runtime_python()), "-c",
         f"import sys; sys.path.insert(0, {str(ROOT)!r}); "
         "import kaldi_native_fbank; import av; import pydantic; import torch; import torchaudio; "
         "from fireredasr2s.fireredasr2 import FireRedAsr2; "
         "from fireredasr2s.fireredvad import FireRedVad; "
         "from fireredasr2s.fireredasr2system import FireRedAsr2System; "
         "from firered_transcriber import load_firered_model, transcribe_firered; "
         + ("print('FireRedASR2 runtime ready; CPU mode')" if cpu_selected else
            "print('FireRedASR2 runtime ready; CUDA:', torch.cuda.is_available())")],
    ]
    with log_path.open("a", encoding="utf-8") as log:
        for index, command in enumerate(commands):
            if progress:
                progress(index / len(commands), "准备 FireRedASR2 独立运行环境（CUDA）" if gpu
                         else "准备 FireRedASR2 独立运行环境（CPU）")
            completed = run_logged(
                command, progress=progress, step=index + 1, steps=len(commands),
                stdout=log, stderr=log, env=env, timeout=3600,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            log.flush()
            if completed.returncode:
                raise RuntimeError(f"FireRedASR2 依赖安装失败，详情见 {log_path}；重试会复用下载缓存。")
    (ENVIRONMENT / "ready.json").write_text(
        json.dumps({"version": RUNTIME_VERSION, "gpu_wheel": gpu}), encoding="utf-8"
    )
    if progress:
        progress(1.0, "FireRedASR2 独立运行环境已就绪")
