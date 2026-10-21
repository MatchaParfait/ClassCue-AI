from types import SimpleNamespace
from threading import Thread

import pytest

import asr_models
from preparation_progress import preparation_events
from scripts import prepare_asr_huggingface as downloader


@pytest.mark.parametrize("model_id", ["whisper-turbo", "whisper-large-v3", "qwen3-asr-1.7b", "fireredasr2-aed"])
def test_fallback_reports_specific_file_and_pinned_revision(tmp_path, monkeypatch, model_id):
    import huggingface_hub
    monkeypatch.setattr(asr_models, "MODEL_ROOT", tmp_path)
    model = asr_models.get_model(model_id)
    def metadata(self, repository, **_kwargs):
        component = next(name for name, repo in model.repositories if repo == repository)
        names = asr_models.FIRERED_REQUIRED_FILES[component] if model.engine == "firered" else ["config.json", "model.safetensors"]
        return SimpleNamespace(sha="a" * 40, siblings=[
            SimpleNamespace(rfilename=name, size=100) for name in names])
    monkeypatch.setattr(huggingface_hub.HfApi, "model_info", metadata)
    requests = []
    def download(**kwargs):
        requests.append(kwargs)
        # Some Hub engines invoke progress callbacks from a worker thread.
        def transfer():
            with kwargs["tqdm_class"](total=100, initial=0) as bar:
                bar.update(50)
        worker = Thread(target=transfer)
        worker.start()
        worker.join()
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", download)
    events = []
    with preparation_events(events.append):
        downloader.download_components(model, asr_models.component_paths(model.id),
                                       asr_models.model_directory(model.id))
    assert requests and all(item["revision"] == "a" * 40 for item in requests)
    file_events = [event for event in events if event["phase"] == "downloading"]
    assert any(event["file_bytes_completed"] == 50 for event in file_events)
    assert {event["current_file"] for event in file_events} == {item["filename"] for item in requests}
    assert file_events[-1]["bytes_completed"] == file_events[-1]["bytes_total"]
