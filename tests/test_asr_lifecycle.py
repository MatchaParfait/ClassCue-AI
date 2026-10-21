from contextvars import ContextVar
import sys
from threading import Event, Thread
from types import SimpleNamespace
import weakref

import pytest

import classcue.asr_models as asr_models
import classcue.batch_processing as batch_processing
from classcue.core import SemanticSelection, TranscriptSegment
from classcue.processing_control import ProcessingCancelled, ResourcePaused
import classcue.transcriber as transcriber
import classcue.video_pipeline as video_pipeline
from classcue.video_store import VideoStore


@pytest.fixture(params=["dolphin", "paraformer"])
def worker_setup(tmp_path, monkeypatch, request):
    engine = request.param
    model_id = "dolphin-small-cn" if engine == "dolphin" else "paraformer-large-cpu"
    monkeypatch.setattr(asr_models, "MODEL_ROOT", tmp_path / "models")
    monkeypatch.setattr(transcriber, "_loaded_model", None)
    monkeypatch.setattr(transcriber, "_loaded_key", None)
    monkeypatch.setattr(transcriber, "_loaded_session", None)
    monkeypatch.setattr(transcriber, "_SESSION", ContextVar("test_asr_session", default=None))
    monkeypatch.setattr(transcriber, "prepare_asr_model", lambda *_a: None)
    monkeypatch.setattr(transcriber, f"selected_{engine}_device", lambda: "cpu")
    workers = []

    class Worker:
        def __init__(self, device):
            self.closed = 0
            self.calls = 0
            self.failure = None
            workers.append(self)

        def transcribe(self, _audio, _paths, _progress, should_stop):
            self.calls += 1
            if self.failure:
                raise self.failure
            if should_stop and should_stop():
                raise ProcessingCancelled("stop")
            return [TranscriptSegment(1, 0, 1000, "test")], 1000

        def close(self):
            self.closed += 1

    monkeypatch.setattr(transcriber, "QwenWorker", Worker)
    monkeypatch.setattr(transcriber, f"{engine.title()}Worker", Worker)
    yield SimpleNamespace(model_id=model_id, workers=workers)
    transcriber.unload_model()


def test_standalone_transcriptions_release_each_worker(worker_setup):
    for _ in range(2):
        transcriber.transcribe_audio("sample.wav", model_id=worker_setup.model_id)
        assert transcriber._loaded_model is transcriber._loaded_key is None
        assert transcriber._loaded_session is None
    assert [w.closed for w in worker_setup.workers] == [1, 1]
    assert transcriber._SESSION.get() is None


def test_nested_batch_scope_reuses_worker_until_outer_exit(worker_setup):
    with transcriber.asr_session():
        owner = transcriber._SESSION.get()
        for _ in range(2):
            with transcriber.asr_session():
                assert transcriber._SESSION.get() is owner
                transcriber.transcribe_audio("sample.wav", model_id=worker_setup.model_id)
            assert worker_setup.workers[0].closed == 0
        assert len(worker_setup.workers) == 1
        assert worker_setup.workers[0].calls == 2
    assert worker_setup.workers[0].closed == 1
    assert transcriber._loaded_model is None


@pytest.mark.parametrize("failure_type", [RuntimeError, ProcessingCancelled, ResourcePaused])
def test_failure_releases_worker_and_batch_can_recreate(worker_setup, failure_type):
    with transcriber.asr_session():
        transcriber.transcribe_audio("sample.wav", model_id=worker_setup.model_id)
        worker_setup.workers[0].failure = failure_type("injected")
        with pytest.raises(failure_type):
            transcriber.transcribe_audio("sample.wav", model_id=worker_setup.model_id)
        assert worker_setup.workers[0].closed == 1
        assert transcriber._loaded_model is None
        transcriber.transcribe_audio("sample.wav", model_id=worker_setup.model_id)
        assert worker_setup.workers[1].closed == 0
    assert worker_setup.workers[1].closed == 1


def test_outer_pipeline_failure_also_releases_worker(worker_setup):
    with pytest.raises(RuntimeError):
        with transcriber.asr_session():
            transcriber.transcribe_audio("sample.wav", model_id=worker_setup.model_id)
            raise RuntimeError("saving transcript failed")
    assert worker_setup.workers[0].closed == 1
    assert transcriber._SESSION.get() is None


def test_busy_request_does_not_close_another_sessions_worker(worker_setup):
    errors = []
    with transcriber.asr_session(), asr_models.model_operation(worker_setup.model_id):
        transcriber.transcribe_audio("sample.wav", model_id=worker_setup.model_id)

        def rejected_request():
            try:
                transcriber.transcribe_audio("other.wav", model_id=worker_setup.model_id)
            except Exception as exc:
                errors.append(exc)

        thread = Thread(target=rejected_request)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], asr_models.ModelBusyError)
        assert worker_setup.workers[0].closed == 0
    assert worker_setup.workers[0].closed == 1


