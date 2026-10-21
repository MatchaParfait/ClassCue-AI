import json
from pathlib import Path
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import classcue.asr_models as asr_models


@pytest.fixture
def cache(monkeypatch, tmp_path):
    from scripts import prepare_asr_modelscope, prepare_firered_modelscope
    from scripts import prepare_asr_huggingface

    monkeypatch.setattr(asr_models, "MODEL_ROOT", tmp_path / "models")
    monkeypatch.setattr(asr_models, "SETTINGS_PATH", tmp_path / "settings.json")
    def offline(*_args):
        raise OSError("fixture ModelScope offline")

    monkeypatch.setattr(prepare_asr_modelscope, "prepare_model", offline)
    monkeypatch.setattr(prepare_firered_modelscope, "prepare_model", offline)
    def fake_components(model, paths, directory, progress):
        import huggingface_hub

        for component, repository in model.repositories:
            if model.engine == "firered" and asr_models._firered_component_ready(directory, component, repository):
                continue
            paths[component].mkdir(parents=True, exist_ok=True)
            huggingface_hub.snapshot_download(
                repo_id=repository, local_dir=str(paths[component]),
                allow_patterns=["VAD/*"] if model.engine == "firered" and component == "vad"
                else list(asr_models.HUB_ALLOW_PATTERNS))

    monkeypatch.setattr(prepare_asr_huggingface, "download_components", fake_components)
    return tmp_path


def fake_download(repo_id, local_dir, **_kwargs):
    directory = Path(local_dir)
    (directory / "config.json").write_text("{}", encoding="utf-8")
    suffix = "model.bin" if "whisper" in repo_id.lower() else "model.safetensors"
    (directory / suffix).write_bytes(b"weights")


def test_selection_and_status_do_not_download_or_create_model_cache(cache, monkeypatch):
    import huggingface_hub

    monkeypatch.setattr(huggingface_hub, "snapshot_download",
                        lambda **_kwargs: pytest.fail("Selection must not download"))
    assert asr_models.selected_model_id() == "whisper-turbo"
    asr_models.select_model("qwen3-asr-1.7b")
    assert asr_models.selected_model_id() == "qwen3-asr-1.7b"
    assert not asr_models.model_is_downloaded("qwen3-asr-1.7b")
    assert asr_models.cache_size("qwen3-asr-1.7b") == 0
    assert not asr_models.MODEL_ROOT.exists()


def test_corrupt_or_unknown_selection_falls_back_without_download(cache):
    asr_models.SETTINGS_PATH.write_text('{"model_id": "invalid"}', encoding="utf-8")
    assert asr_models.selected_model_id() == "whisper-turbo"
    asr_models.SETTINGS_PATH.write_text("bad json", encoding="utf-8")
    assert asr_models.selected_model_id() == "whisper-turbo"


@pytest.mark.parametrize("model_id", ["paraformer-large-vad-punc", "paraformer", "paraformer-zh"])
def test_retired_model_selection_falls_back_without_changing_cache(cache, model_id):
    settings = json.dumps({"model_id": model_id})
    asr_models.SETTINGS_PATH.write_text(settings, encoding="utf-8")
    retired_cache = asr_models.MODEL_ROOT / "paraformer-large-vad-punc"
    retired_cache.mkdir(parents=True)
    weights = retired_cache / "model.pt"
    weights.write_bytes(b"existing weights")
    assert asr_models.selected_model_id() == "whisper-turbo"
    with pytest.raises(ValueError, match="Unknown ASR model"):
        asr_models.get_model(model_id)
    assert weights.read_bytes() == b"existing weights"
    assert asr_models.SETTINGS_PATH.read_text(encoding="utf-8") == settings


def test_concurrent_model_selections_leave_valid_settings(cache):
    choices = ["whisper-turbo", "qwen3-asr-1.7b"] * 10
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(asr_models.select_model, choices))
    assert asr_models.selected_model_id() in choices
    assert not list(cache.glob("asr-settings-*.tmp"))
    assert not asr_models.MODEL_ROOT.exists()


def test_firered_cpu_selection_is_rejected_and_legacy_settings_require_explicit_gpu(cache):
    assert asr_models.selected_firered_device() == "gpu"
    asr_models.select_model("whisper-turbo")
    original = asr_models.SETTINGS_PATH.read_bytes()
    with pytest.raises(ValueError, match="CPU.*禁用"):
        asr_models.select_model("firered", firered_device="cpu")
    assert asr_models.SETTINGS_PATH.read_bytes() == original
    legacy = json.dumps({"model_id": "fireredasr2-aed", "firered_device": "cpu"})
    asr_models.SETTINGS_PATH.write_text(legacy, encoding="utf-8")
    assert asr_models.selected_firered_device() == "cpu"
    with pytest.raises(ValueError, match="CPU.*禁用"):
        asr_models.select_model("firered")
    assert asr_models.SETTINGS_PATH.read_text(encoding="utf-8") == legacy
    asr_models.select_model("whisper-turbo")
    assert asr_models.selected_firered_device() == "cpu"
    asr_models.select_model("firered", firered_device="gpu")
    assert asr_models.selected_firered_device() == "gpu"
    assert not asr_models.MODEL_ROOT.exists()


