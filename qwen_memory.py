"""Low-overhead Windows process RAM and system commit measurements."""

import ctypes
from ctypes import wintypes
import os


def memory_snapshot():
    if os.name != "nt":
        return {}
    size = ctypes.c_size_t

    class ProcessMemory(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD), ("faults", wintypes.DWORD),
            ("peak_working", size), ("working", size), ("peak_paged", size),
            ("paged", size), ("peak_nonpaged", size), ("nonpaged", size),
            ("pagefile", size), ("peak_pagefile", size), ("private", size),
        ]

    class Performance(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD), ("commit", size), ("limit", size),
            ("peak", size), ("physical_total", size), ("physical_available", size),
            ("cache", size), ("kernel", size), ("kernel_paged", size),
            ("kernel_nonpaged", size), ("page_size", size),
            ("handles", wintypes.DWORD), ("processes", wintypes.DWORD),
            ("threads", wintypes.DWORD),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    psapi.GetPerformanceInfo.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    process = ProcessMemory()
    process.cb = ctypes.sizeof(process)
    performance = Performance()
    performance.cb = ctypes.sizeof(performance)
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(process), process.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    if not psapi.GetPerformanceInfo(ctypes.byref(performance), performance.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    mib = 2**20
    return {
        "process_working_mib": process.working / mib,
        "process_peak_working_mib": process.peak_working / mib,
        "process_private_commit_mib": process.private / mib,
        "process_peak_private_commit_mib": process.peak_pagefile / mib,
        "system_available_physical_mib": performance.physical_available * performance.page_size / mib,
        "system_total_physical_mib": performance.physical_total * performance.page_size / mib,
        "system_commit_mib": performance.commit * performance.page_size / mib,
        "system_commit_limit_mib": performance.limit * performance.page_size / mib,
        "system_available_commit_mib": (performance.limit - performance.commit) * performance.page_size / mib,
    }


def ensure_memory_headroom(snapshot=None, *, physical_mib=2048, commit_mib=4096):
    values = memory_snapshot() if snapshot is None else snapshot
    if (values.get("system_available_physical_mib", float("inf")) < physical_mib
            or values.get("system_available_commit_mib", float("inf")) < commit_mib):
        raise RuntimeError(
            "Insufficient memory headroom: "
            f"physical={values.get('system_available_physical_mib', 0):.0f} MiB, "
            f"commit={values.get('system_available_commit_mib', 0):.0f} MiB; "
            f"need >= {physical_mib} / {commit_mib} MiB"
        )
