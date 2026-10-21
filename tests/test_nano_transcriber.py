from contextlib import contextmanager, nullcontext
import sys
from types import SimpleNamespace
import wave

import numpy as np
import pytest

import classcue.nano_transcriber as nano
from classcue.processing_control import ResourcePaused


class CharacterTokenizer:
    def encode(self, text):
        return [ord(c) for c in text]

    def decode(self, ids):
        return "".join(chr(i) for i in ids)


def output(text="\u4f60", start=.06):
    return {"text": text, "timestamps": [
        {"token": c, "start_time": start + i * .06, "end_time": start + (i + 1) * .06}
        for i, c in enumerate(text)
    ]}


def test_ctc_timestamps_preserve_punctuation_and_absolute_timeline():
    result = nano.timestamp_segments(output("\u4f60\u597d\u3002"), 60000, 1000, CharacterTokenizer())
    assert result[0].text == "\u4f60\u597d\u3002"
    assert result[0].start_ms == 60060
    assert result[0].end_ms == 60180
    assert [w["word"] for w in result[0].words] == ["\u4f60", "\u597d"]


@pytest.mark.parametrize("bad", [
    {"text": "\u4f60", "timestamps": []},
    {"text": "\u4f60\u597d", "timestamps": output()["timestamps"]},
    output(start=float("nan")), output(start=-.1), output(start=4),
    {"text": "\u4f60", "timestamps": [{"token": "x", "start_time": 0, "end_time": .1}]},
])
def test_invalid_alignment_fails_instead_of_fabricating_times(bad):
    with pytest.raises(ValueError):
        nano.timestamp_segments(bad, 0, 1000, CharacterTokenizer())


def test_partial_utf8_bpe_tokens_are_joined_using_original_token_ids():
    class ByteTokenizer:
        def encode(self, text):
            return list(text.encode("utf-8"))

        def decode(self, ids):
            return bytes(ids).decode("utf-8", errors="replace")

    tokenizer = ByteTokenizer()
    stamps = [{"token": tokenizer.decode([token]), "start_time": i * .06,
               "end_time": (i + 1) * .06} for i, token in enumerate(tokenizer.encode("\u4f60"))]
    result = nano.timestamp_segments({"text": "\u4f60", "timestamps": stamps}, 1000, 1000, tokenizer)
    assert result[0].words == [{"word": "\u4f60", "start": 1.0, "end": 1.18, "probability": None}]


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    class OOM(RuntimeError):
        pass

    torch = SimpleNamespace(from_numpy=lambda value: value, inference_mode=nullcontext,
                            cuda=SimpleNamespace(OutOfMemoryError=OOM))
    monkeypatch.setitem(sys.modules, "torch", torch)
    intervals = [(0, 300), (500, 700), (900, 1300), (1500, 1750),
                 (2000, 2500), (2700, 3000), (3100, 3500)]

    def decode(_source, target, _progress):
        with wave.open(str(target), "wb") as wav:
            wav.setparams((1, 2, 16000, 0, "NONE", ""))
            wav.writeframes(np.zeros(4 * 16000, dtype="<i2").tobytes())
        return 4000

    monkeypatch.setattr(nano, "stream_decode_audio_to_wav", decode)
    monkeypatch.setattr(nano, "detect_speech", lambda *_a, **_kw: intervals)
    monkeypatch.setattr(nano, "synchronize_device", lambda _m: None)
    monkeypatch.setattr(nano, "release_cuda_memory", lambda: None)
    monkeypatch.setattr(nano, "is_cuda_oom", lambda exc: isinstance(exc, OOM))
    calls, active = [], []

    @contextmanager
    def observe(_recognizer):
        sizes = []
        active[:] = [sizes]
        yield sizes

    def generate(**kwargs):
        calls.append(len(kwargs["input"]))
        active[0].extend([1] * len(kwargs["input"]))
        return [output() for _ in kwargs["input"]]

    monkeypatch.setattr(nano, "observe_nano_batches", observe)
    recognizer = SimpleNamespace(generate=generate, model=SimpleNamespace(ctc_tokenizer=CharacterTokenizer()))
    model = nano.NanoModels(recognizer, SimpleNamespace(), 16, "gpu", "bf16")
    return SimpleNamespace(model=model, intervals=intervals, calls=calls, generate=generate, OOM=OOM)


def test_bs6_submission_records_real_bs1_and_restores_every_original_interval(pipeline, tmp_path):
    audit = {"raw_output_path": str(tmp_path / "raw.jsonl")}
    segments, duration = nano.transcribe_nano(pipeline.model, "source", audit=audit,
                                             temporary_directory=tmp_path, batch_size=6)
    assert duration == 4000
    assert audit["batch_sizes"] == [6, 1]
    assert audit["batches"][0]["encoder_batch_sizes"] == [1] * 6
    assert len(segments) == len(pipeline.intervals)
    assert [s.id for s in segments] == list(range(1, 8))
    assert [s.start_ms for s in segments] == [s + 60 for s, _ in pipeline.intervals]
    assert sorted(audit["input_order"]) == list(range(7))
    assert len((tmp_path / "raw.jsonl").read_text().splitlines()) == 7


def test_cuda_oom_retries_smaller_groups_without_omitting_segments(pipeline, tmp_path):
    def generate(**kwargs):
        if len(kwargs["input"]) > 3:
            raise pipeline.OOM("injected")
        return pipeline.generate(**kwargs)

    pipeline.model.recognizer.generate = generate
    audit = {}
    segments, _ = nano.transcribe_nano(pipeline.model, "source", audit=audit, temporary_directory=tmp_path)
    assert len(segments) == 7
    assert audit["batch_sizes"] == [3, 3, 1]
    assert audit["effective_batch_size"] == 3
    assert len(audit["oom_recovery"]) == 1


def test_bs1_cuda_oom_pauses_and_cpu_errors_are_not_cuda_recovery(pipeline, tmp_path):
    def oom(**_kwargs):
        raise pipeline.OOM("injected")

    pipeline.model.recognizer.generate = oom
    audit = {}
    with pytest.raises(ResourcePaused):
        nano.transcribe_nano(pipeline.model, "source", audit=audit, temporary_directory=tmp_path)
    assert [item["next_batch_size"] for item in audit["oom_recovery"]] == [3, 1, None]
    pipeline.model.device = "cpu"
    audit = {}
    with pytest.raises(pipeline.OOM):
        nano.transcribe_nano(pipeline.model, "source", audit=audit, temporary_directory=tmp_path)
    assert audit["oom_recovery"] == []


def test_inference_errors_are_not_retried_as_resource_errors(pipeline, tmp_path):
    calls = []

    def fail(**kwargs):
        calls.append(kwargs["batch_size"])
        raise ValueError("invalid input")

    pipeline.model.recognizer.generate = fail
    with pytest.raises(ValueError, match="invalid input"):
        nano.transcribe_nano(pipeline.model, "source", temporary_directory=tmp_path)
    assert calls == [6]
