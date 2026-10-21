import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import classcue.asr_models as asr_models
import classcue.paraformer_runtime as paraformer_runtime


def test_cpu_runtime_installer_never_probes_gpu_or_installs_cuda(tmp_path, monkeypatch):
    root = tmp_path / "app"
    monkeypatch.setattr(paraformer_runtime, "ROOT", root)
    monkeypatch.setattr(paraformer_runtime, "ENVIRONMENT", root / ".runtime/paraformer-cpu")
    monkeypatch.setattr(paraformer_runtime.shutil, "which", lambda _: "uv")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert kwargs["env"]["CUDA_VISIBLE_DEVICES"] == ""
        python = paraformer_runtime.runtime_python()
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_bytes(b"python fixture")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(paraformer_runtime.subprocess, "run", run)
    monkeypatch.setattr(paraformer_runtime, "run_logged", run)
    paraformer_runtime.install_runtime()
    assert "torch==2.7.1" in calls[1]
    assert "torchaudio==2.7.1" in calls[1]
    assert "https://download.pytorch.org/whl/cpu" in calls[1]
    assert "assert torch.version.cuda is None" in calls[-1][-1]
    assert paraformer_runtime.runtime_is_ready()
    paraformer_runtime.install_runtime()
    assert len(calls) == 4


def test_cpu_runtime_rejects_old_gpu_ready_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(paraformer_runtime, "ENVIRONMENT", tmp_path)
    python = paraformer_runtime.runtime_python()
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    (tmp_path / "ready.json").write_text(json.dumps({
        "version": paraformer_runtime.RUNTIME_VERSION, "gpu_wheel": True,
    }))
    assert not paraformer_runtime.runtime_is_ready()


def test_gpu_runtime_installs_cuda_without_touching_cpu_environment(tmp_path, monkeypatch):
    root = tmp_path / "app"
    cpu, gpu = root / ".runtime/paraformer-cpu", root / ".runtime/paraformer-gpu"
    cpu.mkdir(parents=True)
    (cpu / "keep.txt").write_text("CPU runtime")
    monkeypatch.setattr(paraformer_runtime, "ROOT", root)
    monkeypatch.setattr(paraformer_runtime, "ENVIRONMENT", cpu)
    monkeypatch.setattr(paraformer_runtime, "GPU_ENVIRONMENT", gpu)
    monkeypatch.setattr(paraformer_runtime, "_has_nvidia", lambda: True)
    monkeypatch.setattr(paraformer_runtime.shutil, "which", lambda _: "uv")
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert "CUDA_VISIBLE_DEVICES" not in kwargs["env"]
        python = paraformer_runtime.runtime_python("gpu")
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_bytes(b"python fixture")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(paraformer_runtime.subprocess, "run", run)
    monkeypatch.setattr(paraformer_runtime, "run_logged", run)
    paraformer_runtime.install_runtime(device="gpu")
    assert "https://download.pytorch.org/whl/cu128" in calls[1]
    assert "torch.cuda.synchronize()" in calls[-1][-1]
    assert paraformer_runtime.runtime_is_ready("gpu")
    assert not paraformer_runtime.runtime_is_ready("cpu")
    assert (cpu / "keep.txt").read_text() == "CPU runtime"


def test_gpu_install_rejects_missing_driver_before_downloading(tmp_path, monkeypatch):
    monkeypatch.setattr(paraformer_runtime, "GPU_ENVIRONMENT", tmp_path / "gpu")
    monkeypatch.setattr(paraformer_runtime, "_has_nvidia", lambda: False)
    monkeypatch.setattr(paraformer_runtime, "_install_runtime",
                        lambda *_a, **_kw: pytest.fail("No download without a driver"))
    with pytest.raises(ValueError, match="NVIDIA"):
        paraformer_runtime.install_runtime(device="gpu")


@pytest.mark.parametrize("returncode,available", [(0, True), (1, False)])
def test_cuda_probe_uses_gpu_interpreter_and_checks_real_allocation(tmp_path, monkeypatch,
                                                                  returncode, available):
    monkeypatch.setattr(paraformer_runtime, "GPU_ENVIRONMENT", tmp_path)
    monkeypatch.setattr(paraformer_runtime, "runtime_is_ready", lambda device: device == "gpu")
    monkeypatch.setattr(paraformer_runtime, "_has_nvidia", lambda: True)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=returncode,
            stdout='{"gpu_name":"test GPU","gpu_total_memory_mib":8188}',
        )

    monkeypatch.setattr(paraformer_runtime.subprocess, "run", run)
    monkeypatch.setattr(paraformer_runtime, "run_logged", run)
    paraformer_runtime.invalidate_cuda_status()
    status = paraformer_runtime.cuda_status()
    assert status["gpu_available"] is available
    if available:
        assert status["gpu_batch_size"] == 16
        assert status["gpu_total_memory_mib"] == 8188
    assert str(paraformer_runtime.runtime_python("gpu")) == calls[0][0]
    assert "torch.ones(1, device='cuda:0')" in calls[0][-1]
    assert "get_device_properties(0).total_memory" in calls[0][-1]
    paraformer_runtime.cuda_status()
    assert len(calls) == 1
    paraformer_runtime.cuda_status(refresh=True)
    assert len(calls) == 2


