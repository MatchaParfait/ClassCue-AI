"""Fixed Paraformer batches based on native CUDA memory, not free memory."""

import math


CAPACITY_ALLOWANCE_MIB = 64
GPU_MEMORY_NOTICE = "Paraformer GPU 要求原生显存至少 2 GiB，共享显存不计入；请手动选择 CPU。"


def gpu_batch_size(total_mib: float | None) -> int:
    if (isinstance(total_mib, bool) or not isinstance(total_mib, (int, float))
            or not math.isfinite(total_mib) or total_mib <= 0):
        raise ValueError("无法检测 GPU 原生显存容量；请手动选择 CPU。")
    if total_mib >= 3 * 1024 - CAPACITY_ALLOWANCE_MIB:
        return 16
    if total_mib >= 2 * 1024 - CAPACITY_ALLOWANCE_MIB:
        return 8
    raise ValueError(GPU_MEMORY_NOTICE)


def select_batch_size(device: str, batch_size: int | None = None, *,
                      gpu_total_mib: float | None = None) -> int:
    if device not in ("cpu", "gpu"):
        raise ValueError("Paraformer device must be cpu or gpu")
    automatic = gpu_batch_size(gpu_total_mib) if device == "gpu" else 4
    if batch_size is None:
        return automatic
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
        raise ValueError("batch_size must be a positive integer")
    return batch_size


def lower_batch_size(batch_size: int) -> int | None:
    return next((size for size in (16, 8, 4, 2, 1) if size < batch_size), None)
