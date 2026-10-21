import io
import json
from queue import Queue
import sys
from types import SimpleNamespace

import pytest

import classcue.asr_cpu as asr_cpu
import classcue.transcriber as transcriber


@pytest.mark.parametrize("available,expected", [
    (1, 1), (2, 1), (4, 2), (8, 6), (16, 14), (24, 16), (64, 16),
])
def test_automatic_budget_leaves_room_without_exceeding_cap(monkeypatch, available, expected):
    monkeypatch.setattr(asr_cpu, "available_cpu_count", lambda: available)
    policy = asr_cpu.cpu_thread_policy("cpu")
    assert policy["cpu_threads"] == expected
    assert policy["cpu_available_logical_processors"] == available
    assert policy["cpu_threads_source"] == "automatic"


def test_gpu_preserves_default_without_cpu_probe(monkeypatch):
    monkeypatch.setattr(asr_cpu, "available_cpu_count", lambda: pytest.fail("GPU policy must not change"))
    assert asr_cpu.cpu_thread_policy("gpu")["cpu_threads"] == 16


def test_internal_explicit_threads_remain_available_for_benchmarks(monkeypatch):
    monkeypatch.setattr(asr_cpu, "available_cpu_count", lambda: 24)
    policy = asr_cpu.cpu_thread_policy("cpu", 8)
    assert policy["cpu_threads"] == 8
    assert policy["cpu_threads_source"] == "explicit"
    assert policy["cpu_thread_reserve"] == 0


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "8"])
def test_invalid_explicit_budget_is_rejected(value):
    with pytest.raises(ValueError, match="positive integer"):
        asr_cpu.cpu_thread_policy("cpu", value)


def test_windows_affinity_overrides_machine_total(monkeypatch):
    monkeypatch.setattr(asr_cpu, "IS_WINDOWS", True)
    monkeypatch.setattr(asr_cpu, "_windows_affinity_count", lambda: 4)
    monkeypatch.setattr(asr_cpu.os, "cpu_count", lambda: 24)
    assert asr_cpu.available_cpu_count() == 4


def test_posix_affinity_overrides_machine_total(monkeypatch):
    monkeypatch.setattr(asr_cpu, "IS_WINDOWS", False)
    monkeypatch.setattr(asr_cpu.os, "sched_getaffinity", lambda _: {2, 3}, raising=False)
    monkeypatch.setattr(asr_cpu.os, "cpu_count", lambda: 24)
    assert asr_cpu.available_cpu_count() == 2


@pytest.mark.parametrize("count,expected", [(8, 8), (None, 1)])
def test_detection_failure_has_safe_fallback(monkeypatch, count, expected):
    monkeypatch.setattr(asr_cpu, "IS_WINDOWS", True)
    monkeypatch.setattr(asr_cpu, "_windows_affinity_count",
                        lambda: (_ for _ in ()).throw(OSError("unavailable")))
    monkeypatch.setattr(asr_cpu.os, "cpu_count", lambda: count)
    assert asr_cpu.available_cpu_count() == expected


def test_empty_windows_group_mask_falls_back(monkeypatch):
    monkeypatch.setattr(asr_cpu, "IS_WINDOWS", True)
    monkeypatch.setattr(asr_cpu, "_windows_affinity_count", lambda: 0)
    monkeypatch.setattr(asr_cpu.os, "cpu_count", lambda: 32)
    assert asr_cpu.available_cpu_count() == 32


