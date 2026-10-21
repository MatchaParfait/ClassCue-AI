"""Explicitly prepared, pinned official Dolphin CPU and CUDA runtimes."""

from __future__ import annotations

from classcue.preparation_progress import run_logged, prepare_component_files

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from threading import RLock
from time import monotonic

from classcue.asr_models import ROOT
from classcue.asr_storage import log_directory, active_storage_root as storage_root


RUNTIME_ROOT = storage_root(ROOT) / ".runtime"
SOURCE_COMMIT = "78ea615194777f91a3970ff010b5d678d0bbf5f0"
SOURCE = RUNTIME_ROOT / "sources" / "dolphin-78ea615"
RUNTIME_VERSION = f"dolphin-{SOURCE_COMMIT}-torch-2.7.1-v1"
_LOCK = RLock()
_CUDA_CACHE = None


def runtime_directory(device="cpu"):
    if device not in ("cpu", "gpu"):
        raise ValueError("Dolphin device must be cpu or gpu")
    return RUNTIME_ROOT / f"dolphin-{device}"


def runtime_python(device="cpu"):
    return runtime_directory(device) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def runtime_is_ready(device="cpu"):
    try:
        marker = json.loads((runtime_directory(device) / "ready.json").read_text(encoding="utf-8"))
        return marker == {"version": RUNTIME_VERSION, "device": device} and runtime_python(device).is_file()
    except (OSError, ValueError):
        return False


def runtime_size():
    return sum(file.stat().st_size for device in ("cpu", "gpu")
               for file in runtime_directory(device).rglob("*")
               if file.is_file() and not file.is_symlink())


