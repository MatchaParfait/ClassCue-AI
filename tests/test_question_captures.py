import pytest
import asyncio
from threading import Event, Thread

from api.main import create_app
from fastapi.testclient import TestClient
from classcue.question_analysis import MAX_IMAGE_BYTES, QuestionMaterial
from classcue.question_captures import QuestionCaptures
from classcue.question_service import load_slide_images
from classcue.question_store import QuestionStore
from tests.test_question_analysis import png, questions, completed
from tests.test_question_api import api


def upload(client, marker, data=None, time=1250, mime="image/png"):
    return client.post(f"/api/markers/{marker}/question/captures?captured_at_ms={time}",
                       content=png() if data is None else data, headers={"Content-Type": mime})


def test_capture_waiting_for_task_lock_keeps_health_responsive(api):
    import httpx

    client, repo, marker, _, model = api
    locked, health_finished = Event(), Event()
    observed = []

    def hold():
        with client.app.state.jobs._lock:
            locked.set()
            observed.append(health_finished.wait(3))

    worker = Thread(target=hold)
    worker.start()
    assert locked.wait(3)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app),
                                    base_url="http://127.0.0.1") as requests:
            async def health():
                await asyncio.sleep(.05)
                try:
                    return await requests.get("/api/health")
                finally:
                    health_finished.set()

            return await asyncio.gather(
                requests.post(f"/api/markers/{marker}/question/captures", params={"captured_at_ms": 0},
                              content=png(), headers={"Content-Type": "image/png"}),
                health(),
            )

    try:
        capture, health = asyncio.run(exercise())
    finally:
        health_finished.set()
        worker.join(5)
    assert capture.status_code == 201
    assert health.status_code == 200
    assert observed == [True]
    assert len(QuestionCaptures(repo.store).list(marker)) == 1
    assert model.calls == 0


def test_capture_rechecks_task_state_after_receiving_body(api):
    import httpx

    client, repo, marker, video, model = api
    data = png()

    async def body():
        yield data[:10]
        repo.store.create_job("started-during-capture", "video_process", video)
        repo.store.update_job("started-during-capture", status="running")
        yield data[10:]

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app),
                                    base_url="http://127.0.0.1") as requests:
            return await requests.post(f"/api/markers/{marker}/question/captures", params={"captured_at_ms": 0},
                                       content=body(), headers={"Content-Type": "image/png"})

    assert asyncio.run(exercise()).status_code == 409
    assert QuestionCaptures(repo.store).list(marker) == []
    assert model.calls == 0


def test_upload_is_local_scoped_timestamped_and_not_selected_until_save(api):
    client, repo, marker, video, model = api
    response = upload(client, marker)
    assert response.status_code == 201
    capture = response.json()
    assert capture["captured_at_ms"] == 1250 and capture["video_id"] == video
    assert (capture["width"], capture["height"]) == (100, 60)
    assert model.calls == 0
    state = client.get(f"/api/markers/{marker}/question").json()
    assert state["material"]["capture_ids"] == []
    assert state["captures"][0]["id"] == capture["id"]
    assert "image_path" not in capture
    image = client.get(f"/api/markers/{marker}/question/captures/{capture['id']}")
    assert image.status_code == 200 and image.content == png()
    assert image.headers["cache-control"] == "private, no-store"
    assert upload(client, marker).json()["id"] == capture["id"]


