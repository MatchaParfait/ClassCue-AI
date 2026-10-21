import json
from types import SimpleNamespace

import pytest

from scripts.benchmark_firered_batch import (
    check_memory_headroom, compare_outputs, edit_distance, percentile, reusable_trial,
)


def test_edit_distance_and_percentile():
    assert edit_distance("abc", "adc") == 1
    assert edit_distance("", "abc") == 3
    assert edit_distance("abc", "") == 3
    assert percentile([4, 1, 3, 2], 0.5) == 2.5
    assert percentile([], 0.95) is None


def test_output_comparison_distinguishes_missing_text_and_timestamps():
    reference = [
        {"uttid": "a", "text": "hello", "timestamp": [["hello", 0, 1]]},
        {"uttid": "b", "text": "cat"},
        {"uttid": "c", "text": "x"},
    ]
    candidate = [
        {"uttid": "a", "text": "hello", "timestamp": [["hello", 0.1, 1]]},
        {"uttid": "b", "text": "car"},
    ]
    result = compare_outputs(reference, candidate)
    assert result["missing_utterance_ids"] == ["c"]
    assert result["changed_text_utterances"] == 2
    assert result["character_edits"] == 2
    assert result["max_matching_token_timestamp_delta_seconds"] == 0.1
    assert result["p95_matching_token_timestamp_delta_seconds"] > 0
    assert result["timestamp_shape_mismatch_utterance_ids"] == []


def test_same_text_with_missing_timestamps_is_reported():
    reference = [{"uttid": "a", "text": "hello", "timestamp": [["hello", 0, 1]]}]
    candidate = [{"uttid": "a", "text": "hello", "timestamp": []}]
    result = compare_outputs(reference, candidate)
    assert result["changed_text_utterances"] == 0
    assert result["timestamp_shape_mismatch_utterance_ids"] == ["a"]


def test_completed_trial_can_be_reused_without_overwriting(tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    directory = tmp_path / "bs1"
    directory.mkdir()
    result = {"status": "completed", "video": str(video), "source_bytes": video.stat().st_size}
    (directory / "result.json").write_text(json.dumps(result), encoding="utf-8")
    for filename in ("raw_outputs.json", "vad_segments.json", "resource_samples.json", "firered.json"):
        (directory / filename).write_text("[]", encoding="utf-8")
    assert reusable_trial(directory, video) == result
    assert json.loads((directory / "result.json").read_text()) == result
    video.write_bytes(b"changed")
    with pytest.raises(FileExistsError, match="different source"):
        reusable_trial(directory, video)


def test_missing_artifacts_and_failed_trial_cannot_be_reused(tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    directory = tmp_path / "bs1"
    directory.mkdir()
    result = {"status": "completed", "video": str(video), "source_bytes": 5}
    (directory / "result.json").write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(FileExistsError, match="artifacts"):
        reusable_trial(directory, video)
    result["status"] = "failed"
    (directory / "result.json").write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(FileExistsError, match="incomplete"):
        reusable_trial(directory, video)


@pytest.mark.parametrize("initial", [True, False])
@pytest.mark.parametrize("disabled", [True, False])
def test_experimental_guard_disable_applies_to_startup_and_batch(monkeypatch, initial, disabled):
    import qwen_memory

    calls = []
    monkeypatch.setattr(qwen_memory, "ensure_memory_headroom", lambda **kwargs: calls.append(kwargs))
    args = SimpleNamespace(disable_memory_guard=disabled,
                           minimum_free_physical_mib=1024, minimum_free_commit_mib=2048)
    check_memory_headroom(args, initial=initial)
    expected = {} if initial else {"physical_mib": 1024, "commit_mib": 2048}
    assert calls == ([] if disabled else [expected])


def test_enabled_memory_guard_still_propagates_resource_error(monkeypatch):
    import qwen_memory

    def fail(**_kwargs):
        raise RuntimeError("Insufficient memory headroom")

    monkeypatch.setattr(qwen_memory, "ensure_memory_headroom", fail)
    args = SimpleNamespace(disable_memory_guard=False)
    with pytest.raises(RuntimeError, match="Insufficient memory headroom"):
        check_memory_headroom(args, initial=True)