def cuda_status(*, refresh=False):
    global _CUDA_CACHE
    with _LOCK:
        if not refresh and _CUDA_CACHE and monotonic() - _CUDA_CACHE[0] < 15:
            return dict(_CUDA_CACHE[1])
        from classcue.paraformer_runtime import _has_nvidia

        ready = runtime_is_ready("gpu")
        status = {"gpu_available": False, "gpu_runtime_ready": ready,
                  "gpu_prepare_available": _has_nvidia(), "gpu_name": None,
                  "gpu_total_memory_mib": None,
                  "gpu_unavailable_reason": "Dolphin GPU runtime is not prepared."}
        if ready:
            try:
                result = subprocess.run(
                    [str(runtime_python("gpu")), "-c",
                     "import json, torch; assert torch.cuda.is_available(); "
                     "torch.ones(1, device='cuda:0'); torch.cuda.synchronize(); "
                     "print(json.dumps({'gpu_name': torch.cuda.get_device_name(0), "
                     "'gpu_total_memory_mib': torch.cuda.get_device_properties(0).total_memory / 2**20}))"],
                    capture_output=True, text=True, timeout=30,
                    env={**os.environ, "PYTHONUTF8": "1"},
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                if result.returncode == 0:
                    status.update(json.loads(result.stdout), gpu_available=True, gpu_unavailable_reason=None)
                else:
                    status["gpu_unavailable_reason"] = "CUDA is unavailable in the Dolphin GPU runtime."
            except (OSError, ValueError, subprocess.TimeoutExpired):
                status["gpu_unavailable_reason"] = "Dolphin CUDA runtime check failed."
        _CUDA_CACHE = (monotonic(), status)
        return dict(status)


def require_cuda():
    status = cuda_status(refresh=True)
    if not status["gpu_available"]:
        raise ValueError(f"Dolphin GPU cannot be selected: {status['gpu_unavailable_reason']}")


def install_runtime(progress=None, *, device="cpu"):
    global _CUDA_CACHE
    from filelock import FileLock

    directory = runtime_directory(device)
    root = RUNTIME_ROOT.resolve()
    if directory.resolve().parent != root or directory.is_symlink():
        raise ValueError("Dolphin runtime must be application-owned")
    root.mkdir(parents=True, exist_ok=True)
    with FileLock(str(root / f"dolphin-{device}-setup.lock"), timeout=0):
        if runtime_is_ready(device):
            return
        with FileLock(str(root / "dolphin-source-setup.lock"), timeout=0):
            SOURCE.parent.mkdir(parents=True, exist_ok=True)
            if not SOURCE.exists():
                result = run_logged(["git", "clone", "--progress", "https://github.com/DataoceanAI/Dolphin.git", str(SOURCE)],
                                    progress=progress, timeout=300)
                if result.returncode:
                    raise RuntimeError("Dolphin official source download failed")
            result = run_logged(["git", "-C", str(SOURCE), "checkout", "--detach", SOURCE_COMMIT],
                                progress=progress, timeout=60)
            if result.returncode:
                raise RuntimeError("Dolphin official source checkout failed")
            sha = subprocess.check_output(["git", "-C", str(SOURCE), "rev-parse", "HEAD"], text=True).strip()
            dirty = subprocess.check_output(["git", "-C", str(SOURCE), "status", "--porcelain", "--untracked-files=no"], text=True)
            if sha != SOURCE_COMMIT or dirty:
                raise RuntimeError("Dolphin official source must match the pinned unmodified commit")
        from classcue.nano_runtime import runtime_python as existing_python

        bootstrap = existing_python("cpu") if existing_python("cpu").is_file() else Path(sys.executable)
        python = runtime_python(device)
        commands = [] if python.is_file() else [[str(bootstrap), "-m", "venv", str(directory)]]
        pip = [str(python), "-m", "pip", "install", "--disable-pip-version-check"]
        wheel = root / "wheels" / "torch-2.7.1+cu128-cp312-cp312-win_amd64.whl"
        if device == "gpu" and wheel.is_file():
            commands.append(pip + [str(wheel)])
        commands += [
            pip + ["torch==2.7.1", "torchaudio==2.7.1", "--index-url",
                   f"https://download.pytorch.org/whl/{'cu128' if device == 'gpu' else 'cpu'}"],
            pip + ["-r", str(ROOT / "scripts" / "dolphin_requirements.txt")],
            pip + ["--no-deps", "--no-build-isolation", str(SOURCE)],
            [str(python), "-c", "import dolphin, torch, torchaudio; "
             "from dolphin.model import ASRModel; assert hasattr(ASRModel, 'get_token_timestamp_torchaudio'); "
             + ("assert torch.cuda.is_available(); torch.ones(1, device='cuda:0'); torch.cuda.synchronize()"
                if device == "gpu" else "assert torch.version.cuda is None; assert not torch.cuda.is_available()")],
        ]
        env = {**os.environ, "PYTHONUTF8": "1", "PIP_DEFAULT_TIMEOUT": "300",
               "PIP_CACHE_DIR": str(root / "cache" / "pip"), "TMP": str(root / "tmp"),
               "TEMP": str(root / "tmp")}
        (root / "tmp").mkdir(exist_ok=True)
        if device == "cpu":
            env["CUDA_VISIBLE_DEVICES"] = ""
        log_path = log_directory(ROOT) / f"dolphin-{device}-install.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log:
            for index, command in enumerate(commands):
                if progress:
                    progress(index / len(commands), f"Preparing Dolphin {device.upper()} runtime")
                result = run_logged(command, progress=progress, step=index + 1, steps=len(commands),
                                        stdout=log, stderr=log, env=env, timeout=3600,
                                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                log.flush()
                if result.returncode:
                    raise RuntimeError(f"Dolphin runtime preparation failed; see {log_path}")
        (directory / "ready.json").write_text(json.dumps({"version": RUNTIME_VERSION, "device": device}), encoding="utf-8")
        _CUDA_CACHE = None


def prepare_weights(progress=None):
    device = "gpu" if runtime_is_ready("gpu") else "cpu"
    if not runtime_is_ready(device):
        raise RuntimeError("Prepare a Dolphin runtime before downloading weights")
    if progress:
        progress(.05, "Downloading official Dolphin small.cn weights")
    prepare_component_files("dolphin-small-cn", progress)
    log_path = log_directory(ROOT) / "dolphin-download.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        result = run_logged(
            [str(runtime_python(device)), "-u", str(ROOT / "scripts" / "prepare_dolphin.py"), "--validate-only"],
            relay=True, progress=progress,
            stdout=log, stderr=log, timeout=7200,
            env={**os.environ, "PYTHONUTF8": "1", "CUDA_VISIBLE_DEVICES": "",
                 "MODELSCOPE_CACHE": str(RUNTIME_ROOT / "cache" / "modelscope")},
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if result.returncode:
        raise RuntimeError(f"Dolphin weights preparation failed; see {log_path}")
    if progress:
        progress(1.0, "Dolphin small.cn weights ready")


def clear_runtime(device=None):
    global _CUDA_CACHE
    from classcue.cleanup_progress import cleanup_directories
    devices = ("cpu", "gpu") if device is None else (device,)
    targets = [(runtime_directory(item), f"Dolphin {item.upper()} 环境") for item in devices]
    try:
        return cleanup_directories(RUNTIME_ROOT, targets, scope="Dolphin 运行环境",
                                   locks=[RUNTIME_ROOT / f"dolphin-{item}-setup.lock" for item in devices])
    finally:
        _CUDA_CACHE = None
