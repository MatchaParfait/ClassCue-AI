from pathlib import Path
import asyncio
from contextlib import contextmanager
from threading import Event, Thread

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
import api.main as api_main
import classcue.video_pipeline as video_pipeline
from classcue.core import TranscriptSegment
from classcue.video_store import VideoStore


@pytest.fixture
def secondary_library(tmp_path):
    store = VideoStore(tmp_path / "project.db")
    ids = []
    for index, duration in enumerate((60000, 15000, 120000)):
        source = tmp_path / f"camera-{index}.mp4"
        source.write_bytes(f"synthetic camera {index}".encode())
        ids.append(store.create_video(
            f"hash-{index}", source.name, str(source), str(source.with_suffix(".wav")), duration,
        ))
    app = create_app(store)
    with TestClient(app) as client:
        yield client, store, ids


@pytest.mark.parametrize("secondary_index", [1, 2])
def test_secondary_view_accepts_any_duration_and_persists_offset(secondary_library, secondary_index):
    client, store, ids = secondary_library
    endpoint = f"/api/videos/{ids[0]}/secondary-view"
    assert client.get(endpoint).json() is None
    result = client.put(endpoint, json={"secondary_video_id": ids[secondary_index], "offset_ms": 0})
    assert result.status_code == 200, result.text
    assert result.json()["duration_ms"] == (15000 if secondary_index == 1 else 120000)
    for offset in (-90000, 1234, 240000):
        result = client.patch(endpoint, json={"offset_ms": offset})
        assert result.status_code == 200
        assert result.json()["offset_ms"] == offset
        reopened = VideoStore(store.path)
        assert reopened.get_secondary_view(ids[0])["offset_ms"] == offset
    assert not store.list_recent_jobs()
    assert not store.has_transcript(ids[secondary_index])
    assert client.get(f"/api/videos/{ids[secondary_index]}/stream", headers={"Range": "bytes=0-8"}).status_code == 206


def test_secondary_view_rejects_self_missing_and_invalid_offsets(secondary_library):
    client, store, ids = secondary_library
    endpoint = f"/api/videos/{ids[0]}/secondary-view"
    assert client.put(endpoint, json={"secondary_video_id": ids[0], "offset_ms": 0}).status_code == 400
    assert client.put(endpoint, json={"secondary_video_id": 999, "offset_ms": 0}).status_code == 404
    assert client.patch(endpoint, json={"offset_ms": 0}).status_code == 404
    for invalid in (True, 1.5, "1000", 2**63):
        assert client.patch(endpoint, json={"offset_ms": invalid}).status_code == 422
    Path(store.get_video(ids[1])["video_path"]).unlink()
    assert client.put(endpoint, json={"secondary_video_id": ids[1], "offset_ms": 0}).status_code == 404
    assert client.get("/api/videos/999/secondary-view").status_code == 404


def test_secondary_audio_choice_persists_independently_of_offset(secondary_library):
    client, store, ids = secondary_library
    endpoint = f"/api/videos/{ids[0]}/secondary-view"
    assert client.put(endpoint, json={"secondary_video_id": ids[1], "offset_ms": 5000}).status_code == 200
    result = client.patch(endpoint, json={"use_primary_audio": True})
    assert result.status_code == 200
    assert result.json()["offset_ms"] == 5000
    assert result.json()["use_primary_audio"] == 1
    assert VideoStore(store.path).get_secondary_view(ids[0])["use_primary_audio"] == 1
    assert client.patch(endpoint, json={"offset_ms": -2000}).json()["use_primary_audio"] == 1
    for invalid in ("true", 1):
        assert client.patch(endpoint, json={"use_primary_audio": invalid}).status_code == 422
    assert client.patch(endpoint, json={"use_primary_audio": False}).json()["offset_ms"] == -2000


def test_remove_secondary_view_preserves_both_recordings_and_resets_new_offset(secondary_library):
    client, store, ids = secondary_library
    endpoint = f"/api/videos/{ids[0]}/secondary-view"
    client.put(endpoint, json={"secondary_video_id": ids[1], "offset_ms": -5000})
    assert client.delete(endpoint).status_code == 200
    assert client.delete(endpoint).status_code == 200
    assert client.get(endpoint).json() is None
    for video_id in ids[:2]:
        assert Path(store.get_video(video_id)["video_path"]).is_file()
    assert client.put(endpoint, json={"secondary_video_id": ids[2], "offset_ms": 0}).json()["offset_ms"] == 0


