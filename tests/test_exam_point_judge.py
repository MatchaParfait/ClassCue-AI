import json
import time
from types import SimpleNamespace

import pytest

from ai_provider import ProviderProfile, create_judge
from core import TranscriptSegment
from exam_point_judge import ExamPointJudge
from processing_control import ProcessingCancelled
from video_pipeline import process_saved_analysis_once, process_video_once
from video_store import VideoStore
from api_failure import ApiUnavailable


class FakeClient:
    max_retries = 0

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **request):
        self.requests.append(request)
        item = self.outcomes.pop(0)
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(
            id="test-response", model="test-model", usage=None,
            choices=[SimpleNamespace(
                finish_reason="stop", message=SimpleNamespace(
                    content=item if isinstance(item, str) else json.dumps(item, ensure_ascii=False),
                ),
            )],
        )


def candidate(**changes):
    return {
        "level": "confirmed", "signal_type": "explicit_exam",
        "start_sentence_id": 1, "end_sentence_id": 1,
        "evidence_sentence_ids": [1], "title": "排序",
        "reason": "教师明确提示考试", "evidence_quote": "排序和查找会考，必须掌握。",
        **changes,
    }


@pytest.fixture
def saved(tmp_path):
    path = tmp_path / "lesson.mp4"
    path.touch()
    store = VideoStore(tmp_path / "video.db")
    video_id = store.create_video("hash", path.name, str(path), str(tmp_path / "audio.wav"), 3600000)
    store.mark_transcribed(video_id, [
        TranscriptSegment(10, 10000, 11000, "排序和查找"),
        TranscriptSegment(11, 11000, 15000, "会考，必须掌握。"),
    ], "fireredasr2-aed")
    profile = ProviderProfile(
        base_url="https://not-called.example/v1", model="test-model",
        max_tokens=4096, max_retries=0,
    )
    return store, video_id, profile


def judge(profile, outcomes, mode="medium_recall"):
    client = FakeClient(outcomes)
    return create_judge(profile, "secret", mode=mode, client=client), client


def test_api_failure_type_survives_secret_redaction(saved):
    adapter, _client = judge(saved[2], [ConnectionError("secret disconnected")])
    with pytest.raises(ApiUnavailable) as result:
        run(saved, adapter)
    assert "secret" not in str(result.value)


@pytest.mark.parametrize("cached", [False, True])
def test_saved_production_analysis_restores_failed_video_status(saved, cached):
    store, video_id, profile = saved
    adapter, client = judge(profile, [{"candidates": []}])
    if cached:
        assert run(saved, adapter) == ("completed", 0)
    store.set_status(video_id, "failed")

    assert run(saved, adapter) == ("skipped" if cached else "completed", 0)
    assert store.get_video(video_id)["status"] == "review_ready"
    assert len(client.requests) == 1


def run(saved, adapter, **kwargs):
    store, video_id, _ = saved
    return process_saved_analysis_once(
        store, video_id, adapter, adapter.mode, filter_formula_noise=False, **kwargs,
    )


@pytest.mark.parametrize("mode", ["high_recall", "medium_recall", "low_recall"])
def test_modes_use_selected_frozen_bundle_and_logged_b_pipeline(saved, mode):
    store, video_id, profile = saved
    adapter, client = judge(profile, [{"candidates": [candidate()]}], mode)
    assert isinstance(adapter, ExamPointJudge)
    assert run(saved, adapter) == ("completed", 1)
    marker = store.list_markers(video_id, mode)[0]
    assert (marker["start_ms"], marker["end_ms"]) == (10000, 15000)
    assert marker["title"] == "排序"
    request = client.requests[0]
    assert request["reasoning_effort"] == "high"
    assert "{1} 排序和查找 会考，必须掌握。" in request["messages"][1]["content"]
    assert "source_sentence_ids" not in request["messages"][1]["content"]
    assert "start_ms" not in request["messages"][1]["content"]
    assert "confidence" not in request["messages"][1]["content"]
    row = store.get_analysis_run(video_id, mode)
    log = store.get_experiment(row["experiment_id"])
    snapshot = json.loads(log["options_json"])
    assert snapshot["config"]["chunk_seconds"] == 600
    assert snapshot["config"]["scheme"] == "few_shot"
    assert snapshot["prompt_bundle"]["version"] == adapter.bundle["version"]
    assert snapshot["authorization"]["kind"] == "production_processing_request"
    assert len(store.list_experiment_calls(row["experiment_id"])) == 1
    assert len(store.get_segments(video_id)) == 2


