from dataclasses import dataclass
from pathlib import Path
import sys
from types import SimpleNamespace
import wave

import numpy as np
import pytest

from qwen_vad_firered import _frame_windows, detect_firered, validate_intervals


def test_native_intervals_are_seconds_not_milliseconds():
    assert validate_intervals([(0.1, 1.2), (10.4, 11.0)], 12) == [
        [0.1, 1.2], [10.4, 11.0],
    ]


@pytest.mark.parametrize("intervals", [
    [[1, 0]], [[-1, 1]], [[0, 13]], [[0, float("nan")]],
    [[0, 2], [1, 3]], [[0, 1, 2]], [[float("inf"), 2]],
])
def test_invalid_intervals_are_rejected(intervals):
    with pytest.raises(ValueError):
        validate_intervals(intervals, 12)


def test_window_frame_coverage_has_no_overlap_or_gaps():
    windows = list(_frame_windows(13501, 6000, 200))
    assert windows == [
        (0, 6000, 0, 6200),
        (6000, 12000, 5800, 12200),
        (12000, 13501, 11800, 13501),
    ]
    assert [i for start, end, _, _ in windows for i in range(start, end)] == list(range(13501))


def test_cpu_only_vad_loader_disables_artificial_pauses_and_preserves_samples(tmp_path, monkeypatch):
    @dataclass
    class Config:
        use_gpu: bool = True
        min_silence_frame: int = 20
        extend_speech_frame: int = 0
        max_speech_frame: int = 2000

    class InferenceContext:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    model_path = tmp_path / "VAD"
    model_path.mkdir()
    for filename in ("model.pth.tar", "cmvn.ark"):
        (model_path / filename).touch()
    audio_path = tmp_path / "input.wav"
    samples = np.arange(32000, dtype=np.int16)
    with wave.open(str(audio_path), "wb") as wav:
        wav.setparams((1, 2, 16000, len(samples), "NONE", "not compressed"))
        wav.writeframes(samples.tobytes())
    calls = []

    class Postprocessor:
        def process(self, probabilities):
            assert len(probabilities) == 198
            return probabilities

        def decision_to_segment(self, decisions, duration):
            assert duration == 2.0
            return [(0.1, 1.2)]

    class Model:
        vad_postprocessor = Postprocessor()

        def detect(self, audio, do_postprocess):
            pcm, rate = audio
            assert rate == 16000
            assert do_postprocess is False
            assert pcm.dtype == np.int16
            calls.append(pcm.copy())
            return None, np.ones((len(pcm) - 400) // 160 + 1)

    def load(path, config):
        assert Path(path) == model_path
        assert config.use_gpu is False
        assert config.min_silence_frame == 30
        assert config.extend_speech_frame == 0
        assert config.max_speech_frame > 100_000_000
        return Model()

    threads = []
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        set_num_threads=threads.append, inference_mode=InferenceContext,
    ))
    monkeypatch.setitem(sys.modules, "fireredasr2s.fireredvad", SimpleNamespace(
        FireRedVad=SimpleNamespace(from_pretrained=load), FireRedVadConfig=Config,
    ))
    result = detect_firered(
        audio_path, model_path, inference_chunk_seconds=1, context_seconds=0.1,
    )
    assert threads == [2]
    assert len(calls) == 2
    assert result["speech_intervals"] == [[0.1, 1.2]]
    assert result["device"] == "cpu"
    assert result["duration_seconds"] == 2
    assert result["parameters"]["max_speech_split_disabled"] is True
    with wave.open(str(audio_path), "rb") as wav:
        assert wav.readframes(wav.getnframes()) == samples.tobytes()
