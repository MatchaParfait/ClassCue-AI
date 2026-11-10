"""Windows Shell recycling uses disposable bundles and never the user's library."""

import os
import json
from pathlib import Path
import shutil
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest

from classcue import windows_recycle
import classcue.video_store as video_store
from classcue.video_store import VideoStore


@pytest.mark.skipif(os.name != "nt", reason="Windows Shell integration")
def test_real_shell_round_trip_preserves_files_and_removes_bin_entry(tmp_path):
    bundle = tmp_path / "ClassCue - \u5408\u6210\u8bfe\u7a0b #1"
    bundle.mkdir()
    video = bundle / "lesson.mp4"
    video.write_bytes(b"synthetic video fixture")
    nested = bundle / "slides"
    nested.mkdir()
    (nested / "state.jpg").write_bytes(b"synthetic slide fixture")

    ticket = windows_recycle.recycle_directory(bundle)
    assert ticket is not None
    recycled = Path(ticket["path"])
    try:
        assert not bundle.exists()
        assert recycled.is_dir()
        assert (recycled / video.name).read_bytes() == b"synthetic video fixture"
    finally:
        windows_recycle.restore_directory(ticket)
    assert video.read_bytes() == b"synthetic video fixture"
    assert (nested / "state.jpg").read_bytes() == b"synthetic slide fixture"
    assert not recycled.exists()
    with windows_recycle._operation(windows_recycle._FLAGS) as (shell, _):
        with pytest.raises(ValueError, match="已被清空或移走"):
            shell.recycled_item(ticket)


def test_restore_after_windows_bin_was_emptied_never_recreates_an_empty_bundle(tmp_path):
    original = tmp_path / "staging" / "ClassCue"
    with pytest.raises(ValueError, match="已被清空或移走"):
        windows_recycle.restore_directory({
            "original": str(original), "path": str(tmp_path / "missing"), "pidl": "",
        })
    assert not original.exists()


@pytest.fixture
def library(tmp_path, monkeypatch):
    root = tmp_path / "library"
    videos = root / "videos"
    videos.mkdir(parents=True)
    store = VideoStore(root / "project.db")
    video = videos / "lesson.mp4"
    video.write_bytes(b"synthetic video")
    audio = root / "audio.wav"
    audio.write_bytes(b"synthetic audio")
    video_id = store.create_video("synthetic", video.name, str(video), str(audio), 10000)
    fake_bin = tmp_path / "windows-bin"
    fake_bin.mkdir()
    tickets = []

    def recycle(directory):
        if not directory.exists():
            return None
        target = fake_bin / uuid4().hex
        directory.rename(target)
        ticket = {"original": str(directory), "path": str(target)}
        tickets.append(ticket)
        return ticket

    def restore(ticket):
        if not ticket.get("path"):
            ticket = next(value for value in tickets if value["original"] == ticket["original"])
        original, source = Path(ticket["original"]), Path(ticket["path"])
        if original.exists():
            raise ValueError("staging conflict")
        if not source.exists():
            raise ValueError("Windows 回收站中的文件已被清空或移走，无法撤销删除")
        original.parent.mkdir(parents=True, exist_ok=True)
        source.rename(original)
        return original

    monkeypatch.setattr(video_store, "recycle_directory", recycle)
    monkeypatch.setattr(video_store, "restore_directory", restore)
    return SimpleNamespace(
        store=store, video=video, audio=audio, video_id=video_id,
        trash=videos / ".recycle_bin", fake_bin=fake_bin, tickets=tickets,
        recycle=recycle, restore=restore,
    )


def payload(store):
    with store.connect() as connection:
        return json.loads(connection.execute(
            "SELECT payload_json FROM recycle_bin WHERE undone_at IS NULL").fetchone()[0])


