import json

import pytest

from core import TranscriptSegment
from scripts.prepare_llm_asr import prepare_video
from scripts.prepare_llm_asr_batch import run_folder
from video_store import VideoStore


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    import transcriber
    import video_pipeline

    folder = tmp_path / "videos"
    folder.mkdir()
    (folder / "one.mp4").write_bytes(b"one")
    (folder / "two.mp4").write_bytes(b"two")
    workspace = tmp_path / "lab"
    monkeypatch.setattr(video_pipeline, "video_duration_ms", lambda _path: 10000)
    calls = []

    def transcribe(source, **_kwargs):
        calls.append(source.name)
        return [TranscriptSegment(1, 500, 2500, "这个知识点必须掌握。", [])], 10000

    monkeypatch.setattr(transcriber, "transcribe_audio", transcribe)
    return folder, workspace, calls


def test_batch_persists_every_video_and_reuses_without_asr_or_llm(corpus):
    folder, workspace, calls = corpus
    first = run_folder(folder, workspace)
    assert first["status"] == "completed" and first["total"] == 2
    assert len(calls) == 2 and first["llm_requests"] == 0
    preparations = {
        item["video_id"]: (workspace / "exports" / f"video-{item['video_id']}" / "preparation.json").read_bytes()
        for item in first["videos"]
    }
    second = run_folder(folder, workspace)
    assert first["batch_id"] != second["batch_id"]
    assert len(calls) == 2
    assert all(item["status"] == "reused" for item in second["videos"])
    store = VideoStore(workspace / "project.db")
    for video in store.list_videos():
        assert video["status"] == "transcribed" and store.get_segments(video["id"])
        assert not store.list_experiments(video["id"]) and not store.list_markers(video["id"])
        export = workspace / "exports" / f"video-{video['id']}"
        assert (export / "subtitles.json").is_file() and (export / "subtitles.vtt").is_file()
        assert (export / "transcript.txt").is_file()
        assert (export / "preparation.json").read_bytes() == preparations[video["id"]]
    assert len(list((workspace / "batches").glob("*/manifest.json"))) == 2
    index = (workspace / "corpus-index.md").read_text(encoding="utf-8")
    assert "one.mp4" in index and "two.mp4" in index


def test_one_failure_does_not_erase_saved_asr_or_prevent_other_videos(corpus, monkeypatch):
    import transcriber

    folder, workspace, calls = corpus
    normal = transcriber.transcribe_audio

    def transcribe(source, **kwargs):
        if source.name == "one.mp4":
            raise RuntimeError("test inference failure")
        return normal(source, **kwargs)

    monkeypatch.setattr(transcriber, "transcribe_audio", transcribe)
    result = run_folder(folder, workspace)
    assert result["status"] == "completed_with_errors"
    assert [item["status"] for item in result["videos"]] == ["failed", "completed"]
    assert calls == ["two.mp4"]
    store = VideoStore(workspace / "project.db")
    assert len(store.get_segments(result["videos"][1]["video_id"])) == 1
    manifest = json.loads((workspace / "asr-batch-latest.json").read_text(encoding="utf-8"))
    assert manifest["videos"][0]["error"] == "test inference failure"


def test_batch_refuses_to_replace_asr_for_changed_source(corpus):
    folder, workspace, calls = corpus
    original = prepare_video(folder / "one.mp4", workspace)
    (folder / "one.mp4").write_bytes(b"changed video")
    with pytest.raises(ValueError, match="refusing to overwrite"):
        run_folder(folder, workspace)
    store = VideoStore(workspace / "project.db")
    assert store.get_segments(original["video_id"])[0].text == "这个知识点必须掌握。"
    assert calls == ["one.mp4"]


def test_batch_pauses_on_resource_failure_and_keeps_pending_queue(corpus, monkeypatch):
    import transcriber
    from processing_control import ResourcePaused

    folder, workspace, _ = corpus

    def transcribe(*_args, **_kwargs):
        raise ResourcePaused("test resource pause")

    monkeypatch.setattr(transcriber, "transcribe_audio", transcribe)
    with pytest.raises(ResourcePaused):
        run_folder(folder, workspace)
    manifest = json.loads((workspace / "asr-batch-latest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "paused"
    assert [item["status"] for item in manifest["videos"]] == ["paused", "pending"]
