"""Explicitly prepared, isolated CPU and CUDA FunASR runtimes."""

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

from classcue.asr_models import Progress, ROOT
from classcue.asr_storage import log_directory, active_storage_root as storage_root
from classcue.paraformer_routing import gpu_batch_size


ENVIRONMENT = storage_root(ROOT) / ".runtime" / "paraformer-cpu"
RUNTIME_VERSION = "paraformer-funasr-1.4.16-torch-2.7.1-cpu-bicif-v1"
GPU_ENVIRONMENT = storage_root(ROOT) / ".runtime" / "paraformer-gpu"
GPU_RUNTIME_VERSION = "paraformer-funasr-1.4.16-torch-2.7.1-cu128-bicif-v1"
_CUDA_LOCK = RLock()
_CUDA_CACHE = None


def runtime_directory(device: str = "cpu") -> Path:
    if device not in ("cpu", "gpu"):
        raise ValueError("Paraformer device must be cpu or gpu")
    return GPU_ENVIRONMENT if device == "gpu" else ENVIRONMENT


def runtime_python(device: str = "cpu") -> Path:
    return runtime_directory(device) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def runtime_is_ready(device: str = "cpu") -> bool:
    directory = runtime_directory(device)
    try:
        marker = json.loads((directory / "ready.json").read_text(encoding="utf-8"))
        version = GPU_RUNTIME_VERSION if device == "gpu" else RUNTIME_VERSION
        return (marker["version"] == version and marker.get("gpu_wheel") is (device == "gpu")
                and runtime_python(device).is_file())
    except (OSError, ValueError, KeyError, TypeError):
        return False


def runtime_size() -> int:
    return sum(file.stat().st_size for directory in (ENVIRONMENT, GPU_ENVIRONMENT)
               for file in directory.rglob("*") if file.is_file() and not file.is_symlink())


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


def invalidate_cuda_status() -> None:
    global _CUDA_CACHE
    with _CUDA_LOCK:
        _CUDA_CACHE = None


