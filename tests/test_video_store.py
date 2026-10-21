import sqlite3

from core import TranscriptSegment, TriggerMatch
from video_store import VideoStore


def test_video_store_persists_transcript_and_deletable_marker(tmp_path):
    store = VideoStore(tmp_path / "video.db")
    video_id = store.create_video("hash", "lesson.mp4", "lesson.mp4", "lesson.wav", 10000)
    segments = [TranscriptSegment(1, 1000, 2500, "这个会考。")]
    store.mark_transcribed(video_id, segments)
    assert store.get_segments(video_id)[0].text == "这个会考。"
    marker_id = store.add_marker(video_id, 1000, 2500, "考点", "笔记", "这个会考。", "会考", 0.9)
    assert store.add_marker(video_id, 1000, 2500, "另一个标题", "另一个笔记", "这个会考。", "会考", 0.8) == marker_id
    assert len(store.list_markers(video_id)) == 1
    store.delete_marker(marker_id)
    assert store.list_markers(video_id) == []
    restored_id = store.add_marker(video_id, 1000, 2500, "考点", "笔记", "这个会考。", "会考", 0.9)
    restored = store.list_markers(video_id)
    assert restored_id != marker_id
    assert len(restored) == 1
    assert restored[0]["source_quote"] == "这个会考。"


def test_file_hash_matches_data_hash_and_streams_from_disk(tmp_path):
    path = tmp_path / "lesson.mp4"
    data = b"small local video fixture"
    path.write_bytes(data)
    assert VideoStore.file_hash(path, chunk_size=4) == VideoStore.content_hash(data)


def test_video_store_persists_keyword_hit_anchors(tmp_path):
    store = VideoStore(tmp_path / "video.db")
    video_id = store.create_video("hash", "lesson.mp4", "lesson.mp4", "lesson.wav", 10000)
    segments = [TranscriptSegment(3, 3000, 4500, "这个会考。")]
    match = TriggerMatch(3, "会考", "strong", "考试明确", 3000, 4500)

    store.replace_keyword_hits(video_id, [match], segments)

    hit = store.list_keyword_hits(video_id)[0]
    assert hit["phrase"] == "会考"
    assert hit["source_quote"] == "这个会考。"


def test_video_store_keeps_markers_and_cached_runs_separate_by_ai_module(tmp_path):
    store = VideoStore(tmp_path / "video.db")
    video_id = store.create_video("hash", "lesson.mp4", "lesson.mp4", "lesson.wav", 10000)
    store.add_marker(video_id, 1000, 2500, "高召回", "", "证据", "AI-confirmed", 0.8, "high_recall")
    store.add_marker(video_id, 1000, 2500, "严格", "", "证据", "AI-confirmed", 0.9, "strict_exam_intent")
    store.set_analysis_run(video_id, "strict_exam_intent", "completed", 1)

    assert len(store.list_markers(video_id, "high_recall")) == 1
    assert len(store.list_markers(video_id, "strict_exam_intent")) == 1
    assert store.get_analysis_run(video_id, "strict_exam_intent")["candidate_count"] == 1


def test_video_store_keeps_experiment_results_separate(tmp_path):
    store = VideoStore(tmp_path / "video.db")
    video_id = store.create_video("hash", "lesson.mp4", "lesson.mp4", "lesson.wav", 10000)
    store.create_experiment("exp-a", video_id, "严格方案", "strict_exam_intent", "experiment:exp-a")
    store.create_experiment("exp-b", video_id, "高召回方案", "high_recall", "experiment:exp-b")
    store.add_marker(video_id, 0, 2000, "A", "", "证据", "AI-confirmed", 0.9, "experiment:exp-a")
    store.add_marker(video_id, 2000, 4000, "B", "", "证据", "AI-possible", 0.6, "experiment:exp-b")

    assert [row["id"] for row in store.list_experiments(video_id)] == ["exp-a", "exp-b"]
    assert len(store.list_markers(video_id, "experiment:exp-a")) == 1
    assert len(store.list_markers(video_id, "experiment:exp-b")) == 1


