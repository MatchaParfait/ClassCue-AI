from types import SimpleNamespace

import numpy as np
import pytest

from classcue.qwen_transcriber import (
    QwenAlignmentError, aligned_segments, recover_aligned_segments,
)


def word(text, start, end):
    return SimpleNamespace(text=text, start_time=start, end_time=end)


class Aligner:
    def __init__(self, items):
        self.items = items
        self.calls = []

    def align(self, **kwargs):
        self.calls.append(kwargs)
        return [SimpleNamespace(items=self.items)]


def test_valid_alignment_does_not_retry():
    model = Aligner([])
    cues = recover_aligned_segments(model, "Test.", [word("Test", .1, .9)],
                                    (np.zeros(16000, dtype=np.float32), 16000))
    assert cues[0].text == "Test."
    assert model.calls == []


def test_end_drift_retries_once_and_keeps_text_audio_and_original_bounds():
    audio = np.linspace(-.5, .5, 16000, dtype=np.float32)
    original = audio.copy()
    model = Aligner([word("Test", .1, .9), word("case", .9, .99)])
    events = []
    cues = recover_aligned_segments(
        model, "Test case!", [word("Test", .1, .9), word("case", .9, 1.2740625)],
        (audio, 16000), diagnostic=events.append,
    )
    assert len(model.calls) == 1
    request = model.calls[0]
    padded, rate = request["audio"][0]
    assert request["text"] == ["Test case!"]
    assert request["language"] == "Chinese"
    assert rate == 16000
    assert len(padded) == 20000
    assert padded.dtype == audio.dtype
    np.testing.assert_array_equal(padded[:16000], original)
    np.testing.assert_array_equal(audio, original)
    assert not padded[16000:].any()
    assert "".join(cue.text for cue in cues) == "Test case!"
    assert cues[-1].end_ms == 990
    assert [event["status"] for event in events] == ["retrying", "recovered"]
    assert events[0]["original_error"]["word_index"] == 1
    assert events[-1]["max_end_seconds"] == .99


@pytest.mark.parametrize("items", [
    [word("Test", .1, 1.2740625)],
    [word("Test", .1, 1.1)],  # Within old tolerance, but not a valid recovery.
    [word("Test", -.01, .9)],
    [word("Wrong", .1, .9)],
    [],
])
def test_invalid_recovery_is_rejected_without_further_retries(items):
    model = Aligner(items)
    events = []
    with pytest.raises(ValueError):
        recover_aligned_segments(
            model, "Test", [word("Test", .1, 1.2740625)],
            (np.zeros(16000), 16000), diagnostic=events.append,
        )
    assert len(model.calls) == 1
    assert [event["status"] for event in events] == ["retrying", "failed"]


@pytest.mark.parametrize("items", [
    [word("Test", .8, .7)], [word("Wrong", .1, .9)],
    [word("Test", float("nan"), .9)], [],
    [word("Test", .5, .7), word("case", .1, .9)],
])
def test_non_boundary_failures_are_not_retried(items):
    model = Aligner([])
    with pytest.raises(ValueError):
        recover_aligned_segments(model, "Test case", items, (np.zeros(16000), 16000))
    assert not model.calls


def test_disabled_auto_recovery_preserves_failure():
    model = Aligner([word("Test", .1, .9)])
    with pytest.raises(QwenAlignmentError):
        recover_aligned_segments(model, "Test", [word("Test", .1, 1.3)],
                                 (np.zeros(16000), 16000), allow_recovery=False)
    assert not model.calls


def test_resource_error_during_recovery_propagates_unchanged():
    error = MemoryError("injected allocation failure")

    class Model:
        def align(self, **kwargs):
            raise error

    with pytest.raises(MemoryError) as result:
        recover_aligned_segments(Model(), "Test", [word("Test", .1, 1.3)],
                                 (np.zeros(16000), 16000))
    assert result.value is error


def test_wrong_recovery_output_count_is_rejected():
    class Model:
        def align(self, **kwargs):
            return []

    with pytest.raises(ValueError, match="output count"):
        recover_aligned_segments(Model(), "Test", [word("Test", .1, 1.3)],
                                 (np.zeros(16000), 16000))


def test_real_failure_numeric_shape_has_actionable_transcript_free_details():
    with pytest.raises(QwenAlignmentError) as result:
        aligned_segments("A B C", [word("A", 94.7, 94.88), word("B", 94.88, 94.88),
                                   word("C", 94.88, 94.88)], 0, 94.6059375)
    assert result.value.details == {
        "reason": "audio_end", "word_index": 0, "start": 94.7, "end": 94.88,
        "previous_start": 0, "duration_seconds": 94.6059375,
    }
    assert "94.88" in str(result.value)


