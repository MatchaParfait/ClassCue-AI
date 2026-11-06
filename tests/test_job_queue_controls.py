from threading import Event
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.main import JobManager, create_app
from classcue.processing_control import ProcessingCancelled, ResourcePaused
from classcue.video_store import VideoStore


@pytest.fixture
def queue(tmp_path):
    store = VideoStore(tmp_path / "jobs.db")
    jobs = JobManager(SimpleNamespace(get=lambda: store))
    try:
        yield store, jobs
    finally:
        jobs.shutdown(wait=True)


@pytest.mark.parametrize("release", ["resume", "cancel"])
def test_resource_pause_holds_existing_queue_until_resume_or_cancel(queue, release):
    store, jobs = queue
    started, allow_pause, second_started = Event(), Event(), Event()
    order = []

    def first(_update, _stop):
        started.set()
        assert allow_pause.wait(5)
        raise ResourcePaused("synthetic resource shortage")

    first_id = jobs.submit("test", None, first)
    assert started.wait(5)
    second_id = jobs.submit("test", None, lambda *_: (order.append("second"), second_started.set()),
                            allow_queue=True)
    allow_pause.set()
    jobs.executor.submit(lambda: None).result(timeout=5)
    assert store.get_job(first_id)["status"] == "resource_paused"
    assert store.get_job(second_id)["status"] == "queued"
    assert not second_started.is_set()

    if release == "resume":
        resume_started, resume_finish = Event(), Event()

        def resumed(_update, _stop):
            order.append("resumed")
            resume_started.set()
            assert resume_finish.wait(5)

        with jobs._lock:
            resumed_id = jobs.submit("test", None, resumed, resume_from=first_id)
            store.update_job(first_id, status="interrupted", stage="resumed")
        try:
            assert resume_started.wait(5)
            assert not second_started.is_set()
        finally:
            resume_finish.set()
        assert second_started.wait(5)
        jobs.executor.submit(lambda: None).result(timeout=5)
        assert store.get_job(resumed_id)["status"] == "completed"
        assert order == ["resumed", "second"]
    else:
        jobs.cancel(first_id)
        assert second_started.wait(5)
        jobs.executor.submit(lambda: None).result(timeout=5)
        assert order == ["second"]
    assert store.get_job(second_id)["status"] == "completed"
    assert not jobs._deferred
    assert not jobs._cancel_events


def test_cancel_resource_blocked_queued_task_never_starts(queue):
    store, jobs = queue
    store.create_job("paused", "test")
    store.update_job("paused", status="resource_paused")
    job_id = jobs.submit("test", None, lambda *_: pytest.fail("cancelled task started"), allow_queue=True)
    jobs.executor.submit(lambda: None).result(timeout=5)
    assert job_id in jobs._deferred
    assert jobs.cancel(job_id)["status"] == "interrupted"
    jobs.cancel("paused")
    jobs.executor.submit(lambda: None).result(timeout=5)
    assert not jobs._deferred
    assert not jobs._futures
    assert not jobs._cancel_events


def test_cancelling_resource_pause_preserves_order_before_pending_executor_wrappers(queue):
    store, jobs = queue
    started, allow_pause, worker_parked, release_worker = Event(), Event(), Event(), Event()
    order = []

    def first(_update, _stop):
        started.set()
        assert allow_pause.wait(5)
        raise ResourcePaused("synthetic resource shortage")

    def park_worker():
        worker_parked.set()
        assert release_worker.wait(5)

    first_id = jobs.submit("test", None, first)
    assert started.wait(5)
    second_id = jobs.submit("test", None, lambda *_: order.append("second"), allow_queue=True)
    # Pause the worker after the second job defers and before the third wrapper
    # starts, making the API cancellation's scheduling window deterministic.
    jobs.executor.submit(park_worker)
    third_id = jobs.submit("test", None, lambda *_: order.append("third"), allow_queue=True)
    try:
        allow_pause.set()
        assert worker_parked.wait(5)
        assert second_id in jobs._deferred
        jobs.cancel(first_id)
    finally:
        allow_pause.set()
        release_worker.set()
    jobs.executor.submit(lambda: None).result(timeout=5)
    # Redispatch may append work after the sentinel above, so wait for both jobs.
    for job_id in [second_id, third_id]:
        future = jobs._futures.get(job_id)
        if future is not None:
            future.result(timeout=5)
    assert order == ["second", "third"]
    assert store.get_job(second_id)["status"] == "completed"
    assert store.get_job(third_id)["status"] == "completed"