def test_video_store_uses_friendly_name_and_tracks_source_folder(tmp_path):
    store = VideoStore(tmp_path / "video.db")
    source_path = tmp_path / "calculus" / "002_opaque_name.mp4"
    video_id = store.create_video("hash", source_path.name, str(source_path), "lesson.wav", 10000)

    video = store.get_video(video_id)
    assert video["display_name"] == "002_opaque_name"
    assert video["source_folder"] == str(source_path.parent.resolve())

    store.update_video_display_name(video_id, "第 02 讲 - 微积分")
    moved_path = tmp_path / "archive" / source_path.name
    store.update_video_path(video_id, str(moved_path))
    updated = store.get_video(video_id)
    assert updated["display_name"] == "第 02 讲 - 微积分"
    assert updated["source_folder"] == str(moved_path.parent.resolve())


def test_video_store_migrates_existing_database_with_friendly_metadata(tmp_path):
    database_path = tmp_path / "legacy.db"
    legacy_path = tmp_path / "legacy" / "unreadable_name.mp4"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content_hash TEXT NOT NULL UNIQUE,
                filename TEXT NOT NULL,
                video_path TEXT NOT NULL,
                audio_path TEXT NOT NULL,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'uploaded',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            "INSERT INTO videos(content_hash, filename, video_path, audio_path, duration_ms) VALUES (?, ?, ?, ?, ?)",
            ("legacy-hash", legacy_path.name, str(legacy_path), "legacy.wav", 1000),
        )

    store = VideoStore(database_path)
    migrated = store.get_video_by_hash("legacy-hash")
    assert migrated["display_name"] == "unreadable_name"
    assert migrated["source_folder"] == str(legacy_path.parent.resolve())


def test_interrupted_processing_video_can_be_marked_failed_for_retry(tmp_path):
    store = VideoStore(tmp_path / "video.db")
    video_id = store.create_video("hash", "lesson.mp4", "lesson.mp4", "lesson.wav", 10000)
    store.set_status(video_id, "processing")
    store.set_analysis_run(video_id, "high_recall", "processing")

    assert store.mark_interrupted_video_failed(video_id) is True
    assert store.get_video(video_id)["status"] == "failed"
    assert store.get_analysis_run(video_id, "high_recall")["status"] == "failed"
    assert store.mark_interrupted_video_failed(video_id) is False


def test_marker_review_status_persists_mastered_and_excluded_items(tmp_path):
    store = VideoStore(tmp_path / "video.db")
    video_id = store.create_video("hash", "lesson.mp4", "lesson.mp4", "temporary.wav", 10000)
    first = store.add_marker(video_id, 1000, 2000, "重点一", "", "证据", "AI-confirmed", 0.9)
    second = store.add_marker(video_id, 3000, 4000, "重点二", "", "证据", "AI-confirmed", 0.8)

    store.set_marker_review_status(first, "mastered")
    store.set_marker_review_status(second, "excluded")

    visible = store.list_markers(video_id)
    assert [row["id"] for row in visible] == [first]
    assert visible[0]["review_status"] == "mastered"
    assert [row["id"] for row in store.list_excluded_markers(video_id)] == [second]

    store.set_marker_review_status(second, "active")
    assert [row["id"] for row in store.list_markers(video_id)] == [first, second]


def test_video_store_caches_ai_chunk_selection(tmp_path):
    store = VideoStore(tmp_path / "video.db")
    video_id = store.create_video("hash", "lesson.mp4", "lesson.mp4", "temporary.wav", 10000)

    assert store.get_ai_chunk_selection(video_id, "strict_exam_intent", "chunk") is None
    store.save_ai_chunk_selection(video_id, "strict_exam_intent", "chunk", '{"candidates":[]}')
    assert store.get_ai_chunk_selection(video_id, "strict_exam_intent", "chunk") == '{"candidates":[]}'


def test_video_store_logical_folders_and_mixed_order(tmp_path):
    store = VideoStore(tmp_path / "video.db")
    first = store.create_video("hash-1", "first.mp4", "first.mp4", "first.wav", 10000)
    second = store.create_video("hash-2", "second.mp4", "second.mp4", "second.wav", 20000)
    folder = store.create_folder("微积分")

    store.set_video_folder(first, folder)
    store.reorder_library(folder, [("video", first)])
    assert [row["id"] for row in store.list_folder_videos(folder)] == [first]
    store.set_video_folder(second, folder)
    store.reorder_library(folder, [("video", second), ("video", first)])
    assert [row["id"] for row in store.list_folder_videos(folder)] == [second, first]

    store.set_video_folder(second, None)
    assert store.get_video(second)["library_folder_id"] is None
    store.delete_folder(folder)
    assert store.get_video(first)["library_folder_id"] is None