@pytest.mark.parametrize("value", [None, "cuda", [], {}])
def test_invalid_saved_firered_device_defaults_to_gpu(cache, value):
    asr_models.SETTINGS_PATH.write_text(json.dumps({"firered_device": value}), encoding="utf-8")
    assert asr_models.selected_firered_device() == "gpu"


def test_invalid_device_cannot_replace_saved_model_selection(cache):
    asr_models.select_model("whisper-turbo")
    with pytest.raises(ValueError, match="device"):
        asr_models.select_model("firered", firered_device="auto")
    assert asr_models.selected_model_id() == "whisper-turbo"


def test_qwen_download_includes_aligner_and_reuses_complete_bundle(cache, monkeypatch):
    import huggingface_hub

    calls = []

    def download(**kwargs):
        calls.append(kwargs["repo_id"])
        fake_download(**kwargs)

    monkeypatch.setattr(huggingface_hub, "snapshot_download", download)
    asr_models.download_model("qwen3-asr-1.7b")
    assert calls == ["Qwen/Qwen3-ASR-1.7B", "Qwen/Qwen3-ForcedAligner-0.6B"]
    assert asr_models.model_is_downloaded("qwen3-asr-1.7b")
    asr_models.download_model("qwen3-asr-1.7b")
    assert len(calls) == 2
    asr_models.component_paths("qwen3-asr-1.7b")["aligner"].joinpath("model.safetensors").unlink()
    assert not asr_models.model_is_downloaded("qwen3-asr-1.7b")


def test_interrupted_download_remains_partial_and_can_be_cleared(cache, monkeypatch):
    import huggingface_hub
    from scripts import prepare_asr_modelscope

    def fail_download(**kwargs):
        Path(kwargs["local_dir"]).joinpath("partial.bin").write_bytes(b"partial")
        raise OSError("network disconnected")

    monkeypatch.setattr(huggingface_hub, "snapshot_download", fail_download)
    monkeypatch.setattr(prepare_asr_modelscope, "prepare_model",
                        lambda *_args: (_ for _ in ()).throw(OSError("backup offline")))
    with pytest.raises(RuntimeError, match="backup offline"):
        asr_models.download_model("whisper-turbo")
    assert not asr_models.model_is_downloaded("whisper-turbo")
    assert asr_models.cache_size("whisper-turbo") > 0
    assert asr_models.delete_model("whisper-turbo") > 0
    assert asr_models.cache_size("whisper-turbo") == 0


def test_delete_only_selected_cache_and_unloads_before_deletion(cache, monkeypatch):
    import huggingface_hub

    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_download)
    asr_models.download_model("whisper-turbo")
    asr_models.download_model("whisper-large-v3")
    database = cache / "project.db"
    database.write_bytes(b"course data")
    calls = []
    asr_models.delete_model("whisper-turbo", unload=calls.append)
    assert calls == ["whisper-turbo"]
    assert asr_models.model_is_downloaded("whisper-large-v3")
    assert database.read_bytes() == b"course data"


def test_models_cannot_be_deleted_during_transcription(cache):
    ready, release = threading.Event(), threading.Event()

    def hold_model():
        with asr_models.model_operation("whisper-turbo"):
            ready.set()
            release.wait(10)

    worker = threading.Thread(target=hold_model)
    worker.start()
    assert ready.wait(5)
    try:
        with pytest.raises(asr_models.ModelBusyError):
            asr_models.delete_model("whisper-turbo")
    finally:
        release.set()
        worker.join(timeout=5)


def test_external_process_model_lock_prevents_deletion(cache):
    from filelock import FileLock

    lock_dir = asr_models.MODEL_ROOT / ".locks"
    lock_dir.mkdir(parents=True)
    with FileLock(str(lock_dir / "whisper-turbo.lock")):
        with pytest.raises(asr_models.ModelBusyError):
            asr_models.delete_model("whisper-turbo")


def test_delete_rejects_external_symlink(cache):
    target = cache / "course"
    target.mkdir()
    (target / "project.db").write_bytes(b"course data")
    asr_models.MODEL_ROOT.mkdir()
    link = asr_models.MODEL_ROOT / "whisper-turbo"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symbolic links is not allowed on this Windows host")
    with pytest.raises(ValueError):
        asr_models.delete_model("whisper-turbo")
    assert (target / "project.db").read_bytes() == b"course data"


