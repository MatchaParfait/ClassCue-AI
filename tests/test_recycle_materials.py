"""Recycling a video or folder must preserve question and Bilibili evidence."""

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from classcue.bilibili_frames import FrameStore
from classcue.bilibili_store import BilibiliStore, StudyContext
from classcue.core import TranscriptSegment
from classcue.question_captures import QuestionCaptures
from classcue.question_analysis import QuestionMaterial
from classcue.question_store import QuestionStore
import classcue.video_store as video_store
from classcue.video_store import VideoStore
from tests.bilibili_visual_fakes import synthetic_frame
from tests.test_question_analysis import png


UNIT = {"id": "BV1synthetic:7", "bvid": "BV1synthetic", "cid": 7, "page": 1, "duration_ms": 10000}


@pytest.fixture
def library(tmp_path):
    videos = tmp_path / "library" / "videos"
    videos.mkdir(parents=True)
    audio = tmp_path / "library" / "audio"
    audio.mkdir()
    store = VideoStore(tmp_path / "library" / "project.db")
    return SimpleNamespace(store=store, videos=videos, audio=audio, trash=videos / ".recycle_bin")


def add_video(library, name, content):
    video_file = library.videos / name
    video_file.write_bytes(content)
    audio_file = library.audio / (video_file.stem + ".wav")
    audio_file.write_bytes(b"audio fixture " + name.encode())
    video = library.store.create_video(
        VideoStore.content_hash(content), name, str(video_file), str(audio_file), 10000)
    return video, video_file, audio_file


def add_evidence(store, video):
    store.mark_transcribed(video, [TranscriptSegment(1, 0, 5000, "合成课堂材料")])
    marker = store.add_marker(video, 0, 5000, "候选键", "", "合成课堂材料", "重点", .8)
    repo = BilibiliStore(store)
    repo.save_context(marker, StudyContext(course="数据库"))
    question = QuestionStore(store)
    question.save_material(marker, QuestionMaterial(text="求候选键"))
    question_run = question.create_run(question.snapshot(marker), {"model": "fake-vision"})
    capture = QuestionCaptures(store).add(marker, 0, png())
    run = repo.create_run(repo.snapshot(marker), {"model": "synthetic"})
    run["candidates"] = [{"units": [dict(UNIT)]}]
    run["trace"] = [{"seq": 1, "event": "planned"}]
    repo.save_run(run)
    frame = FrameStore(store).add(run, UNIT, 1000, 1000, synthetic_frame(), "decoded_frame")
    repo.cache_put(f"cache:{video}", {"video": video}, video_id=video)
    return SimpleNamespace(marker=marker, question_run=question_run, capture=capture, run=run, frame=frame)