def test_delete_moves_one_bundle_to_windows_and_undo_cleans_local_staging(library):
    store = library.store
    store.trash_video(library.video_id, library.trash)
    saved = payload(store)
    ticket = saved["windows_recycle"]
    assert "ClassCue - lesson.mp4" in ticket["original"]
    assert not Path(ticket["original"]).exists()
    assert not library.video.exists() and not library.audio.exists()
    assert len(list(library.fake_bin.iterdir())) == 1
    assert store.get_video(library.video_id) is None

    VideoStore(store.path).undo_latest_library_edit(library.trash)
    assert library.video.read_bytes() == b"synthetic video"
    assert library.audio.read_bytes() == b"synthetic audio"
    assert not list(library.fake_bin.iterdir())
    assert not list(library.trash.iterdir())
    assert store.get_video(library.video_id) is not None


def test_redo_recycles_a_new_bundle_and_keeps_future_undo(library):
    store = library.store
    store.trash_video(library.video_id, library.trash)
    first = payload(store)["windows_recycle"]
    store.undo_latest_library_edit(library.trash)
    store.undo_latest_library_edit(library.trash, redo=True)
    second = payload(store)["windows_recycle"]
    assert first != second
    assert not Path(first["path"]).exists()
    assert Path(second["path"]).is_dir()
    store.undo_latest_library_edit(library.trash)
    assert library.video.read_bytes() == b"synthetic video"


def test_unavailable_windows_bin_rolls_back_files_and_library(library, monkeypatch):
    def unavailable(_directory):
        raise OSError("Windows recycle unavailable")

    monkeypatch.setattr(video_store, "recycle_directory", unavailable)
    with pytest.raises(OSError, match="recycle unavailable"):
        library.store.trash_video(library.video_id, library.trash)
    assert library.video.read_bytes() == b"synthetic video"
    assert library.audio.read_bytes() == b"synthetic audio"
    assert library.store.get_video(library.video_id) is not None
    assert library.store.latest_library_undo() is None
    assert not list(library.trash.rglob("*"))


def test_delete_commit_failure_restores_bundle_from_windows_before_file_rollback(library, monkeypatch):
    class RejectCommit(video_store.ClosingConnection):
        def execute(self, sql, *args, **kwargs):
            result = super().execute(sql, *args, **kwargs)
            if " ".join(sql.upper().split()).startswith("UPDATE RECYCLE_BIN SET PAYLOAD_JSON"):
                self.set_authorizer(
                    lambda action, value, *_: sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_TRANSACTION and value == "COMMIT"
                    else sqlite3.SQLITE_OK)
            return result

    with monkeypatch.context() as scoped:
        scoped.setattr(video_store, "ClosingConnection", RejectCommit)
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            library.store.trash_video(library.video_id, library.trash)
    assert library.video.read_bytes() == b"synthetic video"
    assert library.audio.read_bytes() == b"synthetic audio"
    assert library.store.get_video(library.video_id) is not None
    assert library.store.latest_recycle() is None
    assert not list(library.fake_bin.iterdir())


def test_emptied_bin_fails_undo_without_recreating_library_records(library):
    store = library.store
    store.trash_video(library.video_id, library.trash)
    latest = store.latest_library_undo()
    shutil.rmtree(library.tickets[-1]["path"])
    with pytest.raises(ValueError, match="已被清空或移走"):
        store.undo_latest_library_edit(library.trash)
    assert store.get_video(library.video_id) is None
    assert store.latest_library_undo() == latest
    assert not library.video.exists()


def test_bundle_already_restored_in_windows_can_be_undone_in_app(library):
    library.store.trash_video(library.video_id, library.trash)
    library.restore(library.tickets[-1])
    library.store.undo_latest_library_edit(library.trash)
    assert library.video.read_bytes() == b"synthetic video"
    assert library.store.get_video(library.video_id) is not None


