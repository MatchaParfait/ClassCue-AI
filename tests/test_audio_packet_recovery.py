"""Small corrupt packets must not move subtitles or hide extensive audio loss."""
from fractions import Fraction
from types import SimpleNamespace
import wave

import av
import numpy as np
import pytest

from classcue.qwen_transcriber import stream_decode_audio_to_wav
from classcue.processing_control import ProcessingCancelled


def packet(start, duration=.02, *, corrupt=False, untimed=False):
    def decode():
        if corrupt:
            raise av.error.InvalidDataError(1094995529, "damaged packet")
        frame = av.AudioFrame.from_ndarray(
            np.full((1, round(duration * 16000)), 8192, dtype=np.int16),
            format="s16", layout="mono",
        )
        frame.sample_rate = 16000
        frame.time_base = Fraction(1, 16000)
        frame.pts = None if untimed else round(start * 16000)
        return [frame]
    return SimpleNamespace(
        pts=None if untimed else round(start * 16000), time_base=Fraction(1, 16000),
        duration=round(duration * 16000), decode=decode,
    )


def source(monkeypatch, packets, duration=4):
    class Container:
        streams = SimpleNamespace(audio=[object()])
        start_time = 0

        def __enter__(self): return self
        def __exit__(self, *_): pass
        def demux(self, **_): yield from packets
    container = Container()
    container.duration = round(duration * av.time_base)
    monkeypatch.setattr(av, "open", lambda *_: container)


@pytest.mark.parametrize("tail", [False, True])
def test_small_loss_becomes_silence_with_unchanged_timestamps(monkeypatch, tmp_path, tail, caplog):
    packets = [packet(0, 1), packet(1, corrupt=True)]
    if not tail:
        packets.append(packet(1.02, 1))
    source(monkeypatch, packets)
    target = tmp_path / "timeline.wav"
    duration = stream_decode_audio_to_wav("damaged.mp4", target)
    with wave.open(str(target), "rb") as wav:
        values = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
    assert duration == (1020 if tail else 2020)
    assert np.min(values[:16000]) == 8192
    assert np.max(np.abs(values[16000:16320])) == 0
    if not tail:
        assert np.min(values[16320:]) == 8192
    assert "Recovered 1 corrupt audio packets" in caplog.text


@pytest.mark.parametrize("packets,reason", [
    ([packet(0, 1), packet(1, corrupt=True, untimed=True)], "缺少时间戳"),
    ([packet(0, 1), packet(1, .3, corrupt=True)], "损坏范围过大"),
    ([packet(0, 1), packet(1, corrupt=True), packet(1.02, untimed=True)], "时间轴不完整"),
    ([packet(0, 1), packet(1, corrupt=True), packet(5, 1)], "时间轴不完整"),
    ([packet(0, corrupt=True)], "音频轨道为空"),
])
def test_unsafe_or_empty_recovery_is_rejected(monkeypatch, tmp_path, packets, reason):
    source(monkeypatch, packets, duration=10)
    with pytest.raises(ValueError, match=reason):
        stream_decode_audio_to_wav("damaged.mp4", tmp_path / "timeline.wav")


def test_repeated_scattered_damage_has_total_limit(monkeypatch, tmp_path):
    packets = []
    for index in range(52):
        packets.extend([packet(index * .12, .1), packet(index * .12 + .1, corrupt=True)])
    source(monkeypatch, packets, duration=200)
    with pytest.raises(ValueError, match="损坏范围过大"):
        stream_decode_audio_to_wav("damaged.mp4", tmp_path / "timeline.wav")


def test_consecutive_damage_is_bounded(monkeypatch, tmp_path):
    source(monkeypatch, [packet(0, 1)] + [packet(1 + i * .02, corrupt=True) for i in range(15)], 100)
    with pytest.raises(ValueError, match="损坏范围过大"):
        stream_decode_audio_to_wav("damaged.mp4", tmp_path / "timeline.wav")


def test_cancellation_is_still_observed_during_recovery(monkeypatch, tmp_path):
    state = {"stop": False}
    def packets():
        yield packet(0, 1)
        yield packet(1, corrupt=True)
        state["stop"] = True
        yield packet(1.02, 1)
    source(monkeypatch, packets())
    with pytest.raises(ProcessingCancelled):
        stream_decode_audio_to_wav("damaged.mp4", tmp_path / "timeline.wav", should_stop=lambda: state["stop"])
