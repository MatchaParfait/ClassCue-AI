from contextlib import contextmanager, nullcontext
import sys
from types import SimpleNamespace
import wave

import numpy as np
import pytest

import classcue.dolphin_transcriber as dolphin
from classcue.processing_control import ResourcePaused


def output(text="\u4f60", start=.06):
    return {"text": text, "word_timestamps": [
        {"word": c, "start": start + i * .06, "end": start + (i + 1) * .06}
        for i, c in enumerate(text) if c != "\u3002"]}


def test_native_timestamps_keep_punctuation_and_original_timeline():
    result = dolphin.timestamp_segments(output("\u4f60\u597d\u3002"), 60000, 1000)
    assert result[0].text == "\u4f60\u597d\u3002"
    assert result[0].start_ms == 60060
    assert result[0].end_ms == 60180
    assert [w["word"] for w in result[0].words] == ["\u4f60", "\u597d"]


def test_native_subword_markers_do_not_break_timestamp_text_coverage():
    result = dolphin.timestamp_segments({
        "text": " C T", "word_timestamps": [
            {"word": "\u2581C", "start": .06, "end": .12},
            {"word": "\u2581T", "start": .12, "end": .18},
        ]}, 0, 1000)
    assert result[0].text == "C T"
    assert [word["word"] for word in result[0].words] == ["C", "T"]


@pytest.mark.parametrize("bad", [
    {"text": "\u4f60", "word_timestamps": []},
    {"text": "\u4f60\u597d", "word_timestamps": output()["word_timestamps"]},
    output(start=float("nan")), output(start=-.1), output(start=4),
    {"text": "\u4f60", "word_timestamps": [{"word": "x", "start": 0, "end": .1}]},
    {"text": "\u4f60", "word_timestamps": [{"word": "\u4f60", "start": .1, "end": .1}]},
])
def test_incomplete_or_invalid_alignment_is_rejected(bad):
    with pytest.raises(ValueError):
        dolphin.timestamp_segments(bad, 0, 1000)


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    class OOM(RuntimeError):
        pass

    class Tensor:
        def __init__(self, value):
            self.value = value

        def unsqueeze(self, _axis):
            return self

    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        from_numpy=Tensor, inference_mode=nullcontext, cuda=SimpleNamespace(OutOfMemoryError=OOM)))
    intervals = [(0, 300), (500, 700), (900, 1300), (1500, 1750),
                 (2000, 2500), (2700, 3000), (3100, 3500)]

    def decode(_source, target, _progress):
        with wave.open(str(target), "wb") as wav:
            wav.setparams((1, 2, 16000, 0, "NONE", ""))
            wav.writeframes(np.zeros(4 * 16000, dtype="<i2").tobytes())
        return 4000

    monkeypatch.setattr(dolphin, "stream_decode_audio_to_wav", decode)
    monkeypatch.setattr(dolphin, "detect_speech", lambda *_a, **_kw: intervals)
    monkeypatch.setattr(dolphin, "synchronize_device", lambda _m: None)
    monkeypatch.setattr(dolphin, "release_cuda_memory", lambda: None)
    monkeypatch.setattr(dolphin, "trim_cuda_cache", lambda _model, _audit: None)
    monkeypatch.setattr(dolphin, "is_cuda_oom", lambda exc: isinstance(exc, OOM))
    monkeypatch.setattr("classcue.qwen_memory.ensure_memory_headroom", lambda **_kw: None)
    calls, active = [], []

    @contextmanager
    def observe(_recognizer):
        sizes = []
        active[:] = [sizes]
        yield sizes

    def generate(_model, audios):
        calls.append(len(audios))
        active[0].append(len(audios))
        return [output() for _ in audios]

    monkeypatch.setattr(dolphin, "observe_dolphin_batches", observe)
    monkeypatch.setattr(dolphin, "generate_batch", generate)
    model = dolphin.DolphinModels(SimpleNamespace(), SimpleNamespace(), None, 16, "gpu")
    return SimpleNamespace(model=model, intervals=intervals, calls=calls, generate=generate, OOM=OOM)