def test_undo_conflict_keeps_both_copies_and_allows_retry(library):
    store = library.store
    store.trash_video(library.video_id, library.trash)
    library.video.write_bytes(b"external replacement")
    with pytest.raises(ValueError, match="原位置已有同名文件"):
        store.undo_latest_library_edit(library.trash)
    assert Path(library.tickets[-1]["path"]).is_dir()
    assert library.video.read_bytes() == b"external replacement"
    assert store.get_video(library.video_id) is None
    library.video.unlink()
    store.undo_latest_library_edit(library.trash)
    assert library.video.read_bytes() == b"synthetic video"
    assert not list(library.trash.iterdir())


@pytest.mark.skipif(os.name != "nt", reason="Windows Shell integration")
@pytest.mark.parametrize("folder_delete", [False, True])
def test_real_video_library_delete_undo_and_redo(library, monkeypatch, folder_delete):
    store = library.store
    monkeypatch.setattr(video_store, "recycle_directory", windows_recycle.recycle_directory)
    monkeypatch.setattr(video_store, "restore_directory", windows_recycle.restore_directory)
    if folder_delete:
        folder = store.create_folder("Synthetic course")
        child = store.create_folder("Empty child", folder)
        store.set_video_folder(library.video_id, folder)
        store.trash_folder(folder, library.trash)
    else:
        store.trash_video(library.video_id, library.trash)
    try:
        ticket = payload(store)["windows_recycle"]
        assert Path(ticket["path"]).is_dir()
        assert not Path(ticket["original"]).exists()
        VideoStore(store.path).undo_latest_library_edit(library.trash)
        assert library.video.read_bytes() == b"synthetic video"
        assert library.audio.read_bytes() == b"synthetic audio"
        if folder_delete:
            assert store.get_folder(child) is not None
        store.undo_latest_library_edit(library.trash, redo=True)
        assert Path(payload(store)["windows_recycle"]["path"]).is_dir()
    finally:
        if store.get_video(library.video_id) is None:
            store.undo_latest_library_edit(library.trash)
    assert not list(library.trash.iterdir())


@pytest.mark.skipif(os.name != "nt", reason="Windows Shell integration")
def test_shell_sink_refuses_a_permanent_delete():
    sink = windows_recycle._DeleteSink(None, Path("synthetic"))
    assert sink.callbacks[11](None, 0, None) < 0
    assert isinstance(sink.error, OSError)


def test_lost_worker_reply_uses_durable_receipt(library, monkeypatch):
    def lost_reply(action, value):
        assert action == "recycle"
        directory = Path(value)
        ticket = library.recycle(directory)
        windows_recycle.write_recycle_record(windows_recycle.recycle_receipt_path(directory), ticket)
        raise OSError("synthetic worker exited before stdout")

    monkeypatch.setattr(windows_recycle, "_worker", lost_reply)
    monkeypatch.setattr(video_store, "recycle_directory", windows_recycle.recycle_directory)
    library.store.trash_video(library.video_id, library.trash)
    assert payload(library.store)["windows_recycle"] == library.tickets[-1]
    assert library.store.get_video(library.video_id) is None
    assert not library.store._recycle_journal().exists()
    assert not windows_recycle.recycle_receipt_path(Path(library.tickets[-1]["original"])).exists()
    library.store.undo_latest_library_edit(library.trash)
    assert library.video.read_bytes() == b"synthetic video"


def test_lost_receipt_rolls_back_via_original_shell_identity(library, monkeypatch):
    def lost_receipt(directory):
        library.recycle(directory)
        raise OSError("synthetic receipt lost")

    monkeypatch.setattr(video_store, "recycle_directory", lost_receipt)
    with pytest.raises(OSError, match="receipt lost"):
        library.store.trash_video(library.video_id, library.trash)
    assert library.store.get_video(library.video_id) is not None
    assert library.video.read_bytes() == b"synthetic video"
    assert library.audio.read_bytes() == b"synthetic audio"
    assert not library.store._recycle_journal().exists()


