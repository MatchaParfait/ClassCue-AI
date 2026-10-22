from threading import Event
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from api.main import VideoProcessPayload
from classcue.core import SemanticSelection, TranscriptSegment
from classcue.video_pipeline import build_screening_chunks, create_semantic_markers
from classcue.video_store import VideoStore


@pytest.mark.parametrize("exam, extension, mode", [
    (True, True, "high_recall"),
    (True, False, "strict_exam_intent"),
    (False, True, "high_recall"),
])
def test_ui_choices_override_conflicting_legacy_mode(exam, extension, mode):
    options = VideoProcessPayload(
        exam_focus=exam, extension_focus=extension,
        analysis_mode="strict_exam_intent" if mode == "high_recall" else "high_recall",
    )
    assert options.analysis_mode == mode


def test_legacy_mode_without_ui_flags_is_preserved():
    assert VideoProcessPayload(analysis_mode="strict_exam_intent").analysis_mode == "strict_exam_intent"


def test_keywords_are_normalized_and_enable_prefiltering():
    options = VideoProcessPayload(keywords=[" KKT ", "KKT", "  ", "最优解"])
    assert options.keywords == ["KKT", "最优解"]
    assert options.use_keyword_prefilter is True


def test_formula_filter_uses_literals_and_preserves_original_subtitles():
    segments = [
        TranscriptSegment(1, 0, 1000, "解释 x + y 的含义。"),
        TranscriptSegment(2, 1000, 2000, "其他教学内容。"),
        TranscriptSegment(3, 2000, 3000, "解释 A+B 的含义。"),
    ]
    chunks = build_screening_chunks(
        segments, filter_formula_noise=False, filter_formula="x+y，a+b",
    )
    assert [segment.id for chunk in chunks for segment in chunk] == [2]
    assert len(segments) == 3
    literal = build_screening_chunks(
        segments, filter_formula_noise=False, filter_formula=".*",
    )
    assert {segment.id for chunk in literal for segment in chunk} == {1, 2, 3}


def test_formula_filter_is_forwarded_to_ai_and_does_not_replace_stored_subtitles(tmp_path):
    store = VideoStore(tmp_path / "video.db")
    video_id = store.create_video("hash", "lesson.mp4", "lesson.mp4", "lesson.wav", 3000)
    segments = [
        TranscriptSegment(1, 0, 1000, "x+y"),
        TranscriptSegment(2, 1000, 2000, "课程重点"),
    ]
    store.mark_transcribed(video_id, segments)
    seen = []

    class Judge:
        def judge_semantic_chunk(self, chunk):
            seen.extend(segment.id for segment in chunk)
            return SemanticSelection()

    create_semantic_markers(
        store, video_id, segments, Judge(), filter_formula_noise=False, filter_formula="x+y",
    )
    assert seen == [2]
    assert [segment.text for segment in store.get_segments(video_id)] == ["x+y", "课程重点"]


def test_api_passes_keyword_formula_and_mode_to_processing_pipeline(tmp_path, monkeypatch):
    store = VideoStore(tmp_path / "video.db")
    video_id = store.create_video("hash", "lesson.mp4", "lesson.mp4", "lesson.wav", 3000)
    settings = SimpleNamespace(active_profile=api_main.ProviderProfile())
    monkeypatch.setattr(api_main, "load_provider_settings", lambda: settings)
    monkeypatch.setattr(api_main, "resolve_interface_key", lambda *_args: "fixture-key")
    monkeypatch.setattr(api_main, "interface_provider", lambda *_args: "fixture-provider")
    captured = {}
    processed = Event()

    def create_judge(_profile, _key, _provider, *, mode):
        captured["judge_mode"] = mode
        return SimpleNamespace(close=lambda: None)

    def process(_store, _video_id, _judge, **kwargs):
        captured.update(kwargs)
        processed.set()
        return "completed", 0

    monkeypatch.setattr(api_main, "create_judge", create_judge)
    monkeypatch.setattr(api_main, "process_video_once", process)
    with TestClient(api_main.create_app(store)) as client:
        response = client.post(f"/api/videos/{video_id}/process", json={
            "exam_focus": True, "extension_focus": True,
            "keywords": [" KKT ", "KKT"], "filter_formula": "x+y",
            "filter_formula_noise": False,
            "force_retranscribe": True, "asr_model_id": "fireredasr2-aed",
        })
        assert response.status_code == 202
        assert processed.wait(timeout=3)
        assert captured["judge_mode"] == "high_recall"
        assert captured["analysis_mode"] == "high_recall"
        assert captured["trigger_groups"] == {"custom": {"用户关键词": ["KKT"]}}
        assert captured["use_keyword_prefilter"] is True
        assert captured["filter_formula"] == "x+y"
        assert captured["filter_formula_noise"] is False
        assert captured["force_retranscribe"] is True
        assert captured["asr_model_id"] == "fireredasr2-aed"


