from types import SimpleNamespace

import numpy as np
import pytest

from qwen_boundaries import AudioSpan
from qwen_silence import build_silence_plan, compact_audio, restore_segments, source_time
from qwen_transcriber import aligned_segments
import qwen_transcriber
import qwen_vad


def plan(intervals, enabled=True, total=2000, rate=100, spans=None, **kwargs):
    return build_silence_plan(
        spans or [AudioSpan(0, total, "firered")], total, rate, intervals,
        enabled=enabled, padding_seconds=kwargs.get("padding", .3),
        min_removed_seconds=kwargs.get("minimum", 1.5),
    )


def test_disabled_mode_keeps_every_sample():
    result = plan([[1, 2], [12, 18]], enabled=False)
    audio = np.arange(2000)
    np.testing.assert_array_equal(compact_audio(audio, result["chunks"][0]), audio)
    assert result["removed_samples"] == 0


def test_padding_short_gap_retention_and_tail_removal():
    result = plan([[1, 2], [3, 4], [8, 18]])
    assert [(r["start_sample"], r["end_sample"]) for r in result["deleted_regions"]] == [
        (430, 770), (1830, 2000),
    ]
    assert result["retained_samples"] == 1490
    assert result["removed_samples"] == 510
    compacted = compact_audio(np.arange(2000), result["chunks"][0])
    np.testing.assert_array_equal(compacted, np.concatenate([
        np.arange(430), np.arange(770, 1830),
    ]))


def test_sample_partition_across_external_boundaries_and_skipped_chunk():
    spans = [AudioSpan(0, 1000, "firered"), AudioSpan(1000, 2000, "firered"),
             AudioSpan(2000, 3000, "tail")]
    result = plan([[1, 2], [26, 28]], total=3000, spans=spans)
    assert result["chunks"][1]["input_samples"] == 0
    retained = set()
    removed = set()
    for chunk in result["chunks"]:
        for s in chunk["slices"]:
            retained.update(range(s["source_start_sample"], s["source_end_sample"]))
    for gap in result["deleted_regions"]:
        removed.update(range(gap["start_sample"], gap["end_sample"]))
    assert not retained & removed
    assert retained | removed == set(range(3000))


def test_no_detections_is_explicit_safe_fallback():
    result = plan([])
    assert result["empty_detection_fallback"] is True
    assert result["removed_samples"] == 0
    with pytest.raises(ValueError, match="requires VAD"):
        plan(None)


def test_source_mapping_at_both_sides_of_join():
    result = plan([[0, 2], [10, 12]], padding=0)
    chunk = result["chunks"][0]
    assert source_time(1.5, chunk, 100) == 1.5
    assert source_time(2, chunk, 100, endpoint="end") == 2
    assert source_time(2, chunk, 100, endpoint="start") == 10
    assert source_time(3.5, chunk, 100) == 11.5
    assert source_time(4, chunk, 100, endpoint="end") == 12


def test_mapping_millisecond_rounded_join_to_correct_side():
    chunk = {
        "input_samples": 30000,
        "slices": [
            {"source_start_sample": 1000, "source_end_sample": 3010,
             "compact_start_sample": 0, "compact_end_sample": 2010},
            {"source_start_sample": 6000, "source_end_sample": 33990,
             "compact_start_sample": 2010, "compact_end_sample": 30000},
        ],
    }
    assert source_time(.126, chunk, 16000, endpoint="end") == 3010 / 16000
    assert source_time(.126, chunk, 16000, endpoint="start") == 6000 / 16000


def test_restore_words_regroups_subtitle_at_deleted_long_gap():
    result = plan([[0, 2], [10, 12]], padding=0)
    original = aligned_segments("你好再见。", [
        SimpleNamespace(text="你好", start_time=.5, end_time=1.5),
        SimpleNamespace(text="再见", start_time=2.5, end_time=3.5),
    ], 0, 4)
    restored = restore_segments(original, result["chunks"][0], 100, 2000)
    assert [(s.start_ms, s.end_ms) for s in restored] == [(500, 1500), (10500, 11500)]
    assert "".join(s.text for s in restored) == "你好再见。"
    assert restored[1].words[0]["start"] == 10.5


