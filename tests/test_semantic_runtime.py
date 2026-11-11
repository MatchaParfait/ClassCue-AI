"""Synthetic device setup checks; no model downloads or GPU inference."""

import json
from contextlib import contextmanager
import os
import subprocess

import httpx
import pytest

from classcue import semantic_embeddings as embeddings
from classcue.processing_control import ProcessingCancelled


@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(embeddings, "active_storage_root", lambda _: tmp_path)
    monkeypatch.setattr(embeddings, "log_directory", lambda _: tmp_path / "logs")
    monkeypatch.setattr(embeddings, "_gpu_detection", lambda: True)
    monkeypatch.setattr(embeddings, "close_worker", lambda: None)
    return tmp_path


def runtime(storage, device, *, legacy=False, variant=True):
    directory = storage / ".runtime" / ("semantic-search" if legacy else f"semantic-search-{device}")
    (directory / "Scripts").mkdir(parents=True)
    (directory / "Scripts" / "python.exe").touch()
    marker = {"version": embeddings.RUNTIME_VERSION, "device": device}
    if variant:
        marker["torch_variant"] = device
    (directory / "ready.json").write_text(json.dumps(marker), encoding="utf-8")
    return directory


def weights(storage):
    directory = embeddings.model_directory()
    directory.mkdir(parents=True)
    (directory / "model.safetensors").write_bytes(b"synthetic")
    (directory / "ready.json").write_text(json.dumps({
        "fingerprint": "synthetic-v1", "files": {"model.safetensors": 9},
    }), encoding="utf-8")


def test_new_users_default_cpu_and_status_does_not_load_torch(storage, monkeypatch):
    monkeypatch.setattr(embeddings, "_gpu_detection", lambda: False)
    state = embeddings.model_status()
    assert state["device"] == "cpu"
    assert state["runtimes"] == {"cpu": {"ready": False}, "cuda": {"ready": False}}
    assert not state["ready"] and not state["weights_ready"] and not state["gpu_detected"]
    assert state["gpu_unavailable_reason"]


def test_new_users_with_gpu_default_cuda_but_explicit_cpu_choice_is_preserved(storage):
    assert embeddings.selected_device() == "cuda"
    embeddings._selection_path().parent.mkdir(parents=True)
    embeddings._selection_path().write_text('{"device":"cpu"}', encoding="utf-8")
    assert embeddings.selected_device() == "cpu"


def test_default_preparation_uses_detected_gpu(storage, monkeypatch):
    calls = []
    monkeypatch.setattr(embeddings, "_prepare_environment",
                        lambda device, directory, *_: (calls.append(device), directory.mkdir(parents=True)))
    monkeypatch.setattr(embeddings, "_prepare_weights", lambda *_: None)
    monkeypatch.setattr(embeddings, "_validate_runtime", lambda *_: None)
    assert embeddings.prepare_model()["device"] == "cuda"
    assert calls == ["cuda"]


def test_download_estimates_account_for_shared_weights_prepared_runtime_and_local_wheel(storage):
    state = embeddings.model_status()
    expected = embeddings.WEIGHTS_DOWNLOAD_BYTES
    assert not state["cache_present"]
    assert state["download_estimates"]["weights_bytes"] == expected
    assert state["download_estimates"]["total_bytes"]["cuda"] > 4_000_000_000
    weights(storage)
    runtime(storage, "cuda")
    state = embeddings.model_status()
    assert state["cache_present"] and state["download_estimates"]["weights_bytes"] == 0
    assert state["download_estimates"]["total_bytes"]["cuda"] == 0
    assert state["download_estimates"]["runtimes"]["cpu"] > 200_000_000
    wheel = storage / ".runtime/wheels/torch-2.7.1+cpu-cp312-cp312-win_amd64.whl"
    wheel.parent.mkdir(parents=True)
    wheel.touch()
    assert embeddings.model_status()["download_estimates"]["runtimes"]["cpu"] == \
        embeddings.DEPENDENCIES_DOWNLOAD_BYTES