def test_single_video_and_real_shared_batch_boundaries(worker_setup, tmp_path, monkeypatch):
    store = VideoStore(tmp_path / "videos.db")
    paths, video_ids = [], []
    for index in range(3):
        path = tmp_path / f"video{index}.mp4"
        path.write_bytes(f"video{index}".encode())
        stat = path.stat()
        video_ids.append(store.create_video(
            store.file_hash(path), path.name, str(path.resolve()),
            str(tmp_path / f"video{index}.wav"), 1000, stat.st_size, stat.st_mtime_ns,
        ))
        paths.append(path)
    judge = SimpleNamespace(judge_semantic_chunk=lambda _chunk: SemanticSelection())
    video_pipeline.process_video_once(
        store, video_ids[0], judge, asr_model_id=worker_setup.model_id,
    )
    assert worker_setup.workers[0].closed == 1
    observed = []

    def event(_event):
        if len(worker_setup.workers) > 1:
            observed.append(worker_setup.workers[1].closed)

    monkeypatch.setattr(batch_processing, "_average_transcription_rate", lambda _s: 1.0)
    summary = batch_processing.run_batch(
        store, paths[1:], tmp_path, judge,
        asr_model_id=worker_setup.model_id, on_event=event,
    )
    assert summary.completed == 2 and summary.failed == 0
    assert len(worker_setup.workers) == 2
    assert worker_setup.workers[1].calls == 2
    assert observed and not any(observed)
    assert worker_setup.workers[1].closed == 1


def test_whisper_session_drops_last_resident_reference(tmp_path, monkeypatch):
    references = []

    class WhisperModel:
        def __init__(self, *_args, **_kwargs):
            references.append(weakref.ref(self))

    monkeypatch.setattr(transcriber, "_loaded_model", None)
    monkeypatch.setattr(transcriber, "_loaded_key", None)
    monkeypatch.setattr(transcriber, "_loaded_session", None)
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=WhisperModel))
    monkeypatch.setattr(transcriber, "component_paths", lambda _id: {"recognizer": tmp_path})
    with transcriber.asr_session():
        transcriber._load_whisper_model("whisper-turbo", "cpu", "int8")
        assert references[0]() is not None
    assert transcriber._loaded_model is None
    assert references[0]() is None


def test_api_shutdown_cancels_worker_and_rejects_new_jobs(worker_setup, tmp_path):
    from api.main import JobManager

    store = VideoStore(tmp_path / "jobs.db")
    manager = JobManager(SimpleNamespace(get=lambda: store))
    started, cancellation = Event(), Event()

    def operation(_update, should_stop):
        transcriber.transcribe_audio("sample.wav", model_id=worker_setup.model_id)
        started.set()
        assert cancellation.wait(timeout=5)
        assert should_stop()
        raise ProcessingCancelled("shutdown")

    try:
        job_id = manager.submit("test", None, operation)
        assert started.wait(timeout=5)
        future = manager._futures[job_id]
        assert worker_setup.workers[0].closed == 0
        manager.shutdown()
        with pytest.raises(RuntimeError):
            manager.submit("test", None, operation)
        cancellation.set()
        future.result(timeout=5)
        assert store.get_job(job_id)["status"] == "interrupted"
        assert worker_setup.workers[0].closed == 1
    finally:
        cancellation.set()
        manager.executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize("outcome", ["completed", "failed"])
def test_api_terminal_status_follows_model_cleanup(worker_setup, tmp_path, outcome):
    from api.main import JobManager

    store = VideoStore(tmp_path / "jobs.db")
    manager = JobManager(SimpleNamespace(get=lambda: store))
    started, finish = Event(), Event()

    def operation(_update, _should_stop):
        transcriber.transcribe_audio("sample.wav", model_id=worker_setup.model_id)
        started.set()
        assert finish.wait(timeout=5)
        if outcome == "failed":
            raise RuntimeError("injected")

    try:
        job_id = manager.submit("test", None, operation)
        assert started.wait(timeout=5)
        future = manager._futures[job_id]
        assert worker_setup.workers[0].closed == 0
        finish.set()
        future.result(timeout=5)
        assert store.get_job(job_id)["status"] == outcome
        assert worker_setup.workers[0].closed == 1
    finally:
        finish.set()
        manager.shutdown()
        manager.executor.shutdown(wait=True, cancel_futures=True)