def test_video_trash_and_undo_restore_all_related_tables_and_images(library):
    store = library.store
    video, video_file, audio_file = add_video(library, "lesson.mp4", b"video fixture")
    evidence = add_evidence(store, video)

    store.trash_video(video, library.trash)
    assert store.get_video(video) is None
    assert not video_file.exists() and not audio_file.exists()
    assert QuestionCaptures(store).list(evidence.marker) == []
    assert not QuestionCaptures(store).path(evidence.capture["id"]).exists()
    assert not FrameStore(store).path(evidence.frame["id"]).exists()

    result = store.undo_latest_recycle(library.trash)
    assert result["videos"] == 1
    assert store.get_video(video)["video_path"] == str(video_file.resolve())
    assert video_file.read_bytes() == b"video fixture"
    assert audio_file.read_bytes() == b"audio fixture lesson.mp4"
    assert [marker["id"] for marker in store.list_markers(video)] == [evidence.marker]

    question = QuestionStore(store)
    assert question.material(evidence.marker)["text"] == "求候选键"
    assert [row["id"] for row in QuestionCaptures(store).list(evidence.marker)] == [evidence.capture["id"]]
    capture_bytes = QuestionCaptures(store).path(evidence.capture["id"]).read_bytes()
    assert capture_bytes == png()
    assert hashlib.sha256(capture_bytes).hexdigest() == evidence.capture["image_hash"]
    question_runs = question.runs(evidence.marker)
    assert [run["id"] for run in question_runs] == [evidence.question_run["id"]]
    assert question_runs[0]["stale"] is False

    repo = BilibiliStore(store)
    assert repo.context(evidence.marker)["course"] == "数据库"
    runs = repo.runs(evidence.marker)
    assert [run["id"] for run in runs] == [evidence.run["id"]]
    assert runs[0]["stale"] is False
    assert runs[0]["candidates"][0]["units"][0]["cid"] == UNIT["cid"]
    frame = FrameStore(store).get(evidence.marker, evidence.run["id"], evidence.frame["id"])
    frame_bytes = FrameStore(store).path(frame["id"]).read_bytes()
    assert frame_bytes == synthetic_frame()
    assert hashlib.sha256(frame_bytes).hexdigest() == frame["image_hash"]
    with store.connect() as connection:
        trace = connection.execute(
            "SELECT seq FROM bili_run_trace WHERE run_id = ? ORDER BY seq", (evidence.run["id"],)
        ).fetchall()
        assert [row["seq"] for row in trace] == [1]
        assert connection.execute(
            "SELECT COUNT(*) FROM bili_read_cache WHERE video_id = ?", (video,)
        ).fetchone()[0] == 1


def test_other_video_evidence_and_global_state_stay_untouched(library):
    store = library.store
    first, _, _ = add_video(library, "first.mp4", b"first video")
    second, second_file, _ = add_video(library, "second.mp4", b"second video")
    first_evidence = add_evidence(store, first)
    second_evidence = add_evidence(store, second)
    BilibiliStore(store).set_control("budget", {"units": 5})
    BilibiliStore(store).cache_put("global-cache", {"shared": True})

    store.trash_video(first, library.trash)
    assert QuestionCaptures(store).path(second_evidence.capture["id"]).read_bytes() == png()
    assert FrameStore(store).path(second_evidence.frame["id"]).read_bytes() == synthetic_frame()

    store.undo_latest_recycle(library.trash)
    assert QuestionCaptures(store).path(first_evidence.capture["id"]).read_bytes() == png()
    assert QuestionCaptures(store).path(second_evidence.capture["id"]).read_bytes() == png()
    assert FrameStore(store).path(second_evidence.frame["id"]).read_bytes() == synthetic_frame()
    assert second_file.read_bytes() == b"second video"
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM bili_controls").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM bili_read_cache WHERE video_id IS NULL").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM bili_read_cache WHERE video_id = ?", (second,)).fetchone()[0] == 1


def test_folder_trash_and_undo_restore_nested_videos_and_empty_folders(library):
    store = library.store
    course = store.create_folder("课程")
    chapter = store.create_folder("第 1 章", course)
    empty = store.create_folder("空资料", course)
    video, video_file, _ = add_video(library, "lesson.mp4", b"video fixture")
    store.set_video_folder(video, chapter)
    evidence = add_evidence(store, video)

    result = store.trash_folder(course, library.trash)
    assert result["videos"] == 1 and result["folders"] == 3
    assert store.list_folders() == []
    assert store.get_video(video) is None

    undo = store.undo_latest_recycle(library.trash)
    assert undo["folders"] == 3
    assert [folder["id"] for folder in store.list_folders()] == [course]
    assert store.get_folder(chapter)["name"] == "第 1 章"
    assert store.get_folder(empty) is not None
    assert store.get_video(video)["library_folder_id"] == chapter
    assert video_file.read_bytes() == b"video fixture"
    assert QuestionCaptures(store).path(evidence.capture["id"]).read_bytes() == png()
    assert FrameStore(store).path(evidence.frame["id"]).read_bytes() == synthetic_frame()


