from types import SimpleNamespace

import numpy as np
import pytest

import classcue.qwen_transcriber as qwen_transcriber


def test_batch_calls_official_api_with_list_and_restores_model_setting(monkeypatch):
    monkeypatch.setattr(qwen_transcriber, "decode_audio", lambda _: np.zeros(16000, dtype="float32"))
    calls = []

    class Model:
        max_inference_batch_size = 1

        def transcribe(self, **kwargs):
            calls.append(kwargs)
            assert self.max_inference_batch_size == 2
            word = SimpleNamespace(text="好", start_time=.1, end_time=.5)
            return [
                SimpleNamespace(text="好。", time_stamps=SimpleNamespace(items=[word])),
                SimpleNamespace(text="", time_stamps=None),
            ]

    model = Model()
    results = qwen_transcriber.transcribe_qwen_batch(model, ["a", "b"], batch_size=2)
    assert model.max_inference_batch_size == 1
    assert len(calls[0]["audio"]) == 2
    assert calls[0]["return_time_stamps"] is True
    assert calls[0]["language"] == "Chinese"
    assert results[0][0][0].start_ms == 100
    assert results[0][1] == 1000
    assert results[1] == ([], 1000)


def test_batch_restores_setting_on_inference_failure(monkeypatch):
    monkeypatch.setattr(qwen_transcriber, "decode_audio", lambda _: np.zeros(16000))

    class Model:
        max_inference_batch_size = 1

        def transcribe(self, **kwargs):
            raise RuntimeError("out of memory")

    model = Model()
    with pytest.raises(RuntimeError, match="out of memory"):
        qwen_transcriber.transcribe_qwen_batch(model, ["a"], batch_size=2)
    assert model.max_inference_batch_size == 1


def test_batch_rejects_wrong_output_count(monkeypatch):
    monkeypatch.setattr(qwen_transcriber, "decode_audio", lambda _: np.zeros(16000))
    model = SimpleNamespace(max_inference_batch_size=1, transcribe=lambda **kw: [])
    with pytest.raises(ValueError, match="output count"):
        qwen_transcriber.transcribe_qwen_batch(model, ["a"])


def test_batch_does_not_load_audio_for_invalid_batch_size(monkeypatch):
    monkeypatch.setattr(qwen_transcriber, "decode_audio", lambda _: pytest.fail("Unexpected decoding"))
    with pytest.raises(ValueError, match="positive"):
        qwen_transcriber.transcribe_qwen_batch(object(), ["a"], batch_size=0)
