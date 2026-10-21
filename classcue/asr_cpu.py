"""Automatic CPU budgets for resident ASR workers, without importing torch."""

import ctypes
import os
import subprocess
import sys

CPU_THREAD_LIMIT = 16
INTERACTIVE_THREAD_RESERVE = 2
IS_WINDOWS = sys.platform == "win32"


def _windows_affinity_count():
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    current = kernel.GetCurrentProcess
    current.argtypes, current.restype = [], wintypes.HANDLE
    affinity = kernel.GetProcessAffinityMask
    affinity.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t),
    ]
    affinity.restype = wintypes.BOOL
    process_mask, system_mask = ctypes.c_size_t(), ctypes.c_size_t()
    if not affinity(current(), ctypes.byref(process_mask), ctypes.byref(system_mask)):
        raise ctypes.WinError(ctypes.get_last_error())
    # This mask covers the calling thread's primary processor group on Windows.
    return process_mask.value.bit_count()


def available_cpu_count():
    try:
        if IS_WINDOWS:
            count = _windows_affinity_count()
        else:
            count = len(os.sched_getaffinity(0))
        if count > 0:
            return count
    except (AttributeError, OSError, NotImplementedError):
        pass
    return max(1, os.cpu_count() or 1)


def cpu_thread_policy(device, requested=None):
    if device not in ("cpu", "gpu"):
        raise ValueError("ASR device must be cpu or gpu")
    if requested is not None and (
        isinstance(requested, bool) or not isinstance(requested, int) or requested < 1
    ):
        raise ValueError("cpu_threads must be a positive integer")
    available = available_cpu_count() if device == "cpu" else None
    automatic = device == "cpu" and requested is None
    threads = (max(1, min(CPU_THREAD_LIMIT, available - INTERACTIVE_THREAD_RESERVE))
               if automatic else requested if requested is not None else CPU_THREAD_LIMIT)
    return {
        "cpu_threads": threads,
        "cpu_threads_source": "automatic" if automatic else "explicit" if requested is not None else "device_default",
        "cpu_available_logical_processors": available,
        "cpu_thread_reserve": INTERACTIVE_THREAD_RESERVE if automatic else 0,
    }


def worker_creationflags(device):
    if device not in ("cpu", "gpu"):
        raise ValueError("ASR device must be cpu or gpu")
    if not IS_WINDOWS:
        return 0
    return subprocess.CREATE_NO_WINDOW | subprocess.NORMAL_PRIORITY_CLASS
