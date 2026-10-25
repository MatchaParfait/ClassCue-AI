"""Deterministic regressions for the delete/undo vs job-submit lock window.

All races are driven with ``Event`` parking hooks instead of sleep timing, use
a synthetic temporary library, and never invoke real ASR, AI, or browser IO.
"""
from __future__ import annotations

import io
import json
import pathlib
import threading
from threading import Event
from types import SimpleNamespace

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

import api.main as api_main
import api.questions as api_questions
from classcue.ai_provider import ProviderProfile, ProviderSettings
from classcue.bilibili_store import BilibiliStore
from classcue.core import TranscriptSegment
from classcue.video_store import VideoStore


def tiny_png() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (640, 360), (10, 20, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


class Lab:
    def __init__(self, store, client, application, video_id, marker_id, video_path):
        self.store, self.client = store, client
        self.jobs = application.state.jobs
        self.video_id, self.marker_id, self.video_path = video_id, marker_id, video_path

    def job_rows(self, video_id: int | None = None) -> list:
        with self.store.connect() as connection:
            if video_id is None:
                return connection.execute("SELECT * FROM processing_jobs").fetchall()
            return connection.execute(
                "SELECT * FROM processing_jobs WHERE video_id = ?", (video_id,)).fetchall()


def request_in_thread(target, *args, **kwargs):
    """Run one HTTP call in a thread; never raises into the test thread."""
    box = {}

    def call():
        box["response"] = target(*args, **kwargs)

    worker = threading.Thread(target=call)
    worker.start()
    return box, worker


@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setattr(api_main, "DIRECTORY_PROJECT_ROOT", tmp_path / "application")
    monkeypatch.setattr(api_main, "require_selectable_model", lambda _id: None)
    monkeypatch.setattr(api_main, "load_provider_settings", lambda: ProviderSettings(
        profile=ProviderProfile(base_url="https://example.test/v1", model="test-model")))
    monkeypatch.setattr(api_main, "resolve_interface_key", lambda _profile, _settings: "test-only-secret")
    monkeypatch.setattr(api_main, "prepare_video_for_analysis", lambda *args, **kwargs: ("transcribed", 1))
    monkeypatch.setattr(api_main, "process_video_once", lambda *args, **kwargs: ("completed", 0))
    monkeypatch.setattr(api_main, "create_judge", lambda *args, **kwargs: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(api_main, "extract_and_store_slides", lambda *args, **kwargs: {"slides": 0})
    monkeypatch.setattr(api_main, "process_saved_analysis_once", lambda *args, **kwargs: ("completed", 0))
    monkeypatch.setattr(api_main, "execute_run", lambda *args, **kwargs: {"outcome": "completed"})
    monkeypatch.setattr(api_questions, "load_provider_settings", lambda: ProviderSettings(
        profile=ProviderProfile(base_url="https://example.test/v1", model="test-model")))
    monkeypatch.setattr(api_questions, "resolve_interface_key", lambda _profile, _settings: "test-only-secret")
    monkeypatch.setattr(api_questions, "QuestionModel", lambda *_args, **_kwargs: SimpleNamespace(
        __enter__=lambda self: self, __exit__=lambda self, *_exc: False))
    monkeypatch.setattr(api_questions, "analyze_question", lambda *args, **kwargs: {"status": "completed"})

    store = VideoStore(tmp_path / "workspace" / "project.db")
    video_path = tmp_path / "downloads" / "lesson.mp4"
    video_path.parent.mkdir(parents=True)
    video_path.write_bytes(b"video fixture")
    video_id = store.create_video("hash", "lesson.mp4", str(video_path), "lesson.wav", 60_000)
    store.mark_transcribed(video_id, [TranscriptSegment(1, 0, 2_000, "第一句")])
    marker_id = store.add_marker(video_id, 0, 2_000, "考点", "", "第一句", "会考", .9, "high_recall")
    application = api_main.create_app(store)
    client = TestClient(application, raise_server_exceptions=False)
    yield Lab(store, client, application, video_id, marker_id, video_path)
    application.state.jobs.shutdown(wait=True)


def drain(jobs):
    jobs.executor.submit(lambda: None).result(timeout=20)


def park_trash_video(lab):
    """Park DELETE inside trash_video, after the idle check, holding no fake state."""
    started, proceed = Event(), Event()
    real = lab.store.trash_video

    def parked(video_id, trash_root, slide_root=None):
        started.set()
        assert proceed.wait(20)
        return real(video_id, trash_root, slide_root)

    lab.store.trash_video = parked
    return started, proceed


def park_trash_folder(lab):
    started, proceed = Event(), Event()
    real = lab.store.trash_folder

    def parked(folder_id, trash_root, slide_root=None):
        started.set()
        assert proceed.wait(20)
        return real(folder_id, trash_root, slide_root)

    lab.store.trash_folder = parked
    return started, proceed


@pytest.mark.parametrize("status", ["running", "resource_paused"])
@pytest.mark.parametrize("action", ["delete_video", "delete_folder", "undo"])
def test_file_changes_are_rejected_while_a_job_is_active_or_paused(library, status, action):
    lab = library
    store = lab.store
    folder_id = store.create_folder("章节")
    store.set_video_folder(lab.video_id, folder_id)
    store.create_job("busy", "video_process", lab.video_id)
    store.update_job("busy", status=status)

    if action == "delete_video":
        response = lab.client.delete(f"/api/videos/{lab.video_id}")
    elif action == "delete_folder":
        response = lab.client.delete(f"/api/folders/{folder_id}")
    else:
        response = lab.client.post("/api/recycle-bin/undo")

    assert response.status_code == 409
    assert store.get_video(lab.video_id) is not None
    assert lab.video_path.exists()
    assert store.get_job("busy")["status"] == status


@pytest.mark.parametrize("trash", ["video", "folder"])
def test_a_submit_arriving_in_the_delete_window_is_rejected(library, trash):
    """The idle check passes and trash parks; a task submit must not start."""
    lab = library
    if trash == "folder":
        folder_id = lab.store.create_folder("章节")
        lab.store.set_video_folder(lab.video_id, folder_id)
        trash_started, allow_trash = park_trash_folder(lab)
        delete_target = f"/api/folders/{folder_id}"
    else:
        trash_started, allow_trash = park_trash_video(lab)
        delete_target = f"/api/videos/{lab.video_id}"

    check_reached = Event()
    real_check = api_main._video_or_404

    def parked_check(store, video_id):
        if int(video_id) == lab.video_id:
            check_reached.set()
        return real_check(store, video_id)

    api_main._video_or_404 = parked_check
    try:
        delete_box, delete_worker = request_in_thread(lab.client.delete, delete_target)
        assert trash_started.wait(5), "delete must park inside the trash operation"
        submit_box, submit_worker = request_in_thread(
            lab.client.post, f"/api/videos/{lab.video_id}/transcribe", json={})
        # While the delete owns the critical section the submit cannot pass its
        # existence check; a bounded negative wait is deterministic post-fix.
        assert not check_reached.wait(0.5), "transcribe validated outside the delete lock"
        allow_trash.set()
        delete_worker.join(20)
        submit_worker.join(20)
    finally:
        api_main._video_or_404 = real_check

    assert delete_box["response"].status_code == 200
    assert submit_box["response"].status_code == 404
    assert lab.job_rows(lab.video_id) == []
    assert lab.store.get_video(lab.video_id) is None
    drain(lab.jobs)


def test_a_delete_arriving_in_the_submit_window_is_rejected(library, monkeypatch):
    """The submit validates first; the concurrent delete must see the running job."""
    lab = library
    entered, allow_submit = Event(), Event()
    job_running, release_job = Event(), Event()

    def parked_model_check(_model_id):
        entered.set()
        assert allow_submit.wait(20)
        return None

    def parked_pipeline(*_args, **_kwargs):
        job_running.set()
        assert release_job.wait(20)
        return ("transcribed", 1)

    monkeypatch.setattr(api_main, "require_selectable_model", parked_model_check)
    monkeypatch.setattr(api_main, "prepare_video_for_analysis", parked_pipeline)

    submit_box, submit_worker = request_in_thread(
        lab.client.post, f"/api/videos/{lab.video_id}/transcribe", json={})
    assert entered.wait(5), "transcribe must park after its existence check"
    # Let the submit finish while a delete waits for the same critical section.
    allow_submit.set()
    deleted = lab.client.delete(f"/api/videos/{lab.video_id}")
    submit_worker.join(20)
    assert job_running.wait(5)
    try:
        assert submit_box["response"].status_code == 202
        assert deleted.status_code == 409
        assert lab.store.get_video(lab.video_id) is not None
        assert lab.video_path.exists()
    finally:
        release_job.set()
    drain(lab.jobs)
    assert lab.job_rows(lab.video_id)[0]["status"] == "completed"


def make_paused_batch(lab, job_id="paused-batch"):
    """A version=2 resource_paused batch whose only entry is a registered video."""
    state = {
        "kind": "batch", "version": 2, "options": {},
        "entries": [{
            "source_path": str(lab.video_path), "source_root": "",
            "video_id": lab.video_id, "status": "pending_asr", "error": "",
        }],
        "source_directories": [], "folders": [], "items": [],
        "api_error": "", "current_index": None,
    }
    lab.store.create_job(job_id, "batch_video_process", None)
    lab.store.update_job(job_id, status="resource_paused", stage="resource_paused",
                         request_json=json.dumps(state, ensure_ascii=False))
    return job_id


def record_then_finish_web_batch(videos, entered, release):
    """Fake run_web_batch: record the videos the new task starts from, then park."""
    def fake(store, state, *_args, **_kwargs):
        videos.append([store.get_video(entry["video_id"]) for entry in state["entries"]
                       if entry.get("video_id")])
        entered.set()
        if release is not None:
            assert release.wait(20)
        return {"job_status": "completed", "message": "fixture finished", "committed": True}
    return fake


def test_resume_rebuilds_the_queue_inside_the_job_lock(library, monkeypatch):
    """Cancel and delete overtaking a paused-batch resume must not start the new
    task from a snapshot of a video that was already deleted."""
    lab = library
    job_id = make_paused_batch(lab)

    normalize_entered, allow_normalize = Event(), Event()
    real_normalize = api_main.normalize_batch

    def parked_normalize(request, store):
        normalize_entered.set()
        assert allow_normalize.wait(20)
        return real_normalize(request, store)

    monkeypatch.setattr(api_main, "normalize_batch", parked_normalize)

    trash_started, allow_trash = park_trash_video(lab)
    batch_entered, release_batch = Event(), Event()
    videos_seen_by_new_task = []
    monkeypatch.setattr(api_main, "run_web_batch", record_then_finish_web_batch(
        videos_seen_by_new_task, batch_entered, release_batch))

    resume_box, resume_worker = request_in_thread(
        lab.client.post, f"/api/jobs/{job_id}/resume", json={})
    assert normalize_entered.wait(5), "resume must park while rebuilding the paused queue"
    race_box: dict[str, object] = {}

    def cancel_then_delete():
        race_box["cancel"] = lab.client.post(f"/api/jobs/{job_id}/cancel")
        race_box["delete"] = lab.client.delete(f"/api/videos/{lab.video_id}")

    race_worker = threading.Thread(target=cancel_then_delete)
    race_worker.start()
    try:
        # Resume owns the critical section: the cancel-then-delete pair that
        # removes the paused row and the video must not overtake it.
        assert not trash_started.wait(0.5), "delete validated outside the resume critical section"
        allow_normalize.set()
        resume_worker.join(20)
        race_worker.join(20)
    finally:
        allow_normalize.set()
        allow_trash.set()
        release_batch.set()
        resume_worker.join(20)
        race_worker.join(20)

    assert resume_box["response"].status_code == 202, resume_box["response"].text
    assert race_box["cancel"].status_code == 200
    assert race_box["delete"].status_code == 409
    assert lab.store.get_video(lab.video_id) is not None
    assert lab.video_path.exists()
    assert batch_entered.wait(5), "the resumed task must start"
    assert videos_seen_by_new_task[0][0] is not None, \
        "the resumed task started from a deleted video snapshot"
    old_row = lab.store.get_job(job_id)
    assert old_row["status"] == "interrupted" and old_row["stage"] == "resumed"
    new_id = resume_box["response"].json()["job_id"]
    drain(lab.jobs)
    assert lab.store.get_job(new_id)["status"] == "completed"


def test_resume_is_rejected_when_cancel_wins_the_lock_first(library, monkeypatch):
    """A cancel holding the critical section must make resume fail its paused
    check instead of reading the stale row and submitting a new task."""
    lab = library
    job_id = make_paused_batch(lab)

    cancel_parked, allow_cancel = Event(), Event()
    real_update = lab.store.update_job

    def parked_update(job_id_arg, **fields):
        if job_id_arg == job_id and not cancel_parked.is_set():
            cancel_parked.set()
            assert allow_cancel.wait(20)
        return real_update(job_id_arg, **fields)

    monkeypatch.setattr(lab.store, "update_job", parked_update)

    resume_checked = Event()
    real_get_job = lab.store.get_job

    def counting_get_job(job_id_arg):
        if job_id_arg == job_id and cancel_parked.is_set():
            resume_checked.set()
        return real_get_job(job_id_arg)

    monkeypatch.setattr(lab.store, "get_job", counting_get_job)

    batch_entered = Event()
    monkeypatch.setattr(api_main, "run_web_batch", record_then_finish_web_batch(
        [], batch_entered, None))

    cancel_box, cancel_worker = request_in_thread(lab.client.post, f"/api/jobs/{job_id}/cancel")
    assert cancel_parked.wait(5), "cancel must park while updating the paused row"
    resume_box, resume_worker = request_in_thread(
        lab.client.post, f"/api/jobs/{job_id}/resume", json={})
    try:
        assert not resume_checked.wait(0.5), "resume read the paused row outside the job lock"
    finally:
        allow_cancel.set()
        cancel_worker.join(20)
        resume_worker.join(20)

    assert cancel_box["response"].status_code == 200
    assert resume_box["response"].status_code == 409
    assert resume_box["response"].json()["detail"] == "任务不是资源暂停状态"
    assert lab.store.get_video(lab.video_id) is not None
    assert lab.video_path.exists()
    remaining = lab.job_rows()
    assert [row["id"] for row in remaining] == [job_id], "resume must not submit a new task"
    assert lab.store.get_job(job_id)["status"] == "interrupted"


SURVEY_CASES = {
    "process": lambda lab: (f"/api/videos/{lab.video_id}/process", {}),
    "transcribe": lambda lab: (f"/api/videos/{lab.video_id}/transcribe", {}),
    "slides": lambda lab: (f"/api/videos/{lab.video_id}/slides/extract", {}),
    "analysis": lambda lab: (f"/api/videos/{lab.video_id}/analysis", {"analysis_mode": "high_recall"}),
}


@pytest.mark.parametrize("case", sorted(SURVEY_CASES))
def test_video_task_entries_validate_and_submit_inside_the_job_lock(library, case):
    """Each video-task entry keeps its existence check under the delete/undo lock."""
    lab = library
    path, payload = SURVEY_CASES[case](lab)
    check_reached, request_sent = Event(), Event()
    ownership: list[bool] = []
    real_check = api_main._video_or_404

    def recording_check(store, video_id):
        if int(video_id) == lab.video_id:
            acquired = lab.jobs._lock.acquire(blocking=False)
            ownership.append(acquired)
            if acquired:
                lab.jobs._lock.release()
            check_reached.set()
        return real_check(store, video_id)

    api_main._video_or_404 = recording_check
    try:
        assert lab.jobs._lock.acquire(blocking=False)
        try:
            def call():
                request_sent.set()
                return lab.client.post(path, json=payload)

            box = {}
            worker = threading.Thread(target=lambda: box.__setitem__("response", call()))
            worker.start()
            assert request_sent.wait(5)
            assert not check_reached.wait(0.5), f"{case} validated outside the job lock"
        finally:
            lab.jobs._lock.release()
        worker.join(20)
    finally:
        api_main._video_or_404 = real_check

    assert ownership == [True], f"{case} must re-check the video while holding the lock"
    assert box["response"].status_code == 202, box["response"].text
    drain(lab.jobs)
    assert lab.store.get_video(lab.video_id) is not None


def test_experiment_entry_validates_and_submits_inside_the_job_lock(library):
    lab = library
    preview = lab.client.post(
        f"/api/videos/{lab.video_id}/experiments/preview",
        json={"name": "锁内实验", "scheme": "rules"},
    )
    assert preview.status_code == 200, preview.text
    payload = {"name": "锁内实验", "scheme": "rules",
               "approved_snapshot_hash": preview.json()["snapshot_hash"]}

    check_reached, request_sent = Event(), Event()
    ownership: list[bool] = []
    real_check = api_main._video_or_404

    def recording_check(store, video_id):
        if int(video_id) == lab.video_id:
            acquired = lab.jobs._lock.acquire(blocking=False)
            ownership.append(acquired)
            if acquired:
                lab.jobs._lock.release()
            check_reached.set()
        return real_check(store, video_id)

    api_main._video_or_404 = recording_check
    try:
        assert lab.jobs._lock.acquire(blocking=False)
        try:
            box = {}
            worker = threading.Thread(target=lambda: box.__setitem__(
                "response", lab.client.post(
                    f"/api/videos/{lab.video_id}/experiments", json=payload)))
            request_sent.set()
            worker.start()
            assert not check_reached.wait(0.5), "experiment validated outside the job lock"
        finally:
            lab.jobs._lock.release()
        worker.join(20)
    finally:
        api_main._video_or_404 = real_check

    assert ownership == [True]
    assert box["response"].status_code == 202, box["response"].text
    drain(lab.jobs)


def test_question_analysis_validates_and_submits_inside_the_job_lock(library, monkeypatch):
    lab = library
    check_reached, request_sent = Event(), Event()
    ownership: list[bool] = []
    real_marker = BilibiliStore.marker

    def recording_marker(self, marker_id):
        marker = real_marker(self, marker_id)
        if int(marker_id) == lab.marker_id and not check_reached.is_set():
            acquired = lab.jobs._lock.acquire(blocking=False)
            ownership.append(acquired)
            if acquired:
                lab.jobs._lock.release()
            check_reached.set()
        return marker

    monkeypatch.setattr(BilibiliStore, "marker", recording_marker)
    assert lab.jobs._lock.acquire(blocking=False)
    try:
        box = {}
        worker = threading.Thread(target=lambda: box.__setitem__(
            "response", lab.client.post(f"/api/markers/{lab.marker_id}/question/analyze")))
        request_sent.set()
        worker.start()
        assert not check_reached.wait(0.5), "question analyze validated outside the job lock"
    finally:
        lab.jobs._lock.release()
    worker.join(20)

    assert ownership == [True]
    assert box["response"].status_code == 202, box["response"].text
    drain(lab.jobs)


def park_marker_once(lab):
    """Park the next marker lookup after it succeeds, inside a locked write path."""
    entered, proceed = Event(), Event()
    real_marker = BilibiliStore.marker
    armed = {"active": True}

    def parked_marker(self, marker_id):
        marker = real_marker(self, marker_id)
        if armed["active"] and int(marker_id) == lab.marker_id:
            armed["active"] = False
            entered.set()
            assert proceed.wait(20)
        return marker

    return parked_marker, entered, proceed


def test_capture_upload_wins_against_a_waiting_delete(library, monkeypatch):
    lab = library
    parked_marker, entered, proceed = park_marker_once(lab)
    monkeypatch.setattr(BilibiliStore, "marker", parked_marker)

    upload_box, upload_worker = request_in_thread(
        lab.client.post, f"/api/markers/{lab.marker_id}/question/captures",
        params={"captured_at_ms": 500}, content=tiny_png(),
        headers={"content-type": "image/png"},
    )
    assert entered.wait(5), "upload must park inside its marker validation"
    proceed.set()
    deleted = lab.client.delete(f"/api/videos/{lab.video_id}")
    upload_worker.join(20)

    assert upload_box["response"].status_code == 201, upload_box["response"].text
    assert deleted.status_code == 200
    assert lab.store.get_video(lab.video_id) is None


def test_capture_upload_after_a_delete_that_won_the_lock_is_rejected(library):
    lab = library
    request_sent = Event()
    box = {}

    def call():
        request_sent.set()
        box["response"] = lab.client.post(
            f"/api/markers/{lab.marker_id}/question/captures",
            params={"captured_at_ms": 500}, content=tiny_png(),
            headers={"content-type": "image/png"},
        )

    assert lab.jobs._lock.acquire(blocking=False)
    worker = threading.Thread(target=call)
    worker.start()
    try:
        assert request_sent.wait(5)
        # A delete that owns the critical section removes marker and video.
        trash_root = pathlib.Path(lab.store.path).parent / "videos" / ".recycle_bin"
        lab.store.trash_video(lab.video_id, trash_root)
    finally:
        lab.jobs._lock.release()
    worker.join(20)

    assert box["response"].status_code == 404
    with lab.store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM question_captures").fetchone()[0] == 0


@pytest.mark.parametrize("kind", ["question", "bilibili"])
def test_material_save_wins_against_a_waiting_delete(library, monkeypatch, kind):
    lab = library
    parked_marker, entered, proceed = park_marker_once(lab)
    monkeypatch.setattr(BilibiliStore, "marker", parked_marker)
    if kind == "question":
        path, payload = f"/api/markers/{lab.marker_id}/question", {"text": "题目"}
    else:
        path, payload = f"/api/markers/{lab.marker_id}/bilibili/context", {"course": "课程", "notes": "备注"}

    save_box, save_worker = request_in_thread(lab.client.put, path, json=payload)
    assert entered.wait(5), "save must park inside its marker validation"
    proceed.set()
    deleted = lab.client.delete(f"/api/videos/{lab.video_id}")
    save_worker.join(20)

    assert save_box["response"].status_code == 200, save_box["response"].text
    assert deleted.status_code == 200
    assert lab.store.get_video(lab.video_id) is None


@pytest.mark.parametrize("kind", ["question", "bilibili"])
def test_material_save_after_a_delete_that_won_the_lock_is_rejected(library, kind):
    lab = library
    if kind == "question":
        path, payload = f"/api/markers/{lab.marker_id}/question", {"text": "题目"}
    else:
        path, payload = f"/api/markers/{lab.marker_id}/bilibili/context", {"course": "课程", "notes": "备注"}
    assert lab.client.delete(f"/api/videos/{lab.video_id}").status_code == 200
    assert lab.client.put(path, json=payload).status_code == 404


def test_state_collect_cannot_delete_a_capture_restored_by_undo(library):
    """The deferred capture cleanup must not race recycle-bin undo."""
    from classcue.question_captures import QuestionCaptures

    lab = library
    image = tiny_png()
    capture = QuestionCaptures(lab.store).add(lab.marker_id, 500, image)
    capture_id = capture["id"]
    original = QuestionCaptures(lab.store).path(capture_id)
    assert original.is_file()

    move_phase_seen, collect_parked, allow_collect = Event(), Event(), Event()
    real_path = QuestionCaptures.path

    def parked_path(self, capture_id_arg):
        path = real_path(self, capture_id_arg)
        if capture_id_arg == capture_id:
            if not move_phase_seen.is_set():
                move_phase_seen.set()  # trash moving phase reaches the path first
            elif not collect_parked.is_set():
                collect_parked.set()  # trailing collect parks before unlinking
                assert allow_collect.wait(20)
        return path

    QuestionCaptures.path = parked_path
    try:
        delete_box, delete_worker = request_in_thread(
            lab.client.delete, f"/api/videos/{lab.video_id}")
        assert collect_parked.wait(5), "trailing collect must park before the unlink"
        # Undo arrives while the delete still owns the critical section.
        undo_box, undo_worker = request_in_thread(lab.client.post, "/api/recycle-bin/undo")
        allow_collect.set()
        delete_worker.join(20)
        undo_worker.join(20)
    finally:
        QuestionCaptures.path = real_path

    assert delete_box["response"].status_code == 200
    assert undo_box["response"].status_code == 200, undo_box["response"].text
    restored = QuestionCaptures(lab.store).get(lab.marker_id, capture_id)
    assert restored is not None
    assert original.is_file(), "undo must keep the restored capture file"
    assert original.read_bytes() == image
