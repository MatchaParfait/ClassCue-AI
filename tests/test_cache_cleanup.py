import json
import os
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace
from threading import Event

from fastapi.testclient import TestClient
import pytest

from classcue.cache_cleanup import CacheCleanup, DEFAULTS, cleanup_saved_transcript
from classcue.core import TranscriptSegment
from classcue.video_store import VideoStore


HASH = "a" * 64
SEGMENTS = [TranscriptSegment(1, 0, 1000, "Synthetic lesson")]


def write(path, value=b"synthetic cache", *, age=60):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, dict):
        path.write_text(json.dumps(value), encoding="utf-8")
    else:
        path.write_bytes(value)
    stamp = time.time() - age * 86400
    os.utime(path, (stamp, stamp))
    return path


@pytest.fixture
def library(tmp_path):
    project = tmp_path / "application"
    project.mkdir()
    store = VideoStore(project / "data" / "library" / "project.db")
    audio = store.path.parent / "temporary_audio"
    wav = audio / f"{HASH}.wav"
    video = write(project / "videos" / "lesson.mp4", b"synthetic video")
    video_id = store.create_video(HASH, video.name, str(video), str(wav), 1000)
    manager = CacheCleanup(project, store)
    return SimpleNamespace(project=project, store=store, audio=audio, wav=wav,
                           video=video, video_id=video_id, manager=manager)


def task(library, *, status="completed", engine="qwen", saved=False, identity=HASH):
    directory = library.audio / f"{engine}_jobs" / ("b" * 64)
    write(directory / "manifest.json", {"identity": {"input_sha256": identity}})
    write(directory / "state.json", {"status": status})
    write(directory / "asr" / "chunk.json", {"text": "synthetic"})
    write(directory / "timeline.wav")
    if saved:
        write(directory / "transcript-saved.json", {
            "database": str(library.store.path.absolute()), "video_id": library.video_id,
            "content_hash": HASH, "saved_at": time.time() - 60 * 86400,
        })
    return directory


def backup(directory, completed_at, *, status="complete"):
    write(directory / ".release-install.json", {"payload": "synthetic"})
    write(directory / "library-backup.sqlite3")
    write(directory / "backup-state.json", {
        "id": directory.name, "status": status, "completed_at": completed_at, "database_backed_up": True,
    })


def test_transcript_commit_cleans_audio_and_marks_completed_tasks(library):
    write(library.wav)
    directory = task(library)
    library.store.mark_transcribed(library.video_id, SEGMENTS, "qwen3-asr-1.7b")
    assert not library.wav.exists()
    assert not (directory / "timeline.wav").exists()
    assert (directory / "transcript-saved.json").is_file()
    assert library.video.is_file()
    assert library.store.get_segments(library.video_id)[0].text == "Synthetic lesson"


def test_retranscription_uses_the_same_after_commit_cleanup(library):
    write(library.wav)
    directory = task(library)
    library.store.replace_transcript_for_reanalysis(library.video_id, SEGMENTS, "qwen3-asr-1.7b")
    assert not library.wav.exists()
    assert not (directory / "timeline.wav").exists()


def test_commit_failure_never_deletes_resume_audio(library, monkeypatch):
    import classcue.video_store as storage

    class RejectCommit(storage.ClosingConnection):
        def __enter__(self):
            self.set_authorizer(lambda action, value, *_:
                                sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_TRANSACTION
                                and value == "COMMIT" else sqlite3.SQLITE_OK)
            return super().__enter__()

    write(library.wav)
    directory = task(library)
    monkeypatch.setattr(storage, "ClosingConnection", RejectCommit)
    with pytest.raises(sqlite3.DatabaseError):
        library.store.mark_transcribed(library.video_id, SEGMENTS, "synthetic")
    assert library.wav.is_file()
    assert (directory / "timeline.wav").is_file()
    assert not (directory / "transcript-saved.json").exists()


@pytest.mark.parametrize("state", ["failed", "resource_paused", "interrupted", "running"])
def test_unfinished_task_is_preserved_even_with_saved_transcript(library, state):
    library.store.mark_transcribed(library.video_id, SEGMENTS, "synthetic")
    directory = task(library, status=state)
    cleanup_saved_transcript(library.store, library.video_id)
    library.manager.clean(["transcription"], mode="all")
    assert (directory / "timeline.wav").is_file()
    assert (directory / "asr/chunk.json").is_file()