def cuda_status(*, refresh: bool = False) -> dict:
    """Probe the actual GPU runtime, not the server's possibly CPU-only torch."""
    global _CUDA_CACHE
    with _CUDA_LOCK:
        if not refresh and _CUDA_CACHE is not None:
            timestamp, directory, status = _CUDA_CACHE
            if directory == str(GPU_ENVIRONMENT) and monotonic() - timestamp < 15:
                return dict(status)
        prepared = runtime_is_ready("gpu")
        status = {
            "gpu_available": False, "gpu_runtime_ready": prepared,
            "gpu_prepare_available": _has_nvidia(),
            "gpu_unavailable_reason": "GPU runtime is not prepared.",
            "gpu_name": None,
            "gpu_total_memory_mib": None, "gpu_batch_size": None,
        }
        if prepared:
            try:
                result = subprocess.run(
                    [str(runtime_python("gpu")), "-c",
                     "import json, torch; "
                     "assert torch.cuda.is_available(), 'CUDA is unavailable'; "
                     "x = torch.ones(1, device='cuda:0'); "
                     "torch.cuda.synchronize(); "
                     "print(json.dumps({'gpu_name': torch.cuda.get_device_name(0), "
                     "'gpu_total_memory_mib': torch.cuda.get_device_properties(0).total_memory / 2**20}))"],
                    capture_output=True, text=True, timeout=30,
                    env={**os.environ, "PYTHONUTF8": "1"},
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                if result.returncode == 0:
                    info = json.loads(result.stdout)
                    total_mib = info.get("gpu_total_memory_mib")
                    status.update(gpu_name=info["gpu_name"], gpu_total_memory_mib=total_mib)
                    try:
                        batch = gpu_batch_size(total_mib)
                        status.update(
                            gpu_available=True, gpu_batch_size=batch, gpu_unavailable_reason=None,
                        )
                    except ValueError as exc:
                        status["gpu_unavailable_reason"] = str(exc)
                else:
                    status["gpu_unavailable_reason"] = "CUDA is unavailable in the GPU runtime."
            except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
                status["gpu_unavailable_reason"] = "CUDA runtime check failed."
        elif not status["gpu_prepare_available"]:
            status["gpu_unavailable_reason"] = "No usable NVIDIA driver/device was detected."
        _CUDA_CACHE = (monotonic(), str(GPU_ENVIRONMENT), status)
        return dict(status)


def require_cuda() -> None:
    status = cuda_status(refresh=True)
    if not status["gpu_available"]:
        raise ValueError(f"Paraformer GPU cannot be selected: {status['gpu_unavailable_reason']}")


def clear_runtime(device: str | None = None) -> int:
    from classcue.cleanup_progress import cleanup_directories
    devices = ("cpu", "gpu") if device is None else (device,)
    root = storage_root(ROOT) / ".runtime"
    targets = [(runtime_directory(item), f"Paraformer {item.upper()} 环境") for item in devices]
    try:
        return cleanup_directories(root, targets, scope="Paraformer 运行环境",
                                   locks=[root / f"paraformer-{item}-setup.lock" for item in devices])
    finally:
        invalidate_cuda_status()


def install_runtime(progress: Progress | None = None, *, device: str = "cpu") -> None:
    from filelock import FileLock, Timeout

    directory = runtime_directory(device)
    directory.parent.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(str(directory.parent / f"paraformer-{device}-setup.lock"), timeout=0):
            if runtime_is_ready(device):
                if device == "gpu":
                    require_cuda()
                return
            if device == "gpu" and not _has_nvidia():
                raise ValueError("Paraformer GPU requires a usable NVIDIA driver/device.")
            _install_runtime(progress, device=device)
    except Timeout as exc:
        raise RuntimeError("Paraformer CPU runtime is being prepared by another process") from exc


def _install_runtime(progress: Progress | None, *, device: str = "cpu") -> None:
    directory = runtime_directory(device)
    target = directory.resolve()
    root = (storage_root(ROOT) / ".runtime").resolve()
    if target == root or not target.is_relative_to(root) or target != directory.absolute():
        raise ValueError("Paraformer environment is not application-owned")
    log_path = log_directory(ROOT) / f"paraformer-{device}-install.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    bundled_uv = ROOT / ".runtime" / "bin" / ("uv.exe" if os.name == "nt" else "uv")
    uv = str(bundled_uv) if bundled_uv.is_file() else shutil.which("uv")
    env = os.environ.copy()
    env.update({
        "UV_CACHE_DIR": str(storage_root(ROOT) / ".runtime" / "cache"),
        "UV_PYTHON_INSTALL_DIR": str(ROOT / ".runtime" / "python"),
        "UV_HTTP_TIMEOUT": "300", "PIP_DEFAULT_TIMEOUT": "300", "PYTHONUTF8": "1",
    })
    if device == "cpu":
        env["CUDA_VISIBLE_DEVICES"] = ""
    if uv:
        commands = [[uv, "venv", "--python", "3.12", str(directory)]]
        pip = [uv, "pip", "install", "--python", str(runtime_python(device))]
    else:
        if not (3, 11) <= sys.version_info[:2] <= (3, 12):
            raise RuntimeError("Paraformer preparation needs uv or Python 3.11/3.12")
        commands = [[sys.executable, "-m", "venv", str(directory)]]
        pip = [str(runtime_python(device)), "-m", "pip", "install"]
    if runtime_python(device).is_file():
        commands.clear()
    commands += [
        pip + ["torch==2.7.1", "torchaudio==2.7.1", "--index-url",
               f"https://download.pytorch.org/whl/{'cu128' if device == 'gpu' else 'cpu'}"],
        pip + ["-r", str(ROOT / "scripts" / "paraformer_requirements.txt")],
        [str(runtime_python(device)), "-c",
         "import torch; import torchaudio; import av; from funasr import AutoModel; "
         f"import sys; sys.path.insert(0, {str(ROOT)!r}); "
         "import core; import paraformer_transcriber; "
         + ("assert torch.version.cuda is None; assert not torch.cuda.is_available(); "
            "print('Paraformer CPU runtime ready')" if device == "cpu" else
            "assert torch.cuda.is_available(), 'CUDA is unavailable'; "
            "x = torch.ones(1, device='cuda:0'); torch.cuda.synchronize(); "
            "print('Paraformer GPU runtime ready')")],
    ]
    with log_path.open("a", encoding="utf-8") as log:
        for index, command in enumerate(commands):
            if progress:
                progress(index / len(commands), f"Preparing Paraformer isolated {device.upper()} runtime")
            completed = run_logged(
                command, progress=progress, step=index + 1, steps=len(commands),
                stdout=log, stderr=log, env=env, timeout=3600,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            log.flush()
            if completed.returncode:
                raise RuntimeError(f"Paraformer {device.upper()} dependencies failed; see {log_path}")
    (directory / "ready.json").write_text(json.dumps({
        "version": GPU_RUNTIME_VERSION if device == "gpu" else RUNTIME_VERSION,
        "gpu_wheel": device == "gpu",
    }), encoding="utf-8")
    invalidate_cuda_status()
    if progress:
        progress(1.0, f"Paraformer {device.upper()} runtime ready")


def _validate_weights(paths: dict[str, Path]) -> None:
    try:
        from classcue.paraformer_transcriber import _validate_component

        for component, path in paths.items():
            _validate_component(path, component)
    except ModuleNotFoundError as exc:
        if exc.name != "yaml":
            raise
        device = "cpu" if runtime_is_ready() else "gpu"
        if not runtime_is_ready(device):
            raise RuntimeError("Prepare a Paraformer runtime for weight validation") from exc
        code = (
            f"import sys; sys.path.insert(0, {str(ROOT)!r}); "
            "from classcue.paraformer_transcriber import _validate_component; "
            + "; ".join(
                f"_validate_component({str(path)!r}, {component!r})" for component, path in paths.items()
            )
        )
        subprocess.run(
            [str(runtime_python(device)), "-c", code], check=True, capture_output=True,
            text=True, encoding="utf-8", timeout=30,
            env={**os.environ, "CUDA_VISIBLE_DEVICES": "", "PYTHONUTF8": "1"},
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )


def prepare_weights(progress: Progress | None = None, *, reuse_legacy: bool = True) -> None:
    from classcue.asr_models import (
        MODEL_ROOT, PARAFORMER_REQUIRED_FILES, _managed_path, component_paths,
        get_model, model_directory,
    )

    model = get_model("paraformer-large-cpu")
    directory = model_directory(model.id)
    paths = component_paths(model.id)
    legacy = _managed_path(MODEL_ROOT / "paraformer-large-vad-punc")
    reused = False
    try:
        if not reuse_legacy:
            raise ValueError("Explicit preparation downloads instead of importing a legacy cache")
        marker = json.loads((legacy / "ready.json").read_text(encoding="utf-8"))
        for component, repository in model.repositories:
            aliases = {repository, repository.replace("iic/", "damo/", 1)}
            if marker["repositories"].get(component) not in aliases:
                raise ValueError("Legacy Paraformer cache is not from the expected official repository")
        for component, names in PARAFORMER_REQUIRED_FILES.items():
            for name in names:
                source = _managed_path(legacy / component / name)
                if source.stat().st_size != marker["files"][f"{component}/{name}"]:
                    raise ValueError("Legacy Paraformer cache size mismatch")
                if not source.stat().st_size:
                    raise ValueError("Legacy Paraformer component is empty")
        _validate_weights({name: legacy / name for name in PARAFORMER_REQUIRED_FILES})
        for component, names in PARAFORMER_REQUIRED_FILES.items():
            paths[component].mkdir(parents=True, exist_ok=True)
            for name in names:
                shutil.copy2(legacy / component / name, paths[component] / name)
        reused = True
    except (OSError, ValueError, KeyError, TypeError):
        pass
    if not reused:
        device = "cpu" if runtime_is_ready() else "gpu"
        if not runtime_is_ready(device):
            raise RuntimeError("Prepare a Paraformer runtime before downloading weights")
        if progress:
            progress(.05, "Downloading official Paraformer BiCIF and FSMN-VAD weights")
        prepare_component_files(model.id, progress)
        log_path = log_directory(ROOT) / "paraformer-download.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log:
            completed = run_logged(
                [str(runtime_python(device)), "-u", str(ROOT / "scripts" / "prepare_paraformer.py"), "--validate-only"],
                relay=True, progress=progress,
                stdout=log, stderr=log, timeout=3600,
                env={**os.environ, "PYTHONUTF8": "1", "CUDA_VISIBLE_DEVICES": ""},
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        if completed.returncode:
            raise RuntimeError(f"Paraformer model preparation failed; see {log_path}")
        return
    files = {}
    _validate_weights(paths)
    for component, names in PARAFORMER_REQUIRED_FILES.items():
        for name in names:
            file = _managed_path(paths[component] / name)
            files[file.relative_to(directory).as_posix()] = file.stat().st_size
    directory.joinpath("ready.json").write_text(json.dumps({
        "repositories": dict(model.repositories), "files": files,
        "source": "validated-existing-official-bicif",
    }), encoding="utf-8")
    if progress:
        progress(1.0, "Reused official Paraformer BiCIF and FSMN-VAD weights")
