import json
from types import SimpleNamespace

import pytest

import classcue.qwen_runtime as qwen_runtime


def test_optional_runtime_is_installed_only_into_its_own_environment(tmp_path, monkeypatch):
    root = tmp_path / "app"
    environment = root / ".runtime" / "qwen-asr"
    uv = root / ".runtime" / "bin" / "uv.exe"
    uv.parent.mkdir(parents=True)
    uv.write_bytes(b"uv fixture")
    monkeypatch.setattr(qwen_runtime, "ROOT", root)
    monkeypatch.setattr(qwen_runtime, "ENVIRONMENT", environment)
    monkeypatch.setattr(qwen_runtime, "_has_nvidia", lambda: True)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        environment.mkdir(parents=True, exist_ok=True)
        python = qwen_runtime.runtime_python()
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_bytes(b"python fixture")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(qwen_runtime.subprocess, "run", run)
    monkeypatch.setattr(qwen_runtime, "run_logged", run)
    qwen_runtime.install_runtime()
    assert qwen_runtime.runtime_is_ready()
    assert calls[0][:4] == [str(uv), "venv", "--python", "3.12"]
    assert "torch==2.7.1" in calls[1]
    assert "https://download.pytorch.org/whl/cu128" in calls[1]
    assert str(qwen_runtime.runtime_python()) in calls[1]
    assert calls[-1][0] == str(qwen_runtime.runtime_python())
    qwen_runtime.install_runtime()
    assert len(calls) == 4
    assert not (root / ".venv").exists()


def test_failed_runtime_install_is_not_marked_ready_and_can_be_retried(tmp_path, monkeypatch):
    root = tmp_path / "app"
    environment = root / ".runtime" / "qwen-asr"
    uv = root / ".runtime" / "bin" / "uv.exe"
    uv.parent.mkdir(parents=True)
    uv.write_bytes(b"uv fixture")
    monkeypatch.setattr(qwen_runtime, "ROOT", root)
    monkeypatch.setattr(qwen_runtime, "ENVIRONMENT", environment)
    monkeypatch.setattr(qwen_runtime, "_has_nvidia", lambda: False)
    monkeypatch.setattr(qwen_runtime.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=1))
    monkeypatch.setattr(qwen_runtime, "run_logged", lambda *_args, **_kwargs: SimpleNamespace(returncode=1))
    with pytest.raises(RuntimeError, match="qwen-install.log"):
        qwen_runtime.install_runtime()
    assert not qwen_runtime.runtime_is_ready()


def test_clearing_runtime_preserves_main_environment_and_model_files(tmp_path, monkeypatch):
    root = tmp_path / "app"
    environment = root / ".runtime" / "qwen-asr"
    environment.mkdir(parents=True)
    (environment / "ready.json").write_text(json.dumps({"version": qwen_runtime.RUNTIME_VERSION}))
    (environment / "package.bin").write_bytes(b"dependencies")
    main = root / ".runtime" / "venv"
    main.mkdir()
    (main / "python.exe").write_bytes(b"main runtime")
    models = root / "models"
    models.mkdir()
    (models / "model.bin").write_bytes(b"weights")
    monkeypatch.setattr(qwen_runtime, "ROOT", root)
    monkeypatch.setattr(qwen_runtime, "ENVIRONMENT", environment)
    assert qwen_runtime.clear_runtime() > 0
    assert not environment.exists()
    assert (main / "python.exe").read_bytes() == b"main runtime"
    assert (models / "model.bin").read_bytes() == b"weights"


def test_runtime_deletion_rejects_a_directory_outside_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(qwen_runtime, "ROOT", tmp_path / "app")
    monkeypatch.setattr(qwen_runtime, "ENVIRONMENT", tmp_path / "course")
    with pytest.raises(ValueError):
        qwen_runtime.clear_runtime()


def test_external_storage_runtime_can_be_cleared_without_touching_project(tmp_path, monkeypatch):
    root = tmp_path / "app"
    storage = tmp_path / "asr-assets"
    configuration = root / "data" / "asr_storage.json"
    configuration.parent.mkdir(parents=True)
    configuration.write_text(json.dumps({"storage_root": str(storage)}))
    environment = storage / ".runtime" / "qwen-asr"
    environment.mkdir(parents=True)
    (environment / "package.bin").write_bytes(b"optional dependency")
    main = root / ".runtime" / "venv"
    main.mkdir(parents=True)
    (main / "python.exe").write_bytes(b"main application")
    monkeypatch.setattr(qwen_runtime, "ROOT", root)
    monkeypatch.setattr(qwen_runtime, "ENVIRONMENT", environment)
    assert qwen_runtime.clear_runtime() > 0
    assert not environment.exists()
    assert (main / "python.exe").read_bytes() == b"main application"