def test_windows_cpu_and_gpu_priority_is_normal_and_keeps_hidden_window(monkeypatch):
    monkeypatch.setattr(asr_cpu, "IS_WINDOWS", True)
    monkeypatch.setattr(asr_cpu.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(asr_cpu.subprocess, "NORMAL_PRIORITY_CLASS", 0x20, raising=False)
    assert asr_cpu.worker_creationflags("cpu") == 0x08000020
    assert asr_cpu.worker_creationflags("gpu") == 0x08000020
    monkeypatch.setattr(asr_cpu, "IS_WINDOWS", False)
    assert asr_cpu.worker_creationflags("cpu") == 0
    assert asr_cpu.worker_creationflags("gpu") == 0


@pytest.mark.parametrize("worker_name,ready_name", [
    ("ParaformerWorker", "paraformer_runtime_is_ready"),
    ("NanoWorker", "nano_runtime_is_ready"),
    ("DolphinWorker", "dolphin_runtime_is_ready"),
    ("FireRedWorker", "firered_runtime_is_ready"),
])
@pytest.mark.parametrize("device", ["cpu", "gpu"])
def test_worker_startup_sets_budget_and_normal_priority_before_torch_import(
    tmp_path, monkeypatch, worker_name, ready_name, device,
):
    import classcue.paraformer_runtime as paraformer_runtime

    monkeypatch.setattr(asr_cpu, "available_cpu_count", lambda: 4)
    monkeypatch.setattr(transcriber, "ROOT", tmp_path)
    monkeypatch.setattr(transcriber, ready_name, lambda *_a: True)
    monkeypatch.setattr(paraformer_runtime, "require_cuda", lambda: None)
    monkeypatch.setattr(transcriber, "require_nano_cuda", lambda: None)
    monkeypatch.setattr(transcriber, "require_dolphin_cuda", lambda: None)
    calls = []

    def popen(command, **options):
        calls.append(options)
        return SimpleNamespace(stdout=iter(()))

    monkeypatch.setattr(transcriber.subprocess, "Popen", popen)
    monkeypatch.setattr(transcriber, "WindowsJob", lambda _p: SimpleNamespace(
        resume=lambda _p: None, close=lambda: None))
    worker = getattr(transcriber, worker_name)(device=device)
    worker.reader.join(timeout=2)
    environment = calls[0]["env"]
    if worker_name != "FireRedWorker" or device == "cpu":
        expected_threads = "2" if device == "cpu" else "16"
        assert environment["OMP_NUM_THREADS"] == expected_threads
        assert environment["MKL_NUM_THREADS"] == expected_threads
        assert environment["OPENBLAS_NUM_THREADS"] == expected_threads
    if device == "cpu":
        assert environment["CUDA_VISIBLE_DEVICES"] in ("", "-1")
    assert calls[0]["creationflags"] == asr_cpu.worker_creationflags(device) | (
        4 if transcriber.os.name == "nt" else 0)


def test_legacy_qwen_worker_also_starts_at_normal_priority(tmp_path, monkeypatch):
    monkeypatch.setattr(transcriber, "ROOT", tmp_path)
    monkeypatch.setattr(transcriber, "runtime_is_ready", lambda: True)
    calls = []

    def popen(command, **options):
        calls.append(options)
        return SimpleNamespace(stdout=iter(()))

    monkeypatch.setattr(transcriber.subprocess, "Popen", popen)
    monkeypatch.setattr(transcriber, "WindowsJob", lambda _p: SimpleNamespace(
        resume=lambda _p: None, close=lambda: None))
    worker = transcriber.QwenWorker()
    worker.reader.join(timeout=2)
    assert calls[0]["creationflags"] == asr_cpu.worker_creationflags("gpu") | (
        4 if transcriber.os.name == "nt" else 0)


@pytest.mark.parametrize("worker_name", ["ParaformerWorker", "NanoWorker", "DolphinWorker"])
@pytest.mark.parametrize("explicit", [None, 8])
def test_parent_requests_auto_by_default_but_keeps_internal_override(tmp_path, worker_name, explicit):
    worker = getattr(transcriber, worker_name).__new__(getattr(transcriber, worker_name))
    worker.process = SimpleNamespace(stdin=io.StringIO())
    worker.log_path = tmp_path / "worker.log"
    worker.events = Queue()
    worker.events.put(json.dumps({"event": "result", "duration_ms": 1000, "segments": []}))
    assert worker.transcribe("video.mp4", {"recognizer": tmp_path, "vad": tmp_path},
                             None, cpu_threads=explicit) == ([], 1000)
    request = json.loads(worker.process.stdin.getvalue())
    if explicit is None:
        assert "cpu_threads" not in request
    else:
        assert request["cpu_threads"] == explicit


@pytest.mark.parametrize("engine", ["paraformer", "nano", "dolphin"])
@pytest.mark.parametrize("restricted", [False, True])
def test_child_automatically_resolves_budget_and_reloads_if_affinity_changes(
    monkeypatch, capsys, engine, restricted,
):
    from importlib import import_module

    worker = import_module(f"scripts.{engine}_worker")
    model_module = import_module(f"classcue.{engine}_transcriber")
    counts = iter([8, 4] if restricted else [8, 8])
    monkeypatch.setattr(asr_cpu, "available_cpu_count", lambda: next(counts))
    monkeypatch.setattr(sys, "argv", [f"{engine}_worker.py", "--device", "cpu"])
    request = {"recognizer": "asr", "vad": "vad", "audio_path": "video.mp4"}
    monkeypatch.setattr(sys, "stdin", io.StringIO((json.dumps(request) + "\n") * 2))
    if engine == "paraformer":
        monkeypatch.setattr(worker.faulthandler, "enable", lambda: None)
    loads = []

    def load(*_a, **options):
        loads.append(options["cpu_threads"])
        return object()

    def transcribe(*_a, **options):
        options["audit"]["stage_seconds"] = {}
        return [], 1000

    monkeypatch.setattr(model_module, f"load_{engine}_model", load)
    monkeypatch.setattr(model_module, f"transcribe_{engine}", transcribe)
    worker.main()
    assert loads == ([6, 2] if restricted else [6])
    results = [json.loads(line) for line in capsys.readouterr().out.splitlines()
               if json.loads(line)["event"] == "result"]
    assert len(results) == 2
    assert results[0]["audit"]["cpu_threads"] == 6
    assert results[-1]["audit"]["cpu_threads"] == (2 if restricted else 6)
    assert results[-1]["audit"]["cpu_threads_source"] == "automatic"