@pytest.mark.parametrize("deleted_index", [0, 1])
def test_recycle_and_undo_restore_secondary_association(secondary_library, tmp_path, deleted_index):
    client, store, ids = secondary_library
    endpoint = f"/api/videos/{ids[0]}/secondary-view"
    client.put(endpoint, json={"secondary_video_id": ids[1], "offset_ms": 1234})
    client.patch(endpoint, json={"use_primary_audio": True})
    store.trash_video(ids[deleted_index], tmp_path / "videos" / ".recycle_bin")
    assert store.get_secondary_view(ids[0]) is None
    store.undo_latest_recycle(tmp_path / "videos" / ".recycle_bin")
    assert client.get(endpoint).json()["offset_ms"] == 1234
    assert client.get(endpoint).json()["use_primary_audio"] == 1


@pytest.fixture
def secondary_upload_library(secondary_library, tmp_path, monkeypatch):
    monkeypatch.setattr(api_main, "DIRECTORY_PROJECT_ROOT", tmp_path / "application")
    monkeypatch.setattr(video_pipeline, "video_duration_ms", lambda _: 1000)
    _, store, ids = secondary_library
    with store.connect() as connection:
        for video_id in ids:
            row = store.get_video(video_id)
            connection.execute("UPDATE videos SET content_hash = ? WHERE id = ?",
                               (store.file_hash(Path(row["video_path"])), video_id))
    return secondary_library


def upload_secondary(client, primary, content=b"synthetic secondary upload"):
    return client.post("/api/videos/upload", params={"filename": "secondary.mp4", "secondary_for": primary},
                       content=content)


def test_local_secondary_import_skips_upload_temp_preserves_source_and_stays_hidden(
    secondary_upload_library, tmp_path, monkeypatch,
):
    client, store, ids = secondary_upload_library
    settings = client.get("/api/settings/directories").json()
    values = {key: settings[key] for key in ("asr_storage_root", "audio_cache_dir", "log_dir", "video_library_dir")}
    assert client.put("/api/settings/directories", json={**values, "delete_source_after_import": False}).status_code == 200
    source = tmp_path / "local-camera.mp4"
    source.write_bytes(b"synthetic local secondary")

    def unexpected_temp(**_kwargs):
        raise AssertionError("local import must not stage an upload")

    monkeypatch.setattr(api_main, "TemporaryDirectory", unexpected_temp)
    endpoint = f"/api/videos/{ids[0]}/secondary-view/import"
    for existed in (False, True):
        response = client.post(endpoint, json={"path": str(source)})
        assert response.status_code == 200, response.text
        result = response.json()
        secondary_id = result["video_id"]
        assert result["existed"] is existed
        assert result["secondary_view"]["video_id"] == secondary_id
        assert bool(result["video"]["storage_warning"]) is existed
        assert Path(result["video"]["video_path"]).read_bytes() == source.read_bytes()
        assert store.get_video(secondary_id)["is_secondary"] == 1
        assert {row["id"] for row in store.list_videos()} == set(ids)
        assert not store.has_transcript(secondary_id)
        assert not store.list_recent_jobs()
    managed = result["video"]["video_path"]
    assert client.post(endpoint, json={"path": managed}).json()["existed"]
    assert source.read_bytes() == b"synthetic local secondary"
    assert len(store.list_videos(include_secondary=True)) == 4


def test_local_secondary_rejects_primary_missing_path_and_missing_parent_without_changes(
    secondary_upload_library, tmp_path,
):
    client, store, ids = secondary_upload_library
    before = dict(store.get_video(ids[0]))
    source = Path(before["video_path"])
    endpoint = f"/api/videos/{ids[0]}/secondary-view/import"
    assert client.post(endpoint, json={"path": str(source)}).status_code == 400
    assert client.post(endpoint, json={"path": str(tmp_path / "missing.mp4")}).status_code == 400
    assert client.post("/api/videos/999/secondary-view/import", json={"path": str(source)}).status_code == 404
    assert dict(store.get_video(ids[0])) == before
    assert source.read_bytes() == b"synthetic camera 0"
    assert len(store.list_videos(include_secondary=True)) == 3
    assert store.get_secondary_view(ids[0]) is None


@pytest.mark.parametrize("block", ["running", "resource_paused", "migration"])
def test_local_secondary_import_obeys_file_change_guards(secondary_upload_library, block):
    client, store, ids = secondary_upload_library
    source = Path(store.get_video(ids[1])["video_path"])
    if block == "migration":
        client.app.state.storage_migrating.set()
    else:
        store.create_job("busy-import", "video_process", ids[0])
        store.update_job("busy-import", status=block)
    try:
        response = client.post(f"/api/videos/{ids[0]}/secondary-view/import", json={"path": str(source)})
        assert response.status_code == 409
        assert source.read_bytes() == b"synthetic camera 1"
        assert store.get_secondary_view(ids[0]) is None
    finally:
        client.app.state.storage_migrating.clear()