def test_worker_result_without_database_commit_is_not_cleanable(library):
    directory = task(library)
    cleanup_saved_transcript(library.store, library.video_id)
    assert library.manager.preview("all")["categories"][0]["eligible_bytes"] == 0
    assert (directory / "timeline.wav").is_file()


def test_completed_cache_expires_but_recovery_metadata_remains(library):
    library.store.mark_transcribed(library.video_id, SEGMENTS, "synthetic")
    directory = task(library, saved=True)
    result = library.manager.clean(["transcription"])
    assert result["files_deleted"] == 2
    assert not (directory / "asr/chunk.json").exists()
    assert (directory / "manifest.json").is_file()
    assert (directory / "state.json").is_file()
    assert (directory / "transcript-saved.json").is_file()


def test_only_generated_preview_images_are_cleaned(library):
    old = write(library.audio / "thumbnails" / f"{'c' * 64}.jpg")
    recent = write(library.audio / "search-previews" / f"{'d' * 64}.jpg", age=1)
    unknown = write(library.audio / "thumbnails" / "my-photo.jpg")
    slide = write(library.store.path.parent / "slides" / "slide.jpg")
    recovery = write(library.audio / ".recycle_bin" / "ClassCue" / "lesson.mp4")
    result = library.manager.clean(["previews"])
    assert result["files_deleted"] == 1
    assert not old.exists()
    assert all(path.exists() for path in (recent, unknown, slide, recovery, library.video))


def test_preview_budget_evicts_oldest_even_before_expiry(library):
    old = write(library.audio / "thumbnails" / f"{'c' * 64}.jpg", age=2)
    new = write(library.audio / "thumbnails" / f"{'d' * 64}.jpg", age=1)
    # Sparse synthetic files exercise capacity without large memory allocations.
    with old.open("r+b") as stream:
        stream.truncate(40 * 1024 ** 2)
    with new.open("r+b") as stream:
        stream.truncate(40 * 1024 ** 2)
    stamp = time.time()
    os.utime(old, (stamp - 2 * 86400, stamp - 2 * 86400))
    library.manager.save({**DEFAULTS, "preview_limit_mb": 64})
    library.manager.clean(["previews"])
    assert not old.exists() and new.exists()


def test_preview_does_not_mutate_or_remove_files(library):
    image = write(library.audio / "thumbnails" / f"{'c' * 64}.jpg")
    result = library.manager.preview()
    assert next(item for item in result["categories"] if item["id"] == "previews")["eligible_bytes"] > 0
    assert image.is_file()
    assert not library.manager.settings_path.exists()


def test_linked_preview_directory_is_never_followed(library, tmp_path):
    outside = tmp_path / "outside"
    image = write(outside / f"{'c' * 64}.jpg")
    library.audio.mkdir(parents=True, exist_ok=True)
    link = library.audio / "thumbnails"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("Symlink privileges unavailable")
        import subprocess
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True)
    try:
        assert library.manager.clean(["previews"], mode="all")["files_deleted"] == 0
        assert image.is_file()
    finally:
        if link.is_symlink():
            link.unlink()
        else:
            link.rmdir()


def test_file_changed_since_scan_is_skipped(library, monkeypatch):
    image = write(library.audio / "thumbnails" / f"{'c' * 64}.jpg")
    original = library.manager._inventory

    def change(mode, preferences=None):
        result = original(mode, preferences)
        image.write_bytes(b"newly regenerated synthetic preview")
        return result

    monkeypatch.setattr(library.manager, "_inventory", change)
    result = library.manager.clean(["previews"])
    assert result["skipped"] == 1 and image.is_file()


def test_old_logs_are_cleaned_but_live_logs_are_retained(library):
    import classcue.cache_cleanup as cleanup
    root = library.project / ".runtime/logs"
    old = write(root / "backend-123456789.log")
    active = write(root / f"backend-{cleanup.os.getpid()}.log")
    launcher = write(root / "launcher.log")
    worker = write(root / "qwen-worker.log")
    unrelated = write(root / "notes.txt")
    library.manager.clean(["logs"], mode="all")
    assert not old.exists()
    assert not worker.exists()
    assert all(path.is_file() for path in (active, launcher, unrelated))