def test_failed_compensation_recovers_before_retrying_delete(library, monkeypatch):
    class RejectCommit(video_store.ClosingConnection):
        def execute(self, sql, *args, **kwargs):
            result = super().execute(sql, *args, **kwargs)
            if " ".join(sql.upper().split()).startswith("UPDATE RECYCLE_BIN SET PAYLOAD_JSON"):
                self.set_authorizer(
                    lambda action, value, *_: sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_TRANSACTION and value == "COMMIT"
                    else sqlite3.SQLITE_OK)
            return result

    def failed_restore(_ticket):
        raise OSError("synthetic Shell restore failed")

    with monkeypatch.context() as scoped:
        scoped.setattr(video_store, "ClosingConnection", RejectCommit)
        scoped.setattr(video_store, "restore_directory", failed_restore)
        with pytest.raises(OSError, match="恢复记录已保留"):
            library.store.trash_video(library.video_id, library.trash)
    assert library.store.get_video(library.video_id) is not None
    assert library.store._recycle_journal().is_file()
    original_bundle = Path(library.tickets[-1]["original"])
    library.restore(library.tickets[-1])
    library.store.trash_video(library.video_id, library.trash)
    assert not original_bundle.exists()
    assert all(item.get("moved") for item in payload(library.store)["files"])
    assert payload(library.store)["windows_recycle"] == library.tickets[-1]
    assert not library.store._recycle_journal().exists()
    library.store.undo_latest_library_edit(library.trash)
    assert library.video.read_bytes() == b"synthetic video"


def test_changed_windows_restored_bytes_never_restore_database(library):
    store = library.store
    store.trash_video(library.video_id, library.trash)
    saved = payload(store)
    library.restore(saved["windows_recycle"])
    video_record = next(item for item in saved["files"] if item["role"] == "video")
    staged_video = Path(video_record["trash"])
    staged_video.write_bytes(b"changed synthetic video")
    latest = store.latest_library_undo()
    with pytest.raises(ValueError, match="回收文件内容已变化"):
        store.undo_latest_library_edit(library.trash)
    assert not library.video.exists() and not library.audio.exists()
    assert store.get_video(library.video_id) is None
    assert store.latest_library_undo() == latest
    assert staged_video.read_bytes() == b"changed synthetic video"


def test_committed_delete_journal_does_not_restore_deleted_files(library):
    store = library.store
    store.trash_video(library.video_id, library.trash)
    saved = payload(store)
    windows_recycle.write_recycle_record(store._recycle_journal(), {
        "operation_id": store.latest_recycle()["operation_id"],
        "directory": saved["windows_recycle"]["original"], "files": saved["files"],
        "windows_recycle": saved["windows_recycle"],
    })
    VideoStore(store.path).recover_pending_recycle()
    assert store.get_video(library.video_id) is None
    assert Path(saved["windows_recycle"]["path"]).is_dir()
    assert not store._recycle_journal().exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows Shell integration")
def test_real_shell_recovers_when_only_original_bundle_identity_is_known(tmp_path):
    bundle = tmp_path / "ClassCue - receipt recovery"
    bundle.mkdir()
    video = bundle / "lesson.mp4"
    video.write_bytes(b"synthetic video fixture")
    ticket = windows_recycle.recycle_directory(bundle)
    try:
        restored = windows_recycle.restore_directory({"original": str(bundle)})
        assert restored == bundle
        assert video.read_bytes() == b"synthetic video fixture"
    finally:
        if not bundle.exists():
            windows_recycle.restore_directory(ticket)


def test_worker_timeout_is_a_recoverable_file_error(monkeypatch):
    def timeout(*_args, **_kwargs):
        raise windows_recycle.subprocess.TimeoutExpired("synthetic worker", 60)

    monkeypatch.setattr(windows_recycle.subprocess, "run", timeout)
    with pytest.raises(OSError, match="已保留恢复记录"):
        windows_recycle._worker("restore", {})
