from concurrent.futures import ThreadPoolExecutor
import json
from threading import Event

from fastapi.testclient import TestClient
import pytest

import api.main as api_main
import asr_models
import asr_status_cache
from asr_status_cache import AsrStatusCache
from video_store import VideoStore


def test_directory_sizes_survive_expired_status_and_restart(tmp_path, monkeypatch):
    clock = [1000.]
    monkeypatch.setattr(asr_status_cache, "time", lambda: clock[0])
    path = tmp_path / "cache.json"
    cache = AsrStatusCache(path)
    scans = []

    def inspect():
        return {"cache_size_bytes": cache.size("model|model-size", lambda: scans.append(1) or 123)}

    assert cache.inspect("model|cpu", inspect, full=True)["cache_size_bytes"] == 123
    clock[0] += 301
    cache.inspect("model|cpu", inspect, full=True)
    assert scans == [1]
    restored = AsrStatusCache(path)
    assert restored.peek("model|cpu")["data"]["cache_size_bytes"] == 123
    assert restored.size("model|model-size", lambda: pytest.fail("Persisted size should be reused")) == 123
    restored.clear_sizes("model|")
    assert restored.size("model|model-size", lambda: 456) == 456


@pytest.mark.parametrize("refresh", [False, True])
def test_concurrent_status_checks_share_one_inspection(refresh):
    cache = AsrStatusCache()
    entered, release = Event(), Event()
    calls = []

    def inspect():
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return {"downloaded": True}

    with ThreadPoolExecutor(max_workers=3) as executor:
        first = executor.submit(cache.inspect, "model", inspect, full=True, refresh=refresh)
        assert entered.wait(5)
        second_started = Event()

        def second():
            second_started.set()
            return cache.inspect("model", inspect, full=True, refresh=refresh)

        other = executor.submit(second)
        assert second_started.wait(5)
        # The first callback remains in flight while the second request arrives.
        import time
        time.sleep(.03)
        release.set()
        assert first.result() == other.result() == {"downloaded": True}
    assert calls == [1]


def test_failed_refresh_preserves_previous_observation():
    cache = AsrStatusCache()
    cache.inspect("model", lambda: {"downloaded": True}, full=True)
    with pytest.raises(OSError):
        cache.inspect("model", lambda: (_ for _ in ()).throw(OSError("missing disk")), full=True, refresh=True)
    assert cache.peek("model")["data"]["downloaded"] is True


def test_invalidation_prevents_inflight_checks_from_republishing_old_status():
    cache = AsrStatusCache()
    entered, release = Event(), Event()

    def inspect():
        entered.set()
        assert release.wait(5)
        return {"downloaded": True}

    with ThreadPoolExecutor() as executor:
        future = executor.submit(cache.inspect, "model|cpu", inspect, full=True)
        assert entered.wait(5)
        cache.invalidate("model|")
        release.set()
        future.result()
    assert cache.peek("model|cpu") is None


def test_basic_observation_preserves_full_only_fields():
    cache = AsrStatusCache()
    cache.inspect("model", lambda: {"downloaded": True, "cache_size_bytes": 100}, full=True)
    cache.inspect("model", lambda: {"downloaded": False}, full=False)
    assert cache.peek("model")["data"] == {"downloaded": False, "cache_size_bytes": 100}


@pytest.fixture
def status_api(tmp_path, monkeypatch):
    monkeypatch.setattr(asr_models, "SETTINGS_PATH", tmp_path / "selection.json")
    model_root = tmp_path / "assets/models/asr"
    monkeypatch.setattr(api_main, "MODEL_ROOT", model_root)
    monkeypatch.setattr(asr_models, "MODEL_ROOT", model_root)
    asr_models.select_model("whisper-turbo")
    calls = []

    def record(name, result):
        def run(*args, **kwargs):
            calls.append((name, args, kwargs))
            return result
        return run

    monkeypatch.setattr(api_main, "model_is_downloaded", record("files", True))
    monkeypatch.setattr(api_main, "cache_size", record("model-size", 123))
    for name in ("runtime_is_ready", "firered_runtime_is_ready", "paraformer_runtime_is_ready",
                 "nano_runtime_is_ready", "dolphin_runtime_is_ready"):
        monkeypatch.setattr(api_main, name, record(name, True))
    for name in ("runtime_size", "firered_runtime_size", "paraformer_runtime_size",
                 "nano_runtime_size", "dolphin_runtime_size"):
        monkeypatch.setattr(api_main, name, record(name, 456))
    for name in ("paraformer_cuda_status", "nano_cuda_status", "dolphin_cuda_status"):
        monkeypatch.setattr(api_main, name, record(name, {"gpu_available": True}))
    monkeypatch.setattr(api_main, "inspect_cuda_device", record("gpu-device", {
        "gpu_available": True, "gpu_name": "Runtime GPU", "gpu_runtime_ready": True,
        "gpu_unavailable_reason": None,
    }))
    application = api_main.create_app(VideoStore(tmp_path / "project.db"))
    with TestClient(application) as client:
        yield client, calls, application, model_root


