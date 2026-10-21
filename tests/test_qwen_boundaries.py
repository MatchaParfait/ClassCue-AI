import numpy as np
import pytest

from qwen_boundaries import plan_spans, quietest_sample


def test_energy_matches_qwen_window_search_for_unambiguous_minimum():
    audio = np.ones(10000, dtype=np.float32)
    audio[4500:4650] = .01
    audio[4550] = 0
    assert quietest_sample(audio, 4000, 5000, 1000) == 4550


@pytest.mark.parametrize("mode", ["fixed", "energy", "firered"])
def test_chunks_preserve_every_sample_and_timeline(mode):
    rate = 100
    audio = np.random.default_rng(7).normal(size=215 * rate).astype("float32")
    intervals = [[0, 87], [88, 175], [177, 214]]
    spans = plan_spans(audio, rate, 90, mode, intervals)
    assert spans[0].start_sample == 0
    assert spans[-1].end_sample == len(audio)
    assert all(a.end_sample == b.start_sample for a, b in zip(spans, spans[1:]))
    reconstructed = np.concatenate([audio[s.start_sample:s.end_sample] for s in spans])
    np.testing.assert_array_equal(reconstructed, audio)


def test_vad_selects_gap_even_when_speech_is_quieter():
    rate = 100
    audio = np.zeros(200 * rate, dtype=np.float32)
    audio[87 * rate:88 * rate] = .1
    spans = plan_spans(audio, rate, 90, "firered", [[0, 87], [88, 200]])
    assert 87.1 <= spans[0].end_sample / rate < 87.9
    assert spans[0].reason == "firered"


def test_missing_detection_does_not_silently_change_backend():
    with pytest.raises(ValueError, match="requires detection"):
        plan_spans(np.zeros(20000), 100, mode="firered")


def test_no_speech_detection_falls_back_to_energy_without_discarding_audio():
    spans = plan_spans(np.zeros(20000), 100, mode="firered", speech_intervals=[])
    assert spans[0].reason == "energy-fallback"
    assert spans[-1].end_sample == 20000


@pytest.mark.parametrize("intervals", [[[0, 250]], [[-1, 10]], [[8, 7]], [[0, float("nan")]]])
def test_invalid_vad_intervals_fail(intervals):
    with pytest.raises(ValueError):
        plan_spans(np.zeros(20000), 100, mode="firered", speech_intervals=intervals)
