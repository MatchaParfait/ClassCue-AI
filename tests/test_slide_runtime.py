import json
from contextlib import contextmanager
from pathlib import Path
import time
from threading import Event
from types import SimpleNamespace

import pytest

from classcue import slide_runtime as runtime
from classcue import slide_extractor as extractor
from classcue.processing_control import ProcessingCancelled
from tests.test_slide_extractor import make_slide_video, registered_video


@pytest.fixture(autouse=True)
def isolate_device_state(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "selection_path", lambda *_: tmp_path / "slide_settings.json")
    monkeypatch.setattr(runtime, "_detect_gpu", lambda: None)
    runtime.invalidate_status()
    yield
    runtime.invalidate_status()


def arguments(tmp_path, backend="auto", duration=3_600_000):
    return dict(source=tmp_path / "source.mp4", staging=tmp_path / "generation",
                source_hash="a" * 64, duration_ms=duration, backend=backend,
                roi=extractor.SlideRoi(0, 0, 320, 180), ignore_regions=(),
                progress=None, should_stop=None)


@pytest.fixture
def resources(tmp_path, monkeypatch):
    root = tmp_path / "owned"
    gpu = root / "slides-gpu"
    (gpu / "Scripts").mkdir(parents=True)
    (gpu / "Scripts/python.exe").write_bytes(b"python")
    (gpu / "ready.json").write_text(json.dumps({"version": runtime.RUNTIME_VERSION, "gpu_name": "Synthetic GPU"}))
    (gpu / "torch.dll").write_bytes(b"synthetic GPU dependency")
    cache = root / "slides-cache"
    (cache / "uv").mkdir(parents=True)
    (cache / "uv/wheel").write_bytes(b"synthetic download")
    other = root / "dolphin-gpu"
    other.mkdir()
    (other / "keep.dll").write_bytes(b"ASR dependency")
    monkeypatch.setattr(runtime, "runtime_directory", lambda: gpu)
    monkeypatch.setattr(runtime, "close_workers", lambda: None)
    return SimpleNamespace(root=root, gpu=gpu, cache=cache, other=other)


def wait_slide_job(client, job_id):
    for _ in range(200):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in {"completed", "failed", "interrupted"}:
            return job
        time.sleep(.02)
    pytest.fail("synthetic PPT resource task did not finish")


def test_resource_size_skips_a_directory_removed_after_discovery(resources, monkeypatch):
    removed = resources.cache / "uv"
    survivor = resources.cache / "keep.bin"
    survivor.write_bytes(b"remaining cache")
    original = runtime.os.scandir

    def scan(path):
        if Path(path) == removed:
            (removed / "wheel").unlink()
            removed.rmdir()
        return original(path)

    monkeypatch.setattr(runtime.os, "scandir", scan)
    assert runtime.resource_size(resources.cache) == survivor.stat().st_size


def test_resource_size_skips_a_file_removed_before_stat(resources, monkeypatch):
    survivor = resources.cache / "keep.bin"
    survivor.write_bytes(b"remaining cache")
    removed = resources.cache / "uv/wheel"
    original = runtime.os.scandir

    @contextmanager
    def scan(path):
        with original(path) as entries:
            values = []
            for entry in entries:
                if Path(entry.path) == removed:
                    def missing(**_kwargs):
                        removed.unlink()
                        return removed.lstat()
                    values.append(SimpleNamespace(path=entry.path, stat=missing))
                else:
                    values.append(entry)
            yield values

    monkeypatch.setattr(runtime.os, "scandir", scan)
    assert runtime.resource_size(resources.cache) == survivor.stat().st_size