def test_empty_folder_trash_and_undo(library):
    store = library.store
    folder = store.create_folder("空文件夹")
    store.trash_folder(folder, library.trash)
    assert store.list_folders() == []
    assert store.undo_latest_recycle(library.trash)["folders"] == 1
    assert store.get_folder(folder)["name"] == "空文件夹"


def test_undo_of_legacy_payload_without_new_tables_still_restores_video(library):
    store = library.store
    video, video_file, _ = add_video(library, "lesson.mp4", b"video fixture")
    evidence = add_evidence(store, video)
    store.trash_video(video, library.trash)
    legacy_tables = {
        "video_transcript_segments", "video_keyword_hits", "video_markers", "video_slides",
        "video_analysis_runs", "video_ai_chunks", "video_processing_metrics",
        "video_experiments", "llm_experiment_calls", "llm_experiment_steps",
        "llm_experiment_results", "video_share_imports",
    }
    with store.connect() as connection:
        row = connection.execute(
            "SELECT operation_id, payload_json FROM recycle_bin WHERE undone_at IS NULL").fetchone()
        payload = json.loads(row["payload_json"])
        payload["related"] = {key: value for key, value in payload["related"].items() if key in legacy_tables}
        payload["files"] = [item for item in payload["files"] if item.get("role") in {"video", "audio", "slide"}]
        connection.execute(
            "UPDATE recycle_bin SET payload_json = ? WHERE operation_id = ?",
            (json.dumps(payload, ensure_ascii=False), row["operation_id"]),
        )

    result = store.undo_latest_recycle(library.trash)
    assert result["videos"] == 1
    assert [marker["id"] for marker in store.list_markers(video)] == [evidence.marker]
    assert video_file.read_bytes() == b"video fixture"


def test_failed_move_with_original_conflict_keeps_recycled_video_bytes(library, monkeypatch):
    store = library.store
    video, video_file, audio_file = add_video(library, "lesson.mp4", b"video fixture")
    real_move = shutil.move

    def conflicting_move(source, destination):
        if Path(str(source)) == audio_file:
            video_file.write_bytes(b"external replacement")
            raise OSError("simulated move failure")
        return real_move(source, destination)

    monkeypatch.setattr("classcue.video_store.shutil.move", conflicting_move)
    with pytest.raises(OSError, match="保留"):
        store.trash_video(video, library.trash)
    monkeypatch.undo()

    assert store.get_video(video) is not None
    assert video_file.read_bytes() == b"external replacement"
    retained = [path for path in library.trash.rglob("*")
                if path.is_file() and path.read_bytes() == b"video fixture"]
    assert retained


def test_failed_capture_restore_keeps_unrestorable_screenshots(library, monkeypatch):
    store = library.store
    video, video_file, audio_file = add_video(library, "lesson.mp4", b"video fixture")
    evidence = add_evidence(store, video)
    capture_path = QuestionCaptures(store).path(evidence.capture["id"])
    frame_path = FrameStore(store).path(evidence.frame["id"])
    real_move = shutil.move

    def conflicting_move(source, destination):
        if Path(str(source)) == frame_path:
            capture_path.write_bytes(b"external capture")
            raise OSError("simulated move failure")
        return real_move(source, destination)

    monkeypatch.setattr("classcue.video_store.shutil.move", conflicting_move)
    with pytest.raises(OSError, match="保留"):
        store.trash_video(video, library.trash)
    monkeypatch.undo()

    assert store.get_video(video) is not None
    assert video_file.read_bytes() == b"video fixture"
    assert audio_file.read_bytes() == b"audio fixture lesson.mp4"
    assert frame_path.read_bytes() == synthetic_frame()
    assert capture_path.read_bytes() == b"external capture"
    retained = [path for path in library.trash.rglob("*")
                if path.is_file() and path.read_bytes() == png()]
    assert retained
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM question_captures").fetchone()[0] == 1