def test_ai_cache_cleaning_preserves_learning_results_and_is_never_automatic(library):
    library.store.mark_transcribed(library.video_id, SEGMENTS, "synthetic")
    with library.store.connect() as connection:
        connection.execute("INSERT INTO video_ai_chunks(video_id, analysis_mode, chunk_key, selection_json) "
                           "VALUES (?, 'medium_recall', 'synthetic', '{}')", (library.video_id,))
    with pytest.raises(ValueError, match="手动"):
        library.manager.clean(["ai"], mode="all", automatic=True)
    library.manager.clean(["ai"], mode="all")
    with library.store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM video_ai_chunks").fetchone()[0] == 0
    assert library.store.has_transcript(library.video_id)
    assert library.video.is_file()


def test_due_policy_and_last_result_survive_reopening(library):
    assert library.manager.due()
    library.manager.clean(["previews"])
    assert not CacheCleanup(library.project, library.store).due()
    library.manager.save({**DEFAULTS, "enabled": False})
    assert not CacheCleanup(library.project, library.store).due()


def test_cancel_keeps_remaining_files_and_records_partial_result(library):
    image = write(library.audio / "thumbnails" / f"{'c' * 64}.jpg")
    result = library.manager.clean(["previews"], should_cancel=lambda: True)
    assert result["cancelled"] and result["files_deleted"] == 0
    assert image.is_file()


def test_newest_two_backups_and_current_transaction_are_retained(library):
    root = library.project / ".runtime/updates/backups"
    names = ["1" * 32, "2" * 32, "3" * 32]
    for index, name in enumerate(names):
        directory = root / name
        backup(directory, index + 1)
        os.utime(directory, (index + 1, index + 1))
    result = library.manager.clean(["backups"], mode="all")
    assert result["files_deleted"] == 1
    assert not (root / names[0] / "library-backup.sqlite3").exists()
    assert (root / names[-1] / "library-backup.sqlite3").is_file()


def test_in_progress_update_files_are_preserved(library):
    root = library.project / ".runtime/updates"
    archive = write(root / f"1.0.0-{'a' * 64}.zip")
    write(root / "transaction.json", {"status": "installed"})
    library.manager.clean(["updates", "backups"], mode="all")
    assert archive.is_file()


def test_api_blocks_cleanup_with_paused_tasks_and_rejects_paths(library, monkeypatch):
    import api.main as main
    monkeypatch.setattr(main, "DIRECTORY_PROJECT_ROOT", library.project)
    app = main.create_app(library.store)
    with TestClient(app) as client:
        assert client.get("/api/cache").status_code == 200
        assert client.post("/api/cache/clean", json={"categories": ["../videos"]}).status_code == 422
        assert client.post("/api/cache/clean", json={"path": str(library.video)}).status_code == 422
        library.store.create_job("paused", "video_transcribe", library.video_id)
        library.store.update_job("paused", status="resource_paused")
        assert client.post("/api/cache/clean", json={"categories": ["previews"]}).status_code == 409
        assert client.put("/api/cache/preferences", json={**DEFAULTS, "retention_days": -1}).status_code == 422


def test_unknown_download_cache_layout_is_not_deleted(library, monkeypatch):
    import classcue.cache_cleanup as cleanup
    unknown = write(library.project / ".runtime/cache" / "unknown.bin")
    monkeypatch.setattr(cleanup.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("Unknown cache must stay"))
    result = library.manager.clean(["downloads"], mode="all")
    assert unknown.is_file() and result["released_bytes"] == 0


def test_uv_cache_uses_native_tool_without_force_and_preserves_environments(library, monkeypatch):
    import classcue.cache_cleanup as cleanup
    cache = library.project / ".runtime/cache/uv"
    write(cache / "CACHEDIR.TAG")
    package = write(cache / "archive-v0" / "fixture.bin")
    environment = write(library.project / ".runtime/qwen-asr/Lib/site-packages/fixture.py")
    executable = write(library.project / ".runtime/bin/uv.exe")
    commands = []

    def run(command, **options):
        commands.append(command)
        assert options["timeout"] == 120
        assert "--force" not in command
        package.unlink()
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cleanup.subprocess, "run", run)
    result = library.manager.clean(["downloads"], mode="all")
    assert commands == [[str(executable), "cache", "clean", "--cache-dir", str(cache)]]
    assert environment.is_file()
    assert result["released_bytes"] > 0 and result["files_deleted"] == 1