@pytest.mark.parametrize("scope", ["runtime", "downloads", "all"])
def test_resource_cleanup_is_scoped_and_preserves_asr_and_preferences(resources, scope):
    from classcue.cleanup_progress import cleanup_events

    runtime.select_backend("cpu")
    expected = sum(runtime.resource_size(path) for path in (
        [resources.gpu] if scope == "runtime" else [resources.cache] if scope == "downloads"
        else [resources.gpu, resources.cache]))
    events = []
    with cleanup_events(events.append):
        assert runtime.clear_resources(scope) == expected
    assert resources.gpu.exists() is (scope == "downloads")
    assert resources.cache.exists() is (scope == "runtime")
    assert (resources.other / "keep.dll").read_bytes() == b"ASR dependency"
    assert runtime.selected_backend() == "cpu"
    assert events[-1]["phase"] == "completed"
    assert events[-1]["bytes_deleted"] == expected


def test_selected_gpu_can_clear_downloads_but_not_uninstall_its_environment(resources):
    runtime.selection_path().write_text('{"backend":"cuda"}')
    with pytest.raises(ValueError, match="先将 PPT 默认设备切换"):
        runtime.clear_resources("all")
    assert (resources.gpu / "ready.json").is_file()
    runtime.clear_resources("downloads")
    assert (resources.gpu / "torch.dll").is_file()
    assert runtime.selected_backend() == "cuda"


def test_download_cleanup_removes_only_the_known_shared_wheel(resources):
    wheel = runtime.cached_wheel()
    wheel.parent.mkdir()
    wheel.write_bytes(b"known GPU download")
    other = wheel.with_name("unrelated-package.whl")
    other.write_bytes(b"keep other download")
    runtime.clear_resources("downloads")
    assert not wheel.exists()
    assert other.read_bytes() == b"keep other download"
    assert (resources.gpu / "torch.dll").is_file()
    assert (resources.other / "keep.dll").is_file()