def test_catalog_and_selection_return_unknown_immediately_without_inspection(status_api):
    client, calls, _, _ = status_api
    models = client.get("/api/asr/models").json()
    assert len(models) == len(asr_models.MODELS)
    assert all(model["downloaded"] is None and model["inspection_status"] == "unknown" for model in models)
    assert client.get("/api/asr/selection").json()["id"] == "whisper-turbo"
    assert not calls


def test_basic_check_only_inspects_requested_model_without_sizes_or_cuda(status_api):
    client, calls, _, _ = status_api
    result = client.get("/api/asr/models/dolphin-small-cn/status?full=false").json()
    assert result["downloaded"] is True
    assert result["dolphin_runtime_ready"] is True
    assert result["cache_size_bytes"] is None
    assert [name for name, _, _ in calls] == ["files", "dolphin_runtime_is_ready"]
    assert calls[0][1] == ("dolphin-small-cn",)
    client.get("/api/asr/models/dolphin-small-cn/status?full=false")
    assert len(calls) == 2


@pytest.mark.parametrize("model_id", ["qwen3-asr-1.7b", "fireredasr2-aed"])
def test_gpu_only_models_return_and_cache_their_runtime_device_identity(status_api, model_id):
    client, calls, _, _ = status_api
    path = f"/api/asr/models/{model_id}/status"
    client.get(path + "?full=false")
    assert not any(name == "gpu-device" for name, _, _ in calls)
    result = client.get(path).json()
    assert result["gpu_name"] == "Runtime GPU"
    assert result["gpu_available"] is True
    probe = next(args for name, args, _ in calls if name == "gpu-device")
    assert ("qwen-asr" if model_id.startswith("qwen") else "firered-asr2") in str(probe[0])
    assert probe[1] is True
    count = len(calls)
    assert client.get(path).json()["gpu_name"] == "Runtime GPU"
    listed = next(model for model in client.get("/api/asr/models").json() if model["id"] == model_id)
    assert listed["gpu_name"] == "Runtime GPU"
    assert len(calls) == count
    client.get(path + "?refresh=true")
    assert [name for name, _, _ in calls].count("gpu-device") == 2


def test_full_checks_are_cached_and_manual_refresh_recomputes_sizes(status_api):
    client, calls, _, _ = status_api
    path = "/api/asr/models/dolphin-small-cn/status"
    first = client.get(path).json()
    assert first["runtime_size_bytes"] == 456
    assert first["gpu_available"] is True
    count = len(calls)
    client.get(path)
    assert len(calls) == count
    listed = client.get("/api/asr/models").json()
    saved = next(model for model in listed if model["id"] == "dolphin-small-cn")
    assert saved["downloaded"] is True
    assert saved["inspection_status"] == "cached"
    assert len(calls) == count
    client.get(path + "?refresh=true")
    assert [name for name, _, _ in calls].count("dolphin_runtime_size") == 2
    assert ("dolphin_cuda_status", (), {"refresh": True}) in calls


def test_manifest_change_invalidates_snapshot_without_scanning_other_models(status_api):
    client, calls, _, model_root = status_api
    path = "/api/asr/models/whisper-turbo/status"
    client.get(path)
    directory = model_root / "whisper-turbo"
    directory.mkdir(parents=True)
    (directory / "ready.json").write_text(json.dumps({"updated": True}))
    assert client.get("/api/asr/selection").json()["downloaded"] is None
    calls.clear()
    client.get(path)
    assert [name for name, _, _ in calls] == ["files", "model-size"]


def test_failed_api_refresh_keeps_previous_catalog_values(status_api, monkeypatch):
    client, _, _, _ = status_api
    path = "/api/asr/models/whisper-turbo/status"
    assert client.get(path).json()["downloaded"] is True
    monkeypatch.setattr(api_main, "model_is_downloaded", lambda _: (_ for _ in ()).throw(OSError("offline")))
    assert client.get(path + "?refresh=true").status_code == 503
    assert client.get("/api/asr/selection").json()["downloaded"] is True
