import json
import time
from threading import Event
from types import SimpleNamespace

import pytest

from classcue import slide_runtime as runtime
from classcue import slide_extractor as extractor
from classcue.processing_control import ProcessingCancelled
from tests.test_slide_extractor import make_slide_video, registered_video


def arguments(tmp_path, backend="auto", duration=3_600_000):
    return dict(source=tmp_path / "source.mp4", staging=tmp_path / "generation",
                source_hash="a" * 64, duration_ms=duration, backend=backend,
                roi=extractor.SlideRoi(0, 0, 320, 180), ignore_regions=(),
                progress=None, should_stop=None)


@pytest.fixture
def selection(monkeypatch):
    calls = []
    result = SimpleNamespace(backend="cuda")
    worker = SimpleNamespace(failed_until=0, extract=lambda *a, **kw: calls.append("cuda") or result)
    monkeypatch.setattr(runtime, "_worker", worker)
    monkeypatch.setattr(runtime, "acceleration_status", lambda: {"ready": True})
    monkeypatch.setattr(extractor, "extract_slides", lambda *a, **kw: calls.append("cpu") or result)
    return worker, calls, result


def test_long_auto_uses_prepared_cuda_but_short_and_explicit_cpu_do_not(tmp_path, selection):
    _, calls, result = selection
    assert runtime.extract_with_backend(**arguments(tmp_path)) is result
    runtime.extract_with_backend(**arguments(tmp_path, duration=10_000))
    runtime.extract_with_backend(**arguments(tmp_path, backend="cpu"))
    assert calls == ["cuda", "cpu", "cpu"]


def test_auto_preserves_cpu_without_preparing_or_downloading(tmp_path, selection, monkeypatch):
    _, calls, _ = selection
    monkeypatch.setattr(runtime, "acceleration_status", lambda: {"ready": False})
    runtime.extract_with_backend(**arguments(tmp_path))
    assert calls == ["cpu"]


def test_cuda_failure_falls_back_once_and_auto_observes_cooldown(tmp_path, selection):
    worker, calls, _ = selection
    def fail(*a, **kw):
        calls.append("cuda")
        raise runtime.SlideCudaError("synthetic CUDA failure")
    worker.extract = fail
    runtime.extract_with_backend(**arguments(tmp_path))
    runtime.extract_with_backend(**arguments(tmp_path))
    assert calls == ["cuda", "cpu", "cpu"]
    with pytest.raises(runtime.SlideCudaError):
        runtime.extract_with_backend(**arguments(tmp_path, backend="cuda"))


def test_cancellation_never_restarts_extraction_on_cpu(tmp_path, selection):
    worker, calls, _ = selection
    def cancel(*a, **kw):
        raise ProcessingCancelled("stopped")
    worker.extract = cancel
    with pytest.raises(ProcessingCancelled):
        runtime.extract_with_backend(**arguments(tmp_path))
    assert calls == []


@pytest.mark.parametrize("cpu_failure", [False, True])
def test_auto_fallback_removes_crashed_worker_files_and_preserves_previous_generation(
    tmp_path, monkeypatch, cpu_failure,
):
    source, store, video_id, root = registered_video(tmp_path)
    make_slide_video(source, [
        ((30, 40, 220), 45), ((40, 210, 50), 45), ((30, 40, 220), 45),
    ], rate=1)
    extractor.extract_and_store_slides(store, video_id, root, backend="cpu")
    previous = [dict(row) for row in store.list_slides(video_id, include_auxiliary=True)]
    original_files = {row["image_path"]: (root / row["image_path"]).read_bytes() for row in previous}
    failed_staging = []

    def fail(_source, staging, *_args, **_kwargs):
        failed_staging.append(staging)
        scratch = staging / "features-orphan"
        scratch.mkdir()
        (scratch / "stale.npz").write_bytes(b"unfinished features")
        (staging / "unfinished.tmp").write_bytes(b"unfinished image")
        (staging / "unused.jpg").write_bytes(b"unfinished screenshot")
        raise runtime.SlideCudaError("synthetic CUDA crash")

    monkeypatch.setattr(runtime, "_worker", SimpleNamespace(failed_until=0, extract=fail))
    monkeypatch.setattr(runtime, "acceleration_status", lambda: {"ready": True})
    if cpu_failure:
        def fail_cpu(*_args, **_kwargs):
            assert not list(failed_staging[0].iterdir())
            raise ValueError("synthetic CPU failure")
        monkeypatch.setattr(extractor, "extract_slides", fail_cpu)
        with pytest.raises(ValueError, match="synthetic CPU failure"):
            extractor.extract_and_store_slides(store, video_id, root, force=True)
        assert [dict(row) for row in store.list_slides(video_id, include_auxiliary=True)] == previous
        assert {path: (root / path).read_bytes() for path in original_files} == original_files
    else:
        result = extractor.extract_and_store_slides(store, video_id, root, force=True)
        assert result["backend"] == "cpu"
        assert result["count"] == len(previous)
        assert all((root / row["image_path"]).is_file() for row in result["slides"])
        assert not list(failed_staging[0].glob("features-*"))
        assert not list(failed_staging[0].glob("*.tmp"))
        assert not (failed_staging[0] / "unused.jpg").exists()