def test_unknown_id_and_escaping_manifest_paths_are_rejected(cache):
    with pytest.raises(ValueError):
        asr_models.delete_model("../course")
    directory = asr_models.model_directory("whisper-turbo")
    directory.mkdir(parents=True)
    manifest = {"repositories": dict(asr_models.get_model("whisper-turbo").repositories),
                "files": {"../../outside.bin": 7}}
    (directory / "ready.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert not asr_models.model_is_downloaded("whisper-turbo")


def _legacy_firered_cache():
    directory = asr_models.model_directory("firered")
    recognizer = asr_models.component_paths("firered")["recognizer"]
    recognizer.mkdir(parents=True)
    files = {}
    for name in asr_models.FIRERED_REQUIRED_FILES["recognizer"]:
        data = b"" if name.endswith("config.yaml") else b"existing"
        (recognizer / name).write_bytes(data)
        files[f"recognizer/{name}"] = len(data)
    (directory / "ready.json").write_text(json.dumps({
        "repositories": {"recognizer": "FireRedTeam/FireRedASR2-AED"}, "files": files,
    }))
    return directory


def test_firered_upgrade_downloads_only_vad_and_requires_both_components(cache, monkeypatch):
    import huggingface_hub

    directory = _legacy_firered_cache()
    assert not asr_models.model_is_downloaded("firered")
    requests = []

    def download(**kwargs):
        requests.append(kwargs)
        for name in asr_models.FIRERED_REQUIRED_FILES["vad"]:
            path = Path(kwargs["local_dir"]) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"vad")

    monkeypatch.setattr(huggingface_hub, "snapshot_download", download)
    asr_models.download_model("firered")
    assert [request["repo_id"] for request in requests] == ["FireRedTeam/FireRedVAD"]
    assert requests[0]["allow_patterns"] == ["VAD/*"]
    assert asr_models.model_is_downloaded("firered")
    assert (directory / "recognizer/model.pth.tar").read_bytes() == b"existing"
    asr_models.download_model("firered")
    assert len(requests) == 1
    (directory / "vad/VAD/cmvn.ark").unlink()
    assert not asr_models.model_is_downloaded("firered")


def test_firered_incomplete_vad_download_cannot_upgrade_manifest(cache, monkeypatch):
    import huggingface_hub

    _legacy_firered_cache()
    monkeypatch.setattr(huggingface_hub, "snapshot_download", lambda **_: None)
    with pytest.raises(RuntimeError, match="下载不完整"):
        asr_models.download_model("firered")
    assert not asr_models.model_is_downloaded("firered")


def test_firered_prefers_direct_modelscope_source(cache, monkeypatch):
    import huggingface_hub
    from scripts import prepare_firered_modelscope

    _legacy_firered_cache()
    calls = []

    def fail(**_kwargs):
        pytest.fail("Hugging Face must not be contacted when ModelScope succeeds")

    monkeypatch.setattr(huggingface_hub, "snapshot_download", fail)
    monkeypatch.setattr(prepare_firered_modelscope, "prepare_model", lambda progress: calls.append(progress))
    callback = lambda *_args: None
    asr_models.download_model("firered", callback)
    assert calls == [callback]
    assert not asr_models.model_is_downloaded("firered")


@pytest.mark.parametrize("model_id", ["whisper-turbo", "whisper-large-v3", "qwen3-asr-1.7b"])
def test_direct_modelscope_source_does_not_contact_huggingface(cache, monkeypatch, model_id):
    import huggingface_hub
    from scripts import prepare_asr_modelscope

    def fail(**_kwargs):
        pytest.fail("Hugging Face must not be contacted when ModelScope succeeds")

    calls, messages = [], []
    monkeypatch.setattr(huggingface_hub, "snapshot_download", fail)
    monkeypatch.setattr(prepare_asr_modelscope, "prepare_model",
                        lambda model, progress: calls.append(model))
    asr_models.download_model(model_id, lambda _value, message: messages.append(message))
    assert calls == [model_id]
    assert any("ModelScope" in message for message in messages)
    assert not asr_models.model_is_downloaded(model_id)


def test_cancelled_preparation_never_switches_download_sources(cache, monkeypatch):
    from classcue.processing_control import ProcessingCancelled
    from scripts import prepare_asr_modelscope, prepare_asr_huggingface
    def stop(*_a):
        raise ProcessingCancelled("stop")
    monkeypatch.setattr(prepare_asr_modelscope, "prepare_model", stop)
    monkeypatch.setattr(prepare_asr_huggingface, "download_components",
                        lambda *_a: pytest.fail("Cancellation must not start a fallback download"))
    with pytest.raises(ProcessingCancelled):
        asr_models.download_model("whisper-turbo")