@pytest.mark.parametrize("recovery", ["valid", "invalid", "oom", "text_mismatch"])
def test_worker_commits_valid_peer_and_reports_recovery_chunk(tmp_path, monkeypatch, recovery):
    from contextlib import nullcontext
    from dataclasses import asdict
    import sys
    import wave

    import classcue.qwen_loading as loading
    import classcue.qwen_memory as memory
    import classcue.qwen_resilience as qr
    import classcue.qwen_transcriber as transcriber
    from scripts import qwen_resilient_worker

    class CudaOOM(RuntimeError):
        pass

    class Model:
        calls = 0

        def align(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                bad = word("Wrong", .1, .9) if recovery == "text_mismatch" else word("B", .1, 1.3)
                return [SimpleNamespace(items=[word("A", .1, .9)]),
                        SimpleNamespace(items=[bad])]
            if recovery == "oom":
                raise CudaOOM("injected")
            return [SimpleNamespace(items=[word("B", .1, .9 if recovery == "valid" else 1.1)])]

    model = Model()
    cuda = SimpleNamespace(
        OutOfMemoryError=CudaOOM, is_available=lambda: True, synchronize=lambda: None,
        get_device_name=lambda _: "fake", reset_peak_memory_stats=lambda: None,
        empty_cache=lambda: None, memory_allocated=lambda: 0, memory_reserved=lambda: 0,
        max_memory_allocated=lambda: 0, max_memory_reserved=lambda: 0,
    )
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        cuda=cuda, float16="float16", set_num_threads=lambda _: None))
    monkeypatch.setitem(sys.modules, "qwen_asr", SimpleNamespace(
        Qwen3ForcedAligner=SimpleNamespace(from_pretrained=lambda *a, **kw: model)))
    monkeypatch.setattr(transcriber, "_checkpoint_reader", nullcontext)
    monkeypatch.setattr(loading, "model_loading", lambda _: nullcontext({}))
    monkeypatch.setattr(memory, "memory_snapshot", lambda: {})
    monkeypatch.setattr(qr, "gpu_free_mib", lambda: 7000)
    path = tmp_path / "timeline.wav"
    with wave.open(str(path), "wb") as source:
        source.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        source.writeframes(b"\0\0" * 32000)
    chunks = [{
        "start_sample": start, "end_sample": start + 16000, "input_samples": 16000,
        "source_chunk_index": f"{start}:{start + 16000}",
        "slices": [{"source_start_sample": start, "source_end_sample": start + 16000,
                    "compact_start_sample": 0, "compact_end_sample": 16000}],
    } for start in (0, 16000)]
    cache = qr.Checkpoints(tmp_path, "test")
    for chunk, text in zip(chunks, ("A", "B")):
        cache.save("asr", chunk, text)
    manifest = {
        "job_id": "test", "identity": {"options": asdict(qr.QwenOptions(mode="manual"))},
        "timeline": str(path), "rate": 16000, "total_samples": 32000,
        "chunks": chunks, "paths": {"aligner": "unused"},
    }
    request = tmp_path / "request.json"
    qr.atomic_json(request, {
        "manifest": manifest, "stage": "align", "batch_size": 2,
        "chunks": chunks, "load_profile": "standard", "attempt_id": "test",
    })
    monkeypatch.setattr(sys, "argv", ["worker", "--request", str(request), "--output", str(tmp_path)])
    assert qwen_resilient_worker.main() == (0 if recovery == "valid" else 1)
    assert cache.load("align", chunks[0], "A") is not None
    assert (cache.load("align", chunks[1], "B") is not None) is (recovery == "valid")
    assert model.calls == (1 if recovery == "text_mismatch" else 2)
    if recovery != "valid":
        failure = qr.read_json(tmp_path / "failure-test.json")
        assert failure["alignment_chunk"]["source_chunk_index"] == "16000:32000"
        assert failure["kind"] == ("cuda_oom" if recovery == "oom" else "inference")
        if recovery == "invalid":
            assert failure["alignment_error"]["reason"] == "recovery_bounds"
    if recovery != "text_mismatch":
        events = qr.read_json(tmp_path / "alignment-diagnostics-test.json")
        assert events[0]["source_chunk_index"] == "16000:32000"
        assert events[-1]["status"] == ("recovered" if recovery == "valid" else "failed")