def test_local_secondary_failed_link_keeps_original_and_valid_copy(secondary_upload_library, monkeypatch):
    client, store, ids = secondary_upload_library
    source = Path(store.get_video(ids[1])["video_path"])

    def fail(*_args):
        raise ValueError("synthetic link failure")

    monkeypatch.setattr(store, "set_secondary_view", fail)
    response = client.post(f"/api/videos/{ids[0]}/secondary-view/import", json={"path": str(source)})
    assert response.status_code == 400
    assert source.read_bytes() == b"synthetic camera 1"
    assert Path(store.get_video(ids[1])["video_path"]).read_bytes() == source.read_bytes()
    assert store.get_secondary_view(ids[0]) is None


@pytest.mark.parametrize("local", [False, True])
def test_secondary_import_hashes_only_source_and_verified_copy(secondary_upload_library, tmp_path, monkeypatch, local):
    client, store, ids = secondary_upload_library
    original_hash = store.file_hash
    paths = []

    def file_hash(path):
        paths.append(Path(path))
        return original_hash(path)

    monkeypatch.setattr(store, "file_hash", file_hash)
    if local:
        source = tmp_path / "local-camera.mp4"
        source.write_bytes(b"synthetic local secondary")
        response = client.post(f"/api/videos/{ids[0]}/secondary-view/import", json={"path": str(source)})
    else:
        response = upload_secondary(client, ids[0])
    assert response.status_code == 200, response.text
    assert len(paths) == (2 if local else 1)
    assert paths[0].suffix == ".mp4"
    if local:
        assert paths[1].suffix == ".part"


def test_local_secondary_import_disk_work_keeps_health_responsive(secondary_upload_library, monkeypatch):
    import httpx

    client, store, ids = secondary_upload_library
    entered, health_finished = Event(), Event()
    health_during_import = []
    original_register = api_main.register_managed_video

    def register(*args, **kwargs):
        entered.set()
        health_during_import.append(health_finished.wait(3))
        return original_register(*args, **kwargs)

    monkeypatch.setattr(api_main, "register_managed_video", register)
    source = store.get_video(ids[1])["video_path"]

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app),
                                    base_url="http://127.0.0.1") as requests:
            async def heartbeat():
                try:
                    assert await asyncio.to_thread(entered.wait, 5)
                    return await requests.get("/api/health")
                finally:
                    health_finished.set()

            return await asyncio.gather(
                requests.post(f"/api/videos/{ids[0]}/secondary-view/import", json={"path": source}),
                heartbeat(),
            )

    try:
        imported, health = asyncio.run(exercise())
    finally:
        health_finished.set()
    assert imported.status_code == health.status_code == 200
    assert health_during_import == [True]


def test_secondary_upload_waiting_for_import_lock_keeps_health_responsive(secondary_upload_library):
    import httpx

    client, _, ids = secondary_upload_library
    locked, health_finished = Event(), Event()
    health_during_import = []

    def hold_import_lock():
        with client.app.state.jobs._lock:
            locked.set()
            health_during_import.append(health_finished.wait(3))

    worker = Thread(target=hold_import_lock)
    worker.start()
    assert locked.wait(3)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app),
                                    base_url="http://127.0.0.1") as requests:
            async def heartbeat():
                # Give the upload a turn to reach its lock acquisition.
                await asyncio.sleep(0.05)
                try:
                    return await requests.get("/api/health")
                finally:
                    health_finished.set()

            return await asyncio.gather(
                requests.post("/api/videos/upload", params={
                    "filename": "secondary.mp4", "secondary_for": ids[0],
                }, content=b"synthetic concurrent upload"),
                heartbeat(),
            )

    try:
        uploaded, health = asyncio.run(exercise())
    finally:
        health_finished.set()
        worker.join(5)
    assert uploaded.status_code == 200, uploaded.text
    assert health.status_code == 200
    assert health_during_import == [True], "upload lock acquisition blocked the service event loop"


