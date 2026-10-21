from contextlib import nullcontext
from dataclasses import asdict
import io
import json
from queue import Queue
import sys
from types import SimpleNamespace

import pytest

from core import TranscriptSegment
from live_transcript import LiveTranscriptBuffer
from transcript_events import transcript_observer, transcript_listener
from transcriber import DolphinWorker


@pytest.mark.parametrize("observe", [False, True])
def test_preview_protocol_reaches_buffer_before_result_without_early_llm(tmp_path, observe):
    worker = DolphinWorker.__new__(DolphinWorker)
    worker.events = Queue()
    worker.process = SimpleNamespace(stdin=io.StringIO())
    row = TranscriptSegment(-2, 3000, 4000, "Live Dolphin text")
    final = {"event": "result", "segments": [asdict(TranscriptSegment(1, 3000, 4000, row.text))], "duration_ms": 4000}
    buffer, analysis = LiveTranscriptBuffer(), []
    if observe:
        worker.events.put(json.dumps({"event": "transcript_preview", "segments": [asdict(row)]}))
        worker.events.put(json.dumps({"event": "transcript_preview", "segments": [asdict(row)]}))
    worker.events.put(json.dumps(final))
    def observer(rows, end):
        assert not worker.events.empty(), "Preview must arrive before consuming the final result"
        buffer.publish("job", 1, rows, end)
    with transcript_listener(lambda *args: analysis.append(args)), \
            (transcript_observer(observer) if observe else nullcontext()):
        segments, duration = worker.transcribe("source.wav", {"recognizer": tmp_path, "vad": tmp_path}, None)
    assert json.loads(worker.process.stdin.getvalue())["stream_preview"] is observe
    assert segments[0].id == 1 and duration == 4000
    snapshot = buffer.snapshot("job", 1)
    assert len(snapshot["subtitles"]) == int(observe)
    assert snapshot["settled_until_ms"] == 0 and analysis == []


@pytest.mark.parametrize("device", ["cpu", "gpu"])
@pytest.mark.parametrize("stream", [False, True])
def test_worker_emits_preview_only_when_requested(monkeypatch, capsys, device, stream):
    import dolphin_transcriber
    from scripts import dolphin_worker
    row = TranscriptSegment(-1, 0, 1000, "Recognized speech")
    def transcribe(*_args, on_preview=None, **_kwargs):
        assert (on_preview is not None) is stream
        if on_preview:
            on_preview([row])
        return [TranscriptSegment(1, 0, 1000, row.text)], 1000
    monkeypatch.setattr(dolphin_transcriber, "load_dolphin_model", lambda *_a, **_k: object())
    monkeypatch.setattr(dolphin_transcriber, "release_cuda_memory", lambda: None)
    monkeypatch.setattr(dolphin_transcriber, "transcribe_dolphin", transcribe)
    monkeypatch.setattr("asr_cpu.cpu_thread_policy", lambda *_a: {"cpu_threads": 2})
    monkeypatch.setattr(sys, "argv", ["dolphin_worker", "--device", device])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"recognizer": "model", "vad": "vad",
                                                             "audio_path": "source.wav", "stream_preview": stream}) + "\n"))
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    dolphin_worker.main()
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [event["event"] for event in events] == ["progress"] + (["transcript_preview"] if stream else []) + ["result"]
    if stream:
        assert events[1]["segments"][0]["id"] == -1
    assert events[-1]["segments"][0]["id"] == 1
