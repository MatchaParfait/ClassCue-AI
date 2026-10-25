"""A published analysis must remain completed when cancellation arrives later."""

from threading import Event

from fastapi.testclient import TestClient

import api.main as api_main
import classcue.video_pipeline as video_pipeline
from classcue.ai_provider import ProviderProfile, ProviderSettings
from classcue.core import TranscriptSegment
from tests.test_legacy_cancellation import ScriptedJudge, make_store, marker_titles, selection_for


def test_api_cancel_after_analysis_commit_keeps_completed_job(tmp_path, monkeypatch):
    store, video_id = make_store(tmp_path, [TranscriptSegment(1, 0, 3000, "这个会考。")])
    mode = "medium_recall"
    store.add_marker(video_id, 0, 3000, "旧考点", "", "", "AI-confirmed", 0.9, mode)
    settings = ProviderSettings(profile=ProviderProfile(
        base_url="https://model.test/v1", model="synthetic",
    ))
    monkeypatch.setattr(api_main, "DIRECTORY_PROJECT_ROOT", tmp_path / "application")
    monkeypatch.setattr(api_main, "selected_model_id", lambda: "whisper-turbo")
    monkeypatch.setattr(api_main, "load_provider_settings", lambda: settings)
    monkeypatch.setattr(api_main, "resolve_interface_key", lambda *_args: "synthetic-key")
    judge = ScriptedJudge(lambda _calls, chunk: selection_for(chunk, "新考点"))
    judge.close = lambda: None
    monkeypatch.setattr(api_main, "create_judge", lambda *_args, **_kwargs: judge)
    app = api_main.create_app(store)
    ready = Event()
    process = api_main.process_saved_analysis_once
    publish = store.publish_semantic_markers
    job_id = None

    def gated_process(*args, **kwargs):
        assert ready.wait(10), "API did not return the analysis job id"
        return process(*args, **kwargs)

    def commit_then_cancel(*args, **kwargs):
        result = publish(*args, **kwargs)
        app.state.jobs.cancel(job_id)
        return result

    monkeypatch.setattr(api_main, "process_saved_analysis_once", gated_process)
    monkeypatch.setattr(store, "publish_semantic_markers", commit_then_cancel)
    with TestClient(app) as client:
        try:
            started = client.post(f"/api/videos/{video_id}/analysis", json={"analysis_mode": mode})
            assert started.status_code == 202, started.text
            job_id = started.json()["job_id"]
            future = app.state.jobs._futures[job_id]
            ready.set()
            future.result(timeout=20)
            job = client.get(f"/api/jobs/{job_id}").json()
            assert marker_titles(store, video_id, mode) == ["新考点"]
            assert store.get_analysis_run(video_id, mode)["status"] == "completed"
            assert job["status"] == job["stage"] == "completed"
            assert job["progress"] == 1.0
        finally:
            ready.set()


def make_process_app(tmp_path, monkeypatch, judge):
    """Real API + JobManager + SQLite publish for /process, with fake ASR and AI."""
    judge.close = lambda: None
    store, video_id = make_store(tmp_path, [TranscriptSegment(1, 0, 3000, "这个会考。")])
    settings = ProviderSettings(profile=ProviderProfile(
        base_url="https://model.test/v1", model="synthetic",
    ))
    monkeypatch.setattr(api_main, "DIRECTORY_PROJECT_ROOT", tmp_path / "application")
    monkeypatch.setattr(api_main, "selected_model_id", lambda: "whisper-turbo")
    monkeypatch.setattr(api_main, "load_provider_settings", lambda: settings)
    monkeypatch.setattr(api_main, "resolve_interface_key", lambda *_args: "synthetic-key")
    monkeypatch.setattr(api_main, "create_judge", lambda *_args, **_kwargs: judge)
    # Fake ASR: subtitles are already persisted, so only the AI stage runs for real.
    monkeypatch.setattr(video_pipeline, "prepare_video_for_analysis", lambda *_a, **_k: ("prepared", 1))
    app = api_main.create_app(store)
    ready = Event()
    real_process = api_main.process_video_once
    state = {"job_id": None}

    def gated_process(*args, **kwargs):
        assert ready.wait(10), "API did not return the process job id"
        return real_process(*args, **kwargs)

    monkeypatch.setattr(api_main, "process_video_once", gated_process)
    return app, store, video_id, ready, state


