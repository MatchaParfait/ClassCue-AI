from threading import Event
from types import SimpleNamespace

import pytest

from api.main import JobManager
from classcue.diagnostics import register_secret
from classcue.processing_control import ProcessingCancelled
from classcue.video_store import VideoStore


@pytest.mark.parametrize("phase", ["initial_state", "request_state", "executor"])
def test_initialization_failure_cleans_state_and_allows_next_job(tmp_path, monkeypatch, phase):
    store = VideoStore(tmp_path / "jobs.db")
    manager = JobManager(SimpleNamespace(get=lambda: store))
    original = store.update_job
    failed = False

    def write(job_id, **fields):
        nonlocal failed
        selected = (
            phase == "initial_state" and fields.get("status") == "running"
            or phase == "request_state" and "request_json" in fields
        )
        if selected and not failed:
            failed = True
            raise OSError("injected initialization failure")
        return original(job_id, **fields)

    monkeypatch.setattr(store, "update_job", write)
    if phase == "executor":
        original_submit = manager.executor.submit
        monkeypatch.setattr(
            manager.executor, "submit",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("injected submission failure")),
        )
    try:
        if phase == "initial_state":
            manager.submit("test", None, lambda *_args: pytest.fail("must not start"))
            manager.executor.submit(lambda: None).result(timeout=10)
        else:
            with pytest.raises((OSError, RuntimeError)):
                manager.submit("test", None, lambda *_args: None, request={"options": {}})
        assert not manager._futures
        assert not manager._cancel_events
        assert not store.list_active_jobs()
        if phase == "executor":
            monkeypatch.setattr(manager.executor, "submit", original_submit)
        manager.submit("test", None, lambda *_args: None)
        manager.executor.submit(lambda: None).result(timeout=10)
        assert not manager._futures
        assert not manager._cancel_events
    finally:
        manager.shutdown(wait=True)


def test_persistent_database_failure_still_releases_in_memory_state(tmp_path, monkeypatch, caplog):
    store = VideoStore(tmp_path / "jobs.db")
    manager = JobManager(SimpleNamespace(get=lambda: store))
    monkeypatch.setattr(store, "update_job", lambda *_a, **_k: (_ for _ in ()).throw(OSError("disk full")))
    try:
        manager.submit("test", None, lambda *_args: pytest.fail("must not start"))
        manager.executor.submit(lambda: None).result(timeout=10)
        assert not manager._futures
        assert not manager._cancel_events
        assert "Could not persist job failure" in caplog.text
        assert "disk full" in caplog.text
    finally:
        manager.shutdown(wait=True)


def test_job_error_has_stage_and_traceback_without_secret(tmp_path, caplog):
    store = VideoStore(tmp_path / "jobs.db")
    manager = JobManager(SimpleNamespace(get=lambda: store))
    secret = "test-private-credential-123456"
    register_secret(secret)

    def operation(update, _stop):
        update(.5, "working", "transcribing")
        raise ValueError(f"injected {secret}")

    try:
        job_id = manager.submit("video_transcribe", None, operation, request={"options": {"asr_model_id": "test"}})
        manager.executor.submit(lambda: None).result(timeout=10)
        assert secret not in store.get_job(job_id)["error_message"]
        assert secret not in caplog.text
        assert "transcribing" in caplog.text
        assert "ValueError" in caplog.text
        assert "Traceback" in caplog.text
        assert job_id in caplog.text
    finally:
        manager.shutdown(wait=True)


def test_shutdown_waits_for_cleanup_even_when_cancel_write_fails(tmp_path, monkeypatch):
    store = VideoStore(tmp_path / "jobs.db")
    manager = JobManager(SimpleNamespace(get=lambda: store))
    started, cleaned = Event(), Event()

    def operation(_update, stop):
        started.set()
        while not stop():
            cleaned.wait(.01)
        cleaned.set()
        raise ProcessingCancelled("shutdown")

    manager.submit("test", None, operation)
    assert started.wait(5)
    monkeypatch.setattr(store, "get_job", lambda *_a: (_ for _ in ()).throw(OSError("unavailable")))
    manager.shutdown(wait=True)
    assert cleaned.is_set()
    assert not manager._futures
    assert not manager._cancel_events
    with pytest.raises(RuntimeError):
        manager.submit("test", None, operation)
