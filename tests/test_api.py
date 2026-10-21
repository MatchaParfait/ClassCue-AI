from __future__ import annotations

from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from api.main import VideoProcessPayload, create_app
from classcue.core import TranscriptSegment
from classcue.video_store import VideoStore


def test_new_processing_requests_default_to_unfiltered_subtitles():
    assert VideoProcessPayload().filter_formula_noise is False


def test_application_creation_does_not_open_or_mutate_user_workspace(monkeypatch):
    import api.main as api_main

    def unexpected_workspace():
        pytest.fail("Application construction must not touch the real workspace")

    monkeypatch.setattr(api_main, "load_data_root", unexpected_workspace)
    api_main.create_app()


def test_ai_screening_stage_is_not_misclassified_as_transcription():
    from api.main import _infer_job_stage

    assert _infer_job_stage("AI 正在筛选完整字幕") == "analyzing"
    assert _infer_job_stage("FireRedASR2-AED 转写 1/60") == "transcribing"
    assert _infer_job_stage("FireRedVAD 检测完成，共 12 个语音片段") == "transcribing"


def test_interrupted_jobs_are_recovered_only_when_server_starts(tmp_path):
    store = VideoStore(tmp_path / "video.db")
    video_id = store.create_video("hash", "lesson.mp4", "lesson.mp4", "lesson.wav", 1000)
    store.create_job("previous", "video_process", video_id)
    store.update_job("previous", status="running")
    store.set_status(video_id, "processing")
    application = create_app(store)
    assert store.get_job("previous")["status"] == "running"
    with TestClient(application) as client:
        assert client.get("/api/health").status_code == 200
        assert store.get_job("previous")["status"] == "interrupted"
        assert store.get_video(video_id)["status"] == "failed"

@pytest.fixture
def api_fixture(tmp_path: Path):
    video_path = tmp_path / "lesson.mp4"
    video_path.write_bytes(b"0123456789")

    store = VideoStore(tmp_path / "video.db")
    video_id = store.create_video(
        "content-hash",
        video_path.name,
        str(video_path),
        str(tmp_path / "lesson.wav"),
        10_000,
    )
    store.mark_transcribed(
        video_id,
        [
            TranscriptSegment(1, 0, 2_000, "第一句"),
            TranscriptSegment(2, 2_000, 4_000, "第二句"),
        ],
    )
    active_id = store.add_marker(
        video_id,
        0,
        2_000,
        "期末考点",
        "重点说明",
        "第一句",
        "会考",
        0.95,
        "strict_exam_intent",
    )
    mastered_id = store.add_marker(
        video_id,
        2_000,
        4_000,
        "扩展重点",
        "补充说明",
        "第二句",
        "重点",
        0.8,
        "high_recall",
    )
    store.set_marker_review_status(mastered_id, "mastered")
    excluded_id = store.add_marker(
        video_id,
        4_000,
        5_000,
        "移除重点",
        "",
        "第三句",
        "注意",
        0.5,
        "high_recall",
    )
    store.set_marker_review_status(excluded_id, "excluded")
    store.set_analysis_run(video_id, "strict_exam_intent", "completed", 1)
    store.set_analysis_run(video_id, "high_recall", "completed", 2)

    return TestClient(create_app(store)), store, video_id, active_id, mastered_id, excluded_id


def test_health_and_video_detail(api_fixture):
    client, _store, video_id, *_ = api_fixture

    health = client.get("/api/health").json()
    assert health["status"] == "ok"
    assert len(health["library_id"]) == 12
    response = client.get(f"/api/videos/{video_id}")

    assert response.status_code == 200
    assert response.json()["name"] == "lesson"
    assert response.json()["duration_ms"] == 10_000
    assert response.json()["status"] == "transcribed"
    assert response.json()["has_transcript"] is True
    assert response.json()["library_id"] == health["library_id"]


