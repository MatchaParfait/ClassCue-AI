import json
from threading import Event
import time

from fastapi.testclient import TestClient
import pytest

import api.main as api_main
import asr_models
import cleanup_progress
import dolphin_runtime
from video_store import VideoStore


def wait_job(client, job_id):
    for _ in range(300):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in {"queued", "running"}:
            return job
        time.sleep(.01)
    pytest.fail("Cleanup did not finish")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = tmp_path / "assets"
    monkeypatch.setattr(asr_models, "MODEL_ROOT", root / "models/asr")
    monkeypatch.setattr(api_main, "MODEL_ROOT", root / "models/asr")
    monkeypatch.setattr(asr_models, "SETTINGS_PATH", tmp_path / "selection.json")
    monkeypatch.setattr(dolphin_runtime, "RUNTIME_ROOT", root / ".runtime")
    for device in ("cpu", "gpu"):
        directory = dolphin_runtime.runtime_directory(device)
        directory.mkdir(parents=True)
        (directory / "ready.json").write_bytes(b"ready")
        (directory / "package.py").write_bytes(b"package")
    weight = asr_models.component_paths("dolphin-small-cn")["recognizer"] / "small.cn.pt"
    weight.parent.mkdir(parents=True)
    weight.write_bytes(b"keep weights")
    return VideoStore(tmp_path / "videos.db"), weight


def test_runtime_cleanup_persists_progress_and_only_removes_selected_device(setup, monkeypatch):
    store, weight = setup
    entered, release = Event(), Event()
    remove = cleanup_progress._remove_file

    def blocking_remove(path):
        entered.set()
        assert release.wait(4)
        return remove(path)

    monkeypatch.setattr(cleanup_progress, "_remove_file", blocking_remove)
    with TestClient(api_main.create_app(store)) as client:
        response = client.delete("/api/asr/models/dolphin-small-cn/runtime?device=gpu")
        assert response.status_code == 202
        job_id = response.json()["job_id"]
        try:
            assert entered.wait(3)
            job = client.get(f"/api/jobs/{job_id}").json()
            assert job["job_type"] == "asr_runtime_delete"
            assert job["cleanup"]["phase"] == "deleting"
            assert job["cleanup"]["files_total"] == 2
            assert job["cleanup"]["files_deleted"] == 0
            assert store.get_job(job_id)["cleanup_json"]
            assert json.loads(store.get_job(job_id)["request_json"])["device"] == "gpu"
            models = client.get("/api/asr/models").json()
            assert next(model for model in models if model["id"] == "dolphin-small-cn")["active_job_kind"] == "cleaning_runtime"
            assert client.delete("/api/asr/models/dolphin-small-cn").status_code == 409
        finally:
            release.set()
        job = wait_job(client, job_id)
        assert job["status"] == "completed"
        assert job["cleanup"]["phase"] == "completed"
        assert job["cleanup"]["files_deleted"] == 2
        assert job["cleanup"]["directories_deleted"] == 1
        assert not dolphin_runtime.runtime_directory("gpu").exists()
        assert (dolphin_runtime.runtime_directory("cpu") / "package.py").read_bytes() == b"package"
        assert weight.read_bytes() == b"keep weights"


def test_file_occupation_fails_with_filename_and_retry_keeps_weights(setup, monkeypatch):
    store, weight = setup
    remove = cleanup_progress._remove_file

    def locked(path):
        if path.name == "package.py":
            raise PermissionError("file is occupied")
        remove(path)

    with TestClient(api_main.create_app(store)) as client:
        monkeypatch.setattr(cleanup_progress, "_remove_file", locked)
        job_id = client.delete("/api/asr/models/dolphin-small-cn/runtime?device=gpu").json()["job_id"]
        failed = wait_job(client, job_id)
        assert failed["status"] == "failed"
        assert "package.py" in failed["error_message"]
        assert not (dolphin_runtime.runtime_directory("gpu") / "ready.json").exists()
        assert weight.exists()
        monkeypatch.setattr(cleanup_progress, "_remove_file", remove)
        retry = client.delete("/api/asr/models/dolphin-small-cn/runtime?device=gpu").json()["job_id"]
        assert wait_job(client, retry)["status"] == "completed"
        assert weight.read_bytes() == b"keep weights"


def test_historical_jobs_and_invalid_device_requests(setup):
    store, _weight = setup
    store.create_job("historical", "asr_delete", None)
    store.update_job("historical", status="completed")
    with TestClient(api_main.create_app(store)) as client:
        assert client.get("/api/jobs/historical").json()["cleanup"] is None
        assert client.delete("/api/asr/models/qwen3-asr-1.7b/runtime?device=cpu").status_code == 400
        assert client.delete("/api/asr/models/whisper-turbo/runtime").status_code == 400