def test_capture_becomes_actual_model_image_and_invalidates_old_analysis(questions, tmp_path):
    repo, marker, _ = questions
    completed(questions)
    captures = QuestionCaptures(repo.store)
    capture = captures.add(marker, 1500, png())
    repo.save_material(marker, QuestionMaterial(capture_ids=[capture["id"]]))
    assert repo.runs(marker)[0]["stale"]
    snapshot = repo.snapshot(marker)
    assert snapshot["question"]["images"][0]["source"] == f"capture:{capture['id']}"
    assert snapshot["question"]["images"][0]["captured_at_ms"] == 1500
    images = load_slide_images(repo.store, snapshot, tmp_path / "unused-slides")
    assert images[0].data == png()
    assert images[0].content()[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert repo.search_projection(marker, snapshot["classroom"])["status"] == "needs_analysis"


def test_cross_marker_capture_is_never_read_selected_or_deleted(api):
    client, repo, marker, video, _ = api
    capture = upload(client, marker).json()
    other = repo.store.add_marker(video, 6000, 9000, "其他考点", "", "", "重点", .8)
    assert other != marker
    url = f"/api/markers/{other}/question"
    assert client.get(f"{url}/captures/{capture['id']}").status_code == 404
    assert client.delete(f"{url}/captures/{capture['id']}").status_code == 404
    assert client.put(url, json={"capture_ids": [capture["id"]]}).status_code == 400


@pytest.mark.parametrize("time,content,mime,status", [
    (-1, b"x", "image/png", 422), (10001, None, "image/png", 400),
    (0, b"broken", "image/png", 400), (0, b"", "image/png", 400),
    (0, b"x", "text/plain", 415), ("nan", None, "image/png", 422),
])
def test_bad_uploads_rejected_without_writing_images(api, time, content, mime, status):
    client, repo, marker, _, model = api
    assert upload(client, marker, content, time, mime).status_code == status
    assert QuestionCaptures(repo.store).list(marker) == []
    assert model.calls == 0


def test_oversized_body_and_combined_selection_limit(api):
    client, _, marker, _, _ = api
    assert upload(client, marker, b"x" * (MAX_IMAGE_BYTES + 1)).status_code == 413
    response = client.put(f"/api/markers/{marker}/question", json={"slide_ids": [1, 2, 3, 4], "capture_ids": ["a" * 32]})
    assert response.status_code == 422
    assert client.put(f"/api/markers/{marker}/question", json={"capture_ids": ["a" * 32] * 2}).status_code == 422


def test_capture_selection_survives_reopen_and_delete_cleans_file(api):
    client, repo, marker, _, _ = api
    capture = upload(client, marker).json()
    url = f"/api/markers/{marker}/question"
    assert client.put(url, json={"capture_ids": [capture["id"]]}).status_code == 200
    reopened = QuestionStore(type(repo.store)(repo.store.path))
    assert reopened.material(marker)["capture_ids"] == [capture["id"]]
    path = QuestionCaptures(repo.store).path(capture["id"])
    assert path.is_file()
    assert client.delete(f"{url}/captures/{capture['id']}").status_code == 200
    assert not path.exists() and repo.material(marker)["capture_ids"] == []


def test_missing_or_changed_capture_does_not_reach_model(api):
    client, repo, marker, _, model = api
    capture = upload(client, marker).json()
    repo.save_material(marker, QuestionMaterial(capture_ids=[capture["id"]]))
    path = QuestionCaptures(repo.store).path(capture["id"])
    path.unlink()
    assert client.post(f"/api/markers/{marker}/question/analyze").status_code == 400
    assert model.calls == 0


def test_marker_cascade_queues_cleanup_and_startup_removes_file(questions):
    repo, marker, _ = questions
    captures = QuestionCaptures(repo.store)
    capture = captures.add(marker, 0, png())
    path = captures.path(capture["id"])
    repo.store.delete_marker(marker)
    assert captures.list(marker) == []
    with TestClient(create_app(repo.store)):
        assert not path.exists()


def test_video_deletion_cleans_screenshot_files(questions, tmp_path):
    repo, marker, video = questions
    captures = QuestionCaptures(repo.store)
    capture = captures.add(marker, 0, png())
    repo.store.trash_video(video, tmp_path / "trash")
    assert not captures.path(capture["id"]).exists()


def test_library_scope_preserves_capture_location_after_database_move(questions, tmp_path, monkeypatch):
    import shutil
    from classcue.video_store import VideoStore
    repo, marker, _ = questions
    captures = QuestionCaptures(repo.store)
    capture = captures.add(marker, 0, png())
    destination = tmp_path / "moved"
    destination.mkdir()
    shutil.copy2(repo.store.path, destination / "project.db")
    moved = VideoStore(destination / "project.db")
    monkeypatch.setattr("classcue.question_captures.library_scope", lambda *_: str(captures.base))
    recovered = QuestionCaptures(moved)
    assert recovered.get(marker, capture["id"])["image_hash"] == capture["image_hash"]
    assert recovered.path(capture["id"]).read_bytes() == png()


def test_capture_source_can_be_cited_by_analysis(api, monkeypatch):
    client, repo, marker, _, model = api
    capture = upload(client, marker).json()
    repo.save_material(marker, QuestionMaterial(capture_ids=[capture["id"]]))
    from tests.test_question_analysis import analysis
    from classcue.question_analysis import QuestionAnalysis
    from tests.test_bilibili_api import wait
    def analyze(self, snapshot, images):
        assert images[0].data == png()
        return QuestionAnalysis.model_validate(analysis(f"capture:{capture['id']}"))
    monkeypatch.setattr(model, "analyze", analyze)
    job = client.post(f"/api/markers/{marker}/question/analyze").json()
    assert wait(client, job["job_id"])["status"] == "completed"
    assert repo.runs(marker)[0]["analysis"]["facts"][0]["sources"] == [f"capture:{capture['id']}"]
