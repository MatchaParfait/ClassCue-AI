import json
import wave

import pytest

from core import TranscriptSegment
from scripts import benchmark_paraformer_cpu as benchmark
from scripts.benchmark_paraformer_cpu import (
    cpu_percent, prepare_sample, save_json, validate_output, write_transcript,
)
from scripts.compare_paraformer_cpu_batches import trial_order


def test_interleaved_batch_order_and_thread_sweep_single_batch():
    assert trial_order([1, 4], 3) == [1, 4, 4, 1, 1, 4]
    assert trial_order([4], 3) == [4, 4, 4]
    with pytest.raises(ValueError):
        trial_order([0], 3)
    with pytest.raises(ValueError):
        trial_order([4, 4], 3)
    with pytest.raises(ValueError):
        trial_order([4], 0)


def test_cpu_percentage_normalized_to_host():
    assert cpu_percent(2, .5, 24) == pytest.approx(100 / 6)


def test_gpu_telemetry_missing_fields_are_not_fabricated():
    values = benchmark.parse_gpu_sample("2048, 6144, 73, 65, [N/A]")
    assert values["device_utilization_percent"] == 73
    assert values["power_w"] is None
    summary = benchmark.summarize_gpu([values, benchmark.parse_gpu_sample("2500, 5000, 87, 65, 80")])
    assert summary["mean_device_utilization_percent"] == 80
    assert summary["peak_device_utilization_percent"] == 87
    assert summary["peak_device_used_mib"] == 2500
    assert benchmark.summarize_gpu([])["mean_device_utilization_percent"] is None
    with pytest.raises(ValueError):
        benchmark.parse_gpu_sample("2048, 6144")


def test_real_four_utterance_batches_and_word_timestamps():
    segment = TranscriptSegment(1, 1000, 2000, "test",
                                [{"word": "test", "start": 1.0, "end": 2.0}])
    audit = {"batch_sizes": [4, 4, 2], "batch_size": 4,
             "speech_intervals_ms": [(1000, 2000)]}
    assert validate_output([segment], 5000, audit)["word_count"] == 1
    audit["batch_sizes"] = [3, 4]
    with pytest.raises(ValueError, match="nonfinal"):
        validate_output([segment], 5000, audit)


def test_word_outside_subtitle_rejected():
    segment = TranscriptSegment(1, 1000, 2000, "test",
                                [{"word": "test", "start": 0.0, "end": 2.0}])
    audit = {"batch_sizes": [1], "batch_size": 4, "speech_intervals_ms": [(1000, 2000)]}
    with pytest.raises(ValueError, match="outside"):
        validate_output([segment], 5000, audit)


def test_sorted_input_coverage_and_sequential_subtitle_ids_are_validated():
    segment = TranscriptSegment(1, 1000, 2000, "test",
                                [{"word": "test", "start": 1.0, "end": 2.0}])
    audit = {"batch_sizes": [2], "batch_size": 8,
             "speech_intervals_ms": [(1000, 2000), (3000, 3100)],
             "input_order": [1, 0], "batches": [{"original_indices": [1, 0]}]}
    assert validate_output([segment], 5000, audit)["word_count"] == 1
    audit["input_order"] = [1, 1]
    with pytest.raises(ValueError, match="omitted or duplicated"):
        validate_output([segment], 5000, audit)
    audit["input_order"] = [1, 0]
    audit["batches"][0]["original_indices"] = [0, 1]
    with pytest.raises(ValueError, match="planned interval order"):
        validate_output([segment], 5000, audit)
    audit["batches"][0]["original_indices"] = [1, 0]
    bad_id = TranscriptSegment(2, 1000, 2000, "test", segment.words)
    with pytest.raises(ValueError, match="sequential identifiers"):
        validate_output([bad_id], 5000, audit)


def test_incremental_json_and_transcript_export(tmp_path):
    path = tmp_path / "result.json"
    save_json(path, {"status": "running"})
    save_json(path, {"status": "completed"})
    assert json.loads(path.read_text())["status"] == "completed"
    assert not path.with_suffix(".json.tmp").exists()
    segment = TranscriptSegment(1, 1000, 2000, "test",
                                [{"word": "test", "start": 1.0, "end": 2.0}])
    write_transcript(tmp_path, [segment], 5000)
    assert "00:00:01,000 --> 00:00:02,000" in (tmp_path / "paraformer.srt").read_text()
    assert json.loads((tmp_path / "paraformer.json").read_text())["segments"][0]["words"]


def test_json_retries_transient_windows_reader_lock(tmp_path, monkeypatch):
    path = tmp_path / "result.json"
    save_json(path, {"status": "running"})
    replace = type(path).replace
    attempts, delays = [], []

    def locked_replace(source, target):
        attempts.append(target)
        if len(attempts) < 3:
            raise PermissionError("file is being read")
        return replace(source, target)

    monkeypatch.setattr(type(path), "replace", locked_replace)
    monkeypatch.setattr(benchmark.time, "sleep", delays.append)
    save_json(path, {"status": "completed"})
    assert json.loads(path.read_text())["status"] == "completed"
    assert len(attempts) == 3
    assert delays == [.05, .1]
    assert not path.with_suffix(".json.tmp").exists()


def test_json_persistent_lock_is_not_silently_ignored(tmp_path, monkeypatch):
    path = tmp_path / "result.json"
    save_json(path, {"status": "running"})
    attempts = []

    def locked_replace(source, target):
        attempts.append(target)
        raise PermissionError("persistent lock")

    monkeypatch.setattr(type(path), "replace", locked_replace)
    monkeypatch.setattr(benchmark.time, "sleep", lambda _: None)
    with pytest.raises(PermissionError):
        save_json(path, {"status": "completed"})
    assert len(attempts) == 10
    assert json.loads(path.read_text())["status"] == "running"


def test_sample_crop_uses_exact_same_audio(tmp_path):
    source, target = tmp_path / "timeline.wav", tmp_path / "sample.wav"
    with wave.open(str(source), "wb") as output:
        output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        output.writeframes(b"\x01\x00" * 32000)
    prepare_sample(source, target, start_seconds=.5, duration_seconds=1)
    with wave.open(str(target), "rb") as audio:
        assert audio.getnframes() == 16000
        assert audio.readframes(16000) == b"\x01\x00" * 16000