def test_legacy_gpu_is_preserved_and_cpu_uses_its_own_environment(storage):
    legacy = runtime(storage, "cuda", legacy=True, variant=False)
    weights(storage)
    assert embeddings.selected_device() == "cuda"
    assert embeddings.runtime_directory("cuda") == legacy
    assert embeddings.runtime_directory("cpu") == storage / ".runtime" / "semantic-search-cpu"
    assert embeddings.model_status()["ready"]
    assert not embeddings.model_status()["runtimes"]["cpu"]["ready"]


def test_cpu_marker_without_verified_torch_variant_is_not_reused(storage):
    runtime(storage, "cpu", legacy=True, variant=False)
    weights(storage)
    assert not embeddings.model_status()["ready"]
    assert embeddings.runtime_directory("cpu").name == "semantic-search-cpu"


def test_ready_device_switches_offline_after_actual_validation(storage, monkeypatch):
    runtime(storage, "cuda", legacy=True, variant=False)
    runtime(storage, "cpu")
    weights(storage)
    calls = []
    monkeypatch.setattr(embeddings, "_prepare_environment", lambda *args: pytest.fail("must reuse runtime"))
    monkeypatch.setattr(embeddings, "_validate_runtime", lambda device, _: calls.append(device))
    monkeypatch.setattr("httpx.get", lambda *args, **kwargs: pytest.fail("offline switch must not fetch metadata"))
    assert embeddings.prepare_model("cpu")["reused"]
    assert embeddings.selected_device() == "cpu"
    assert embeddings.prepare_model("cuda")["reused"]
    assert calls == ["cpu", "cuda"] and embeddings.selected_device() == "cuda"


@pytest.mark.parametrize("error", [RuntimeError("validation failed"), ProcessingCancelled("cancelled")])
def test_failed_or_cancelled_switch_keeps_previous_device(storage, monkeypatch, error):
    legacy = runtime(storage, "cuda", legacy=True, variant=False)
    weights(storage)
    legacy_marker = (legacy / "ready.json").read_bytes()
    monkeypatch.setattr(embeddings, "_prepare_environment",
                        lambda device, directory, *_: directory.mkdir(parents=True))
    def fail(*args):
        raise error
    monkeypatch.setattr(embeddings, "_validate_runtime", fail)
    with pytest.raises(type(error)):
        embeddings.prepare_model("cpu")
    assert embeddings.selected_device() == "cuda"
    assert not embeddings._selection_path().exists()
    assert not (embeddings.runtime_directory("cpu") / "ready.json").exists()
    assert (legacy / "ready.json").read_bytes() == legacy_marker
    assert embeddings.model_status()["ready"]


def test_completed_cpu_setup_is_activated_only_after_validation(storage, monkeypatch):
    runtime(storage, "cuda", legacy=True, variant=False)
    weights(storage)
    monkeypatch.setattr(embeddings, "_prepare_environment",
                        lambda device, directory, *_: directory.mkdir(parents=True))
    def validate(device, _):
        assert device == "cpu"
        assert embeddings.selected_device() == "cuda"
        assert not (embeddings.runtime_directory("cpu") / "ready.json").exists()
    monkeypatch.setattr(embeddings, "_validate_runtime", validate)
    embeddings.prepare_model("cpu")
    assert embeddings.selected_device() == "cpu"
    marker = json.loads((embeddings.runtime_directory("cpu") / "ready.json").read_text())
    assert marker["torch_variant"] == "cpu"


