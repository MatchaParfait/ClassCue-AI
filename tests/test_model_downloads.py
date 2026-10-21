import hashlib
import json
from threading import Event

import pytest

import asr_models
import model_downloads as downloads
from preparation_progress import preparation_events, stage


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(asr_models, "MODEL_ROOT", tmp_path / "models")
    return tmp_path


@pytest.mark.parametrize("model_id", list(asr_models.MODELS))
def test_pause_survives_new_reader_without_starting_a_download(cache, model_id):
    directory = asr_models.component_paths(model_id)["recognizer"]
    with pytest.raises(downloads.PreparationPaused):
        with downloads.download_session(model_id, device="cpu"):
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "model.bin").write_bytes(b"complete")
            (directory / "other.bin.part").write_bytes(b"partial")
            stage("downloading", "recognizer/other.bin", component="recognizer",
                  current_file="other.bin", bytes_total=100)
            raise downloads.PreparationPaused("pause")
    state = downloads.download_snapshot(model_id)
    assert state["status"] == "paused"
    assert state["retained_bytes"] == 15
    assert state["completed_bytes"] == 8
    assert state["device"] == "cpu"
    assert state["can_cleanup"]
    # A fresh read never changes state or schedules anything.
    assert downloads.download_snapshot(model_id) == state


def test_crash_record_is_interrupted_and_success_removes_it(cache):
    model_id = "whisper-turbo"
    downloads._write(model_id, {"status": "preparing", "device": None})
    assert downloads.download_snapshot(model_id)["status"] == "interrupted"
    assert downloads.download_snapshot(model_id, active=True)["status"] == "preparing"
    with downloads.download_session(model_id):
        stage("checking", "existing weights")
    assert downloads.download_snapshot(model_id) is None


def test_cleanup_preserves_completed_weights_markers_metadata_and_environment(cache):
    model_id = "qwen3-asr-1.7b"
    root = asr_models.model_directory(model_id)
    files = {
        "recognizer/model.bin": b"verified",
        "recognizer/model2.bin.part": b"partial",
        "recognizer/model2.bin.part.aria2": b"piece-map",
        "recognizer/model2.bin.part.json": b"{}",
        "aligner/model.bin": b"aligner",
        "recognizer/.cache/huggingface/download/model3.bin.hash.incomplete": b"partial",
        "recognizer/.cache/huggingface/download/model.bin.metadata": b"metadata",
        ".hub-cache/blobs/abc.incomplete": b"partial",
        ".hub-cache/blobs/complete-blob": b"cached",
        "download.pending.json": b"{}",
        "ready.tmp": b"{}",
        "ready.json": b'{"fixture": true}',
    }
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    runtime = cache / "runtime/python.exe"
    runtime.parent.mkdir()
    runtime.write_bytes(b"runtime")
    downloads._write(model_id, {"status": "paused", "device": "cpu"})
    assert downloads.cleanup_incomplete(model_id) > 0
    for name in ("recognizer/model.bin", "aligner/model.bin", "ready.json",
                 "recognizer/.cache/huggingface/download/model.bin.metadata", ".hub-cache/blobs/complete-blob"):
        assert (root / name).read_bytes() == files[name]
    assert runtime.read_bytes() == b"runtime"
    assert not list(root.rglob("*.part"))
    assert not list(root.rglob("*.incomplete"))
    state = downloads.download_snapshot(model_id)
    assert state["retained_bytes"] == 15
    assert not state["can_cleanup"]
    assert state["device"] == "cpu"


def test_only_temporary_files_cleanup_returns_to_empty(cache):
    root = asr_models.component_paths("whisper-turbo")["recognizer"]
    root.mkdir(parents=True)
    (root / "model.bin.part").write_bytes(b"half")
    downloads._write("whisper-turbo", {"status": "paused"})
    downloads.cleanup_incomplete("whisper-turbo")
    assert downloads.download_snapshot("whisper-turbo") is None


def test_ready_model_with_leftover_transfer_only_offers_cleanup(cache):
    model_id = "whisper-turbo"
    root = asr_models.component_paths(model_id)["recognizer"]
    root.mkdir(parents=True)
    (root / "model.bin").write_bytes(b"ready")
    (root / "old.bin.part").write_bytes(b"unused")
    manifest = {"repositories": dict(asr_models.get_model(model_id).repositories),
                "files": {"recognizer/model.bin": 5}}
    (root.parent / "ready.json").write_text(json.dumps(manifest))
    assert asr_models.model_is_downloaded(model_id)
    snapshot = downloads.download_snapshot(model_id)
    assert not snapshot["resumable"]
    assert snapshot["can_cleanup"]
    downloads.cleanup_incomplete(model_id)
    assert asr_models.model_is_downloaded(model_id)
    assert downloads.download_snapshot(model_id) is None


def test_sparse_partial_is_not_counted_as_received_bytes(cache):
    model_id = "whisper-turbo"
    root = asr_models.component_paths(model_id)["recognizer"]
    root.mkdir(parents=True)
    (root / "model.bin.part").write_bytes(b"x" * 1000)
    (root / "model.bin.part.aria2").write_bytes(b"pieces")
    downloads._write(model_id, {"status": "paused", "component": "recognizer",
                               "current_file": "model.bin", "file_bytes_completed": 120})
    state = downloads.download_snapshot(model_id)
    assert state["partial_bytes"] == 120
    assert state["retained_bytes_estimated"]


def test_pause_during_report_retains_original_exception_and_state(cache):
    signal = Event()

    def control():
        if signal.is_set():
            raise downloads.PreparationPaused("pause")

    with pytest.raises(downloads.PreparationPaused):
        with preparation_events(lambda _event: None, checkpoint=control):
            with downloads.download_session("whisper-turbo"):
                stage("downloading", "weights")
                signal.set()
                stage("downloading", "weights")
    assert downloads.download_snapshot("whisper-turbo")["status"] == "paused"


def test_cleanup_rejects_escape_before_deleting_any_file(cache):
    root = asr_models.component_paths("whisper-turbo")["recognizer"]
    root.mkdir(parents=True)
    (root / "model.bin.part").write_bytes(b"keep")
    outside = cache / "outside"
    outside.mkdir()
    (outside / "secret.part").write_bytes(b"keep")
    try:
        (root / "escape").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Windows symlink privilege unavailable")
    with pytest.raises(ValueError):
        downloads.cleanup_incomplete("whisper-turbo")
    assert (root / "model.bin.part").read_bytes() == b"keep"
    assert (outside / "secret.part").read_bytes() == b"keep"


def test_changed_revision_partial_is_discarded_before_range_resume(cache, monkeypatch):
    from scripts import prepare_asr_modelscope as source
    root = asr_models.component_paths("whisper-turbo")["recognizer"]
    root.mkdir(parents=True)
    path = root / "model.bin"
    (root / "model.bin.part").write_bytes(b"old")
    (root / "model.bin.part.json").write_text(json.dumps({"sha256": "old", "size": 3}))
    monkeypatch.setattr(source, "ROOT", cache)
    content = b"new-content"

    def stream(_url, partial, _size, report):
        assert not partial.exists()
        partial.write_bytes(content)
        report(len(content))

    monkeypatch.setattr(source, "_stream", stream)
    source._download(path, {"Size": len(content), "Sha256": hashlib.sha256(content).hexdigest()},
                     "https://unused.test/model", lambda _: None)
    assert path.read_bytes() == content
    assert not (root / "model.bin.part.json").exists()
