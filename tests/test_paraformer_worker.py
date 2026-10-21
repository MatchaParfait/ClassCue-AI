import io
import json
import sys
from types import SimpleNamespace

import pytest

import classcue.paraformer_transcriber as paraformer
from scripts import paraformer_worker


class CudaOOM(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def disable_fault_handler_for_captured_streams(monkeypatch):
    monkeypatch.setattr(paraformer_worker.faulthandler, "enable", lambda: None)


@pytest.mark.parametrize("failures,event,attempts", [
    (0, "result", 1), (1, "result", 2), (2, "resource_paused", 2),
])
def test_gpu_worker_automatic_route_and_bounded_load_recovery(
    monkeypatch, capsys, failures, event, attempts,
):
    monkeypatch.setattr(sys, "argv", ["paraformer_worker.py", "--device", "gpu"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "audio_path": "video.mp4", "recognizer": "asr", "vad": "vad",
    }) + "\n"))
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(
        OutOfMemoryError=CudaOOM, is_available=lambda: True,
        get_device_properties=lambda _: SimpleNamespace(total_memory=8188 * 2**20),
        mem_get_info=lambda: pytest.fail("free memory must not determine the route"),
    )))
    loads, cleanups, batches = [], [], []

    def load(*args, **kwargs):
        loads.append(True)
        if len(loads) <= failures:
            raise CudaOOM("test allocation failure")
        return object()

    def transcribe(*args, **kwargs):
        batches.append(kwargs["batch_size"])
        kwargs["audit"]["stage_seconds"] = {}
        return [], 1000

    monkeypatch.setattr(paraformer, "load_paraformer_model", load)
    monkeypatch.setattr(paraformer, "transcribe_paraformer", transcribe)
    monkeypatch.setattr(paraformer, "release_cuda_memory", lambda: cleanups.append(True))
    paraformer_worker.main()
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(loads) == attempts
    assert events[-1]["event"] == event
    if event == "result":
        assert batches == [16]
        assert events[-1]["audit"]["batch_size_source"] == "hardware"
    else:
        assert batches == []
        assert events[-1]["failure"] == {"stage": "load", "attempts": 2}


def test_cpu_worker_defaults_four_without_probing_cuda(monkeypatch, capsys):
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    monkeypatch.setattr(sys, "argv", ["paraformer_worker.py", "--device", "cpu"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "audio_path": "video.mp4", "recognizer": "asr", "vad": "vad",
    }) + "\n"))
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(
        get_device_properties=lambda _: pytest.fail("CPU path must not probe CUDA"),
    )))
    monkeypatch.setattr(paraformer, "load_paraformer_model", lambda *_a, **_kw: object())
    batches = []

    def transcribe(*args, **kwargs):
        batches.append(kwargs["batch_size"])
        kwargs["audit"]["stage_seconds"] = {}
        return [], 1000

    monkeypatch.setattr(paraformer, "transcribe_paraformer", transcribe)
    paraformer_worker.main()
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert batches == [4]
    assert events[-1]["event"] == "result"