@pytest.mark.parametrize("device,variant", [("cpu", "cpu"), ("cuda", "cu128")])
def test_installer_pins_torch_variant_and_falls_back_from_mirror(storage, monkeypatch, device, variant):
    commands = []
    monkeypatch.setattr(embeddings, "_uv_prefix", lambda: ["uv"])
    def run(command, **kwargs):
        commands.append((command, kwargs["env"]))
        code = 1 if "https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple" in command else 0
        return subprocess.CompletedProcess(command, code)
    monkeypatch.setattr("classcue.preparation_progress.run_logged", run)
    embeddings._prepare_environment(device, embeddings.runtime_directory(device), None, None)
    assert any(f"torch==2.7.1+{variant}" in command for command, _ in commands)
    assert commands[-1][0][-1] == "https://pypi.org/simple"
    assert all("--trusted-host" not in command for command, _ in commands)
    if device == "cpu":
        assert all(env["CUDA_VISIBLE_DEVICES"] == "" for _, env in commands)


def test_gpu_without_driver_is_rejected_before_install(storage, monkeypatch):
    monkeypatch.setattr(embeddings, "_gpu_detection", lambda: False)
    monkeypatch.setattr(embeddings, "_prepare_environment", lambda *args: pytest.fail("unsupported GPU"))
    with pytest.raises(RuntimeError, match="请选择 CPU"):
        embeddings.prepare_model("cuda")


def test_cached_cuda_wheel_is_never_installed_in_cpu_environment(storage, monkeypatch):
    directory = storage / ".runtime" / "wheels"
    directory.mkdir(parents=True)
    wheel = directory / "torch-2.7.1+cu128-cp312-cp312-win_amd64.whl"
    wheel.touch()
    commands = []
    monkeypatch.setattr(embeddings, "_uv_prefix", lambda: ["uv"])
    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr("classcue.preparation_progress.run_logged", run)
    embeddings._prepare_environment("cpu", embeddings.runtime_directory("cpu"), None, None)
    assert any("torch==2.7.1+cpu" in command for command in commands)
    assert all(str(wheel) not in command for command in commands)
    commands.clear()
    embeddings._prepare_environment("cuda", embeddings.runtime_directory("cuda"), None, None)
    assert any(str(wheel) in command for command in commands)


def test_cancel_before_setup_does_not_touch_selection(storage):
    runtime(storage, "cuda", legacy=True, variant=False)
    with pytest.raises(ProcessingCancelled):
        embeddings.prepare_model("cpu", should_stop=lambda: True)
    assert embeddings.selected_device() == "cuda"


def test_weight_marker_does_not_allow_path_escape(storage):
    directory = embeddings.model_directory()
    directory.mkdir(parents=True)
    (directory / "ready.json").write_text(json.dumps({
        "fingerprint": "unsafe", "files": {"../outside": 0},
    }), encoding="utf-8")
    assert not embeddings.model_status()["weights_ready"]


def test_non_object_persisted_markers_degrade_to_unprepared(storage):
    directory = embeddings.model_directory()
    directory.mkdir(parents=True)
    (directory / "ready.json").write_text("[]", encoding="utf-8")
    embeddings._selection_path().parent.mkdir(parents=True, exist_ok=True)
    embeddings._selection_path().write_text('"cpu"', encoding="utf-8")
    assert embeddings.selected_device() == "cuda"
    assert not embeddings.model_status()["ready"]


def test_worker_uses_requested_runtime_and_never_changes_selected_device(storage, monkeypatch):
    runtime(storage, "cuda", legacy=True, variant=False)
    runtime(storage, "cpu")
    seen = []
    class Verifier:
        def encode(self, texts, **kwargs):
            seen.append(kwargs["_status"])
        def close(self):
            seen.append("closed")
    monkeypatch.setattr(embeddings, "EmbeddingWorker", Verifier)
    embeddings._validate_runtime("cpu", None)
    assert seen[0]["device"] == "cpu" and seen[-1] == "closed"
    assert embeddings.selected_device() == "cuda"