def test_failed_move_without_conflict_restores_everything_and_clears_trash(library, monkeypatch):
    store = library.store
    video, video_file, audio_file = add_video(library, "lesson.mp4", b"video fixture")
    evidence = add_evidence(store, video)
    capture_path = QuestionCaptures(store).path(evidence.capture["id"])
    frame_path = FrameStore(store).path(evidence.frame["id"])
    real_move = shutil.move

    def failing_move(source, destination):
        if Path(str(source)) == frame_path:
            raise OSError("simulated move failure")
        return real_move(source, destination)

    monkeypatch.setattr("classcue.video_store.shutil.move", failing_move)
    with pytest.raises(OSError, match="simulated move failure"):
        store.trash_video(video, library.trash)
    monkeypatch.undo()

    assert store.get_video(video) is not None
    assert video_file.read_bytes() == b"video fixture"
    assert audio_file.read_bytes() == b"audio fixture lesson.mp4"
    assert capture_path.read_bytes() == png()
    assert frame_path.read_bytes() == synthetic_frame()
    assert not list(library.trash.rglob("*"))


def test_failed_undo_commit_returns_all_files_to_recycle_and_allows_retry(library, monkeypatch):
    store = library.store
    video, video_file, audio_file = add_video(library, "lesson.mp4", b"video fixture")
    evidence = add_evidence(store, video)
    store.trash_video(video, library.trash)
    with store.connect() as connection:
        row = connection.execute(
            "SELECT payload_json FROM recycle_bin WHERE undone_at IS NULL").fetchone()
    moved = [record for record in json.loads(row["payload_json"])["files"] if record.get("moved")]
    original_bytes = {record["trash"]: Path(record["trash"]).read_bytes() for record in moved}

    class RejectCommit(video_store.ClosingConnection):
        def execute(self, sql, *args, **kwargs):
            result = super().execute(sql, *args, **kwargs)
            if " ".join(sql.upper().split()).startswith("UPDATE RECYCLE_BIN SET UNDONE_AT"):
                self.set_authorizer(
                    lambda action, value, *_: sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_TRANSACTION and value == "COMMIT"
                    else sqlite3.SQLITE_OK
                )
            return result

    with monkeypatch.context() as scoped:
        scoped.setattr(video_store, "ClosingConnection", RejectCommit)
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            store.undo_latest_recycle(library.trash)

    assert store.get_video(video) is None
    assert not video_file.exists() and not audio_file.exists()
    for record in moved:
        assert not Path(record["original"]).exists()
        assert Path(record["trash"]).read_bytes() == original_bytes[record["trash"]]
    assert store.latest_recycle() is not None

    store.undo_latest_recycle(library.trash)
    assert store.get_video(video) is not None
    assert video_file.read_bytes() == b"video fixture"
    assert audio_file.read_bytes() == b"audio fixture lesson.mp4"
    assert QuestionCaptures(store).path(evidence.capture["id"]).read_bytes() == png()
    assert FrameStore(store).path(evidence.frame["id"]).read_bytes() == synthetic_frame()
    assert store.latest_recycle() is None


def test_undo_clears_restored_screenshot_garbage_so_later_collect_keeps_files(library):
    store = library.store
    video, _, _ = add_video(library, "lesson.mp4", b"video fixture")
    evidence = add_evidence(store, video)
    store.trash_video(video, library.trash)
    # Simulate a process interruption between the delete commit and the
    # deferred file GC: the queued cleanup entries are still in the database.
    with store.connect() as connection:
        connection.execute("INSERT INTO question_capture_garbage(id) VALUES (?)",
                           (evidence.capture["id"],))
        connection.execute("INSERT INTO bili_frame_garbage(id) VALUES (?)",
                           (evidence.frame["id"],))

    store.undo_latest_recycle(library.trash)

    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM question_capture_garbage").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM bili_frame_garbage").fetchone()[0] == 0
    QuestionCaptures(store).collect()
    FrameStore(store).collect()
    assert QuestionCaptures(store).path(evidence.capture["id"]).read_bytes() == png()
    assert FrameStore(store).path(evidence.frame["id"]).read_bytes() == synthetic_frame()


