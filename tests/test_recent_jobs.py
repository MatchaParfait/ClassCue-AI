from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from classcue.video_store import VideoStore


@pytest.fixture
def recent_jobs(tmp_path):
    store = VideoStore(tmp_path / "jobs.db")
    video_id = store.create_video("synthetic", "lesson.mp4", "lesson.mp4", "lesson.wav", 1000)
    store.update_video_display_name(video_id, "合成课程")
    application = create_app(store)
    client = TestClient(application)
    yield client, store, video_id
    client.close()
    application.state.jobs.shutdown(wait=True)


def add_job(store, job_id, job_type, *, created, finished=None, status="completed", video_id=None):
    store.create_job(job_id, job_type, video_id)
    store.update_job(job_id, status=status, finished_at=finished)
    with store.connect() as connection:
        connection.execute(
            "UPDATE processing_jobs SET created_at = ? WHERE id = ?", (created, job_id),
        )


def test_recent_jobs_prioritize_active_then_completion_time_across_job_types(recent_jobs):
    client, store, video_id = recent_jobs
    add_job(store, "old-batch", "batch_video_process",
            created="2026-10-20 12:00:00", finished="2026-10-20T13:00:00+00:00")
    add_job(store, "video", "video_process", video_id=video_id,
            created="2026-10-19 12:00:00", finished="2026-10-26T12:00:00+00:00")
    add_job(store, "slides", "slide_extract", video_id=video_id,
            created="2026-10-26 11:00:00", finished="2026-10-26T20:30:00+08:00")
    add_job(store, "model", "asr_prepare",
            created="2026-10-26 12:00:00", finished="2026-10-26T12:15:00+00:00")
    add_job(store, "analysis", "video_analysis", video_id=video_id,
            created="2026-10-26 11:30:00", status="running")

    response = client.get("/api/jobs/recent")

    assert response.status_code == 200
    jobs = response.json()
    assert [job["id"] for job in jobs] == ["analysis", "slides", "model", "video", "old-batch"]
    assert jobs[0]["video_name"] == "合成课程"
    assert jobs[1]["video_name"] == "合成课程"
    assert jobs[2]["video_name"] is None
    assert jobs[3]["finished_at"] == "2026-10-26T12:00:00+00:00"
    assert client.get("/api/jobs/video").json()["video_name"] == "合成课程"


def test_recent_jobs_use_start_or_creation_for_older_incomplete_timestamps(recent_jobs):
    client, store, _ = recent_jobs
    add_job(store, "created-fallback", "video_process", created="2026-10-23 14:00:00")
    add_job(store, "started-fallback", "video_analysis", created="2026-10-20 14:00:00")
    store.update_job("started-fallback", started_at="2026-10-24T14:00:00+00:00")
    add_job(store, "queued", "asr_prepare", created="2026-10-20 10:00:00", status="queued")
    add_job(store, "failed", "slide_extract",
            created="2026-10-20 14:00:00", finished="2026-10-25 14:00:00", status="failed")
    add_job(store, "paused", "video_process",
            created="2026-10-20 14:00:00", finished="2026-10-25T14:30:00+00:00",
            status="resource_paused")

    jobs = client.get("/api/jobs/recent").json()

    assert [job["id"] for job in jobs] == [
        "queued", "paused", "failed", "started-fallback", "created-fallback",
    ]


def test_recent_jobs_limit_and_stable_ties(recent_jobs):
    client, store, _ = recent_jobs
    for index in range(25):
        add_job(store, f"job-{index}", "asr_prepare", created="2026-10-26 12:00:00",
                finished="2026-10-26T13:00:00+00:00")

    jobs = client.get("/api/jobs/recent").json()
    assert len(jobs) == 20
    assert [job["id"] for job in jobs[:2]] == ["job-24", "job-23"]
    assert [job["id"] for job in client.get("/api/jobs/recent?limit=1").json()] == ["job-24"]
    assert len(client.get("/api/jobs/recent?limit=100").json()) == 25
    assert client.get("/api/jobs/recent?limit=0").status_code == 422
    assert client.get("/api/jobs/recent?limit=101").status_code == 422


def test_recent_jobs_preserve_legacy_batch_details_without_writes(recent_jobs, monkeypatch):
    client, store, video_id = recent_jobs
    add_job(store, "legacy", "batch_video_process", created="2026-10-26 12:00:00")
    store.update_job("legacy", request_json=json.dumps({
        "kind": "batch", "items": [[video_id, "old-lesson.mp4"]],
        "options": {"analysis_mode": "question_extraction", "api_key": "synthetic-private-key"},
    }))
    before = dict(store.get_job("legacy"))
    monkeypatch.setattr(store, "update_job", lambda *args, **kwargs: pytest.fail("read must not write"))

    response = client.get("/api/jobs/recent")

    assert response.status_code == 200
    payload = response.json()[0]
    assert payload["analysis_modes"] == ["question_extraction"]
    assert payload["batch_summary"]["total"] == 1
    assert payload["batch_entries"][0]["video_id"] == video_id
    assert "synthetic-private-key" not in response.text
    assert dict(store.get_job("legacy")) == before


def test_recent_jobs_empty_and_missing_video_name(recent_jobs):
    client, store, video_id = recent_jobs
    assert client.get("/api/jobs/recent").json() == []
    add_job(store, "video", "video_transcribe", created="2026-10-26 12:00:00", video_id=video_id)
    with store.connect() as connection:
        connection.execute("UPDATE videos SET display_name = '' WHERE id = ?", (video_id,))
    assert client.get("/api/jobs/recent").json()[0]["video_name"] == "lesson.mp4"
    with store.connect() as connection:
        connection.execute("DELETE FROM videos WHERE id = ?", (video_id,))
    payload = client.get("/api/jobs/recent").json()[0]
    assert payload["video_name"] is None
    assert payload["video_id"] is None
