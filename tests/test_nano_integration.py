from types import SimpleNamespace
import json

import pytest

import classcue.asr_models as asr_models
import classcue.nano_runtime as nano_runtime
import classcue.transcriber as transcriber


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr(asr_models, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(asr_models, "MODEL_ROOT", tmp_path / "models")
    return tmp_path


def test_retired_nano_cannot_replace_active_selection(settings, monkeypatch):
    monkeypatch.setattr(nano_runtime, "require_cuda", lambda: pytest.fail("No CUDA probe"))
    asr_models.select_model("paraformer-large-cpu", paraformer_device="cpu")
    original = asr_models.SETTINGS_PATH.read_bytes()
    for device in ("cpu", "gpu"):
        with pytest.raises(ValueError, match="已下架"):
            asr_models.select_model("fun-asr-nano-2512", nano_device=device)
    assert asr_models.SETTINGS_PATH.read_bytes() == original
    assert asr_models.selected_model_id() == "paraformer-large-cpu"
    assert asr_models.selected_paraformer_device() == "cpu"
    assert not asr_models.MODEL_ROOT.exists()


@pytest.mark.parametrize("device", ["cpu", "gpu"])
def test_legacy_nano_selection_uses_default_without_rewriting_settings_or_weights(settings, device):
    original = json.dumps({"model_id": "fun-asr-nano-2512", "nano_device": device})
    asr_models.SETTINGS_PATH.write_text(original, encoding="utf-8")
    recognizer = asr_models.component_paths("fun-asr-nano-2512")["recognizer"]
    recognizer.mkdir(parents=True)
    weights = recognizer / "model.pt"
    weights.write_bytes(b"retained experiment checkpoint")
    assert "fun-asr-nano-2512" not in asr_models.MODELS
    assert asr_models.selected_model_id() == asr_models.DEFAULT_MODEL_ID
    assert asr_models.selected_nano_device() == device
    assert asr_models.SETTINGS_PATH.read_text(encoding="utf-8") == original
    assert weights.read_bytes() == b"retained experiment checkpoint"
    with pytest.raises(ValueError, match="only valid"):
        asr_models.select_model("whisper-turbo", nano_device="cpu")


@pytest.mark.parametrize("device", ["cpu", "gpu"])
def test_retired_nano_preparation_rejected_before_install_or_download(settings, monkeypatch, device):
    monkeypatch.setattr(transcriber, "install_nano_runtime",
                        lambda *_a, **_kw: pytest.fail("No runtime install"))
    monkeypatch.setattr(transcriber, "download_model", lambda *_a: pytest.fail("No download"))
    with pytest.raises(ValueError, match="已下架"):
        transcriber.prepare_asr_model("fun-asr-nano-2512", nano_device=device)
    with pytest.raises(ValueError, match="已下架"):
        asr_models.download_model("fun-asr-nano-2512")
    assert not asr_models.MODEL_ROOT.exists()


def test_retired_nano_transcription_rejected_before_preparation_or_worker_start(settings, monkeypatch):
    monkeypatch.setattr(transcriber, "prepare_asr_model", lambda *_a: pytest.fail("No preparation"))
    monkeypatch.setattr(transcriber, "NanoWorker", lambda **_kw: pytest.fail("No worker"))
    with pytest.raises(transcriber.ASRSetupError, match="已下架"):
        transcriber.transcribe_audio("missing.wav", model_id="fun-asr-nano-2512")
    assert not asr_models.MODEL_ROOT.exists()


def test_cpu_worker_environment_hides_cuda_before_process_start(settings, monkeypatch):
    captured = {}
    monkeypatch.setattr(transcriber, "nano_runtime_is_ready", lambda _device: True)
    monkeypatch.setattr(transcriber, "require_nano_cuda", lambda: pytest.fail("CPU must not probe CUDA"))
    monkeypatch.setattr(transcriber, "ROOT", settings)

    def popen(command, **kwargs):
        captured.update(command=command, **kwargs)
        return SimpleNamespace(stdout=iter(()))

    monkeypatch.setattr(transcriber.subprocess, "Popen", popen)
    monkeypatch.setattr(transcriber, "WindowsJob", lambda _p: SimpleNamespace(
        resume=lambda _p: None, close=lambda: None))
    worker = transcriber.NanoWorker(device="cpu")
    worker.reader.join(timeout=2)
    assert captured["env"]["CUDA_VISIBLE_DEVICES"] == ""
    assert captured["env"]["HF_HUB_OFFLINE"] == "1"
    assert captured["command"][-2:] == ["--device", "cpu"]