def test_backend_matches_spacing_and_preserves_two_objects_at_same_time(saved):
    store, video_id, profile = saved
    adapter, client = judge(profile, [{"candidates": [candidate(), candidate(title="查找")]}])
    assert run(saved, adapter) == ("completed", 2)
    assert {m["title"] for m in store.list_markers(video_id, adapter.mode)} == {"排序", "查找"}
    assert len(client.requests) == 1


def test_signature_cache_reuses_only_identical_config_and_keeps_history(saved):
    store, video_id, profile = saved
    adapter, client = judge(profile, [{"candidates": []}, {"candidates": []}, {"candidates": []}])
    assert run(saved, adapter) == ("completed", 0)
    assert run(saved, adapter) == ("skipped", 0)
    adapter.profile = profile.model_copy(update={"reasoning_effort": "low"})
    assert run(saved, adapter) == ("completed", 0)
    assert run(saved, adapter, force=True) == ("completed", 0)
    assert len(client.requests) == 3
    assert len(store.list_experiments(video_id)) == 3


def test_other_modes_and_historical_markers_are_not_overwritten(saved):
    store, video_id, profile = saved
    store.add_marker(video_id, 1, 2, "旧版", "", "", "AI-confirmed", .8, "strict_exam_intent")
    for mode in ("high_recall", "medium_recall", "low_recall"):
        adapter, _ = judge(profile, [{"candidates": [candidate()]}], mode)
        run(saved, adapter)
    assert len(store.list_markers(video_id)) == 4


def test_success_preserves_review_status_and_format_repair_logs(saved):
    store, video_id, profile = saved
    adapter, client = judge(profile, [
        {"candidates": [candidate()]}, "broken JSON", {"candidates": [candidate()]},
    ])
    run(saved, adapter)
    marker = store.list_markers(video_id, adapter.mode)[0]
    store.set_marker_review_status(marker["id"], "mastered")
    run(saved, adapter, force=True)
    assert store.list_markers(video_id, adapter.mode)[0]["review_status"] == "mastered"
    row = store.get_analysis_run(video_id, adapter.mode)
    report = store.get_experiment_results(row["experiment_id"])
    assert report["format_repair_count"] == 1
    assert report["format_metrics"]["first_pass_invalid_count"] == 1
    calls = store.list_experiment_calls(row["experiment_id"])
    assert [call["stage"] for call in calls] == ["extract", "json_repair"]
    assert calls[0]["raw_content"] == "broken JSON"
    assert "format_errors_json" in client.requests[-1]["messages"][1]["content"]


@pytest.mark.parametrize("outcomes", [
    [ConnectionError("offline")],
    ["broken", "still broken"],
])
def test_failed_run_preserves_previous_markers_and_logs(saved, outcomes):
    store, video_id, profile = saved
    adapter, _ = judge(profile, [{"candidates": [candidate()]}])
    run(saved, adapter)
    before = dict(store.list_markers(video_id, adapter.mode)[0])
    adapter, client = judge(profile, outcomes)
    with pytest.raises((ValueError, ConnectionError)):
        run(saved, adapter, force=True)
    assert dict(store.list_markers(video_id, adapter.mode)[0]) == before
    assert len(store.list_experiments(video_id)) == 2
    assert len(client.requests) == len(outcomes)


def test_no_chunks_after_filtering_is_logged_success_without_api(saved):
    store, video_id, profile = saved
    adapter, client = judge(profile, [])
    assert run(saved, adapter, filter_formula="排序和查找,会考") == ("completed", 0)
    assert not client.requests
    assert len(store.get_segments(video_id)) == 2
    assert len(store.list_experiments(video_id)) == 1