def test_api_process_cancel_after_final_commit_keeps_completed_job(tmp_path, monkeypatch):
    app, store, video_id, ready, state = make_process_app(
        tmp_path, monkeypatch, ScriptedJudge(lambda _calls, chunk: selection_for(chunk, "新考点")))
    mode = "medium_recall"
    real_publish = store.publish_semantic_markers

    def commit_then_cancel(*args, **kwargs):
        result = real_publish(*args, **kwargs)
        app.state.jobs.cancel(state["job_id"])
        return result

    monkeypatch.setattr(store, "publish_semantic_markers", commit_then_cancel)
    with TestClient(app) as client:
        try:
            started = client.post(f"/api/videos/{video_id}/process", json={"analysis_mode": mode})
            assert started.status_code == 202, started.text
            state["job_id"] = started.json()["job_id"]
            future = app.state.jobs._futures[state["job_id"]]
            ready.set()
            future.result(timeout=20)
            job = client.get(f"/api/jobs/{state['job_id']}").json()
            assert marker_titles(store, video_id, mode) == ["新考点"]
            assert store.get_analysis_run(video_id, mode)["status"] == "completed"
            assert store.get_video(video_id)["status"] == "review_ready"
            assert job["status"] == job["stage"] == "completed"
            assert job["progress"] == 1.0
        finally:
            ready.set()


def test_api_process_cancel_after_first_mode_keeps_it_and_interrupts_job(tmp_path, monkeypatch):
    judge = ScriptedJudge(lambda _calls, chunk: selection_for(chunk, "新考点"))
    app, store, video_id, ready, state = make_process_app(tmp_path, monkeypatch, judge)
    first, second = "medium_recall", "high_recall"
    real_publish = store.publish_semantic_markers

    def commit_first_mode_then_cancel(video_id_arg, analysis_mode, *args, **kwargs):
        result = real_publish(video_id_arg, analysis_mode, *args, **kwargs)
        if analysis_mode == first:
            app.state.jobs.cancel(state["job_id"])
        return result

    monkeypatch.setattr(store, "publish_semantic_markers", commit_first_mode_then_cancel)
    with TestClient(app) as client:
        try:
            started = client.post(
                f"/api/videos/{video_id}/process", json={"analysis_modes": [first, second]})
            assert started.status_code == 202, started.text
            state["job_id"] = started.json()["job_id"]
            future = app.state.jobs._futures[state["job_id"]]
            ready.set()
            future.result(timeout=20)
            job = client.get(f"/api/jobs/{state['job_id']}").json()
            assert job["status"] == job["stage"] == "interrupted"
            assert judge.calls == 1, "the second mode must not start any AI work"
            assert marker_titles(store, video_id, first) == ["新考点"]
            assert store.get_analysis_run(video_id, first)["status"] == "completed"
            assert store.get_analysis_run(video_id, second) is None
            assert store.get_video(video_id)["status"] == "review_ready"
        finally:
            ready.set()


def seed_completed_mode(store, video_id, mode, title):
    store.add_marker(video_id, 0, 3000, title, "", "", "AI-confirmed", 0.9, mode)
    store.set_analysis_run(video_id, mode, "completed", 1)
    store.set_status(video_id, "review_ready")


def test_api_process_mixed_reuse_and_publish_cancel_after_final_commit_completes_job(tmp_path, monkeypatch):
    judge = ScriptedJudge(lambda _calls, chunk: selection_for(chunk, "新考点"))
    app, store, video_id, ready, state = make_process_app(tmp_path, monkeypatch, judge)
    first, second = "medium_recall", "high_recall"
    seed_completed_mode(store, video_id, first, "复用考点")
    real_publish = store.publish_semantic_markers

    def commit_new_mode_then_cancel(video_id_arg, analysis_mode, *args, **kwargs):
        result = real_publish(video_id_arg, analysis_mode, *args, **kwargs)
        if analysis_mode == second:
            app.state.jobs.cancel(state["job_id"])
        return result

    monkeypatch.setattr(store, "publish_semantic_markers", commit_new_mode_then_cancel)
    with TestClient(app) as client:
        try:
            started = client.post(
                f"/api/videos/{video_id}/process", json={"analysis_modes": [first, second]})
            assert started.status_code == 202, started.text
            state["job_id"] = started.json()["job_id"]
            future = app.state.jobs._futures[state["job_id"]]
            ready.set()
            future.result(timeout=20)
            job = client.get(f"/api/jobs/{state['job_id']}").json()
            assert judge.calls == 1, "only the not-yet-completed mode may start AI work"
            assert marker_titles(store, video_id, first) == ["复用考点"]
            assert marker_titles(store, video_id, second) == ["新考点"]
            assert store.get_analysis_run(video_id, first)["status"] == "completed"
            assert store.get_analysis_run(video_id, second)["status"] == "completed"
            assert store.get_video(video_id)["status"] == "review_ready"
            assert job["status"] == job["stage"] == "completed"
            assert job["progress"] == 1.0
        finally:
            ready.set()