def test_cuda_probe_no_runtime_disables_selection_even_with_nvidia(tmp_path, monkeypatch):
    monkeypatch.setattr(paraformer_runtime, "GPU_ENVIRONMENT", tmp_path)
    monkeypatch.setattr(paraformer_runtime, "_has_nvidia", lambda: True)
    monkeypatch.setattr(paraformer_runtime.subprocess, "run",
                        lambda *_a, **_kw: pytest.fail("Unprepared interpreter must not run"))
    paraformer_runtime.invalidate_cuda_status()
    status = paraformer_runtime.cuda_status()
    assert status["gpu_prepare_available"] is True
    assert status["gpu_available"] is False
    with pytest.raises(ValueError, match="cannot be selected"):
        paraformer_runtime.require_cuda()


@pytest.mark.parametrize("total,available,batch", [
    (1024, False, None), (None, False, None), (2048, True, 8), (3072, True, 16),
])
def test_cuda_probe_rejects_small_or_unknown_capacity_and_reports_route(
    tmp_path, monkeypatch, total, available, batch,
):
    monkeypatch.setattr(paraformer_runtime, "GPU_ENVIRONMENT", tmp_path)
    monkeypatch.setattr(paraformer_runtime, "runtime_is_ready", lambda device: device == "gpu")
    monkeypatch.setattr(paraformer_runtime, "_has_nvidia", lambda: True)
    monkeypatch.setattr(paraformer_runtime.subprocess, "run", lambda *_args, **_kwargs:
                        SimpleNamespace(returncode=0, stdout=json.dumps({
                            "gpu_name": "test GPU", "gpu_total_memory_mib": total,
                        })))
    paraformer_runtime.invalidate_cuda_status()
    status = paraformer_runtime.cuda_status()
    assert status["gpu_available"] is available
    assert status["gpu_batch_size"] == batch
    if not available:
        assert status["gpu_unavailable_reason"]
        with pytest.raises(ValueError, match="cannot be selected"):
            paraformer_runtime.require_cuda()


def test_clear_removes_both_managed_runtimes_only(tmp_path, monkeypatch):
    root = tmp_path / "app"
    monkeypatch.setattr(paraformer_runtime, "ROOT", root)
    monkeypatch.setattr(paraformer_runtime, "ENVIRONMENT", root / ".runtime/paraformer-cpu")
    monkeypatch.setattr(paraformer_runtime, "GPU_ENVIRONMENT", root / ".runtime/paraformer-gpu")
    for directory in (paraformer_runtime.ENVIRONMENT, paraformer_runtime.GPU_ENVIRONMENT):
        directory.mkdir(parents=True)
        (directory / "weights.txt").write_bytes(b"runtime")
    user_file = root / "user.txt"
    user_file.write_text("keep")
    assert paraformer_runtime.clear_runtime() == 14
    assert not paraformer_runtime.ENVIRONMENT.exists()
    assert not paraformer_runtime.GPU_ENVIRONMENT.exists()
    assert user_file.read_text() == "keep"