@pytest.mark.parametrize("phase", ["temporary_creation", "registration", "temporary_cleanup"])
def test_secondary_upload_disk_work_keeps_health_responsive(secondary_upload_library, monkeypatch, phase):
    import httpx

    client, store, ids = secondary_upload_library
    entered, health_finished = Event(), Event()
    health_during_disk_work, temporary_paths = [], []
    original_directory = api_main.TemporaryDirectory
    original_register = api_main.register_managed_video

    def pause():
        entered.set()
        health_during_disk_work.append(health_finished.wait(3))

    @contextmanager
    def directory(**kwargs):
        if phase == "temporary_creation":
            pause()
        with original_directory(**kwargs) as temporary:
            temporary_paths.append(Path(temporary))
            try:
                yield temporary
            finally:
                if phase == "temporary_cleanup":
                    pause()

    def register(*args, **kwargs):
        if phase == "registration":
            pause()
        return original_register(*args, **kwargs)

    monkeypatch.setattr(api_main, "TemporaryDirectory", directory)
    monkeypatch.setattr(api_main, "register_managed_video", register)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app),
                                    base_url="http://127.0.0.1") as requests:
            async def heartbeat():
                try:
                    assert await asyncio.to_thread(entered.wait, 5)
                    return await requests.get("/api/health")
                finally:
                    health_finished.set()

            return await asyncio.gather(
                requests.post("/api/videos/upload", params={
                    "filename": "secondary.mp4", "secondary_for": ids[0],
                }, content=b"synthetic disk work upload"),
                heartbeat(),
            )

    try:
        uploaded, health = asyncio.run(exercise())
    finally:
        health_finished.set()
    assert uploaded.status_code == 200, uploaded.text
    assert health.status_code == 200
    assert health_during_disk_work == [True], f"{phase} blocked the service event loop"
    assert store.get_secondary_view(ids[0])["video_id"] == uploaded.json()["video_id"]
    assert temporary_paths and all(not path.exists() for path in temporary_paths)


@pytest.mark.parametrize("cancellation", ["asyncio", "anyio"])
def test_cancelled_secondary_upload_closes_file_before_removing_directory(
    secondary_upload_library, monkeypatch, cancellation,
):
    import anyio
    from starlette.requests import Request

    client, store, ids = secondary_upload_library
    original_directory, original_open = api_main.TemporaryDirectory, Path.open
    directories, contexts, files, closed_before_cleanup = [], [], [], []

    @contextmanager
    def directory(**kwargs):
        temporary = original_directory(**kwargs)
        directories.append(temporary)
        try:
            yield temporary.name
        finally:
            closed_before_cleanup.append(bool(files) and all(file.closed for file in files))
            temporary.cleanup()

    def make_directory(**kwargs):
        context = directory(**kwargs)
        contexts.append(context)  # Keep GC from masking skipped __exit__ calls.
        return context

    def open_file(path, *args, **kwargs):
        file = original_open(path, *args, **kwargs)
        if path.name == "secondary.mp4" and args == ("wb",):
            files.append(file)
        return file

    monkeypatch.setattr(api_main, "TemporaryDirectory", make_directory)
    monkeypatch.setattr(Path, "open", open_file)
    endpoint = next(route.endpoint for route in client.app.routes
                    if getattr(route, "path", None) == "/api/videos/upload")

    async def exercise():
        received = False
        scope = None

        async def receive():
            nonlocal received
            if not received:
                received = True
                return {"type": "http.request", "body": b"synthetic partial upload", "more_body": True}
            if cancellation == "asyncio":
                raise asyncio.CancelledError()
            scope.cancel()
            await anyio.lowlevel.checkpoint()
            raise AssertionError("cancelled stream continued")

        request = Request({"type": "http", "method": "POST"}, receive)
        if cancellation == "asyncio":
            with pytest.raises(asyncio.CancelledError):
                await endpoint(request, filename="secondary.mp4", secondary_for=ids[0])
        else:
            with anyio.CancelScope() as scope:
                await endpoint(request, filename="secondary.mp4", secondary_for=ids[0])
            assert scope.cancelled_caught

    try:
        asyncio.run(exercise())
        assert files and all(file.closed for file in files)
        assert closed_before_cleanup == [True]
        assert directories and all(not Path(directory.name).exists() for directory in directories)
        assert store.get_secondary_view(ids[0]) is None
        assert len(store.list_videos(include_secondary=True)) == 3
    finally:
        for file in files:
            file.close()
        for temporary in directories:
            temporary.cleanup()


