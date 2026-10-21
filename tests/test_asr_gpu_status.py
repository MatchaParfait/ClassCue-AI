import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import classcue.asr_gpu_status as asr_gpu_status


def test_device_identity_uses_the_requested_runtime_without_loading_a_model(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "gpu_available": True, "gpu_name": "NVIDIA Test GPU",
        }))

    monkeypatch.setattr(asr_gpu_status.subprocess, "run", run)
    result = asr_gpu_status.inspect_cuda_device(Path("isolated/python"), True, "Qwen")
    assert result["gpu_name"] == "NVIDIA Test GPU"
    assert result["gpu_available"] is True
    assert result["gpu_unavailable_reason"] is None
    assert calls[0][0][0] == str(Path("isolated/python"))
    assert "get_device_name(0)" in calls[0][0][2]
    assert "from_pretrained" not in calls[0][0][2]
    assert calls[0][1]["timeout"] == 30


def test_unprepared_runtime_does_not_run_a_probe(monkeypatch):
    monkeypatch.setattr(asr_gpu_status.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("not prepared"))
    result = asr_gpu_status.inspect_cuda_device(Path("missing/python"), False, "FireRedASR2S")
    assert result["gpu_name"] is None
    assert result["gpu_available"] is False
    assert "环境未准备" in result["gpu_unavailable_reason"]


@pytest.mark.parametrize("payload", [
    {"gpu_available": False, "gpu_name": None},
    {"gpu_available": True, "gpu_name": ""},
    {"gpu_available": True},
    None,
])
def test_unavailable_or_invalid_identity_is_not_reported_as_ready(monkeypatch, payload):
    monkeypatch.setattr(asr_gpu_status.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(
        returncode=0, stdout=json.dumps(payload),
    ))
    result = asr_gpu_status.inspect_cuda_device(Path("runtime/python"), True, "Qwen")
    assert result["gpu_available"] is False
    assert result["gpu_name"] is None
    assert result["gpu_unavailable_reason"]


@pytest.mark.parametrize("failure", [OSError("missing"), asr_gpu_status.subprocess.TimeoutExpired("python", 30)])
def test_device_probe_failure_remains_a_status_observation(monkeypatch, failure):
    def run(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(asr_gpu_status.subprocess, "run", run)
    result = asr_gpu_status.inspect_cuda_device(Path("runtime/python"), True, "Qwen")
    assert result["gpu_available"] is False
    assert "检查失败" in result["gpu_unavailable_reason"]