def test_manager_itself_refuses_live_or_paused_work(library):
    library.store.create_job("synthetic-running", "video_transcribe", library.video_id)
    with pytest.raises(RuntimeError, match="任务"):
        library.manager.clean(["previews"])


def test_recently_served_preview_is_preserved_even_in_all_mode(library):
    image = write(library.audio / "thumbnails" / f"{'c' * 64}.jpg", age=0)
    assert library.manager.clean(["previews"], mode="all")["files_deleted"] == 0
    assert image.is_file()


def test_legacy_completed_cache_requires_matching_saved_library_transcript(library):
    library.store.mark_transcribed(library.video_id, SEGMENTS, "synthetic")
    directory = task(library)
    library.manager.clean(["transcription"])
    assert not (directory / "timeline.wav").exists()
    assert not (directory / "asr/chunk.json").exists()


def test_legacy_cache_for_failed_or_missing_source_is_preserved(library):
    library.store.mark_transcribed(library.video_id, SEGMENTS, "synthetic")
    directory = task(library)
    library.store.set_status(library.video_id, "failed")
    library.manager.clean(["transcription"], mode="all")
    assert (directory / "timeline.wav").is_file()
    library.store.set_status(library.video_id, "transcribed")
    library.video.unlink()
    library.manager.clean(["transcription"], mode="all")
    assert (directory / "timeline.wav").is_file()


@pytest.mark.parametrize("change", ["paused", "source_missing", "transcript_missing"])
def test_saved_task_protection_is_rechecked_under_lock(library, monkeypatch, change):
    library.store.mark_transcribed(library.video_id, SEGMENTS, "synthetic")
    directory = task(library, saved=True)
    original = library.manager._inventory

    def inventory(mode, preferences=None):
        result = original(mode, preferences)
        if change == "paused":
            write(directory / "state.json", {"status": "resource_paused"})
        elif change == "source_missing":
            library.video.unlink()
        else:
            with library.store.connect() as connection:
                connection.execute("DELETE FROM video_transcript_segments WHERE video_id = ?", (library.video_id,))
        return result

    monkeypatch.setattr(library.manager, "_inventory", inventory)
    result = library.manager.clean(["transcription"], mode="all")
    assert result["files_deleted"] == 0 and result["skipped"] == 2
    assert (directory / "asr/chunk.json").is_file()
    assert (directory / "timeline.wav").is_file()


@pytest.mark.parametrize("saved_at", ["invalid", None, float("nan"), float("inf")])
def test_invalid_saved_timestamp_preserves_task_without_blocking_other_cleanup(library, saved_at):
    library.store.mark_transcribed(library.video_id, SEGMENTS, "synthetic")
    directory = task(library, saved=True)
    marker = json.loads((directory / "transcript-saved.json").read_text(encoding="utf-8"))
    write(directory / "transcript-saved.json", {**marker, "saved_at": saved_at})
    image = write(library.audio / "thumbnails" / f"{'c' * 64}.jpg")
    library.manager.clean(["transcription", "previews"])
    assert (directory / "asr/chunk.json").is_file()
    assert not image.exists()


def test_cancelled_backup_cleanup_remains_identifiable_and_keeps_newest_two(library):
    root = library.project / ".runtime/updates/backups"
    names = ["1" * 32, "2" * 32, "3" * 32]
    for index, name in enumerate(names):
        directory = root / name
        backup(directory, index + 1)
        write(directory / "program.bin")
    old = root / names[0]
    result = library.manager.clean(["backups"], mode="all",
        should_cancel=lambda: not (old / "library-backup.sqlite3").exists())
    assert result["cancelled"] and result["files_deleted"] == 1
    assert (old / ".release-install.json").is_file()
    assert json.loads((old / "backup-state.json").read_text())["status"] == "cleaning"
    assert (old / "program.bin").is_file()
    second = library.manager.clean(["backups"], mode="all")
    assert second["files_deleted"] == 1
    assert not (old / "program.bin").exists()
    for name in names[1:]:
        assert (root / name / "library-backup.sqlite3").is_file()


