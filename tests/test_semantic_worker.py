"""Exercise the embedding process protocol without model files or inference."""

import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from classcue import semantic_embeddings as embeddings
from classcue.processing_control import ProcessingCancelled


@pytest.fixture
def worker(tmp_path, monkeypatch):
    package = tmp_path / "classcue"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "embedding_worker.py").write_text(
        "import base64, json, os, struct, sys, time\n"
        "mode = os.environ['SYNTHETIC_EMBEDDING_MODE']\n"
        "if mode == 'startup_exit':\n"
        "    sys.exit(4)\n"
        "print(json.dumps({'event': 'ready'}), flush=True)\n"
        "for line in sys.stdin:\n"
        "    request = json.loads(line)\n"
        "    if mode == 'crash':\n"
        "        sys.exit(5)\n"
        "    if mode == 'hang':\n"
        "        open('processing', 'w').close()\n"
        "        time.sleep(30)\n"
        "    elif mode == 'invalid_json':\n"
        "        print('invalid protocol', flush=True)\n"
        "    elif mode == 'error':\n"
        "        print(json.dumps({'event': 'error', 'message': 'synthetic failure'}), flush=True)\n"
        "    else:\n"
        "        count = len(request['texts'])\n"
        "        vectors = base64.b64encode(struct.pack('<f', 1.) * count * 1024).decode()\n"
        "        print(json.dumps({'event': 'result', 'count': count,\n"
        "                          'dimensions': 7 if mode == 'bad_result' else 1024,\n"
        "                          'vectors': vectors}), flush=True)\n",
        encoding="utf-8",
    )
    mode = {"value": "success"}
    monkeypatch.setattr(embeddings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(embeddings, "runtime_python", lambda *_: Path(sys.executable))
    monkeypatch.setattr(embeddings, "model_directory", lambda: tmp_path / "weights")
    monkeypatch.setattr(embeddings, "log_directory", lambda *_: tmp_path / "logs")
    monkeypatch.setattr(embeddings, "_environment", lambda *_: {
        **os.environ, "PYTHONUNBUFFERED": "1", "SYNTHETIC_EMBEDDING_MODE": mode["value"],
    })
    processes = []
    original = subprocess.Popen

    def start(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(embeddings.subprocess, "Popen", start)
    status = {"ready": True, "device": "cpu", "model_version": "synthetic-worker"}
    instance = embeddings.EmbeddingWorker()
    try:
        yield instance, mode, processes, status
    finally:
        instance.close()


def assert_released(instance, processes):
    assert processes
    assert instance.process is None and instance.job is None and instance.timer is None
    assert all(process.poll() is not None and process.stdin.closed and process.stdout.closed
               for process in processes)


def test_protocol_reuses_process_and_returns_independent_cached_vectors(worker):
    instance, _, processes, status = worker
    result = instance.encode(["synthetic query"], query=True, _status=status)
    assert result.shape == (1, embeddings.DIMENSIONS) and np.all(result == 1)
    result[:] = 0
    cached = instance.encode(["synthetic query"], query=True, _status=status)
    assert np.all(cached == 1)
    assert len(processes) == 1 and processes[0].poll() is None
    instance.close()
    assert_released(instance, processes)


@pytest.mark.parametrize("mode", ["startup_exit", "crash", "invalid_json", "error", "bad_result"])
def test_protocol_failure_reaps_process_and_closes_pipes(worker, mode):
    instance, selected, processes, status = worker
    selected["value"] = mode
    with pytest.raises((RuntimeError, ValueError, OSError)):
        instance.encode(["synthetic input"], _status=status)
    assert_released(instance, processes)
    selected["value"] = "success"
    assert instance.encode(["retry"], _status=status).shape == (1, embeddings.DIMENSIONS)
    assert len(processes) == 2


def test_timeout_reaps_unresponsive_worker(worker, monkeypatch):
    instance, selected, processes, status = worker
    selected["value"] = "hang"
    ticks = iter((0., 0.))
    monkeypatch.setattr(embeddings, "time", SimpleNamespace(
        monotonic=lambda: next(ticks, 121.), sleep=embeddings.time.sleep))
    with pytest.raises(RuntimeError, match="响应超时"):
        instance.encode(["synthetic input"], _status=status)
    assert_released(instance, processes)


@pytest.mark.parametrize("after_request", [False, True])
def test_cancellation_reaps_worker_before_returning(worker, after_request):
    instance, selected, processes, status = worker
    selected["value"] = "hang"
    processing = embeddings.PROJECT_ROOT / "processing"
    with pytest.raises(ProcessingCancelled):
        instance.encode(["synthetic input"], should_stop=lambda: processing.is_file() if after_request else True,
                        _status=status)
    if after_request:
        assert processing.is_file()
    assert_released(instance, processes)