def test_validation_withdraws_completed_download_progress(storage, monkeypatch):
    from classcue.preparation_progress import preparation_events, stage
    runtime(storage, "cpu")
    weights(storage)
    events = []
    def prepare_weights(*_):
        stage("downloading", "下载完成", phase_progress=1, bytes_completed=100, bytes_total=100,
              speed_bytes_per_second=20, eta_seconds=0)
    def validate(*_):
        assert events[-1]["phase"] == "verifying"
        assert events[-1]["phase_progress"] is None
        assert events[-1]["label"] == "验证检索模型"
        assert "bytes_total" not in events[-1]
        assert events[-1]["speed_bytes_per_second"] is None
    monkeypatch.setattr(embeddings, "_prepare_weights", prepare_weights)
    monkeypatch.setattr(embeddings, "_validate_runtime", validate)
    with preparation_events(events.append):
        embeddings.prepare_model("cpu")


def test_interrupted_download_retries_with_range_and_retains_verified_prefix(tmp_path, monkeypatch):
    partial = tmp_path / "model.part"
    partial.write_bytes(b"first")
    requests = []
    monkeypatch.setattr(embeddings.time, "sleep", lambda _: None)
    @contextmanager
    def stream(method, url, **kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            response = httpx.Response(503, request=httpx.Request(method, url))
        else:
            response = httpx.Response(206, headers={"Content-Range": "bytes 5-9/10"},
                                      content=b"other", request=httpx.Request(method, url))
        yield response
    monkeypatch.setattr(httpx, "stream", stream)
    embeddings._stream_weight("https://example.invalid/model", partial, 10, lambda _: None, None)
    assert partial.read_bytes() == b"firstother"
    assert len(requests) == 2
    assert all(request["headers"]["Range"] == "bytes=5-" and request["trust_env"] is False
               for request in requests)


def test_invalid_resume_response_never_appends_to_existing_bytes(tmp_path, monkeypatch):
    partial = tmp_path / "model.part"
    partial.write_bytes(b"first")
    @contextmanager
    def stream(method, url, **kwargs):
        yield httpx.Response(206, headers={"Content-Range": "bytes 0-4/10"}, content=b"other",
                             request=httpx.Request(method, url))
    monkeypatch.setattr(httpx, "stream", stream)
    with pytest.raises(ValueError, match="断点续传范围"):
        embeddings._stream_weight("https://example.invalid/model", partial, 10, lambda _: None, None)
    assert partial.read_bytes() == b"first"


def test_cancelled_download_preserves_partial_for_next_attempt(tmp_path):
    partial = tmp_path / "model.part"
    partial.write_bytes(b"first")
    with pytest.raises(ProcessingCancelled):
        embeddings._stream_weight("https://example.invalid/model", partial, 10, lambda _: None, lambda: True)
    assert partial.read_bytes() == b"first"


def test_cleanup_removes_only_rag_assets_and_preserves_shared_dependencies(storage):
    weights(storage)
    runtime(storage, "cpu")
    runtime(storage, "cuda")
    runtime(storage, "cuda", legacy=True, variant=False)
    selection = embeddings._selection_path()
    selection.write_text('{"device":"cpu"}', encoding="utf-8")
    selection.with_suffix(".tmp").write_text("temporary", encoding="utf-8")
    preserved = [
        storage / ".runtime/paraformer-cpu/ready.json",
        storage / ".runtime/cache/uv/shared.bin",
        storage / ".runtime/wheels/torch-2.7.1+cpu-cp312-cp312-win_amd64.whl",
        storage / "models/asr/asr.bin",
        storage / "courses/subtitles.srt",
    ]
    for path in preserved:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"keep")
    embeddings.encoder.query_cache["synthetic"] = b"cached"
    assert embeddings.cleanup_model() > 0
    assert not embeddings.encoder.query_cache
    assert all(not path.exists() for path, _ in embeddings._cleanup_targets())
    assert all(path.read_bytes() == b"keep" for path in preserved)
    assert not embeddings.model_status()["cache_present"]


