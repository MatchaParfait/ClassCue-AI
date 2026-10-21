import time

from fastapi.testclient import TestClient
import pytest

import api.main as api_main
import classcue.asr_models as asr_models
import classcue.model_downloads as model_downloads
from classcue.preparation_progress import checkpoint, stage
from classcue.video_store import VideoStore


def wait_job(client, job_id):
    for _ in range(300):
        row = client.get(f"/api/jobs/{job_id}").json()
        if row["status"] not in {"queued", "running"}:
            return row
        time.sleep(.01)
    pytest.fail("Model job did not stop")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(asr_models, "MODEL_ROOT", tmp_path / "models")
    monkeypatch.setattr(api_main, "MODEL_ROOT", tmp_path / "models")
    monkeypatch.setattr(asr_models, "SETTINGS_PATH", tmp_path / "selection.json")
    store = VideoStore(tmp_path / "videos.db")
    return store


def test_pause_resume_and_cleanup_are_durable_and_mutually_exclusive(setup, monkeypatch):
    model_id = "paraformer-large-cpu"
    calls = []

    def prepare(model_id, progress, **options):
        calls.append(options.get("paraformer_device"))
        with asr_models.model_operation(model_id), model_downloads.download_session(
            model_id, device=options.get("paraformer_device")
        ):
            root = asr_models.component_paths(model_id)["recognizer"]
            root.mkdir(parents=True, exist_ok=True)
            (root / "completed.bin").write_bytes(b"verified")
            partial = root / "model.pt.part"
            with partial.open("ab") as target:
                for _ in range(200):
                    checkpoint()
                    target.write(b"x")
                    target.flush()
                    size = partial.stat().st_size
                    stage("downloading", "model.pt", component="recognizer", current_file="model.pt",
                          bytes_completed=size + 8, bytes_total=1000, file_bytes_completed=size)
                    time.sleep(.01)

    monkeypatch.setattr(api_main, "prepare_asr_model", prepare)
    with TestClient(api_main.create_app(setup)) as client:
        first = client.post(f"/api/asr/models/{model_id}/prepare?device=cpu").json()["job_id"]
        for _ in range(100):
            job = client.get(f"/api/jobs/{first}").json()
            if (job.get("preparation") or {}).get("phase") == "downloading":
                break
            time.sleep(.01)
        assert client.delete(f"/api/asr/models/{model_id}/download/incomplete").status_code == 409
        assert client.post(f"/api/asr/models/{model_id}/download/resume").status_code == 409
        assert client.post(f"/api/jobs/{first}/download/pause").status_code == 200
        paused = wait_job(client, first)
        assert paused["status"] == "interrupted"
        assert paused["stage"] == "download_paused"
        assert not paused["error_message"]
        assert paused["model_id"] == model_id
        models = client.get("/api/asr/models").json()
        state = next(model["download_state"] for model in models if model["id"] == model_id)
        assert state["status"] == "paused"
        assert state["retained_bytes"] > 8
        assert state["device"] == "cpu"
        assert calls == ["cpu"]
        second = client.post(f"/api/asr/models/{model_id}/download/resume").json()["job_id"]
        assert second != first
        for _ in range(100):
            if client.get(f"/api/jobs/{second}").json().get("preparation"):
                break
            time.sleep(.01)
        client.post(f"/api/jobs/{second}/download/pause")
        assert wait_job(client, second)["stage"] == "download_paused"
        assert calls == ["cpu", "cpu"]
        cleanup = client.delete(f"/api/asr/models/{model_id}/download/incomplete")
        assert cleanup.status_code == 202
        assert wait_job(client, cleanup.json()["job_id"])["status"] == "completed"
        root = asr_models.component_paths(model_id)["recognizer"]
        assert (root / "completed.bin").read_bytes() == b"verified"
        assert not (root / "model.pt.part").exists()
        state = model_downloads.download_snapshot(model_id)
        assert state["retained_bytes"] == 8
        assert not state["can_cleanup"]


def test_restart_exposes_interrupted_files_without_resubmitting(setup, monkeypatch):
    model_id = "whisper-turbo"
    setup.create_job("old-prepare", "asr_prepare", None)
    setup.update_job("old-prepare", status="running", request_json='{"model_id":"whisper-turbo"}')
    root = asr_models.component_paths(model_id)["recognizer"]
    root.mkdir(parents=True)
    (root / "model.bin.part").write_bytes(b"partial")
    model_downloads._write(model_id, {"status": "preparing"})
    monkeypatch.setattr(api_main, "prepare_asr_model", lambda *_a, **_kw: pytest.fail("Must not auto-resume"))
    with TestClient(api_main.create_app(setup)) as client:
        models = client.get("/api/asr/models").json()
        model = next(model for model in models if model["id"] == model_id)
        assert model["download_state"]["status"] == "interrupted"
        assert model["download_state"]["retained_bytes"] == 7
        assert model["active_job_id"] is None
        assert not setup.list_active_jobs()
        assert client.get("/api/jobs/old-prepare").json()["status"] == "interrupted"
        cleanup = client.delete(f"/api/asr/models/{model_id}/download/incomplete")
        assert wait_job(client, cleanup.json()["job_id"])["status"] == "completed"
        assert model_downloads.download_snapshot(model_id) is None


def test_installation_cannot_be_misreported_as_paused(setup):
    from threading import Event
    entered, release = Event(), Event()
    application = api_main.create_app(setup)
    # Use the manager's public job boundary rather than starting an installer.
    manager = application.state.jobs

    def operation(update, _cancel):
        stage("installing", "install fixture")
        entered.set()
        assert release.wait(3)

    with TestClient(application) as client:
        try:
            job_id = manager.submit("asr_prepare", None, operation, request={"model_id": "whisper-turbo"})
            assert entered.wait(2)
            response = client.post(f"/api/jobs/{job_id}/download/pause")
            assert response.status_code == 409
            assert "安装环境" in response.json()["detail"]
        finally:
            release.set()
        assert wait_job(client, job_id)["status"] == "completed"
