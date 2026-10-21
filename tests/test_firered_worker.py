import io
import json
import sys
from types import SimpleNamespace

import pytest

import classcue.firered_transcriber as firered_transcriber
from scripts import firered_direct_worker as firered_worker


@pytest.mark.parametrize("device, batch_size", [("gpu", 1), ("cpu", 3)])
def test_worker_device_controls_model_loading_and_reuses_same_model(monkeypatch, device, batch_size):
    requests = [
        {"recognizer": "asr", "vad": "vad", "audio_path": "video.mp4",
         "temporary_directory": "temp", "device": device},
    ] * 2
    incoming = io.StringIO("".join(json.dumps(request) + "\n" for request in requests))
    outgoing = io.StringIO()
    calls = []
    model = object()
    monkeypatch.setattr(sys, "argv", ["firered_worker.py", "--device", device])
    monkeypatch.setattr(sys, "stdin", incoming)
    monkeypatch.setattr(sys, "stdout", outgoing)
    monkeypatch.setattr(firered_worker.faulthandler, "enable", lambda: None)
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")
    threads = []
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        set_num_threads=lambda count: threads.append(count),
        set_num_interop_threads=lambda _count: None,
    ))

    def load(*paths, **options):
        calls.append((paths, options))
        return model

    monkeypatch.setattr(firered_transcriber, "load_firered_model", load)
    monkeypatch.setattr(firered_transcriber, "transcribe_firered", lambda *a, **kw: ([], 1000))
    firered_worker.main()
    assert calls == [(("asr", "vad"), {"use_gpu": device == "gpu", "batch_size": batch_size})]
    events = [json.loads(line) for line in outgoing.getvalue().splitlines()]
    assert sum(event["event"] == "result" for event in events) == 2
    if device == "cpu":
        import os

        assert os.environ["CUDA_VISIBLE_DEVICES"] == "-1"
        assert len(threads) == 1
        assert events[-1]["audit"]["cpu_threads"] == threads[0]
    else:
        assert threads == []