def test_real_batch_audit_and_complete_original_order(pipeline, tmp_path):
    audit = {"raw_output_path": str(tmp_path / "raw.jsonl")}
    segments, duration = dolphin.transcribe_dolphin(
        pipeline.model, "source", audit=audit, temporary_directory=tmp_path, batch_size=4)
    assert duration == 4000
    assert audit["batch_sizes"] == [4, 3]
    assert [b["encoder_batch_sizes"] for b in audit["batches"]] == [[4], [3]]
    assert len(segments) == 7
    assert [s.id for s in segments] == list(range(1, 8))
    assert [s.start_ms for s in segments] == [start + 60 for start, _ in pipeline.intervals]
    assert sorted(audit["input_order"]) == list(range(7))
    assert len((tmp_path / "raw.jsonl").read_text().splitlines()) == 7


@pytest.mark.parametrize("device", ["cpu", "gpu"])
def test_live_batches_preserve_timestamps_ids_and_final_result(pipeline, tmp_path, monkeypatch, device):
    pipeline.model.device = device
    batches = []
    def generate(model, audios):
        if pipeline.calls:
            assert batches, "First batch must reach the UI before the next inference"
        return pipeline.generate(model, audios)
    monkeypatch.setattr(dolphin, "generate_batch", generate)
    final, duration = dolphin.transcribe_dolphin(
        pipeline.model, "source", temporary_directory=tmp_path, batch_size=4,
        on_preview=lambda rows: batches.append(rows))
    preview = [row for batch in batches for row in batch]
    assert len(batches) == 2
    assert len({row.id for row in preview}) == len(final) == 7
    assert all(row.id < 0 for row in preview)
    assert [row.start_ms for row in preview] != sorted(row.start_ms for row in preview)
    assert [(row.start_ms, row.end_ms, row.text, row.words) for row in sorted(preview, key=lambda row: row.start_ms)] == [
        (row.start_ms, row.end_ms, row.text, row.words) for row in final]
    assert [row.id for row in final] == list(range(1, 8)) and duration == 4000
    batches.clear()
    # Different batch sizes retain each original interval's preview identity.
    monkeypatch.setattr(dolphin, "generate_batch", pipeline.generate)
    dolphin.transcribe_dolphin(pipeline.model, "source", temporary_directory=tmp_path,
                              batch_size=2, on_preview=lambda rows: batches.append(rows))
    assert {row.start_ms: row.id for row in preview} == {row.start_ms: row.id for batch in batches for row in batch}


def test_failed_batch_does_not_publish_partially_validated_text(pipeline, tmp_path, monkeypatch):
    def generate(model, audios):
        rows = pipeline.generate(model, audios)
        rows[-1] = {"text": "invalid", "word_timestamps": []}
        return rows
    monkeypatch.setattr(dolphin, "generate_batch", generate)
    seen = []
    with pytest.raises(ValueError):
        dolphin.transcribe_dolphin(pipeline.model, "source", temporary_directory=tmp_path,
                                  batch_size=4, on_preview=seen.extend)
    assert seen == []


def test_pcm_normalization_preserves_all_int16_values(pipeline, tmp_path, monkeypatch):
    samples = np.arange(-32768, 32768, dtype=np.int16)
    expected = samples.astype(np.float32) / 32768

    def decode(_source, target, _progress):
        with wave.open(str(target), "wb") as wav:
            wav.setparams((1, 2, 16000, 0, "NONE", ""))
            wav.writeframes(samples.tobytes())
        return 4096

    def generate(model, audios):
        assert audios[0].value.dtype == np.float32
        np.testing.assert_array_equal(audios[0].value, expected)
        return pipeline.generate(model, audios)

    monkeypatch.setattr(dolphin, "stream_decode_audio_to_wav", decode)
    monkeypatch.setattr(dolphin, "detect_speech", lambda *_a, **_kw: [(0, 4096)])
    monkeypatch.setattr(dolphin, "generate_batch", generate)
    segments, duration = dolphin.transcribe_dolphin(
        pipeline.model, "source", temporary_directory=tmp_path)
    assert len(segments) == 1 and duration == 4096


