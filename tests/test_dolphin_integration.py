import json
from types import SimpleNamespace

import pytest

import classcue.asr_models as asr_models
import classcue.dolphin_runtime as dolphin_runtime
import classcue.transcriber as transcriber


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr(asr_models, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(asr_models, "MODEL_ROOT", tmp_path / "models")
    return tmp_path


def test_device_is_independent_and_selection_does_not_download(settings, monkeypatch):
    monkeypatch.setattr(dolphin_runtime, "require_cuda", lambda: None)
    asr_models.select_model("paraformer-large-cpu", paraformer_device="cpu")
    asr_models.select_model("dolphin-small-cn", dolphin_device="gpu")
    assert asr_models.selected_dolphin_device() == "gpu"
    assert asr_models.selected_paraformer_device() == "cpu"
    assert not asr_models.MODEL_ROOT.exists()
    asr_models.select_model("whisper-turbo")
    assert asr_models.selected_dolphin_device() == "gpu"


def test_unavailable_cuda_cannot_change_selection(settings, monkeypatch):
    asr_models.select_model("whisper-turbo")

    def unavailable():
        raise ValueError("CUDA unavailable")

    monkeypatch.setattr(dolphin_runtime, "require_cuda", unavailable)
    with pytest.raises(ValueError, match="CUDA unavailable"):
        asr_models.select_model("dolphin-small-cn", dolphin_device="gpu")
    assert asr_models.selected_model_id() == "whisper-turbo"
    with pytest.raises(ValueError, match="only valid"):
        asr_models.select_model("whisper-turbo", dolphin_device="cpu")
    with pytest.raises(ValueError, match="cpu or gpu"):
        asr_models.select_model("dolphin-small-cn", dolphin_device="auto")


def test_preparation_uses_selected_device(settings, monkeypatch):
    asr_models.select_model("dolphin-small-cn", dolphin_device="cpu")
    calls = []
    monkeypatch.setattr(transcriber, "install_dolphin_runtime", lambda _p, **kw: calls.append(kw["device"]))
    monkeypatch.setattr(transcriber, "download_model", lambda *_a: calls.append("download"))
    transcriber.prepare_asr_model("dolphin-small-cn")
    transcriber.prepare_asr_model("dolphin-small-cn", dolphin_device="gpu")
    assert calls == ["cpu", "download", "gpu", "download"]


def test_device_change_recreates_worker(settings, monkeypatch):
    monkeypatch.setattr(dolphin_runtime, "require_cuda", lambda: None)
    monkeypatch.setattr(transcriber, "prepare_asr_model", lambda *_a: None)
    monkeypatch.setattr(transcriber, "_loaded_model", None)
    monkeypatch.setattr(transcriber, "_loaded_key", None)
    calls = []

    class Worker:
        def __init__(self, device):
            calls.append(("new", device))

        def transcribe(self, _audio, _paths, _progress, stop):
            calls.append(("stop", stop))
            return [], 1000

        def close(self):
            calls.append(("close",))

    monkeypatch.setattr(transcriber, "DolphinWorker", Worker)
    # The production worker inherits QwenWorker, which owns process cleanup.
    monkeypatch.setattr(transcriber, "QwenWorker", Worker)
    stop = lambda: False
    asr_models.select_model("dolphin-small-cn", dolphin_device="cpu")
    with transcriber.asr_session():
        transcriber.transcribe_audio("test.wav", should_stop=stop)
        asr_models.select_model("dolphin-small-cn", dolphin_device="gpu")
        transcriber.transcribe_audio("test.wav", should_stop=stop)
        assert calls == [("new", "cpu"), ("stop", stop), ("close",), ("new", "gpu"), ("stop", stop)]
    assert calls[-1] == ("close",)
    transcriber.unload_model()


def test_cpu_worker_hides_cuda_before_import(settings, monkeypatch):
    captured = {}
    monkeypatch.setattr(transcriber, "dolphin_runtime_is_ready", lambda _d: True)
    monkeypatch.setattr(transcriber, "require_dolphin_cuda", lambda: pytest.fail("CPU must not probe CUDA"))
    monkeypatch.setattr(transcriber, "ROOT", settings)

    def popen(command, **kwargs):
        captured.update(command=command, **kwargs)
        return SimpleNamespace(stdout=iter(()))

    monkeypatch.setattr(transcriber.subprocess, "Popen", popen)
    monkeypatch.setattr(transcriber, "WindowsJob", lambda _p: SimpleNamespace(
        resume=lambda _p: None, close=lambda: None))
    worker = transcriber.DolphinWorker(device="cpu")
    worker.reader.join(timeout=2)
    assert captured["env"]["CUDA_VISIBLE_DEVICES"] == ""
    assert captured["env"]["HF_HUB_OFFLINE"] == "1"
    assert captured["command"][-2:] == ["--device", "cpu"]


def test_manifest_requires_all_checkpoint_and_vad_files(settings):
    model = asr_models.get_model("dolphin-small-cn")
    directory = asr_models.model_directory(model.id)
    files = {}
    for component, names in asr_models.DOLPHIN_REQUIRED_FILES.items():
        for name in names:
            file = directory / component / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b"test")
            files[file.relative_to(directory).as_posix()] = 4
    (directory / "ready.json").write_text(json.dumps({
        "repositories": dict(model.repositories), "files": files}), encoding="utf-8")
    assert asr_models.model_is_downloaded(model.id)
    (directory / "recognizer" / "units.txt").unlink()
    assert not asr_models.model_is_downloaded(model.id)