def test_api_rejects_disabling_all_analysis_choices(tmp_path):
    store = VideoStore(tmp_path / "video.db")
    video_id = store.create_video("hash", "lesson.mp4", "lesson.mp4", "lesson.wav", 3000)
    with TestClient(api_main.create_app(store)) as client:
        response = client.post(f"/api/videos/{video_id}/process", json={
            "exam_focus": False, "extension_focus": False,
        })
        assert response.status_code == 422
        assert not store.list_active_jobs()


def test_processing_options_exposes_saved_api_concurrency(tmp_path, monkeypatch):
    store = VideoStore(tmp_path / "video.db")
    video_id = store.create_video("fixture", "lesson.mp4", "lesson.mp4", "audio.wav", 1000)
    settings = SimpleNamespace(active_profile=api_main.ProviderProfile(request_concurrency=4))
    monkeypatch.setattr(api_main, "load_provider_settings", lambda: settings)
    monkeypatch.setattr(api_main, "resolve_interface_key", lambda *_args: None)
    with TestClient(api_main.create_app(store)) as client:
        response = client.get(f"/api/videos/{video_id}/processing-options")
        assert response.status_code == 200, response.text
        assert response.json()["llm_request_concurrency"] == 4


def test_single_job_snapshots_connection_before_its_worker_starts(tmp_path, monkeypatch):
    store = VideoStore(tmp_path / "video.db")
    video_id = store.create_video("hash", "lesson.mp4", "lesson.mp4", "lesson.wav", 3000)
    settings = SimpleNamespace(active_profile=api_main.ProviderProfile(
        base_url="https://first.test/v1", model="original", request_concurrency=4,
    ))
    monkeypatch.setattr(api_main, "load_provider_settings", lambda: settings)
    monkeypatch.setattr(api_main, "resolve_interface_key", lambda profile, _: f"{profile.model}-key")
    monkeypatch.setattr(api_main, "require_selectable_model", lambda *_: None)
    captured = []

    def create_judge(profile, key, *_args, **_kwargs):
        captured.append((profile.model, key, profile.request_concurrency))
        return SimpleNamespace(close=lambda: None)

    monkeypatch.setattr(api_main, "create_judge", create_judge)
    monkeypatch.setattr(api_main, "process_video_once", lambda *_args, **_kwargs: ("completed", 0))
    application = api_main.create_app(store)
    gate = Event()
    with TestClient(application) as client:
        application.state.jobs.executor.submit(lambda: gate.wait(timeout=5))
        try:
            response = client.post(f"/api/videos/{video_id}/process", json={})
            assert response.status_code == 202
            settings.active_profile.model = "edited-in-place"
            settings.active_profile.request_concurrency = 16
            settings.active_profile = api_main.ProviderProfile(base_url="https://second.test/v1", model="new")
        finally:
            gate.set()
        application.state.jobs.executor.submit(lambda: None).result(timeout=5)
    assert captured == [("original", "original-key", 4)]