def test_retired_asr_cannot_be_prepared_or_selected(api_fixture, monkeypatch):
    import api.main as api_main

    client, *_ = api_fixture
    monkeypatch.setattr(api_main, "model_is_downloaded", lambda _: False)
    monkeypatch.setattr(api_main, "cache_size", lambda _: 0)
    monkeypatch.setattr(api_main, "runtime_is_ready", lambda: False)
    monkeypatch.setattr(api_main, "firered_runtime_is_ready", lambda: False)
    monkeypatch.setattr(api_main, "firered_runtime_size", lambda: 0)
    models = client.get("/api/asr/models").json()
    assert {model["id"] for model in models} == set(api_main.MODELS)
    retired = "paraformer-large-vad-punc"
    assert client.get(f"/api/asr/models/{retired}/status").status_code == 404
    assert client.post(f"/api/asr/models/{retired}/prepare").status_code == 404
    assert client.put("/api/asr/selection", json={"model_id": retired}).status_code == 400


def test_firered_device_selection_roundtrip_does_not_scan_cache(api_fixture, tmp_path, monkeypatch):
    import api.main as api_main
    import classcue.asr_models as asr_models

    client, *_ = api_fixture
    monkeypatch.setattr(asr_models, "SETTINGS_PATH", tmp_path / "asr.json")
    monkeypatch.setattr(api_main, "model_is_downloaded", lambda _: pytest.fail("No selection scan"))
    asr_models.select_model("firered")
    response = client.put("/api/asr/selection", json={
        "model_id": "fireredasr2-aed", "firered_device": "cpu",
    })
    assert response.status_code == 400
    assert "CPU 处理已禁用" in response.json()["detail"]
    assert asr_models.selected_firered_device() == "gpu"
    assert client.put("/api/asr/selection", json={
        "model_id": "fireredasr2-aed", "firered_device": "auto",
    }).status_code == 422
    client.put("/api/asr/selection", json={"model_id": "whisper-turbo"})
    assert asr_models.selected_firered_device() == "gpu"


def test_firered_api_reports_saved_device_and_cpu_notice(api_fixture, tmp_path, monkeypatch):
    import api.main as api_main
    import classcue.asr_models as asr_models

    client, *_ = api_fixture
    monkeypatch.setattr(asr_models, "SETTINGS_PATH", tmp_path / "asr.json")
    monkeypatch.setattr(api_main, "model_is_downloaded", lambda _: False)
    monkeypatch.setattr(api_main, "cache_size", lambda _: 0)
    monkeypatch.setattr(api_main, "firered_runtime_is_ready", lambda: False)
    monkeypatch.setattr(api_main, "firered_runtime_size", lambda: 0)
    status = client.get("/api/asr/models/firered/status").json()
    assert status["device"] == "gpu"
    assert "CPU 处理已禁用" in status["cpu_notice"]
    assert status["cpu_available"] is False
    asr_models.SETTINGS_PATH.write_text(
        '{"model_id": "fireredasr2-aed", "firered_device": "cpu"}', encoding="utf-8",
    )
    assert client.get("/api/asr/models/firered/status").json()["device"] == "cpu"
    assert client.put("/api/asr/selection", json={"model_id": "firered"}).status_code == 400
    assert client.put("/api/asr/selection", json={
        "model_id": "firered", "firered_device": "gpu",
    }).status_code == 200


def test_paraformer_api_device_selection_checks_cuda_and_preserves_previous_selection(api_fixture, tmp_path, monkeypatch):
    import classcue.asr_models as asr_models
    import classcue.paraformer_runtime as paraformer_runtime

    client, *_ = api_fixture
    monkeypatch.setattr(asr_models, "SETTINGS_PATH", tmp_path / "settings.json")
    asr_models.select_model("paraformer-large-cpu", paraformer_device="cpu")
    monkeypatch.setattr(paraformer_runtime, "require_cuda",
                        lambda: (_ for _ in ()).throw(ValueError("CUDA unavailable")))
    response = client.put("/api/asr/selection", json={
        "model_id": "paraformer-large-cpu", "paraformer_device": "gpu",
    })
    assert response.status_code == 400
    assert asr_models.selected_paraformer_device() == "cpu"
    monkeypatch.setattr(paraformer_runtime, "require_cuda", lambda: None)
    response = client.put("/api/asr/selection", json={
        "model_id": "paraformer-large-cpu", "paraformer_device": "gpu",
    })
    assert response.status_code == 200
    assert response.json()["paraformer_device"] == "gpu"
    assert client.put("/api/asr/selection", json={
        "model_id": "paraformer-large-cpu", "paraformer_device": "auto",
    }).status_code == 422