def test_processing_reuses_asr_and_does_not_skip_legacy_completed_mode(saved, monkeypatch):
    store, video_id, profile = saved
    store.set_status(video_id, "review_ready")
    store.set_analysis_run(video_id, "high_recall", "completed", 0)
    monkeypatch.setattr("video_pipeline._transcribe_video_with_fallback", lambda *a, **k: pytest.fail("ASR rerun"))
    adapter, client = judge(profile, [{"candidates": []}], "high_recall")
    assert process_video_once(
        store, video_id, adapter, analysis_mode="high_recall", filter_formula_noise=False,
    ) == ("completed", 0)
    assert len(client.requests) == 1
    assert process_video_once(
        store, video_id, adapter, analysis_mode="high_recall", filter_formula_noise=False,
    ) == ("skipped", 0)


def test_cancel_after_response_retains_previous_markers(saved):
    store, video_id, profile = saved
    adapter, _ = judge(profile, [{"candidates": [candidate()]}])
    run(saved, adapter)
    adapter, client = judge(profile, [{"candidates": []}])
    with pytest.raises(ProcessingCancelled):
        run(saved, adapter, force=True, should_stop=lambda: bool(client.requests))
    assert len(store.list_markers(video_id, adapter.mode)) == 1
    assert any(row["status"] == "interrupted" for row in store.list_experiments(video_id))


def test_unresolved_evidence_is_not_silently_promoted(saved):
    store, video_id, profile = saved
    adapter, _ = judge(profile, [{"candidates": [candidate()]}])
    run(saved, adapter)
    invalid = candidate(evidence_quote="老师说所有知识都必考")
    adapter, client = judge(profile, [{"candidates": [invalid]}] * 3)
    with pytest.raises(ValueError, match="旧结果已保留"):
        run(saved, adapter, force=True)
    assert len(client.requests) == 3
    record = next(row for row in store.list_experiments(video_id) if row["status"] == "completed_with_validation_errors")
    report = store.get_experiment_results(record["id"])
    assert report["citation_metrics"]["evidence_repair_count"] == 2
    assert report["citation_metrics"]["evidence_limit_reached_count"] == 1
    assert report["citation_metrics"]["unresolved_count"] == 1
    assert store.list_markers(video_id, adapter.mode)[0]["source_quote"] != invalid["evidence_quote"]


def test_prompt_changes_invalidate_production_cache(saved):
    _, _, profile = saved
    adapter, client = judge(profile, [{"candidates": []}, {"candidates": []}])
    run(saved, adapter)
    adapter.bundle = {**adapter.bundle, "task": adapter.bundle["task"] + "\n新版本"}
    assert run(saved, adapter) == ("completed", 0)
    assert len(client.requests) == 2


def test_api_jobs_publish_selected_mode_and_reuse_saved_asr(saved, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import api.main as api_main
    from ai_provider import ProviderSettings

    store, video_id, profile = saved
    client = FakeClient([{"candidates": [candidate()]}] * 3)
    monkeypatch.setattr(api_main, "load_provider_settings", lambda: ProviderSettings(profile=profile))
    monkeypatch.setattr(api_main, "resolve_interface_key", lambda *a: "secret")
    monkeypatch.setattr(api_main, "create_judge", lambda p, key, provider, mode: create_judge(
        p, key, mode=mode, client=client,
    ))
    monkeypatch.setattr("video_pipeline._transcribe_video_with_fallback", lambda *a, **k: pytest.fail("ASR rerun"))
    with TestClient(api_main.create_app(store)) as api:
        for mode in ("high_recall", "medium_recall", "low_recall", "medium_recall"):
            submitted = api.post(f"/api/videos/{video_id}/process", json={
                "analysis_mode": mode, "filter_formula_noise": False,
            })
            assert submitted.status_code == 202
            job_id = submitted.json()["job_id"]
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                job = api.get(f"/api/jobs/{job_id}").json()
                if job["status"] in {"completed", "failed", "interrupted"}:
                    break
                time.sleep(.01)
            assert job["status"] == "completed", job
            markers = api.get(f"/api/videos/{video_id}/markers", params={"mode": mode}).json()
            assert len(markers) == 1
            assert markers[0]["analysis_mode"] == mode
        runs = api.get(f"/api/videos/{video_id}/analysis-runs").json()
        assert len(runs) == 3
        assert all(row["experiment_id"] and row["analysis_signature"] for row in runs)
    assert len(client.requests) == 3
    assert len(store.list_experiments(video_id)) == 3