def test_paraformer_saved_device_defaults_cpu_and_gpu_requires_cuda(tmp_path, monkeypatch):
    monkeypatch.setattr(asr_models, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(paraformer_runtime, "require_cuda",
                        lambda: (_ for _ in ()).throw(ValueError("CUDA unavailable")))
    assert asr_models.selected_paraformer_device() == "cpu"
    asr_models.select_model("paraformer-large-cpu", paraformer_device="cpu")
    before = asr_models.SETTINGS_PATH.read_text()
    with pytest.raises(ValueError, match="CUDA"):
        asr_models.select_model("paraformer-large-cpu", paraformer_device="gpu")
    assert asr_models.SETTINGS_PATH.read_text() == before
    monkeypatch.setattr(paraformer_runtime, "require_cuda", lambda: None)
    asr_models.select_model("paraformer-large-cpu", paraformer_device="gpu")
    assert asr_models.selected_paraformer_device() == "gpu"
    asr_models.select_model("whisper-turbo")
    assert asr_models.selected_paraformer_device() == "gpu"
    with pytest.raises(ValueError):
        asr_models.select_model("paraformer-large-cpu", paraformer_device="auto")


def test_gpu_only_runtime_can_download_weights(tmp_path, monkeypatch):
    monkeypatch.setattr(paraformer_runtime, "prepare_component_files", lambda *_a: None)
    monkeypatch.setattr(asr_models, "MODEL_ROOT", tmp_path / "models")
    monkeypatch.setattr(paraformer_runtime, "ROOT", tmp_path)
    monkeypatch.setattr(paraformer_runtime, "runtime_is_ready", lambda device="cpu": device == "gpu")
    monkeypatch.setattr(paraformer_runtime, "runtime_python", lambda device="cpu": tmp_path / device / "python.exe")
    calls = []
    monkeypatch.setattr(paraformer_runtime, "run_logged",
                        lambda command, **kw: calls.append(command) or SimpleNamespace(returncode=0))
    paraformer_runtime.prepare_weights()
    assert calls[0][0] == str(tmp_path / "gpu/python.exe")


def test_explicit_preparation_bypasses_legacy_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(paraformer_runtime, "prepare_component_files", lambda *_a: None)
    monkeypatch.setattr(asr_models, "MODEL_ROOT", tmp_path / "models")
    monkeypatch.setattr(paraformer_runtime, "ROOT", tmp_path)
    monkeypatch.setattr(paraformer_runtime, "runtime_is_ready", lambda device="cpu": True)
    monkeypatch.setattr(paraformer_runtime, "runtime_python", lambda device="cpu": tmp_path / "python.exe")
    legacy = asr_models.MODEL_ROOT / "paraformer-large-vad-punc"
    legacy.mkdir(parents=True)
    files = {}
    for component, names in asr_models.PARAFORMER_REQUIRED_FILES.items():
        for name in names:
            file = legacy / component / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b"legacy")
            files[file.relative_to(legacy).as_posix()] = 6
    marker = json.dumps({"repositories": dict(asr_models.get_model("paraformer-large-cpu").repositories),
                         "files": files})
    (legacy / "ready.json").write_text(marker)
    monkeypatch.setattr(paraformer_runtime.shutil, "copy2",
                        lambda *_args: pytest.fail("Explicit preparation must not copy legacy files"))
    calls = []
    monkeypatch.setattr(paraformer_runtime, "run_logged",
                        lambda command, **kw: calls.append(command) or SimpleNamespace(returncode=0))
    paraformer_runtime.prepare_weights(reuse_legacy=False)
    assert calls and calls[0][-2].endswith("prepare_paraformer.py")
    assert calls[0][-1] == "--validate-only"
    assert (legacy / "ready.json").read_text() == marker


def test_runtime_clear_cannot_escape_managed_storage(tmp_path, monkeypatch):
    root = tmp_path / "app"
    external = tmp_path / "user-files"
    external.mkdir()
    (external / "keep.txt").write_text("user content")
    monkeypatch.setattr(paraformer_runtime, "ROOT", root)
    monkeypatch.setattr(paraformer_runtime, "ENVIRONMENT", external)
    with pytest.raises(ValueError, match="应用管理目录"):
        paraformer_runtime.clear_runtime()
    assert (external / "keep.txt").read_text() == "user content"


def test_model_catalog_uses_explicit_bicif_and_fsmn_without_punctuation_or_speaker():
    model = asr_models.get_model("paraformer-large-cpu")
    assert model.engine == "paraformer"
    assert model.repositories == (
        ("recognizer", "iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch"),
        ("vad", "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch"),
    )


def test_weight_validation_rejects_standard_non_timestamp_predictor(tmp_path):
    from classcue.paraformer_transcriber import _validate_component

    for name in asr_models.PARAFORMER_REQUIRED_FILES["recognizer"]:
        (tmp_path / name).write_text("existing")
    (tmp_path / "config.yaml").write_text("model: Paraformer\npredictor: CifPredictorV2\n")
    with pytest.raises(ValueError, match="BiCifParaformer"):
        _validate_component(tmp_path, "recognizer")


def test_existing_complete_official_cache_is_reused_without_network_or_mutation(tmp_path, monkeypatch):
    monkeypatch.setattr(asr_models, "MODEL_ROOT", tmp_path / "models")
    legacy = asr_models.MODEL_ROOT / "paraformer-large-vad-punc"
    files = {}
    for component, names in asr_models.PARAFORMER_REQUIRED_FILES.items():
        for name in names:
            file = legacy / component / name
            file.parent.mkdir(parents=True, exist_ok=True)
            config = ("model: BiCifParaformer\npredictor: CifPredictorV3\n"
                      if component == "recognizer" else "model: FsmnVADStreaming\n")
            file.write_text(config if name == "config.yaml" else "existing", encoding="utf-8")
            files[file.relative_to(legacy).as_posix()] = file.stat().st_size
    repositories = {
        name: repository.replace("iic/", "damo/", 1)
        for name, repository in asr_models.get_model("paraformer-large-cpu").repositories
    }
    manifest = json.dumps({"files": files, "repositories": repositories})
    (legacy / "ready.json").write_text(manifest)
    monkeypatch.setattr(paraformer_runtime.subprocess, "run",
                        lambda *_args, **_kwargs: pytest.fail("Complete local weights need no download"))
    asr_models.download_model("paraformer-large-cpu")
    assert asr_models.model_is_downloaded("paraformer-large-cpu")
    assert (legacy / "ready.json").read_text() == manifest
    paths = asr_models.component_paths("paraformer-large-cpu")
    assert paths["recognizer"].joinpath("model.pt").read_text() == "existing"
    paths["vad"].joinpath("am.mvn").unlink()
    assert not asr_models.model_is_downloaded("paraformer-large-cpu")
