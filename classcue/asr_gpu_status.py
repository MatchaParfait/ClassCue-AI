"""Read CUDA device identity from an ASR runtime without loading a model."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess


def inspect_cuda_device(python: Path, ready: bool, name: str) -> dict[str, object]:
    status: dict[str, object] = {
        "gpu_available": False,
        "gpu_runtime_ready": ready,
        "gpu_name": None,
        "gpu_unavailable_reason": f"{name} GPU 环境未准备",
    }
    if not ready:
        return status
    try:
        result = subprocess.run(
            [str(python), "-c",
             "import json, torch; available = bool(torch.cuda.is_available()); "
             "print(json.dumps({'gpu_available': available, "
             "'gpu_name': torch.cuda.get_device_name(0) if available else None}))"],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "PYTHONUTF8": "1"},
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode != 0:
            raise ValueError("CUDA device probe failed")
        info = json.loads(result.stdout)
        if info["gpu_available"] is not True:
            status["gpu_unavailable_reason"] = f"{name} 运行环境中 CUDA 不可用"
            return status
        gpu_name = info["gpu_name"]
        if not isinstance(gpu_name, str) or not gpu_name.strip():
            raise ValueError("Missing CUDA device name")
        status.update(gpu_available=True, gpu_name=gpu_name.strip(), gpu_unavailable_reason=None)
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        status["gpu_unavailable_reason"] = f"{name} GPU 设备检查失败"
    return status