def test_invalid_ready_records_are_not_reported_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "runtime_directory", lambda: tmp_path)
    (tmp_path / "ready.json").write_text("[]")
    assert runtime.acceleration_status()["ready"] is False
    (tmp_path / "ready.json").write_text(json.dumps({"version": "old"}))
    assert runtime.acceleration_status()["ready"] is False


def test_prefetched_decode_preserves_all_time_and_image_results(tmp_path):
    source = tmp_path / "slides.mkv"
    make_slide_video(source, [((30, 40, 220), 5), ((40, 210, 50), 5), ((30, 40, 220), 5)])
    sequential = extractor.extract_slides(source, tmp_path / "sequential", "a" * 64)
    prefetched = extractor.extract_slides(source, tmp_path / "prefetched", "a" * 64, prefetch=True)
    def signature(result):
        return [(s.start_ms, s.end_ms, s.visible_from_ms, s.visible_until_ms,
                 s.representative_ms, s.image_hash, s.retention_class, s.is_llm_preferred)
                for s in result.slides]
    assert signature(prefetched) == signature(sequential)
    assert prefetched.duration_ms == sequential.duration_ms


def test_prefetched_decode_can_stop_without_publishing(tmp_path):
    source, store, video_id, root = registered_video(tmp_path)
    extractor.extract_and_store_slides(store, video_id, root, backend="cpu")
    old = [dict(row) for row in store.list_slides(video_id, include_auxiliary=True)]
    stopped = Event()
    with pytest.raises(ProcessingCancelled):
        extractor.extract_slides(source, tmp_path / "cancelled", "a" * 64, prefetch=True,
                                 progress=lambda *_: stopped.set(), should_stop=stopped.is_set)
    assert [dict(row) for row in store.list_slides(video_id, include_auxiliary=True)] == old


def test_gpu_failure_during_replacement_preserves_old_generation(tmp_path, monkeypatch):
    _, store, video_id, root = registered_video(tmp_path)
    extractor.extract_and_store_slides(store, video_id, root, backend="cpu")
    previous = [dict(row) for row in store.list_slides(video_id, include_auxiliary=True)]
    def fail(*a, **kw):
        raise runtime.SlideCudaError("synthetic GPU failure")
    monkeypatch.setattr(runtime, "extract_with_backend", fail)
    with pytest.raises(runtime.SlideCudaError):
        extractor.extract_and_store_slides(store, video_id, root, force=True, backend="cuda")
    assert [dict(row) for row in store.list_slides(video_id, include_auxiliary=True)] == previous
    assert all((root / row["image_path"]).is_file() for row in previous)


def test_cancelled_worker_is_closed_before_result_can_be_published(tmp_path, monkeypatch):
    worker = runtime.SlideWorker()
    process = SimpleNamespace(poll=lambda: None)
    closed = []
    worker.process = process
    worker.messages.put({"event": "result", "result": {}})
    monkeypatch.setattr(worker, "_start", lambda: None)
    monkeypatch.setattr(worker, "_send", lambda _: None)
    monkeypatch.setattr(worker, "_close_locked", lambda: closed.append(True) or setattr(worker, "process", None))
    values = arguments(tmp_path)
    with pytest.raises(ProcessingCancelled):
        worker.extract(values["source"], values["staging"], values["source_hash"],
                       roi=values["roi"], ignore_regions=(), progress=None, should_stop=lambda: True)
    assert closed == [True]