def test_second_resource_pause_preserves_original_submission_order(queue):
    store, jobs = queue
    started, allow_pause, worker_parked, release_worker = Event(), Event(), Event(), Event()
    order = []

    def first(_update, _stop):
        started.set()
        assert allow_pause.wait(5)
        raise ResourcePaused("first resource shortage")

    def second(_update, _stop):
        raise ResourcePaused("second resource shortage")

    def park_worker():
        worker_parked.set()
        assert release_worker.wait(5)

    first_id = jobs.submit("test", None, first)
    assert started.wait(5)
    second_id = jobs.submit("test", None, second, allow_queue=True)
    third_id = jobs.submit("test", None, lambda *_: order.append("third"), allow_queue=True)
    jobs.executor.submit(park_worker)
    fourth_id = jobs.submit("test", None, lambda *_: order.append("fourth"), allow_queue=True)
    try:
        allow_pause.set()
        assert worker_parked.wait(5)
        assert second_id in jobs._deferred and third_id in jobs._deferred
        jobs.cancel(first_id)
    finally:
        allow_pause.set()
        release_worker.set()
    jobs.executor.submit(lambda: None).result(timeout=5)
    assert store.get_job(second_id)["status"] == "resource_paused"
    assert third_id in jobs._deferred and fourth_id in jobs._deferred
    jobs.cancel(second_id)
    jobs.executor.submit(lambda: None).result(timeout=5)
    assert order == ["third", "fourth"]
    assert store.get_job(third_id)["status"] == "completed"
    assert store.get_job(fourth_id)["status"] == "completed"


def test_stop_all_closes_dispatch_before_cancelling_running_and_queued_tasks(queue):
    store, jobs = queue
    started, cancelled = Event(), Event()

    def first(_update, stop):
        started.set()
        while not stop():
            cancelled.wait(.01)
        cancelled.set()
        raise ProcessingCancelled("stop all")

    first_id = jobs.submit("test", None, first)
    assert started.wait(5)
    second_id = jobs.submit("test", None, lambda *_: pytest.fail("queued task started"), allow_queue=True)
    store.create_job("paused", "test")
    store.update_job("paused", status="resource_paused")
    jobs.stop_all()
    assert cancelled.wait(5)
    jobs.executor.submit(lambda: None).result(timeout=5)
    for job_id in [first_id, second_id, "paused"]:
        assert store.get_job(job_id)["status"] == "interrupted"
    with pytest.raises(RuntimeError, match="关闭"):
        jobs.submit("test", None, lambda *_: None, allow_queue=True)
    assert not jobs._futures
    assert not jobs._cancel_events


def test_stop_all_api_protects_migration_and_rejects_later_submissions(tmp_path):
    store = VideoStore(tmp_path / "jobs.db")
    app = create_app(store)
    with TestClient(app) as client:
        store.create_job("migration", "storage_migration")
        assert client.post("/api/jobs/stop-all").status_code == 409
        assert not app.state.jobs._closing
        assert store.get_job("migration")["status"] == "queued"
        store.update_job("migration", status="completed")
        assert client.post("/api/jobs/stop-all").json() == {"status": "stopping"}
        with pytest.raises(RuntimeError, match="关闭"):
            app.state.jobs.submit("test", None, lambda *_: None)