def test_failed_undo_compensation_keeps_original_and_allows_retry(library, monkeypatch):
    store = library.store
    video, video_file, audio_file = add_video(library, "lesson.mp4", b"video fixture")
    evidence = add_evidence(store, video)
    store.trash_video(video, library.trash)
    with store.connect() as connection:
        row = connection.execute(
            "SELECT payload_json FROM recycle_bin WHERE undone_at IS NULL").fetchone()
    files = json.loads(row["payload_json"])["files"]
    video_trash = Path(next(item["trash"] for item in files if item.get("role") == "video"))
    audio_trash = Path(next(item["trash"] for item in files if item.get("role") == "audio"))
    real_move = shutil.move

    def failing_move(source, destination):
        if Path(str(source)) == audio_trash:
            raise OSError("simulated restore failure")
        if Path(str(source)) == video_file:
            raise OSError("simulated compensation failure")
        return real_move(source, destination)

    monkeypatch.setattr("classcue.video_store.shutil.move", failing_move)
    with pytest.raises(OSError, match="保留"):
        store.undo_latest_recycle(library.trash)
    monkeypatch.undo()

    assert store.get_video(video) is None
    assert video_file.read_bytes() == b"video fixture"
    assert not audio_file.exists()
    assert audio_trash.is_file()
    assert store.latest_recycle() is not None

    store.undo_latest_recycle(library.trash)
    assert store.get_video(video) is not None
    assert video_file.read_bytes() == b"video fixture"
    assert audio_file.read_bytes() == b"audio fixture lesson.mp4"
    assert QuestionCaptures(store).path(evidence.capture["id"]).read_bytes() == png()
    assert FrameStore(store).path(evidence.frame["id"]).read_bytes() == synthetic_frame()
    assert store.latest_recycle() is None


def test_undo_retry_does_not_treat_replaced_same_name_original_as_recycle_material(library):
    store = library.store
    video, video_file, _ = add_video(library, "lesson.mp4", b"video fixture")
    store.trash_video(video, library.trash)
    with store.connect() as connection:
        row = connection.execute(
            "SELECT payload_json FROM recycle_bin WHERE undone_at IS NULL").fetchone()
    video_trash = Path(next(
        item["trash"] for item in json.loads(row["payload_json"])["files"]
        if item.get("role") == "video"))
    # A failed undo compensation can leave the trash copy gone while a file
    # reappears at the original location; only matching recorded bytes count.
    video_trash.unlink()
    video_file.write_bytes(b"external replacement")

    with pytest.raises(ValueError, match="回收站文件不存在"):
        store.undo_latest_recycle(library.trash)

    assert video_file.read_bytes() == b"external replacement"
    assert store.get_video(video) is None
    assert store.latest_recycle() is not None


def test_trash_identity_read_failure_keeps_original_files_and_database(library, monkeypatch):
    store = library.store
    video, video_file, audio_file = add_video(library, "lesson.mp4", b"video fixture")

    def unreadable_hash(_path):
        raise OSError("simulated identity read failure")

    monkeypatch.setattr(VideoStore, "file_hash", staticmethod(unreadable_hash))
    with pytest.raises(OSError, match="identity read failure"):
        store.trash_video(video, library.trash)

    assert store.get_video(video) is not None
    assert video_file.exists()
    assert video_file.read_bytes() == b"video fixture"
    assert audio_file.read_bytes() == b"audio fixture lesson.mp4"
    assert store.latest_recycle() is None


def strip_file_identity(store):
    """Rewrite the active recycle payload in the pre-identity legacy format."""
    with store.connect() as connection:
        row = connection.execute(
            "SELECT operation_id, payload_json FROM recycle_bin WHERE undone_at IS NULL"
        ).fetchone()
        payload = json.loads(row["payload_json"])
        for item in payload["files"]:
            item.pop("size", None)
            item.pop("sha256", None)
        connection.execute(
            "UPDATE recycle_bin SET payload_json = ? WHERE operation_id = ?",
            (json.dumps(payload, ensure_ascii=False), row["operation_id"]),
        )