def test_stale_idle_timer_cannot_close_newer_worker(monkeypatch):
    worker = runtime.SlideWorker()
    closed = []
    worker.epoch = 2
    monkeypatch.setattr(worker, "_close_locked", lambda: closed.append(True))
    worker._expire(1)
    assert not closed
    worker._expire(2)
    assert closed


def test_exited_worker_is_reaped_before_starting_a_replacement(monkeypatch):
    worker = runtime.SlideWorker()
    worker.process = SimpleNamespace(poll=lambda: 1)
    closed = []
    monkeypatch.setattr(worker, "_close_locked", lambda: closed.append(True) or setattr(worker, "process", None))
    monkeypatch.setattr(runtime, "acceleration_status", lambda: {"ready": False})
    with pytest.raises(runtime.SlideCudaError, match="请先准备"):
        worker._start()
    assert closed == [True]


def test_long_page_offloads_features_and_preserves_every_timed_state(tmp_path):
    import cv2
    import numpy as np
    from classcue.slide_features import FeatureReference

    collector = extractor._PageCollector(tmp_path, extractor.SlideRoi(0, 0, 320, 180), ())
    try:
        for index in range(40):
            image = np.full((180, 320, 3), 245, np.uint8)
            cv2.putText(image, f"Formula {index}", (20, 90), cv2.FONT_HERSHEY_SIMPLEX, .7, (20, 20, 20), 2)
            collector.accept(index * 3000, (index + 1) * 3000, index * 3000 + 2500, image)
        assert collector.feature_store is not None
        assert len(collector.feature_store.recent) <= 16
        assert all(isinstance(item.feature, FeatureReference) for item in collector.candidates)
        collector.flush(120_000)
        assert len(collector.slides) == 40
        assert collector.slides[-1].end_ms == 120_000
    finally:
        collector.close()
    assert not list(tmp_path.glob("features-*"))


def test_scratch_feature_eviction_is_lossless(tmp_path):
    import numpy as np
    from classcue.slide_features import FeatureStore

    store = FeatureStore(tmp_path)
    values = []
    try:
        for index in range(20):
            feature = {
                "rgb": np.full((8, 8, 3), index, np.uint8),
                "gray": np.full((8, 8), index, np.uint8),
                "edges": np.full((8, 8), index % 2, bool),
                "valid": np.ones((8, 8), bool),
            }
            values.append((store.store(feature), feature))
        assert len(store.recent) == 16
        for reference, expected in values:
            for name in expected:
                np.testing.assert_array_equal(reference[name], expected[name])
    finally:
        store.close()


@pytest.mark.parametrize("event", [
    {"event": "result", "result": {}},
    {"event": "result", "result": None},
    {"event": "progress"},
])
def test_malformed_worker_results_close_worker_and_allow_auto_fallback(tmp_path, monkeypatch, event):
    worker = runtime.SlideWorker()
    worker.process = SimpleNamespace(poll=lambda: None)
    worker.messages.put(event)
    closed = []
    monkeypatch.setattr(worker, "_start", lambda: None)
    monkeypatch.setattr(worker, "_send", lambda _: None)
    monkeypatch.setattr(worker, "_close_locked", lambda: closed.append(True) or setattr(worker, "process", None))
    values = arguments(tmp_path)
    with pytest.raises(runtime.SlideCudaError):
        worker.extract(values["source"], values["staging"], values["source_hash"],
                       roi=values["roi"], ignore_regions=(), progress=lambda *_: None, should_stop=None)
    assert closed == [True]


def test_cancel_during_transport_failure_never_falls_back_to_cpu(tmp_path, monkeypatch):
    worker = runtime.SlideWorker()
    stopped = Event()
    monkeypatch.setattr(worker, "_start", lambda: None)
    def fail(_):
        stopped.set()
        raise BrokenPipeError("synthetic disconnect")
    monkeypatch.setattr(worker, "_send", fail)
    monkeypatch.setattr(runtime, "_worker", worker)
    monkeypatch.setattr(runtime, "acceleration_status", lambda: {"ready": True})
    values = arguments(tmp_path)
    values["should_stop"] = stopped.is_set
    with pytest.raises(ProcessingCancelled):
        runtime.extract_with_backend(**values)