def test_secondary_upload_stays_out_of_library_counts_search_and_folder_batches(secondary_upload_library):
    client, store, ids = secondary_upload_library
    folder = store.create_folder("Synthetic course")
    store.set_video_folder(ids[0], folder)
    response = upload_secondary(client, ids[0])
    assert response.status_code == 200, response.text
    secondary = response.json()["video"]["id"]
    assert response.json()["secondary_view"]["video_id"] == secondary
    assert store.get_video(secondary)["is_secondary"] == 1
    assert {row["id"] for row in store.list_videos()} == set(ids)
    assert {row["id"] for row in client.get("/api/videos").json()} == set(ids)
    assert client.get(f"/api/videos/{secondary}/stream").content == b"synthetic secondary upload"
    store.set_video_folder(secondary, folder)
    assert store.get_folder(folder)["video_count"] == 1
    assert [row["id"] for row in store.list_folder_videos(folder, recursive=True)] == [ids[0]]
    listing = client.get("/api/library", params={"folder_id": folder}).json()["items"]
    assert [item["video"]["id"] for item in listing] == [ids[0]]
    assert client.post(f"/api/folders/{folder}/process").json()["video_ids"] == [ids[0]]
    reordered = client.post("/api/library/reorder", json={
        "parent_id": folder, "ordered_items": [{"type": "video", "id": ids[0]}],
        "previous_items": [{"type": "video", "id": ids[0]}],
    })
    assert reordered.status_code == 200, reordered.text
    assert not store.has_transcript(secondary)
    assert not store.list_recent_jobs()
    store.mark_transcribed(secondary, [TranscriptSegment(1, 0, 1000, "secondary-only-search")])
    assert client.get("/api/library/search", params={"q": "secondary-only-search"}).json()["total"] == 0


def test_secondary_upload_reuses_binding_and_stays_hidden_after_unlink_and_restart(secondary_upload_library):
    client, store, ids = secondary_upload_library
    first = upload_secondary(client, ids[0]).json()
    second = upload_secondary(client, ids[0]).json()
    assert first["secondary_view"] == second["secondary_view"]
    assert second["existed"]
    assert len(store.list_videos(include_secondary=True)) == 4
    client.delete(f"/api/videos/{ids[0]}/secondary-view")
    reopened = VideoStore(store.path)
    assert {row["id"] for row in reopened.list_videos()} == set(ids)
    assert reopened.get_video(first["video"]["id"])["is_secondary"] == 1
    assert Path(reopened.get_video(first["video"]["id"])["video_path"]).is_file()


def test_secondary_upload_preserves_existing_primary_and_explicit_normal_import_promotes(secondary_upload_library):
    client, store, ids = secondary_upload_library
    normal = Path(store.get_video(ids[1])["video_path"]).read_bytes()
    linked = upload_secondary(client, ids[0], normal)
    assert linked.status_code == 200, linked.text
    assert linked.json()["secondary_view"]["video_id"] == ids[1]
    assert store.get_video(ids[1])["is_secondary"] == 0
    hidden = upload_secondary(client, ids[0]).json()["video"]["id"]
    promoted = client.post("/api/videos/upload", params={"filename": "secondary.mp4"},
                           content=b"synthetic secondary upload")
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["video"]["id"] == hidden
    assert store.get_video(hidden)["is_secondary"] == 0
    assert hidden in {row["id"] for row in VideoStore(store.path).list_videos()}
    assert store.get_secondary_view(ids[0])["video_id"] == hidden


def test_secondary_upload_rejects_main_recording_and_missing_parent_without_registration(secondary_upload_library):
    client, store, ids = secondary_upload_library
    before = dict(store.get_video(ids[0]))
    content = Path(before["video_path"]).read_bytes()
    assert upload_secondary(client, ids[0], content).status_code == 400
    assert dict(store.get_video(ids[0])) == before
    assert upload_secondary(client, 999).status_code == 404
    assert len(store.list_videos(include_secondary=True)) == 3
    assert store.get_secondary_view(ids[0]) is None


@pytest.mark.parametrize("independent_study", [False, True])
def test_legacy_secondary_migration_hides_unused_uploads_and_keeps_study_data(secondary_library, independent_study):
    _, store, ids = secondary_library
    store.set_secondary_view(ids[0], ids[1], 123)
    if independent_study:
        store.mark_transcribed(ids[1], [TranscriptSegment(1, 0, 1000, "Independent study")])
    with store.connect() as connection:
        connection.execute("ALTER TABLE videos DROP COLUMN is_secondary")
    for _ in range(2):
        store = VideoStore(store.path)
        assert bool(store.get_video(ids[1])["is_secondary"]) is not independent_study
        assert (ids[1] in {row["id"] for row in store.list_videos()}) is independent_study
        assert store.get_secondary_view(ids[0])["offset_ms"] == 123
        assert Path(store.get_video(ids[1])["video_path"]).is_file()
