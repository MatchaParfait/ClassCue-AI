from dataclasses import asdict
import json

from classcue.core import TranscriptSegment
from classcue.live_transcript import LiveTranscriptBuffer
from classcue.qwen_preview import chunk_preview, publish_cached_preview
from classcue.qwen_resilience import Checkpoints, run_attempt
from classcue.transcript_events import transcript_listener, transcript_observer, emit_transcript_preview


def chunk(start=16000, end=48000):
    return {"start_sample": start, "end_sample": end, "input_samples": end - start,
            "source_chunk_index": f"{start}:{end}", "slices": []}


def test_chunk_preview_uses_real_bounds_and_stable_ids_without_claiming_alignment():
    row = chunk_preview(chunk(), "第一批识别文字", 16000)
    assert (row.id, row.start_ms, row.end_ms, row.text) == (-16001, 1000, 3000, "第一批识别文字")
    assert row.words is None or row.words == []
    assert chunk_preview(chunk(), "  ", 16000) is None
    assert chunk_preview(chunk(), None, 16000) is None


def test_preview_only_never_reaches_early_llm_and_failure_is_best_effort():
    delivered = []
    def broken(*_):
        raise RuntimeError("display unavailable")
    with transcript_listener(lambda *args: delivered.append(args)), transcript_observer(broken):
        emit_transcript_preview([TranscriptSegment(-1, 0, 1000, "未对齐文字")])
    assert delivered == []


def test_worker_events_reach_ui_before_exit_without_progress_callback(tmp_path, monkeypatch):
    import classcue.qwen_process as qwen_process
    import classcue.qwen_runtime as qwen_runtime
    buffer, analysis = LiveTranscriptBuffer(), []
    row = chunk_preview(chunk(), "真实 Qwen 文字", 16000)
    def worker(_command, **kwargs):
        request = json.loads((tmp_path / "request.json").read_text(encoding="utf-8"))
        assert request["stream_preview"] is True
        line = json.dumps({"event": "transcript_preview", "segments": [asdict(row)]})
        kwargs["on_line"](line)
        snap = buffer.snapshot("job", 7)
        assert snap["subtitles"][0]["text"] == row.text
        assert snap["settled_until_ms"] == 0
        kwargs["on_line"](line)  # Retries cannot append duplicate chunk rows.
        assert len(buffer.snapshot("job", 7)["subtitles"]) == 1
        assert analysis == []
        return 0
    monkeypatch.setattr(qwen_process, "run_process", worker)
    monkeypatch.setattr(qwen_runtime, "runtime_python", lambda: "python")
    with transcript_observer(lambda rows, end: buffer.publish("job", 7, rows, end)), \
            transcript_listener(lambda *args: analysis.append(args)):
        result = run_attempt(tmp_path, {}, [chunk()], "asr", 2, "standard", "test")
    assert result["returncode"] == 0


def test_resume_only_previews_valid_cached_text_without_writing_alignment(tmp_path):
    cache = Checkpoints(tmp_path, "job")
    first, second = chunk(0, 16000), chunk(16000, 32000)
    cache.save("asr", first, "已识别文字")
    seen = []
    with transcript_observer(lambda rows, end: seen.extend(rows)):
        publish_cached_preview(cache, [first, second], 16000)
    assert [row.text for row in seen] == ["已识别文字"]
    assert not (tmp_path / "align").exists()
    assert not (tmp_path / "result.json").exists()