def test_cleanup_cancellation_keeps_retryable_partial_state(storage, monkeypatch):
    weights(storage)
    directory = runtime(storage, "cpu")
    (directory / "package.bin").write_bytes(b"package")
    from classcue import cleanup_progress
    remove = cleanup_progress._remove_file
    removed = []

    def record(path):
        remove(path)
        removed.append(path)

    monkeypatch.setattr(cleanup_progress, "_remove_file", record)
    with pytest.raises(ProcessingCancelled):
        embeddings.cleanup_model(should_stop=lambda: bool(removed))
    assert removed and all(path.name == "ready.json" for path in removed)
    assert not embeddings.model_status()["ready"]
    assert embeddings.model_status()["cache_present"]
    embeddings.cleanup_model()
    assert not embeddings.model_status()["cache_present"]


def test_cache_inventory_distinguishes_ready_missing_and_incomplete_device_assets(storage):
    weights(storage)
    directory = runtime(storage, "cuda")
    (directory / "package.bin").write_bytes(b"gpu-data")
    empty = embeddings.cache_inventory()
    assert empty["weights"]["ready"] and empty["weights"]["present"]
    assert empty["runtimes"]["cuda"]["ready"] and empty["runtimes"]["cuda"]["bytes"] > 8
    assert empty["runtimes"]["cpu"] == {"ready": False, "present": False, "bytes": 0, "files": 0}
    (directory / "ready.json").unlink()
    residual = embeddings.cache_inventory()["runtimes"]["cuda"]
    assert not residual["ready"] and residual["present"]
    assert residual["bytes"] == 8 and residual["files"] == 2


@pytest.mark.parametrize("target", ["cpu", "cuda", "weights"])
def test_selected_cleanup_preserves_other_device_weights_and_device_preference(storage, target):
    weights(storage)
    cpu = runtime(storage, "cpu")
    cuda = runtime(storage, "cuda")
    selection = embeddings._selection_path()
    selection.write_text('{"device":"cpu"}', encoding="utf-8")
    embeddings.cleanup_model(target=target)
    assert cpu.exists() is (target != "cpu")
    assert cuda.exists() is (target != "cuda")
    assert embeddings.model_directory().exists() is (target != "weights")
    assert selection.read_text(encoding="utf-8") == '{"device":"cpu"}'


def test_cancelled_gpu_cleanup_reports_residual_files_without_removing_shared_weights(storage, monkeypatch):
    weights(storage)
    runtime(storage, "cpu")
    cuda = runtime(storage, "cuda")
    (cuda / "package.bin").write_bytes(b"remaining")
    from classcue import cleanup_progress
    remove = cleanup_progress._remove_file
    removed = []

    def record(path):
        remove(path)
        removed.append(path)

    monkeypatch.setattr(cleanup_progress, "_remove_file", record)
    with pytest.raises(ProcessingCancelled):
        embeddings.cleanup_model(should_stop=lambda: bool(removed), target="cuda")
    cache = embeddings.cache_inventory()
    assert cache["weights"]["ready"] and cache["runtimes"]["cpu"]["ready"]
    assert not cache["runtimes"]["cuda"]["ready"]
    assert cache["runtimes"]["cuda"]["present"] and cache["runtimes"]["cuda"]["bytes"] == 9
    embeddings.cleanup_model(target="cuda")
    assert embeddings.cache_inventory()["runtimes"]["cuda"] == {
        "present": False, "ready": False, "bytes": 0, "files": 0}