@pytest.mark.parametrize("cancel_probe", [False, True])
def test_preparation_publishes_ready_only_after_successful_cancellable_probe(tmp_path, monkeypatch, cancel_probe):
    from classcue import preparation_progress

    directory = tmp_path / "slides-gpu"
    (directory / "Scripts").mkdir(parents=True)
    (directory / "Scripts" / "python.exe").touch()
    monkeypatch.setattr(runtime, "runtime_directory", lambda: directory)
    monkeypatch.setattr(runtime, "_environment", lambda: {})
    monkeypatch.setattr(runtime, "log_directory", lambda *_: tmp_path / "logs")
    monkeypatch.setattr(runtime, "close_workers", lambda: None)
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if "--probe" in command:
            if cancel_probe:
                raise ProcessingCancelled("probe stopped")
            kwargs["stdout"].write(runtime.PREFIX + json.dumps({"event": "ready", "gpu_name": "Synthetic GPU"}) + "\n")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(preparation_progress, "run_logged", run)
    if cancel_probe:
        with pytest.raises(ProcessingCancelled):
            runtime.install_runtime()
        assert not runtime.acceleration_status()["ready"]
    else:
        runtime.install_runtime()
        assert runtime.acceleration_status()["ready"]
        assert runtime.acceleration_status()["gpu_name"] == "Synthetic GPU"
    assert len(calls) == 2
    assert "--probe" in calls[-1]


def test_selection_stage_can_be_cancelled_without_publishing(tmp_path):
    source, store, video_id, root = registered_video(tmp_path)
    extractor.extract_and_store_slides(store, video_id, root, backend="cpu")
    before = [dict(row) for row in store.list_slides(video_id, include_auxiliary=True)]
    stopped = Event()
    def progress(_fraction, _message, stage=None):
        if stage == "selecting":
            stopped.set()
    with pytest.raises(ProcessingCancelled):
        extractor.extract_and_store_slides(
            store, video_id, root, force=True, backend="cpu",
            progress=progress, should_stop=stopped.is_set,
        )
    assert [dict(row) for row in store.list_slides(video_id, include_auxiliary=True)] == before
    assert all((root / row["image_path"]).is_file() for row in before)


def test_acceleration_api_and_explicit_backend_are_separate_jobs(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import api.main as api_main

    _, store, video_id, root = registered_video(tmp_path)
    monkeypatch.setattr(api_main, "DIRECTORY_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(api_main.asr_storage, "slide_directory", lambda *_: root)
    monkeypatch.setattr(runtime, "acceleration_status", lambda: {
        "ready": True, "gpu_name": "Synthetic GPU", "message": "ready"})
    prepared, selected = [], []
    monkeypatch.setattr(runtime, "install_runtime", lambda update: prepared.append(True))
    def extract(*args, **kwargs):
        selected.append(kwargs["backend"])
        return {"committed": True, "backend": kwargs["backend"]}
    monkeypatch.setattr(api_main, "extract_and_store_slides", extract)

    def wait(client, job_id):
        for _ in range(100):
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] in {"completed", "failed", "interrupted"}:
                assert job["status"] == "completed", job
                return
            time.sleep(.02)
        pytest.fail("synthetic PPT job did not finish")

    with TestClient(api_main.create_app(store)) as client:
        assert client.get("/api/slides/acceleration").json()["gpu_name"] == "Synthetic GPU"
        preparation = client.post("/api/slides/acceleration/prepare")
        assert preparation.status_code == 202
        wait(client, preparation.json()["job_id"])
        assert prepared == [True]
        assert not selected
        assert client.post(f"/api/videos/{video_id}/slides/extract", json={"backend": "invalid"}).status_code == 422
        response = client.post(f"/api/videos/{video_id}/slides/extract", json={"backend": "cuda"})
        assert response.status_code == 202
        wait(client, response.json()["job_id"])
        assert selected == ["cuda"]