def active_recycle_payload(store):
    with store.connect() as connection:
        row = connection.execute(
            "SELECT payload_json FROM recycle_bin WHERE undone_at IS NULL").fetchone()
    return json.loads(row["payload_json"])


def test_legacy_payload_double_failure_retry_uses_backfilled_identity(library, monkeypatch):
    store = library.store
    video, video_file, audio_file = add_video(library, "lesson.mp4", b"video fixture")
    evidence = add_evidence(store, video)
    store.trash_video(video, library.trash)
    strip_file_identity(store)
    payload = active_recycle_payload(store)
    video_trash = Path(next(item["trash"] for item in payload["files"] if item.get("role") == "video"))
    audio_trash = Path(next(item["trash"] for item in payload["files"] if item.get("role") == "audio"))
    real_move = shutil.move

    def failing_move(source, destination):
        if Path(str(source)) == audio_trash:
            raise OSError("simulated restore failure")
        if Path(str(source)) == video_file:
            raise OSError("simulated compensation failure")
        return real_move(source, destination)

    monkeypatch.setattr("classcue.video_store.shutil.move", failing_move)
    with pytest.raises(OSError, match="保留"):
        store.undo_latest_recycle(library.trash)
    monkeypatch.undo()

    # The first attempt captured byte identities from the intact trash copies
    # and committed them before moving anything, so bytes a failed compensation
    # left at the original location stay recognizable on retry.
    payload = active_recycle_payload(store)
    video_record = next(item for item in payload["files"] if item.get("role") == "video")
    assert video_record["size"] == len(b"video fixture")
    assert video_record["sha256"] == hashlib.sha256(b"video fixture").hexdigest()
    assert store.get_video(video) is None
    assert video_file.read_bytes() == b"video fixture"
    assert audio_trash.is_file()

    store.undo_latest_recycle(library.trash)
    assert store.get_video(video) is not None
    assert video_file.read_bytes() == b"video fixture"
    assert audio_file.read_bytes() == b"audio fixture lesson.mp4"
    assert QuestionCaptures(store).path(evidence.capture["id"]).read_bytes() == png()
    assert FrameStore(store).path(evidence.frame["id"]).read_bytes() == synthetic_frame()
    assert store.latest_recycle() is None


def test_legacy_payload_with_lost_trash_does_not_claim_unknown_same_name_file(library):
    store = library.store
    video, video_file, _ = add_video(library, "lesson.mp4", b"video fixture")
    store.trash_video(video, library.trash)
    strip_file_identity(store)
    video_trash = Path(next(
        item["trash"] for item in active_recycle_payload(store)["files"]
        if item.get("role") == "video"))
    # The trash copy was already lost before any undo established identity, so
    # an unrelated same-name file at the original location cannot be claimed.
    video_trash.unlink()
    video_file.write_bytes(b"external replacement")

    with pytest.raises(ValueError, match="缺少删除时的字节记录"):
        store.undo_latest_recycle(library.trash)

    assert video_file.read_bytes() == b"external replacement"
    assert store.get_video(video) is None
    assert store.latest_recycle() is not None


def test_legacy_identity_read_failure_moves_no_files(library, monkeypatch):
    store = library.store
    video, video_file, audio_file = add_video(library, "lesson.mp4", b"video fixture")
    store.trash_video(video, library.trash)
    strip_file_identity(store)

    def unreadable_hash(_path):
        raise OSError("simulated identity read failure")

    monkeypatch.setattr(VideoStore, "file_hash", staticmethod(unreadable_hash))
    with pytest.raises(OSError, match="identity read failure"):
        store.undo_latest_recycle(library.trash)
    monkeypatch.undo()

    assert store.get_video(video) is None
    assert not video_file.exists() and not audio_file.exists()
    assert all("sha256" not in item for item in active_recycle_payload(store)["files"])
    assert store.latest_recycle() is not None

    store.undo_latest_recycle(library.trash)
    assert store.get_video(video) is not None
    assert video_file.read_bytes() == b"video fixture"
    assert audio_file.read_bytes() == b"audio fixture lesson.mp4"