def test_paraformer_status_reports_runtime_cuda_capability(api_fixture, tmp_path, monkeypatch):
    import classcue.asr_models as asr_models
    import api.main as api_main

    client, *_ = api_fixture
    monkeypatch.setattr(asr_models, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(api_main, "model_is_downloaded", lambda _: False)
    monkeypatch.setattr(api_main, "cache_size", lambda _: 0)
    monkeypatch.setattr(api_main, "paraformer_runtime_size", lambda: 0)
    monkeypatch.setattr(api_main, "paraformer_runtime_is_ready", lambda device: device == "cpu")
    monkeypatch.setattr(api_main, "paraformer_cuda_status", lambda: {
        "gpu_available": False, "gpu_runtime_ready": True,
        "gpu_prepare_available": False, "gpu_unavailable_reason": "CUDA unavailable",
    })
    status = client.get("/api/asr/models/paraformer-large-cpu/status").json()
    assert status["device"] == "cpu"
    assert status["gpu_available"] is False
    assert status["gpu_runtime_ready"] is True
    assert client.post("/api/asr/models/paraformer-large-cpu/prepare?device=gpu").status_code == 400
    assert client.post("/api/asr/models/whisper-turbo/prepare?device=gpu").status_code == 400


def test_nano_api_retirement_blocks_selection_preparation_and_transcription(api_fixture, tmp_path, monkeypatch):
    import classcue.asr_models as asr_models
    import classcue.nano_runtime as nano_runtime
    import api.main as api_main

    client, store, video_id, *_ = api_fixture
    monkeypatch.setattr(asr_models, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(api_main, "model_is_downloaded", lambda _: True)
    monkeypatch.setattr(api_main, "cache_size", lambda _: 0)
    monkeypatch.setattr(api_main, "nano_runtime_size", lambda: 0)
    monkeypatch.setattr(api_main, "nano_runtime_is_ready", lambda device: device == "cpu")
    monkeypatch.setattr(api_main, "nano_cuda_status", lambda: {
        "gpu_available": False, "gpu_runtime_ready": True,
        "gpu_prepare_available": False, "gpu_unavailable_reason": "CUDA unavailable",
    })
    monkeypatch.setattr(nano_runtime, "require_cuda",
                        lambda: (_ for _ in ()).throw(ValueError("CUDA unavailable")))
    status = client.get("/api/asr/models/fun-asr-nano-2512/status").json()
    assert status["device"] == "cpu"
    assert status["selectable"] is False
    assert "已下架" in status["retired_notice"]
    assert status["nano_runtime_ready"] is True
    assert status["batch_size"] == 6
    assert status["timestamp_batch_mode"] == "sequential"
    assert "word_timestamps" in status["capabilities"]
    monkeypatch.setattr(nano_runtime, "require_cuda", lambda: pytest.fail("No CUDA probe"))
    monkeypatch.setattr(api_main, "prepare_asr_model", lambda *_a, **_kw: pytest.fail("No preparation"))
    legacy = '{"model_id": "fun-asr-nano-2512", "nano_device": "gpu"}'
    asr_models.SETTINGS_PATH.write_text(legacy, encoding="utf-8")
    listed = client.get("/api/asr/models").json()
    assert "fun-asr-nano-2512" not in {model["id"] for model in listed}
    assert client.get("/api/asr/selection").json()["id"] == asr_models.DEFAULT_MODEL_ID
    for device in ("cpu", "gpu"):
        response = client.put("/api/asr/selection", json={"model_id": "fun-asr-nano-2512", "nano_device": device})
        assert response.status_code == 400
        assert "已下架" in response.json()["detail"]
        response = client.post(f"/api/asr/models/fun-asr-nano-2512/prepare?device={device}")
        assert response.status_code == 400
        assert "已下架" in response.json()["detail"]
    response = client.post(f"/api/videos/{video_id}/transcribe", json={
        "asr_model_id": "fun-asr-nano-2512", "force_retranscribe": True,
    })
    assert response.status_code == 400
    assert "已下架" in response.json()["detail"]
    response = client.post(f"/api/videos/{video_id}/process", json={
        "asr_model_id": "fun-asr-nano-2512", "force_retranscribe": True,
    })
    assert response.status_code == 400
    assert "已下架" in response.json()["detail"]
    assert not store.list_active_jobs()
    assert asr_models.SETTINGS_PATH.read_text(encoding="utf-8") == legacy
    assert store.get_segments(video_id)[0].text == "第一句"


def test_dolphin_api_reports_batch_and_gates_gpu_selection(api_fixture, tmp_path, monkeypatch):
    import api.main as api_main
    import classcue.asr_models as asr_models
    import classcue.dolphin_runtime as dolphin_runtime

    client, *_ = api_fixture
    monkeypatch.setattr(asr_models, "SETTINGS_PATH", tmp_path / "asr-settings.json")
    monkeypatch.setattr(asr_models, "MODEL_ROOT", tmp_path / "models")
    asr_models.select_model("dolphin-small-cn", dolphin_device="cpu")
    monkeypatch.setattr(api_main, "dolphin_runtime_size", lambda: 0)
    monkeypatch.setattr(api_main, "dolphin_runtime_is_ready", lambda device: device == "cpu")
    monkeypatch.setattr(api_main, "dolphin_cuda_status", lambda: {
        "gpu_available": False, "gpu_runtime_ready": False, "gpu_prepare_available": False,
        "gpu_unavailable_reason": "CUDA unavailable",
    })
    monkeypatch.setattr(dolphin_runtime, "require_cuda",
                        lambda: (_ for _ in ()).throw(ValueError("CUDA unavailable")))
    status = client.get("/api/asr/models/dolphin-small-cn/status").json()
    assert status["device"] == "cpu"
    assert status["dolphin_runtime_ready"] is True
    assert status["batch_size"] == 8
    assert status["timestamp_batch_mode"] == "batched-encoder"
    assert status["gpu_available"] is False
    response = client.put("/api/asr/selection", json={"model_id": "dolphin-small-cn", "dolphin_device": "gpu"})
    assert response.status_code == 400
    assert asr_models.selected_dolphin_device() == "cpu"
    monkeypatch.setattr(dolphin_runtime, "require_cuda", lambda: None)
    response = client.put("/api/asr/selection", json={"model_id": "dolphin-small-cn", "dolphin_device": "gpu"})
    assert response.status_code == 200
    assert response.json()["dolphin_device"] == "gpu"
    assert client.post("/api/asr/models/dolphin-small-cn/prepare?device=gpu").status_code == 400


def test_paraformer_gpu_preparation_does_not_change_saved_device(api_fixture, tmp_path, monkeypatch):
    import classcue.asr_models as asr_models
    import api.main as api_main
    import time

    client, *_ = api_fixture
    monkeypatch.setattr(asr_models, "SETTINGS_PATH", tmp_path / "settings.json")
    asr_models.select_model("paraformer-large-cpu", paraformer_device="cpu")
    monkeypatch.setattr(api_main, "paraformer_cuda_status", lambda: {"gpu_prepare_available": True})
    calls = []

    def prepare(model, progress, *, paraformer_device=None):
        calls.append((model, paraformer_device))
        progress(1.0, "Ready")

    monkeypatch.setattr(api_main, "prepare_asr_model", prepare)
    response = client.post("/api/asr/models/paraformer-large-cpu/prepare?device=gpu")
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    for _ in range(100):
        status = client.get(f"/api/jobs/{job_id}").json()
        if status["status"] in ("completed", "failed"):
            break
        time.sleep(.01)
    assert status["status"] == "completed"
    assert calls == [("paraformer-large-cpu", "gpu")]
    assert asr_models.selected_paraformer_device() == "cpu"


def test_model_preparation_telemetry_survives_readers_and_identifies_the_active_model(api_fixture, monkeypatch):
    import time
    from threading import Event
    import api.main as api_main
    from classcue.preparation_progress import publish

    client, store, *_ = api_fixture
    ready, release = Event(), Event()
    def prepare(model, progress, **kwargs):
        publish({"phase": "downloading", "label": "下载模型权重", "message": "recognizer/model.pt",
                 "current_file": "model.pt", "component": "recognizer", "bytes_completed": 500,
                 "bytes_total": 1000, "phase_progress": .5, "eta_seconds": 10,
                 "speed_bytes_per_second": 50})
        progress(.5, "下载 model.pt")
        ready.set()
        assert release.wait(5)
    monkeypatch.setattr(api_main, "prepare_asr_model", prepare)
    response = client.post("/api/asr/models/paraformer-large-cpu/prepare?device=cpu")
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    try:
        assert ready.wait(3)
        payload = client.get(f"/api/jobs/{job_id}").json()
        assert payload["preparation"]["bytes_completed"] == 500
        assert payload["preparation"]["eta_seconds"] == 10
        assert payload["preparation"]["current_file"] == "model.pt"
        models = client.get("/api/asr/models").json()
        model = next(item for item in models if item["id"] == "paraformer-large-cpu")
        assert model["active_job_id"] == job_id
        assert model["active_job_kind"] == "preparing"
        assert store.get_job(job_id)["preparation_json"]
    finally:
        release.set()
    for _ in range(100):
        status = client.get(f"/api/jobs/{job_id}").json()
        if status["status"] == "completed":
            break
        time.sleep(.01)
    assert status["status"] == "completed"
    assert status["progress"] == 1


def test_retired_asr_history_remains_readable(api_fixture):
    client, store, video_id, *_ = api_fixture
    segments = store.get_segments(video_id)
    store.mark_transcribed(video_id, segments, "paraformer-large-vad-punc")
    response = client.get(f"/api/videos/{video_id}")
    assert response.status_code == 200
    assert response.json()["transcription_model"] == "paraformer-large-vad-punc"
    assert store.get_segments(video_id) == segments


def test_video_library_returns_workspace_scoped_metadata(api_fixture):
    client, _store, video_id, *_ = api_fixture

    response = client.get("/api/videos")

    assert response.status_code == 200
    assert [video["id"] for video in response.json()] == [video_id]
    assert response.json()[0]["library_id"] == client.get("/api/health").json()["library_id"]


def test_library_folders_can_be_created_moved_and_reordered(api_fixture):
    client, store, video_id, *_ = api_fixture
    second_id = store.create_video(
        "content-hash-2", "second.mp4", "second.mp4", "second.wav", 5_000,
    )
    folder = client.post("/api/folders", json={"name": "微积分"}).json()
    folder_id = folder["id"]
    assert folder["video_count"] == 0

    moved = client.post(
        f"/api/videos/{video_id}/folder", json={"folder_id": folder_id}
    )
    assert moved.status_code == 200
    assert moved.json()["folder_id"] == folder_id
    client.post(f"/api/videos/{second_id}/folder", json={"folder_id": folder_id})

    reordered = client.post(
        "/api/library/reorder",
        json={
            "parent_id": folder_id,
            "ordered_items": [
                {"type": "video", "id": second_id},
                {"type": "video", "id": video_id},
            ],
        },
    )
    assert reordered.status_code == 200
    assert [item["video"]["id"] for item in reordered.json()["items"]] == [second_id, video_id]

    root = client.get("/api/library").json()
    assert all(item.get("video", {}).get("id") != video_id for item in root["items"])
    assert client.post(f"/api/folders/{folder_id}/process").status_code == 501

    # Folder deletion now trashes its videos as a group, with an undo action.
    source_folder = store.get_video(video_id)["source_folder"]
    assert client.delete(f"/api/folders/{folder_id}").status_code == 200
    assert store.get_video(video_id) is None
    assert client.post("/api/recycle-bin/undo").status_code == 200
    assert store.get_video(video_id)["source_folder"] == source_folder
    assert store.get_video(video_id)["library_folder_id"] == folder_id


def test_failed_video_retains_available_transcript_flag(api_fixture):
    client, store, video_id, *_ = api_fixture
    store.set_status(video_id, "failed")
    assert client.get(f"/api/videos/{video_id}").json()["has_transcript"] is True
    empty_id = store.create_video("empty", "empty.mp4", "empty.mp4", "empty.wav", 1000)
    assert client.get(f"/api/videos/{empty_id}").json()["has_transcript"] is False


def test_subtitles_markers_filters_and_analysis_runs(api_fixture):
    client, _store, video_id, active_id, mastered_id, excluded_id = api_fixture

    assert client.get(f"/api/videos/{video_id}/subtitles").json() == [
        {"id": 1, "start_ms": 0, "end_ms": 2_000, "text": "第一句"},
        {"id": 2, "start_ms": 2_000, "end_ms": 4_000, "text": "第二句"},
    ]

    strict = client.get(
        f"/api/videos/{video_id}/markers",
        params={"mode": "strict_exam_intent"},
    ).json()
    assert [marker["id"] for marker in strict] == [active_id]

    mastered = client.get(
        f"/api/videos/{video_id}/markers",
        params={"status": "mastered"},
    ).json()
    assert [marker["id"] for marker in mastered] == [mastered_id]

    excluded = client.get(
        f"/api/videos/{video_id}/markers",
        params={"status": "excluded"},
    ).json()
    assert [marker["id"] for marker in excluded] == [excluded_id]

    all_markers = client.get(
        f"/api/videos/{video_id}/markers",
        params={"status": "all"},
    ).json()
    assert {marker["id"] for marker in all_markers} == {
        active_id,
        mastered_id,
        excluded_id,
    }

    runs = client.get(f"/api/videos/{video_id}/analysis-runs")
    assert runs.status_code == 200
    assert {run["analysis_mode"] for run in runs.json()} == {
        "strict_exam_intent",
        "high_recall",
    }


def test_patch_marker_only_changes_review_status_and_404s(api_fixture):
    client, store, video_id, active_id, *_ = api_fixture

    response = client.patch(
        f"/api/markers/{active_id}",
        json={"review_status": "mastered"},
    )

    assert response.status_code == 200
    assert response.json()["review_status"] == "mastered"
    marker = store.get_video(video_id)
    assert marker is not None
    assert store.list_markers(video_id)[0]["review_status"] == "mastered"

    status_alias = client.patch(
        f"/api/markers/{active_id}",
        json={"status": "active"},
    )
    assert status_alias.status_code == 200
    assert status_alias.json()["review_status"] == "active"

    invalid_field = client.patch(
        f"/api/markers/{active_id}",
        json={"review_status": "mastered", "title": "不能修改"},
    )
    assert invalid_field.status_code == 422
    both_status_fields = client.patch(
        f"/api/markers/{active_id}",
        json={"review_status": "mastered", "status": "active"},
    )
    assert both_status_fields.status_code == 422
    assert client.patch(
        "/api/markers/99999", json={"review_status": "active"}
    ).status_code == 404
    assert client.patch(
        f"/api/markers/{active_id}", json={"review_status": "invalid"}
    ).status_code == 422


def test_video_stream_supports_full_and_range_requests(api_fixture):
    client, _store, video_id, *_ = api_fixture

    full = client.get(f"/api/videos/{video_id}/stream")
    assert full.status_code == 200
    assert full.content == b"0123456789"
    assert full.headers["accept-ranges"] == "bytes"

    partial = client.get(
        f"/api/videos/{video_id}/stream",
        headers={"Range": "bytes=2-5"},
    )
    assert partial.status_code == 206
    assert partial.content == b"2345"
    assert partial.headers["content-range"] == "bytes 2-5/10"
    assert partial.headers["content-length"] == "4"

    suffix = client.get(
        f"/api/videos/{video_id}/stream",
        headers={"Range": "bytes=-3"},
    )
    assert suffix.status_code == 206
    assert suffix.content == b"789"

    invalid = client.get(
        f"/api/videos/{video_id}/stream",
        headers={"Range": "bytes=99-100"},
    )
    assert invalid.status_code == 416
    assert invalid.headers["content-range"] == "bytes */10"


@pytest.mark.parametrize("path", ["/api/videos/99999", "/api/videos/99999/subtitles"])
def test_missing_video_returns_clear_404(api_fixture, path):
    client, *_ = api_fixture

    response = client.get(path)

    assert response.status_code == 404
    assert "不存在" in response.json()["detail"]


@pytest.fixture
def ai_key_client(api_fixture, monkeypatch):
    import api.main as api_main
    from classcue.ai_provider import ProviderProfile, ProviderSettings

    settings = ProviderSettings(profile=ProviderProfile(base_url="https://example.test/v1", model="test-model"))
    monkeypatch.setattr(api_main, "load_provider_settings", lambda: settings)
    monkeypatch.setattr(
        api_main, "resolve_interface_key",
        lambda profile, _settings: "test-only-secret" if profile.base_url == settings.active_profile.base_url else "",
    )
    client, *_ = api_fixture
    return client


def test_ai_settings_do_not_expose_secret_by_default(ai_key_client):
    response = ai_key_client.get("/api/settings/ai")
    assert response.status_code == 200
    assert response.json()["has_api_key"] is True
    assert "api_key" not in response.json()
    assert "test-only-secret" not in response.text
    assert ai_key_client.get("/api/settings/ai/key/reveal").status_code == 405


def test_ai_key_reveal_is_explicit_and_not_cacheable(ai_key_client):
    response = ai_key_client.post(
        "/api/settings/ai/key/reveal",
        json={"base_url": "https://example.test/v1"},
        headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
    )
    assert response.status_code == 200
    assert response.json() == {"api_key": "test-only-secret"}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"


def test_ai_key_reveal_does_not_reuse_key_for_other_endpoint(ai_key_client):
    response = ai_key_client.post("/api/settings/ai/key/reveal", json={"base_url": "https://other.test/v1"})
    assert response.status_code == 404
    assert "test-only-secret" not in response.text


@pytest.mark.parametrize("headers", [
    {"Origin": "https://untrusted.test"},
    {"Sec-Fetch-Site": "cross-site"},
    {"Origin": "http://testserver", "Sec-Fetch-Site": "cross-site"},
])
def test_ai_key_reveal_rejects_cross_site_requests(ai_key_client, headers):
    response = ai_key_client.post("/api/settings/ai/key/reveal", headers=headers)
    assert response.status_code == 403
    assert "test-only-secret" not in response.text


def test_processing_payload_accepts_three_modes_and_keeps_legacy_requests():
    from api.main import VideoProcessPayload

    assert VideoProcessPayload().analysis_mode == "medium_recall"
    for mode in ("high_recall", "medium_recall", "low_recall"):
        assert VideoProcessPayload(analysis_mode=mode).analysis_mode == mode
    assert VideoProcessPayload(exam_focus=True, extension_focus=False).analysis_mode == "strict_exam_intent"
    with pytest.raises(ValueError):
        VideoProcessPayload(analysis_mode="unknown")


def test_processing_options_publish_three_modes_and_serial_chunk_configuration(ai_key_client):
    options = ai_key_client.get("/api/videos/1/processing-options").json()
    assert options["analysis_modes"] == ["high_recall", "medium_recall", "low_recall"]
    assert options["llm_chunk_seconds"] == 600
    assert options["llm_request_concurrency"] == 1
    assert "reasoning_effort" in options


def test_ai_settings_roundtrip_reasoning_effort(ai_key_client, monkeypatch):
    import api.main as api_main

    saved = []
    monkeypatch.setattr(api_main, "save_provider_settings", saved.append)
    payload = ai_key_client.get("/api/settings/ai").json()
    payload.update(reasoning_effort="high")
    assert ai_key_client.put("/api/settings/ai", json=payload).status_code == 200
    assert saved[0].active_profile.reasoning_effort == "high"