def test_resource_cleanup_rejects_escaped_cache_before_deleting_any_files(resources, tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_bytes(b"keep")
    monkeypatch.setattr(runtime, "cache_directory", lambda: outside)
    with pytest.raises(ValueError, match="越出"):
        runtime.clear_resources("all")
    assert (resources.gpu / "ready.json").is_file()
    assert (outside / "keep").is_file()


def test_resource_cleanup_observes_installation_lock(resources):
    from filelock import FileLock, Timeout

    with FileLock(str(resources.root / "slides-gpu-setup.lock")):
        with pytest.raises(Timeout):
            runtime.clear_resources("all")
    assert (resources.gpu / "ready.json").is_file()
    assert (resources.cache / "uv/wheel").is_file()


def test_cancelled_gpu_removal_invalidates_ready_state_and_can_be_retried(resources, monkeypatch):
    from classcue import cleanup_progress
    from classcue.preparation_progress import preparation_events

    stopped = Event()
    original = cleanup_progress._remove_file
    def remove(path):
        original(path)
        stopped.set()
    monkeypatch.setattr(cleanup_progress, "_remove_file", remove)
    def checkpoint():
        if stopped.is_set():
            raise ProcessingCancelled("synthetic stop")
    with preparation_events(None, checkpoint=checkpoint), pytest.raises(ProcessingCancelled):
        runtime.clear_resources("runtime")
    assert not (resources.gpu / "ready.json").exists()
    assert not runtime.acceleration_status()["runtime_ready"]
    assert (resources.cache / "uv/wheel").is_file()
    stopped.clear()
    runtime.clear_resources("all")
    assert not resources.gpu.exists() and not resources.cache.exists()


def test_ppt_uses_private_caches_instead_of_shared_asr_downloads(resources, monkeypatch):
    monkeypatch.setenv("UV_CACHE_DIR", str(resources.other / "shared-uv"))
    monkeypatch.setenv("PIP_CACHE_DIR", str(resources.other / "shared-pip"))
    monkeypatch.setenv("CUDA_CACHE_PATH", str(resources.other / "shared-cuda"))
    env = runtime._environment()
    for name in ("UV_CACHE_DIR", "PIP_CACHE_DIR", "CUDA_CACHE_PATH", "TORCH_HOME", "TORCH_EXTENSIONS_DIR"):
        assert Path(env[name]).is_relative_to(resources.cache)
    assert not (resources.other / "shared-uv").exists()


def test_resource_api_reports_cached_sizes_and_refreshes_after_cleanup(resources, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import api.main as api_main

    _, store, _video_id, _root = registered_video(tmp_path)
    monkeypatch.setattr(api_main, "DIRECTORY_PROJECT_ROOT", tmp_path)
    with TestClient(api_main.create_app(store)) as client:
        first = client.get("/api/slides/resources").json()
        assert first["runtime_size_bytes"] == runtime.resource_size(resources.gpu)
        assert first["download_cache_size_bytes"] == runtime.resource_size(resources.cache)
        (resources.cache / "more").write_bytes(b"extra cache")
        assert client.get("/api/slides/resources").json()["download_cache_size_bytes"] == first["download_cache_size_bytes"]
        refreshed = client.get("/api/slides/resources?refresh=true").json()
        assert refreshed["download_cache_size_bytes"] == first["download_cache_size_bytes"] + 11
        cleanup = client.post("/api/slides/cache/cleanup", json={"scope": "all"})
        assert cleanup.status_code == 202
        job = wait_slide_job(client, cleanup.json()["job_id"])
        assert job["status"] == "completed", job
        assert job["cleanup"]["files_deleted"] > 0
        assert job["cleanup"]["bytes_deleted"] == refreshed["runtime_size_bytes"] + refreshed["download_cache_size_bytes"]
        assert client.get("/api/slides/resources").json()["runtime_size_bytes"] == 0
        assert client.get("/api/slides/resources").json()["download_cache_size_bytes"] == 0
        assert (resources.other / "keep.dll").is_file()
        assert client.post("/api/slides/cache/cleanup", json={"scope": "cpu"}).status_code == 422


def test_resource_api_persists_counts_when_stopped_after_unlink(resources, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import api.main as api_main
    from classcue import cleanup_progress

    _, store, _video_id, _root = registered_video(tmp_path)
    monkeypatch.setattr(api_main, "DIRECTORY_PROJECT_ROOT", tmp_path)
    deleted, resume = Event(), Event()
    original = cleanup_progress._remove_file
    file = resources.cache / "uv/wheel"
    size = file.stat().st_size

    def remove(path):
        original(path)
        deleted.set()
        if not resume.wait(5):
            raise RuntimeError("synthetic cleanup did not resume")

    monkeypatch.setattr(cleanup_progress, "_remove_file", remove)
    with TestClient(api_main.create_app(store)) as client:
        response = client.post("/api/slides/cache/cleanup", json={"scope": "downloads"})
        assert response.status_code == 202
        job_id = response.json()["job_id"]
        try:
            assert deleted.wait(5)
            assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 200
        finally:
            resume.set()
        job = wait_slide_job(client, job_id)
        assert job["status"] == "interrupted", job
        assert not file.exists()
        assert job["cleanup"]["files_deleted"] == 1
        assert job["cleanup"]["bytes_deleted"] == size
        assert client.get("/api/slides/resources").json()["download_cache_size_bytes"] == 0
        assert (resources.gpu / "ready.json").is_file()
        assert (resources.other / "keep.dll").is_file()


def test_resource_api_blocks_running_tasks_and_saved_gpu_removal(resources, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import api.main as api_main

    _, store, video_id, _root = registered_video(tmp_path)
    monkeypatch.setattr(api_main, "DIRECTORY_PROJECT_ROOT", tmp_path)
    application = api_main.create_app(store)
    release = Event()
    with TestClient(application) as client:
        runtime.selection_path().write_text('{"backend":"cuda"}')
        assert client.post("/api/slides/cache/cleanup", json={"scope": "all"}).status_code == 400
        runtime.select_backend("cpu")
        job_id = application.state.jobs.submit("slide_extract", video_id, lambda *_: release.wait(5))
        try:
            assert client.get("/api/slides/resources").json()["busy"]
            assert client.post("/api/slides/cache/cleanup", json={"scope": "downloads"}).status_code == 409
        finally:
            release.set()
        assert wait_slide_job(client, job_id)["status"] == "completed"
        assert (resources.gpu / "ready.json").is_file()


def test_idle_maintenance_cleans_old_installers_and_keeps_the_verified_environment(resources, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import api.main as api_main
    from api.cache_cleanup import router

    _, store, _video_id, _root = registered_video(tmp_path)
    monkeypatch.setattr(api_main, "DIRECTORY_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(runtime, "_detect_gpu", lambda: "Synthetic GPU")
    monkeypatch.setattr(runtime, "_probe_cuda", lambda: "Synthetic GPU")
    installed = resources.gpu / "installed.bin"
    installed.hardlink_to(resources.cache / "uv/wheel")
    compute = resources.cache / "cuda"
    compute.mkdir()
    (compute / "kernel.bin").write_bytes(b"keep compiled kernel")
    wheel = runtime.cached_wheel()
    wheel.parent.mkdir()
    wheel.write_bytes(b"old shared installer")
    other_wheel = wheel.with_name("other.whl")
    other_wheel.write_bytes(b"keep unrelated installer")
    refreshed = Event()
    app = api_main.create_app(store)
    with TestClient(app) as client:
        _, schedule = router(tmp_path, app.state.jobs.provider, app.state.jobs, on_slide_cleanup=refreshed.set)
        job_id = schedule(installers_only=True)
        assert job_id is not None
        job = wait_slide_job(client, job_id)
        assert job["status"] == "completed", job
        assert job["cleanup"]["bytes_deleted"] == len(b"synthetic download") + len(b"old shared installer")
        assert json.loads(store.get_job(job_id)["request_json"])["automatic"]
        assert refreshed.is_set()
        assert not (resources.cache / "uv").exists() and not wheel.exists()
        assert installed.read_bytes() == b"synthetic download"
        assert (compute / "kernel.bin").read_bytes() == b"keep compiled kernel"
        assert other_wheel.read_bytes() == b"keep unrelated installer"
        assert (resources.other / "keep.dll").is_file()
        assert runtime.acceleration_status(refresh=True)["ready"]
        assert not runtime.installer_cleanup_pending()
        assert schedule(installers_only=True) is None


@pytest.mark.parametrize("reason", ["unverified", "no_gpu", "running", "paused"])
def test_idle_installer_maintenance_preserves_downloads_until_safe(resources, tmp_path, monkeypatch, reason):
    from fastapi.testclient import TestClient
    import api.main as api_main
    from api.cache_cleanup import router

    _, store, video_id, _root = registered_video(tmp_path)
    monkeypatch.setattr(api_main, "DIRECTORY_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(runtime, "_detect_gpu", lambda: None if reason == "no_gpu" else "Synthetic GPU")
    monkeypatch.setattr(runtime, "_probe_cuda", lambda: "Synthetic GPU")
    if reason == "unverified":
        (resources.gpu / "ready.json").unlink()
    if reason == "paused":
        store.create_job("paused", "video_process", video_id)
        store.update_job("paused", status="resource_paused")
    release = Event()
    app = api_main.create_app(store)
    with TestClient(app):
        try:
            if reason == "running":
                app.state.jobs.submit("slide_extract", video_id, lambda *_: release.wait(5))
            _, schedule = router(tmp_path, app.state.jobs.provider, app.state.jobs)
            assert schedule(installers_only=True) is None
            assert (resources.cache / "uv/wheel").read_bytes() == b"synthetic download"
        finally:
            release.set()


def test_installer_cleanup_rechecks_ready_marker_and_setup_lock(resources):
    from filelock import FileLock, Timeout

    with FileLock(str(resources.root / "slides-gpu-setup.lock")), pytest.raises(Timeout):
        runtime.clear_installer_downloads()
    (resources.gpu / "ready.json").unlink()
    assert runtime.clear_installer_downloads() == 0
    assert (resources.cache / "uv/wheel").read_bytes() == b"synthetic download"


def test_due_cache_maintenance_runs_despite_repeated_installer_cleanup_failures(monkeypatch):
    from threading import RLock
    from unittest.mock import Mock
    import api.cache_cleanup as cleanup_api

    cache = Mock()
    cache.due.return_value = True
    cache.state.return_value = {"preferences": {"categories": ["logs"]}}
    monkeypatch.setattr(cleanup_api, "CacheCleanup", lambda *_args, **_kwargs: cache)
    monkeypatch.setattr(runtime, "installer_cleanup_pending", lambda: True)
    monkeypatch.setattr(runtime, "acceleration_status", lambda: {"ready": True})
    attempts = []

    def blocked():
        attempts.append(True)
        raise PermissionError("synthetic installer in use")

    monkeypatch.setattr(runtime, "clear_installer_downloads", blocked)
    provider = Mock()
    provider.get.return_value.list_paused_jobs.return_value = []
    jobs = Mock(_lock=RLock())
    jobs.is_running.return_value = False

    def submit(kind, _video, operation, **_kwargs):
        try:
            operation(lambda *_: None, lambda: False)
        except PermissionError:
            pass
        return kind

    jobs.submit.side_effect = submit
    _, schedule = cleanup_api.router(Path("application"), provider, jobs)
    for _ in range(3):
        assert schedule(installers_only=True) == "slide_cache_cleanup"
        assert schedule() == "cache_cleanup"
    assert len(attempts) == 3
    assert cache.clean.call_count == 3
    assert cache.clean.call_args.kwargs["automatic"] is True


@pytest.fixture
def selection(monkeypatch):
    calls = []
    result = SimpleNamespace(backend="cuda")
    worker = SimpleNamespace(failed_until=0, extract=lambda *a, **kw: calls.append("cuda") or result)
    monkeypatch.setattr(runtime, "_worker", worker)
    monkeypatch.setattr(runtime, "acceleration_status", lambda **_: {"ready": True})
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
    monkeypatch.setattr(runtime, "acceleration_status", lambda **_: {"ready": False})
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
    monkeypatch.setattr(runtime, "acceleration_status", lambda **_: {"ready": True})
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


@pytest.mark.parametrize("saved", ["[]", "null", '{"backend":"unknown"}', "{broken"])
def test_invalid_saved_selection_uses_auto_without_changing_files(tmp_path, saved):
    path = runtime.selection_path()
    path.write_text(saved, encoding="utf-8")
    assert runtime.selected_backend() == "auto"
    assert path.read_text(encoding="utf-8") == saved


def test_selection_persists_without_installing_dependencies(monkeypatch):
    monkeypatch.setattr(runtime, "install_runtime", lambda *_: pytest.fail("selection must not install"))
    runtime.select_backend("cpu")
    assert json.loads(runtime.selection_path().read_text()) == {"backend": "cpu"}
    assert runtime.selected_backend() == "cpu"
    runtime.select_backend("auto")
    assert runtime.selected_backend() == "auto"
    with pytest.raises(ValueError):
        runtime.select_backend("unknown")
    assert runtime.selected_backend() == "auto"


def test_prepared_files_cannot_enable_gpu_without_hardware(tmp_path, monkeypatch):
    (tmp_path / "Scripts").mkdir()
    (tmp_path / "Scripts/python.exe").touch()
    (tmp_path / "ready.json").write_text(json.dumps({
        "version": runtime.RUNTIME_VERSION, "gpu_name": "Old GPU",
    }))
    monkeypatch.setattr(runtime, "runtime_directory", lambda: tmp_path)
    monkeypatch.setattr(runtime, "_probe_cuda", lambda: pytest.fail("no hardware to probe"))
    status = runtime.acceleration_status()
    assert status["runtime_ready"]
    assert not status["ready"]
    assert not status["gpu_prepare_available"]
    runtime.select_backend("cpu")
    with pytest.raises(runtime.SlideCudaError, match="NVIDIA"):
        runtime.select_backend("cuda")
    assert runtime.selected_backend() == "cpu"
    with pytest.raises(ValueError, match="NVIDIA"):
        runtime.install_runtime()
    assert (tmp_path / "ready.json").is_file()


def test_gpu_probe_is_cached_and_explicit_refresh_rechecks_hardware(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "_runtime_record", lambda: (True, {"gpu_name": "Synthetic GPU"}))
    monkeypatch.setattr(runtime, "_detect_gpu", lambda: "Synthetic GPU")
    probes = []
    monkeypatch.setattr(runtime, "_probe_cuda", lambda: probes.append(True) or "Synthetic GPU")
    assert runtime.acceleration_status()["ready"]
    assert runtime.acceleration_status()["ready"]
    assert len(probes) == 1
    monkeypatch.setattr(runtime, "_detect_gpu", lambda: None)
    assert not runtime.acceleration_status(refresh=True)["ready"]
    assert len(probes) == 1


def test_failed_actual_cuda_probe_locks_gpu_selection(monkeypatch):
    monkeypatch.setattr(runtime, "_runtime_record", lambda: (True, {}))
    monkeypatch.setattr(runtime, "_detect_gpu", lambda: "Synthetic GPU")
    def fail():
        raise ValueError("incompatible CUDA")
    monkeypatch.setattr(runtime, "_probe_cuda", fail)
    status = runtime.acceleration_status()
    assert status["runtime_ready"]
    assert status["gpu_prepare_available"]
    assert not status["ready"]
    with pytest.raises(runtime.SlideCudaError, match="验证失败"):
        runtime.select_backend("cuda")


def test_extract_defaults_to_persisted_cpu(tmp_path, monkeypatch):
    _, store, video_id, root = registered_video(tmp_path)
    runtime.select_backend("cpu")
    calls = []
    original = runtime.extract_with_backend
    def extract(*args, **kwargs):
        calls.append(kwargs["backend"])
        return original(*args, **kwargs)
    monkeypatch.setattr(runtime, "extract_with_backend", extract)
    extractor.extract_and_store_slides(store, video_id, root)
    assert calls == ["cpu"]


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
    monkeypatch.setattr(runtime, "acceleration_status", lambda **_: {"ready": False})
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
    monkeypatch.setattr(runtime, "acceleration_status", lambda **_: {"ready": True})
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
    monkeypatch.setattr(runtime, "_detect_gpu", lambda: "Synthetic GPU")
    monkeypatch.setattr(runtime, "_probe_cuda", lambda: "Synthetic GPU")
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


def test_preparation_finds_uv_beside_the_app_python_without_activation(tmp_path, monkeypatch):
    from classcue import preparation_progress

    directory = tmp_path / "slides-gpu"
    (directory / "Scripts").mkdir(parents=True)
    (directory / "Scripts/python.exe").touch()
    host = tmp_path / "host"
    host.mkdir()
    (host / "uv.exe").touch()
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path / "application")
    monkeypatch.setattr(runtime.sys, "executable", str(host / "python.exe"))
    monkeypatch.setattr(runtime, "runtime_directory", lambda: directory)
    monkeypatch.setattr(runtime, "_environment", lambda: {})
    monkeypatch.setattr(runtime, "_detect_gpu", lambda: "Synthetic GPU")
    monkeypatch.setattr(runtime, "log_directory", lambda *_: tmp_path / "logs")
    monkeypatch.setattr(runtime, "close_workers", lambda: None)
    monkeypatch.setattr(runtime.shutil, "which", lambda *_args, **_kwargs: None)
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if "--probe" in command:
            kwargs["stdout"].write(runtime.PREFIX + json.dumps({"event": "ready", "gpu_name": "Synthetic GPU"}))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(preparation_progress, "run_logged", run)
    runtime.install_runtime()
    assert calls[0][0] == str(host / "uv.exe")
    assert calls[0][1:3] == ["pip", "install"]


@pytest.mark.parametrize("outcome", ["success", "install_failed", "probe_failed", "cancelled", "cleanup_failed"])
def test_preparation_cleans_installer_files_only_after_verification(resources, monkeypatch, outcome):
    from classcue import cleanup_progress, preparation_progress

    cache_file = resources.cache / "uv/wheel"
    installed = resources.gpu / "installed.bin"
    installed.hardlink_to(cache_file)
    pip_cache = resources.cache / "pip"
    pip_cache.mkdir()
    (pip_cache / "download.bin").write_bytes(b"installer download")
    compute_cache = resources.cache / "cuda"
    compute_cache.mkdir()
    (compute_cache / "kernel.bin").write_bytes(b"compiled kernel")
    wheel = runtime.cached_wheel()
    wheel.parent.mkdir()
    wheel.write_bytes(b"known installer")
    other_wheel = wheel.with_name("other-package.whl")
    other_wheel.write_bytes(b"keep other installer")
    monkeypatch.setattr(runtime, "_environment", lambda: {})
    monkeypatch.setattr(runtime, "log_directory", lambda *_: resources.root / "logs")
    monkeypatch.setattr(runtime, "_detect_gpu", lambda: "Synthetic GPU")
    monkeypatch.setattr(runtime, "_probe_cuda", lambda: "Synthetic GPU")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if "--probe" in command:
            assert cache_file.exists() and wheel.exists(), "Keep downloads until verification succeeds"
            if outcome == "cancelled":
                raise ProcessingCancelled("probe stopped")
            kwargs["stdout"].write(runtime.PREFIX + json.dumps({"event": "ready", "gpu_name": "Synthetic GPU"}))
            return SimpleNamespace(returncode=1 if outcome == "probe_failed" else 0)
        return SimpleNamespace(returncode=1 if outcome == "install_failed" else 0)

    monkeypatch.setattr(preparation_progress, "run_logged", run)
    if outcome == "cleanup_failed":
        def blocked(_path):
            raise PermissionError("synthetic installer in use")
        monkeypatch.setattr(cleanup_progress, "_remove_file", blocked)
    if outcome in {"install_failed", "probe_failed", "cancelled"}:
        with pytest.raises(ProcessingCancelled if outcome == "cancelled" else RuntimeError):
            runtime.install_runtime()
        assert not runtime.acceleration_status()["ready"]
    else:
        message = runtime.install_runtime()
        assert runtime.acceleration_status()["ready"]
        assert ("未能清理" in message) is (outcome == "cleanup_failed")
    cleaned = outcome == "success"
    assert cache_file.exists() is not cleaned
    assert pip_cache.exists() is not cleaned
    assert wheel.exists() is not cleaned
    assert installed.read_bytes() == b"synthetic download"
    assert (compute_cache / "kernel.bin").read_bytes() == b"compiled kernel"
    assert other_wheel.read_bytes() == b"keep other installer"
    assert (resources.other / "keep.dll").read_bytes() == b"ASR dependency"
    assert str(wheel) in calls[0]


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


@pytest.mark.parametrize("preparation_message", [None, "PPT GPU 环境已准备，部分安装包未能清理。"])
def test_acceleration_api_and_explicit_backend_are_separate_jobs(tmp_path, monkeypatch, preparation_message):
    from fastapi.testclient import TestClient
    import api.main as api_main

    _, store, video_id, root = registered_video(tmp_path)
    monkeypatch.setattr(api_main, "DIRECTORY_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(api_main.asr_storage, "slide_directory", lambda *_: root)
    monkeypatch.setattr(runtime, "acceleration_status", lambda **_: {
        "ready": True, "gpu_name": "Synthetic GPU", "message": "ready"})
    monkeypatch.setattr(runtime, "_detect_gpu", lambda: "Synthetic GPU")
    prepared, selected = [], []
    monkeypatch.setattr(runtime, "install_runtime", lambda update: prepared.append(True) or preparation_message)
    def extract(*args, **kwargs):
        selected.append(kwargs["backend"])
        return {"committed": True, "backend": kwargs["backend"]}
    monkeypatch.setattr(api_main, "extract_and_store_slides", extract)

    def wait(client, job_id):
        for _ in range(100):
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] in {"completed", "failed", "interrupted"}:
                assert job["status"] == "completed", job
                return job
            time.sleep(.02)
        pytest.fail("synthetic PPT job did not finish")

    with TestClient(api_main.create_app(store)) as client:
        assert client.get("/api/slides/acceleration").json()["gpu_name"] == "Synthetic GPU"
        preparation = client.post("/api/slides/acceleration/prepare")
        assert preparation.status_code == 202
        prepared_job = wait(client, preparation.json()["job_id"])
        assert prepared_job["message"] == (preparation_message or "PPT GPU 环境已准备")
        assert prepared == [True]
        assert not selected
        assert client.post(f"/api/videos/{video_id}/slides/extract", json={"backend": "invalid"}).status_code == 422
        response = client.post(f"/api/videos/{video_id}/slides/extract", json={"backend": "cuda"})
        assert response.status_code == 202
        wait(client, response.json()["job_id"])
        assert selected == ["cuda"]


def test_selection_api_survives_app_recreation_and_blocks_unavailable_gpu(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import api.main as api_main

    _, store, video_id, root = registered_video(tmp_path)
    monkeypatch.setattr(api_main, "DIRECTORY_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(api_main.asr_storage, "slide_directory", lambda *_: root)
    selected = []
    def extract(*args, **kwargs):
        selected.append(kwargs["backend"])
        return {"committed": True}
    monkeypatch.setattr(api_main, "extract_and_store_slides", extract)
    with TestClient(api_main.create_app(store)) as client:
        assert client.get("/api/slides/selection").json() == {"backend": "auto"}
        assert client.put("/api/slides/selection", json={"backend": "cpu"}).json() == {"backend": "cpu"}
        assert client.put("/api/slides/selection", json={"backend": "invalid"}).status_code == 422
        assert client.put("/api/slides/selection", json={"backend": "cuda"}).status_code == 400
        assert client.post("/api/slides/acceleration/prepare").status_code == 400
        assert client.post(f"/api/videos/{video_id}/slides/extract",
                           json={"backend": "cuda"}).status_code == 400
    application = api_main.create_app(store)
    with TestClient(application) as client:
        assert client.get("/api/slides/selection").json() == {"backend": "cpu"}
        response = client.post(f"/api/videos/{video_id}/slides/extract", json={})
        assert response.status_code == 202
        for _ in range(100):
            job = client.get(f"/api/jobs/{response.json()['job_id']}").json()
            if job["status"] in {"completed", "failed"}:
                assert job["status"] == "completed", job
                break
            time.sleep(.02)
        assert selected == ["cpu"]
        application.state.course_batch_tools["slides"](video_id, lambda *_: None, lambda: False)
        assert selected == ["cpu", "cpu"]
        response = client.post(f"/api/videos/{video_id}/slides/extract", json={"backend": "auto"})
        assert response.status_code == 202
        for _ in range(100):
            job = client.get(f"/api/jobs/{response.json()['job_id']}").json()
            if job["status"] in {"completed", "failed"}:
                assert job["status"] == "completed", job
                break
            time.sleep(.02)
        assert selected == ["cpu", "cpu", "auto"]
        assert client.get("/api/slides/selection").json() == {"backend": "cpu"}