class RejectRecycleCommit(video_store.ClosingConnection):
    deny_after = ""

    def execute(self, sql, *args, **kwargs):
        result = super().execute(sql, *args, **kwargs)
        if self.deny_after and " ".join(sql.upper().split()).startswith(self.deny_after):
            self.set_authorizer(
                lambda action, value, *_: sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_TRANSACTION and value == "COMMIT"
                else sqlite3.SQLITE_OK
            )
        return result


def test_legacy_identity_commit_failure_moves_no_files(library, monkeypatch):
    store = library.store
    video, video_file, audio_file = add_video(library, "lesson.mp4", b"video fixture")
    store.trash_video(video, library.trash)
    strip_file_identity(store)

    class RejectIdentityCommit(RejectRecycleCommit):
        deny_after = "UPDATE RECYCLE_BIN SET PAYLOAD_JSON"

    with monkeypatch.context() as scoped:
        scoped.setattr(video_store, "ClosingConnection", RejectIdentityCommit)
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            store.undo_latest_recycle(library.trash)

    assert store.get_video(video) is None
    assert not video_file.exists() and not audio_file.exists()
    assert all("sha256" not in item for item in active_recycle_payload(store)["files"])
    assert store.latest_recycle() is not None

    store.undo_latest_recycle(library.trash)
    assert store.get_video(video) is not None
    assert video_file.read_bytes() == b"video fixture"
    assert audio_file.read_bytes() == b"audio fixture lesson.mp4"


def test_legacy_payload_final_commit_failure_returns_files_and_keeps_identity(library, monkeypatch):
    store = library.store
    video, video_file, audio_file = add_video(library, "lesson.mp4", b"video fixture")
    store.trash_video(video, library.trash)
    strip_file_identity(store)

    class RejectUndoCommit(RejectRecycleCommit):
        deny_after = "UPDATE RECYCLE_BIN SET UNDONE_AT"

    with monkeypatch.context() as scoped:
        scoped.setattr(video_store, "ClosingConnection", RejectUndoCommit)
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            store.undo_latest_recycle(library.trash)

    assert store.get_video(video) is None
    assert not video_file.exists() and not audio_file.exists()
    video_record = next(
        item for item in active_recycle_payload(store)["files"] if item.get("role") == "video")
    assert video_record["sha256"] == hashlib.sha256(b"video fixture").hexdigest()
    assert Path(video_record["trash"]).read_bytes() == b"video fixture"

    store.undo_latest_recycle(library.trash)
    assert store.get_video(video) is not None
    assert video_file.read_bytes() == b"video fixture"
    assert audio_file.read_bytes() == b"audio fixture lesson.mp4"
    assert store.latest_recycle() is None


def test_undo_of_legacy_payload_without_files_key_still_restores_database(library):
    store = library.store
    video, _, _ = add_video(library, "lesson.mp4", b"video fixture")
    store.trash_video(video, library.trash)
    with store.connect() as connection:
        row = connection.execute(
            "SELECT operation_id, payload_json FROM recycle_bin WHERE undone_at IS NULL").fetchone()
        payload = json.loads(row["payload_json"])
        del payload["files"]
        connection.execute(
            "UPDATE recycle_bin SET payload_json = ? WHERE operation_id = ?",
            (json.dumps(payload, ensure_ascii=False), row["operation_id"]),
        )

    assert store.undo_latest_recycle(library.trash)["videos"] == 1
    assert store.get_video(video) is not None