@pytest.mark.parametrize("keep_device", ["cpu", "cuda"])
def test_keep_device_validates_and_activates_before_removing_other_runtime(storage, monkeypatch, keep_device):
    weights(storage)
    runtime(storage, "cpu")
    runtime(storage, "cuda")
    target = "cuda" if keep_device == "cpu" else "cpu"
    embeddings._selection_path().write_text(json.dumps({"device": target}), encoding="utf-8")
    validated = []

    def validate(device, stop):
        assert embeddings.selected_device() == target
        assert embeddings.cache_inventory()["runtimes"][target]["ready"]
        validated.append(device)

    monkeypatch.setattr(embeddings, "_validate_runtime", validate)
    embeddings.cleanup_model(target=target, keep_device=keep_device)
    assert validated == [keep_device]
    assert embeddings.selected_device() == keep_device
    cache = embeddings.cache_inventory()
    assert cache["weights"]["ready"] and cache["runtimes"][keep_device]["ready"]
    assert not cache["runtimes"][target]["present"]


@pytest.mark.parametrize("failure", ["missing_runtime", "missing_weights", "unavailable_gpu", "validation"])
def test_keep_device_failure_preserves_prior_selection_and_all_other_assets(storage, monkeypatch, failure):
    weights(storage)
    cpu = runtime(storage, "cpu")
    runtime(storage, "cuda")
    embeddings._selection_path().write_text('{"device":"cpu"}', encoding="utf-8")
    if failure == "missing_runtime":
        (embeddings.runtime_directory("cuda") / "ready.json").unlink()
    elif failure == "missing_weights":
        (embeddings.model_directory() / "ready.json").unlink()
    elif failure == "unavailable_gpu":
        monkeypatch.setattr(embeddings, "_gpu_detection", lambda: False)

    def fail(*_):
        raise RuntimeError("合成验证失败")

    monkeypatch.setattr(embeddings, "_validate_runtime", fail)
    with pytest.raises(RuntimeError):
        embeddings.cleanup_model(target="cpu", keep_device="cuda")
    assert embeddings.selected_device() == "cpu"
    assert (cpu / "ready.json").exists()
    assert (embeddings.model_directory() / "model.safetensors").read_bytes() == b"synthetic"


def test_interrupted_legacy_cpu_cleanup_retains_ownership_until_completion(storage, monkeypatch):
    weights(storage)
    legacy = runtime(storage, "cpu", legacy=True)
    (legacy / "package.bin").write_bytes(b"legacy-cpu")
    runtime(storage, "cuda")
    from classcue import cleanup_progress
    remove = cleanup_progress._remove_file
    removed = []

    def record(path):
        remove(path)
        removed.append(path)

    monkeypatch.setattr(cleanup_progress, "_remove_file", record)
    with pytest.raises(ProcessingCancelled):
        embeddings.cleanup_model(should_stop=lambda: bool(removed), target="cpu")
    assert not (legacy / "ready.json").exists()
    owner = storage / ".runtime/semantic-search-legacy-device.json"
    assert json.loads(owner.read_text())["device"] == "cpu"
    cache = embeddings.cache_inventory()
    assert cache["runtimes"]["cpu"]["present"] and not cache["runtimes"]["cpu"]["ready"]
    assert cache["runtimes"]["cuda"]["ready"]
    embeddings.cleanup_model(target="cpu")
    assert not legacy.exists() and not owner.exists()
    assert embeddings.cache_inventory()["runtimes"]["cuda"]["ready"]


@pytest.mark.skipif(os.name != "nt", reason="Windows junction safety")
def test_cleanup_rejects_nested_junction_before_any_owned_asset_is_deleted(storage):
    weights(storage)
    directory = runtime(storage, "cpu")
    outside = storage.parent / "keep-outside"
    outside.mkdir()
    (outside / "keep.txt").write_bytes(b"keep")
    link = directory / "escape"
    subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(link), str(outside)],
                   capture_output=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        with pytest.raises(ValueError, match="目录联接"):
            embeddings.cleanup_model()
        with pytest.raises(ValueError, match="目录联接"):
            embeddings.cache_inventory()
        assert (embeddings.model_directory() / "ready.json").exists()
        assert (directory / "ready.json").exists()
        assert (outside / "keep.txt").read_bytes() == b"keep"
    finally:
        link.rmdir()
