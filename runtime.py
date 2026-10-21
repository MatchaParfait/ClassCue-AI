"""Detect the independent GPU capabilities used by Whisper and Qwen."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BackendStatus:
    name: str
    device: str
    compute_type: str
    detail: str

    @property
    def is_gpu(self) -> bool:
        return self.device == "cuda"


def select_whisper_backend(cuda_device_count: int, compute_types: set[str]) -> BackendStatus:
    if cuda_device_count > 0 and "float16" in compute_types:
        return BackendStatus("Whisper", "cuda", "float16", f"检测到 {cuda_device_count} 个 CTranslate2 CUDA 设备")
    return BackendStatus("Whisper", "cpu", "int8", "CTranslate2 未检测到可用 CUDA 后端")


def whisper_backend() -> BackendStatus:
    try:
        import ctranslate2

        return select_whisper_backend(
            ctranslate2.get_cuda_device_count(),
            set(ctranslate2.get_supported_compute_types("cuda")),
        )
    except Exception as exc:
        return BackendStatus("Whisper", "cpu", "int8", f"CTranslate2 CUDA 不可用：{exc}")


def qwen_backend() -> BackendStatus:
    try:
        import torch

        if torch.cuda.is_available():
            return BackendStatus("Qwen", "cuda", "4-bit", f"PyTorch {torch.__version__} · CUDA {torch.version.cuda}")
        return BackendStatus("Qwen", "cpu", "float32", f"PyTorch {torch.__version__} 没有可用 CUDA")
    except Exception as exc:
        return BackendStatus("Qwen", "cpu", "unavailable", f"PyTorch 不可用：{exc}")