def test_oom_reduces_actual_batch_without_omitting_intervals(pipeline, tmp_path, monkeypatch):
    def generate(model, audios):
        if len(audios) > 3:
            raise pipeline.OOM("injected")
        return pipeline.generate(model, audios)

    monkeypatch.setattr(dolphin, "generate_batch", generate)
    audit = {}
    preview = []
    segments, _ = dolphin.transcribe_dolphin(
        pipeline.model, "source", audit=audit, temporary_directory=tmp_path, batch_size=6,
        on_preview=preview.extend)
    assert len(segments) == 7
    assert audit["batch_sizes"] == [3, 3, 1]
    assert audit["effective_batch_size"] == 3
    assert len(audit["oom_recovery"]) == 1
    assert len(preview) == len({row.id for row in preview}) == 7


def test_bs1_oom_pauses_but_cpu_errors_are_not_retried(pipeline, tmp_path, monkeypatch):
    def fail(_model, _audios):
        raise pipeline.OOM("injected")

    monkeypatch.setattr(dolphin, "generate_batch", fail)
    audit = {}
    with pytest.raises(ResourcePaused):
        dolphin.transcribe_dolphin(
            pipeline.model, "source", audit=audit, temporary_directory=tmp_path, batch_size=4)
    assert [item["next_batch_size"] for item in audit["oom_recovery"]] == [2, 1, None]
    pipeline.model.device = "cpu"
    audit = {}
    with pytest.raises(pipeline.OOM):
        dolphin.transcribe_dolphin(pipeline.model, "source", audit=audit, temporary_directory=tmp_path)
    assert audit["oom_recovery"] == []


def test_internal_serialization_is_not_misreported_as_batch(pipeline, tmp_path, monkeypatch):
    @contextmanager
    def observe(_recognizer):
        yield [1, 1, 1, 1]

    monkeypatch.setattr(dolphin, "observe_dolphin_batches", observe)
    monkeypatch.setattr(dolphin, "generate_batch", lambda _model, audios: [output() for _ in audios])
    with pytest.raises(ValueError, match="real batch"):
        dolphin.transcribe_dolphin(
            pipeline.model, "source", temporary_directory=tmp_path, batch_size=4)


def test_reject_invalid_batch_size(pipeline, tmp_path):
    for batch_size in (True, 0, -1, 1.5):
        with pytest.raises(ValueError, match="positive integer"):
            dolphin.transcribe_dolphin(
                pipeline.model, "source", temporary_directory=tmp_path, batch_size=batch_size)


@pytest.mark.parametrize("unused_mib,expected", [(768, 0), (769, 1)])
def test_cuda_cache_trim_only_releases_large_unused_reservations(monkeypatch, unused_mib, expected):
    calls = []
    cuda = SimpleNamespace(
        memory_reserved=lambda: (1000 + unused_mib) * 2**20,
        memory_allocated=lambda: 1000 * 2**20,
        empty_cache=lambda: calls.append(True))
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=cuda))
    audit = {"cache_trims": []}
    dolphin.trim_cuda_cache(SimpleNamespace(device="gpu"), audit)
    assert len(calls) == len(audit["cache_trims"]) == expected
    dolphin.trim_cuda_cache(SimpleNamespace(device="cpu"), audit)
    assert len(calls) == expected


def test_system_memory_guard_pauses_before_inference(pipeline, tmp_path, monkeypatch):
    def fail(**_kwargs):
        raise RuntimeError("insufficient system commit")

    monkeypatch.setattr("classcue.qwen_memory.ensure_memory_headroom", fail)
    with pytest.raises(ResourcePaused, match="system commit") as error:
        dolphin.transcribe_dolphin(pipeline.model, "source", temporary_directory=tmp_path)
    assert error.value.failure == {"stage": "memory_headroom"}
    assert pipeline.calls == []


