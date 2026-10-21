from types import SimpleNamespace
import sys

import pytest

from scripts import benchmark_asr_resources as benchmark


def test_process_snapshot_only_tracks_requested_tree(monkeypatch):
    children_calls = []

    class Process:
        def __init__(self, pid):
            self.pid = pid

        def create_time(self):
            return self.pid + .5

        def ppid(self):
            return 1 if self.pid == 10 else 10

        def children(self, recursive):
            children_calls.append(recursive)
            return [Process(11)]

        def memory_info(self):
            return SimpleNamespace(rss=2**20, private=2 * 2**20)

    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(Process=Process, Error=RuntimeError))
    known = set()
    result = benchmark.process_snapshot(10, known)
    assert children_calls == [True]
    assert known == {(10, 10.5), (11, 11.5)}
    assert result["tree_rss_mib"] == 2 and result["tree_private_mib"] == 4


def test_pid_reuse_is_not_reported_as_owned_survivor(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(
        Process=lambda pid: SimpleNamespace(create_time=lambda: 999, is_running=lambda: True),
        Error=RuntimeError,
    ))
    assert benchmark.surviving_processes({(10, 10.5)}) == []


def test_gpu_query_filters_to_owned_pids_and_preserves_wddm_unknown(monkeypatch):
    responses = iter([
        SimpleNamespace(returncode=0, stdout="1000, 7000\n"),
        SimpleNamespace(returncode=0, stdout="10, [N/A]\n20, 500\n"),
    ])
    monkeypatch.setattr(benchmark.subprocess, "run", lambda *_a, **_k: next(responses))
    result = benchmark.gpu_snapshot({10})
    assert result["owned_processes"] == [{"pid": 10, "used_mib": None}]
    assert result["owned_used_mib"] is None
    assert result["device_used_mib"] == 1000


def test_excerpt_is_exact_requested_mono_pcm16(tmp_path):
    import numpy as np
    import wave

    source = tmp_path / "source.wav"
    with wave.open(str(source), "wb") as output:
        output.setparams((1, 2, 16000, 0, "NONE", ""))
        output.writeframes(np.arange(32000, dtype=np.int16).tobytes())
    target = tmp_path / "excerpt.wav"
    result = benchmark.prepare_excerpt(source, target, seconds=1)
    with wave.open(str(target), "rb") as output:
        assert output.getnframes() == 16000
        assert (output.getframerate(), output.getnchannels(), output.getsampwidth()) == (16000, 1, 2)
    assert result["duration_seconds"] == 1 and result["sha256"]
    with pytest.raises(ValueError, match="shorter"):
        benchmark.prepare_excerpt(source, tmp_path / "too-long.wav", seconds=3)