def test_crossing_join_is_recorded_and_zero_length_token_stays_valid():
    result = plan([[0, 2], [10, 12]], padding=0)
    original = aligned_segments("好。再见", [
        SimpleNamespace(text="好", start_time=1.8, end_time=2.2),
        SimpleNamespace(text="再见", start_time=3, end_time=3),
    ], 0, 4)
    warnings = []
    restored = restore_segments(original, result["chunks"][0], 100, 2000, warnings=warnings)
    assert warnings[0]["reason"] == "word_crosses_compact_join"
    assert warnings[0]["source_start_seconds"] == 1.8
    assert warnings[0]["source_end_seconds"] == 10.2
    assert restored[-1].end_ms > restored[-1].start_ms


@pytest.mark.parametrize("intervals", [[[-1, 2]], [[1, 25]], [[2, 1]], [[float("nan"), 2]]])
def test_invalid_detections_are_not_hidden_by_padding(intervals):
    with pytest.raises(ValueError):
        plan(intervals)


@pytest.mark.parametrize("kwargs", [{"padding": -1}, {"padding": float("inf")}, {"minimum": 0}])
def test_invalid_options_fail(kwargs):
    with pytest.raises(ValueError):
        plan([[0, 2]], **kwargs)


def test_missing_source_coverage_is_rejected():
    with pytest.raises(ValueError, match="contiguously"):
        plan([[0, 2]], spans=[AudioSpan(5, 2000, "tail")])


def test_empty_chunks_never_produce_timestamps():
    chunk = plan([[0, 2], [26, 28]], total=3000, spans=[
        AudioSpan(0, 1000, "firered"), AudioSpan(1000, 2000, "firered"),
        AudioSpan(2000, 3000, "tail"),
    ])["chunks"][1]
    assert compact_audio(np.arange(3000), chunk).shape == (0,)
    with pytest.raises(ValueError, match="empty audio"):
        source_time(0, chunk, 100)


@pytest.mark.parametrize("routing", [
    {"skip_non_speech": True},
    {"audio_profile": "mostly-silent"},
])
def test_internal_transcriber_skips_silence_and_returns_source_times(monkeypatch, routing):
    import qwen_memory
    monkeypatch.setattr(qwen_memory, "ensure_memory_headroom", lambda: None)
    monkeypatch.setattr(qwen_transcriber, "decode_audio", lambda _: np.zeros(20 * 16000, dtype="float32"))
    monkeypatch.setattr(qwen_vad, "detect_speech", lambda *_args: {
        "backend": "firered", "speech_intervals": [[0, 2], [10, 12]],
    })

    class Model:
        max_inference_batch_size = 1

        def transcribe(self, **kwargs):
            assert self.max_inference_batch_size == 2
            assert len(kwargs["audio"]) == 1
            assert len(kwargs["audio"][0][0]) == 4 * 16000
            return [SimpleNamespace(text="你好再见。", time_stamps=SimpleNamespace(items=[
                SimpleNamespace(text="你好", start_time=.5, end_time=1.5),
                SimpleNamespace(text="再见", start_time=2.5, end_time=3.5),
            ]))]

    model, audit = Model(), {}
    segments, duration = qwen_transcriber.transcribe_qwen(
        model, "source.wav", speech_padding_seconds=0, silence_report=audit, **routing,
    )
    assert duration == 20000
    assert model.max_inference_batch_size == 1
    assert [(s.id, s.start_ms, s.end_ms) for s in segments] == [(1, 500, 1500), (2, 10500, 11500)]
    assert audit["silence"]["removed_samples"] == 16 * 16000


def test_internal_skip_restores_batch_setting_after_failure(monkeypatch):
    import qwen_memory
    monkeypatch.setattr(qwen_memory, "ensure_memory_headroom", lambda: None)
    monkeypatch.setattr(qwen_transcriber, "decode_audio", lambda _: np.zeros(20 * 16000, dtype="float32"))
    monkeypatch.setattr(qwen_vad, "detect_speech", lambda *_args: {
        "speech_intervals": [[1, 2]],
    })

    class Model:
        max_inference_batch_size = 1

        def transcribe(self, **kwargs):
            raise RuntimeError("test failure")

    model = Model()
    with pytest.raises(RuntimeError, match="test failure"):
        qwen_transcriber.transcribe_qwen(model, "source.wav", skip_non_speech=True)
    assert model.max_inference_batch_size == 1