@pytest.mark.parametrize("change", ["paused", "source_missing", "transcript_missing"])
def test_standalone_audio_protection_is_rechecked_before_delete(library, monkeypatch, change):
    library.store.mark_transcribed(library.video_id, SEGMENTS, "synthetic")
    write(library.wav)
    original = library.manager._inventory

    def inventory(mode, preferences=None):
        result = original(mode, preferences)
        if change == "paused":
            library.store.create_job("external-transcription", "video_transcribe", library.video_id)
            library.store.update_job("external-transcription", status="resource_paused")
        elif change == "source_missing":
            library.video.unlink()
        else:
            with library.store.connect() as connection:
                connection.execute("DELETE FROM video_transcript_segments WHERE video_id = ?", (library.video_id,))
        return result

    monkeypatch.setattr(library.manager, "_inventory", inventory)
    result = library.manager.clean(["transcription"], mode="all")
    assert library.wav.is_file()
    assert result["files_deleted"] == 0 and result["skipped"] == 1


def test_unknown_and_incomplete_backups_do_not_displace_complete_versions(library):
    root = library.project / ".runtime/updates/backups"
    names = [str(index) * 32 for index in range(1, 6)]
    for index, name in enumerate(names[:3]):
        backup(root / name, index + 1)
    unknown, incomplete = (root / name for name in names[3:])
    write(unknown / ".release-install.json", {"payload": "synthetic"})
    write(unknown / "program.bin")
    backup(incomplete, 5)
    (incomplete / "library-backup.sqlite3").unlink()
    library.manager.clean(["backups"], mode="all")
    assert not (root / names[0] / "library-backup.sqlite3").exists()
    for name in names[1:3]:
        assert (root / name / "library-backup.sqlite3").is_file()
    assert (unknown / "program.bin").is_file()
    assert (incomplete / ".release-install.json").is_file()


def test_partial_backup_does_not_reenter_complete_retention_slots(library):
    root = library.project / ".runtime/updates/backups"
    names = [str(index) * 32 for index in range(1, 4)]
    for index, name in enumerate(names):
        backup(root / name, index + 1)
        write(root / name / "program.bin")
    old = root / names[0]
    library.manager.clean(["backups"], mode="all",
        should_cancel=lambda: not (old / "library-backup.sqlite3").exists())
    # Even if a newer complete backup disappears outside the app, the partial
    # older backup remains resumable and cannot claim a complete retention slot.
    (root / names[-1] / "library-backup.sqlite3").unlink()
    library.manager.clean(["backups"], mode="all")
    assert not (old / "program.bin").exists()
    assert (root / names[1] / "library-backup.sqlite3").is_file()


def test_real_background_cancellation_persists_partial_result(library, monkeypatch):
    from fastapi import FastAPI
    from api.cache_cleanup import router
    from api.main import JobManager, StoreProvider

    images = [write(library.audio / "thumbnails" / f"{character * 64}.jpg") for character in ("c", "d")]
    removed, resume = Event(), Event()
    unlink = Path.unlink

    def gated_unlink(path, *args, **kwargs):
        result = unlink(path, *args, **kwargs)
        if path == images[0]:
            removed.set()
            assert resume.wait(10), "Synthetic cancellation gate timed out"
        return result

    monkeypatch.setattr(Path, "unlink", gated_unlink)
    jobs = JobManager(StoreProvider(library.store))
    app = FastAPI()
    routes, _ = router(library.project, jobs.provider, jobs)
    app.include_router(routes)
    try:
        with TestClient(app) as client:
            response = client.post("/api/cache/clean", json={"categories": ["previews"], "mode": "all"})
            assert response.status_code == 202
            job_id = response.json()["job_id"]
            assert removed.wait(10)
            jobs.cancel(job_id)
            resume.set()
        jobs.shutdown(wait=True)
        result, job = library.manager.state()["last_run"], jobs.get(job_id)
        assert not images[0].exists() and images[1].is_file()
        assert result["cancelled"] and result["files_deleted"] == 1
        assert result["released_bytes"] == len(b"synthetic cache")
        assert job["status"] == "interrupted"
        assert job["cleanup"]["files_deleted"] == 1
        assert job["cleanup"]["phase_progress"] == .5
    finally:
        resume.set()
        jobs.shutdown(wait=True)