def test_api_process_pure_reuse_cancel_keeps_job_interruptible(tmp_path, monkeypatch):
    judge = ScriptedJudge(lambda _calls, chunk: selection_for(chunk, "新考点"))
    app, store, video_id, ready, state = make_process_app(tmp_path, monkeypatch, judge)
    first, second = "medium_recall", "high_recall"
    seed_completed_mode(store, video_id, first, "复用考点一")
    seed_completed_mode(store, video_id, second, "复用考点二")
    real_set_video_folder = store.set_video_folder

    def set_folder_then_cancel(*args, **kwargs):
        result = real_set_video_folder(*args, **kwargs)
        app.state.jobs.cancel(state["job_id"])
        return result

    monkeypatch.setattr(store, "set_video_folder", set_folder_then_cancel)
    with TestClient(app) as client:
        try:
            started = client.post(
                f"/api/videos/{video_id}/process", json={"analysis_modes": [first, second]})
            assert started.status_code == 202, started.text
            state["job_id"] = started.json()["job_id"]
            future = app.state.jobs._futures[state["job_id"]]
            ready.set()
            future.result(timeout=20)
            job = client.get(f"/api/jobs/{state['job_id']}").json()
            assert job["status"] == job["stage"] == "interrupted"
            assert judge.calls == 0, "pure reuse must not start any AI work"
            assert marker_titles(store, video_id, first) == ["复用考点一"]
            assert marker_titles(store, video_id, second) == ["复用考点二"]
            assert store.get_analysis_run(video_id, first)["status"] == "completed"
            assert store.get_analysis_run(video_id, second)["status"] == "completed"
            assert store.get_video(video_id)["status"] == "review_ready"
        finally:
            ready.set()


def test_api_process_cancel_after_new_publish_before_reused_mode_interrupts_job(tmp_path, monkeypatch):
    judge = ScriptedJudge(lambda _calls, chunk: selection_for(chunk, "新考点"))
    app, store, video_id, ready, state = make_process_app(tmp_path, monkeypatch, judge)
    first, second = "medium_recall", "high_recall"
    seed_completed_mode(store, video_id, second, "复用考点")
    real_publish = store.publish_semantic_markers

    def commit_first_mode_then_cancel(video_id_arg, analysis_mode, *args, **kwargs):
        result = real_publish(video_id_arg, analysis_mode, *args, **kwargs)
        if analysis_mode == first:
            app.state.jobs.cancel(state["job_id"])
        return result

    monkeypatch.setattr(store, "publish_semantic_markers", commit_first_mode_then_cancel)
    with TestClient(app) as client:
        try:
            started = client.post(
                f"/api/videos/{video_id}/process", json={"analysis_modes": [first, second]})
            assert started.status_code == 202, started.text
            state["job_id"] = started.json()["job_id"]
            future = app.state.jobs._futures[state["job_id"]]
            ready.set()
            future.result(timeout=20)
            job = client.get(f"/api/jobs/{state['job_id']}").json()
            assert job["status"] == job["stage"] == "interrupted"
            assert judge.calls == 1, "the reused mode must not start any AI work"
            assert marker_titles(store, video_id, first) == ["新考点"]
            assert marker_titles(store, video_id, second) == ["复用考点"]
            assert store.get_analysis_run(video_id, first)["status"] == "completed"
            assert store.get_analysis_run(video_id, second)["status"] == "completed"
            assert store.get_video(video_id)["status"] == "review_ready"
        finally:
            ready.set()


def test_api_process_failure_after_reused_mode_fails_job_and_keeps_reused_results(tmp_path, monkeypatch):
    def fail_every_chunk(_calls, _chunk):
        raise ConnectionError("fixture offline")

    judge = ScriptedJudge(fail_every_chunk)
    app, store, video_id, ready, state = make_process_app(tmp_path, monkeypatch, judge)
    first, second = "medium_recall", "high_recall"
    seed_completed_mode(store, video_id, first, "复用考点")
    with TestClient(app) as client:
        try:
            started = client.post(
                f"/api/videos/{video_id}/process", json={"analysis_modes": [first, second]})
            assert started.status_code == 202, started.text
            state["job_id"] = started.json()["job_id"]
            future = app.state.jobs._futures[state["job_id"]]
            ready.set()
            future.result(timeout=20)
            job = client.get(f"/api/jobs/{state['job_id']}").json()
            assert job["status"] == job["stage"] == "failed"
            assert marker_titles(store, video_id, first) == ["复用考点"]
            assert marker_titles(store, video_id, second) == []
            assert store.get_analysis_run(video_id, first)["status"] == "completed"
            assert store.get_analysis_run(video_id, second)["status"] == "failed"
            assert store.get_video(video_id)["status"] == "failed"
        finally:
            ready.set()
