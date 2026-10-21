from pathlib import Path
import json
from types import SimpleNamespace

import pytest

import firered_runtime


@pytest.mark.parametrize("gpu, device, wheel_index", [
    (True, "gpu", "https://download.pytorch.org/whl/cu128"),
    (False, "gpu", "https://download.pytorch.org/whl/cpu"),
])
def test_runtime_install_uses_supported_index_and_isolated_python(tmp_path, monkeypatch, gpu, device, wheel_index):
    root = tmp_path / "app"
    environment = root / ".runtime" / "firered-asr2"
    uv = root / ".runtime" / "bin" / "uv.exe"
    uv.parent.mkdir(parents=True)
    uv.write_bytes(b"uv fixture")
    monkeypatch.setattr(firered_runtime, "ROOT", root)
    monkeypatch.setattr(firered_runtime, "ENVIRONMENT", environment)
    monkeypatch.setattr(firered_runtime, "selected_firered_device", lambda: device)

    def has_nvidia():
        if device == "cpu":
            pytest.fail("CPU preparation must not probe NVIDIA")
        return gpu

    monkeypatch.setattr(firered_runtime, "_has_nvidia", has_nvidia)
    calls = []

    def run(command, **kwargs):
        if device == "cpu":
            assert kwargs["env"]["CUDA_VISIBLE_DEVICES"] == "-1"
        calls.append(command)
        python = firered_runtime.runtime_python()
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_bytes(b"python fixture")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(firered_runtime.subprocess, "run", run)
    monkeypatch.setattr(firered_runtime, "run_logged", run)
    firered_runtime.install_runtime()
    assert wheel_index in calls[1]
    assert "torch==2.10.0" in calls[1]
    assert "torchaudio==2.10.0" in calls[1]
    assert str(firered_runtime.runtime_python()) in calls[1]
    assert "kaldi_native_fbank" in calls[-1][-1]
    assert "firered_transcriber" in calls[-1][-1]
    assert "FireRedVad" in calls[-1][-1]
    assert "FireRedAsr2System" in calls[-1][-1]
    if device == "cpu":
        assert "torch.cuda.is_available()" not in calls[-1][-1]
    assert firered_runtime.runtime_is_ready()
    firered_runtime.install_runtime()
    assert len(calls) == 4
    assert not (root / ".venv").exists()


def test_failed_import_probe_does_not_mark_runtime_ready(tmp_path, monkeypatch):
    root = tmp_path / "app"
    environment = root / ".runtime" / "firered-asr2"
    monkeypatch.setattr(firered_runtime, "ROOT", root)
    monkeypatch.setattr(firered_runtime, "ENVIRONMENT", environment)
    monkeypatch.setattr(firered_runtime, "_has_nvidia", lambda: False)
    monkeypatch.setattr(firered_runtime.shutil, "which", lambda _name: "uv")

    def run(command, **kwargs):
        python = firered_runtime.runtime_python()
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_bytes(b"python fixture")
        return SimpleNamespace(returncode=1 if "-c" in command else 0)

    monkeypatch.setattr(firered_runtime.subprocess, "run", run)
    monkeypatch.setattr(firered_runtime, "run_logged", run)
    with pytest.raises(RuntimeError, match="firered-install.log"):
        firered_runtime.install_runtime()
    assert not firered_runtime.runtime_is_ready()


def test_runtime_requirements_include_native_fbank_and_worker_dependencies():
    requirements = (Path(firered_runtime.__file__).parent / "scripts" / "firered_requirements.txt").read_text()
    assert "kaldi-native-fbank==" in requirements
    assert "pydantic>=" in requirements
    assert "4e7d9aaf4482a47cec1724807026b9b151926eb5" in requirements


@pytest.mark.parametrize("device, gpu_wheel, upgrade", [
    ("gpu", False, True), ("gpu", True, False),
])
def test_cpu_runtime_can_upgrade_when_user_switches_to_gpu(tmp_path, monkeypatch, device, gpu_wheel, upgrade):
    environment = tmp_path / ".runtime" / "firered-asr2"
    environment.mkdir(parents=True)
    (environment / "ready.json").write_text(json.dumps({"gpu_wheel": gpu_wheel}), encoding="utf-8")
    monkeypatch.setattr(firered_runtime, "ENVIRONMENT", environment)
    monkeypatch.setattr(firered_runtime, "runtime_is_ready", lambda: True)
    monkeypatch.setattr(firered_runtime, "selected_firered_device", lambda: device)

    def has_nvidia():
        if device == "cpu":
            pytest.fail("Ready CPU environment must not probe NVIDIA")
        return True

    monkeypatch.setattr(firered_runtime, "_has_nvidia", has_nvidia)
    calls = []
    monkeypatch.setattr(firered_runtime, "_install_runtime", calls.append)
    firered_runtime.install_runtime()
    assert calls == ([None] if upgrade else [])


def test_legacy_cpu_runtime_preparation_is_blocked_before_any_changes(tmp_path, monkeypatch):
    environment = tmp_path / ".runtime" / "firered-asr2"
    monkeypatch.setattr(firered_runtime, "ENVIRONMENT", environment)
    monkeypatch.setattr(firered_runtime, "selected_firered_device", lambda: "cpu")
    monkeypatch.setattr(firered_runtime, "_install_runtime", lambda *_: pytest.fail("No installation"))
    with pytest.raises(ValueError, match="CPU.*禁用"):
        firered_runtime.install_runtime()
    assert not environment.parent.exists()