def test_headroom_recovery_frees_cache_once_then_requires_the_same_threshold(monkeypatch):
    calls = []

    def check(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError("insufficient commit")

    released = []
    monkeypatch.setattr("classcue.qwen_memory.ensure_memory_headroom", check)
    monkeypatch.setattr(dolphin, "release_cuda_memory", lambda: released.append(True))
    audit = {"headroom_recovery": []}
    dolphin.ensure_dolphin_headroom(SimpleNamespace(device="gpu"), audit)
    assert calls == [{"physical_mib": 0, "commit_mib": 2048}] * 2
    assert released == [True]
    assert len(audit["headroom_recovery"]) == 1


def test_cpu_headroom_failure_never_probes_or_releases_cuda(monkeypatch):
    def check(**kwargs):
        raise RuntimeError("insufficient commit")

    monkeypatch.setattr("classcue.qwen_memory.ensure_memory_headroom", check)
    monkeypatch.setattr(dolphin, "release_cuda_memory", lambda: pytest.fail("CPU must not release CUDA"))
    audit = {"headroom_recovery": []}
    with pytest.raises(ResourcePaused):
        dolphin.ensure_dolphin_headroom(SimpleNamespace(device="cpu"), audit)
    assert audit["headroom_recovery"] == []


@pytest.mark.parametrize("device", ["cpu", "gpu"])
def test_low_physical_memory_with_adequate_commit_does_not_pause(monkeypatch, device):
    monkeypatch.setattr("classcue.qwen_memory.memory_snapshot", lambda: {
        "system_available_physical_mib": 128,
        "system_available_commit_mib": 4096,
    })
    monkeypatch.setattr(dolphin, "release_cuda_memory", lambda: pytest.fail("No recovery needed"))
    dolphin.ensure_dolphin_headroom(SimpleNamespace(device=device), {"headroom_recovery": []})


def test_alignment_receives_full_batch_instead_of_last_rescored_slice(monkeypatch):
    callback = []
    removed = []
    encoder_out = np.zeros((2, 20, 8))
    mask = np.ones((2, 1, 20), dtype=bool)
    hypotheses = [SimpleNamespace(tokens=[1, 2, 3, 4, ord(c)], times=None) for c in "\u4f60\u597d"]

    class Tensor:
        def to(self, _device):
            return self

    class Tokenizer:
        def tokens2ids(self, _tokens):
            return [5]

        def detokenize(self, ids):
            return "".join(chr(token) for token in ids), ids

    def hook(fn):
        callback.append(fn)
        return SimpleNamespace(remove=lambda: removed.append(True))

    def decode(**kwargs):
        assert kwargs["infos"]["word_timestamp"] is False
        assert kwargs["infos"]["langs"] == ["<zh>", "<zh>"]
        callback[0](None, None, (encoder_out, mask))
        return {"attention_rescoring": hypotheses}

    def align(full_encoder, lengths, hyps, _tokenizer):
        assert full_encoder is encoder_out
        assert list(lengths) == [20, 20]
        for hyp in hyps:
            hyp.times = [{"word": chr(hyp.tokens[-1]), "start": .06, "end": .12}]
        return hyps

    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "dolphin.processor", SimpleNamespace(
        extract_feats=lambda audios, config: {"feats": Tensor(), "feats_lengths": Tensor()}))
    recognizer = SimpleNamespace(
        encoder=SimpleNamespace(register_forward_hook=hook), device="cpu",
        model_configs={}, decode=decode, get_token_timestamp_torchaudio=align)
    model = dolphin.DolphinModels(recognizer, None, Tokenizer(), 16)
    outputs = dolphin.generate_batch(model, ["first", "second"])
    assert [item["text"] for item in outputs] == list("\u4f60\u597d")
    assert all(item["word_timestamps"] for item in outputs)
    assert removed == [True]
