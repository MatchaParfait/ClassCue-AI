import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_provider import ProviderProfile
from core import TranscriptSegment
import llm_experiments as lab
from qwen_resilience import atomic_json
from scripts import llm_reasoning_matrix as matrix
from scripts.llm_lab_batch import asr_hash
from video_store import VideoStore


@pytest.mark.parametrize("variant", ["high_recall", "precision", "low_recall"])
def test_twelve_frozen_sequential_experiments_and_immutable_asr(tmp_path, monkeypatch, variant):
    store = VideoStore(tmp_path / "project.db")
    profile = ProviderProfile(base_url="https://not-called.example/v1", model="test")
    source_directory = tmp_path / "batches/few-shot-20261012T151954Z-772190d5"
    source = {
        "database": str(store.path), "directory": str(source_directory), "videos": [],
    }
    for index in range(1, 12):
        video_id = store.create_video(str(index), f"{index}.mp4", "", "", 1000)
        store.mark_transcribed(video_id, [
            TranscriptSegment(1, 0, 500, "这个定理很重要，"),
            TranscriptSegment(2, 500, 1000, "必须掌握。"),
        ], "firered")
        if video_id in (5, 11):
            snapshot = lab.prepare_snapshot(store, video_id, lab.LabConfig(scheme="few_shot"), profile)
            if variant == "low_recall":
                snapshot["input_segments"][0]["text"] = "这个定理很重要，必须掌握。"
                snapshot["input_view_hash"] = lab.digest(snapshot["input_segments"])
                snapshot["snapshot_hash"] = lab.digest({
                    k: v for k, v in snapshot.items() if k != "snapshot_hash"
                })
            atomic_json(source_directory / f"video-{video_id}/snapshot.json", snapshot)
            source["videos"].append({
                "video_id": video_id, "asr_sha256": asr_hash(store, video_id), "source": f"{video_id}.mp4",
            })
    atomic_json(source_directory / "manifest.json", source)
    seed_path = tmp_path / "batches/citation-repair-20261012T162436Z-aff5c17a/video-5/snapshot.json"
    atomic_json(seed_path, json.loads((source_directory / "video-5/snapshot.json").read_text("utf-8")))
    monkeypatch.setattr(matrix, "load_provider_settings", lambda: SimpleNamespace(active_profile=profile))
    monkeypatch.setattr(matrix, "resolve_interface_key", lambda *_args: "secret")
    selected = {
        "efforts": ("high",), "chunk_sizes": (600,), "prompt_variant": variant,
    } if variant != "high_recall" else {}
    manifest = matrix.prepare_matrix(tmp_path, **selected)
    count = 2 if variant != "high_recall" else 12
    assert manifest["planned_experiments"] == count
    assert manifest["prompt_variant"] == variant
    assert manifest["concurrency"] == 1
    assert all(item["baseline_spacing_changed"] == (variant == "low_recall") for item in manifest["experiments"])
    assert not store.list_experiments(5)
    actual_execute = lab.execute_run
    active = 0
    peaks = []
    actual_requests = []

    def execute(store, run_id, snapshot, key, approved, **kwargs):
        nonlocal active
        active += 1
        peaks.append(active)
        try:
            def create(**request):
                actual_requests.append(request)
                return SimpleNamespace(
                    choices=[SimpleNamespace(
                        message=SimpleNamespace(content='{"candidates":[]}'), finish_reason="stop",
                    )],
                    usage=SimpleNamespace(
                        prompt_tokens=100, completion_tokens=20, total_tokens=120,
                        completion_tokens_details=SimpleNamespace(reasoning_tokens=12),
                    ),
                )
            client = SimpleNamespace(max_retries=0, chat=SimpleNamespace(
                completions=SimpleNamespace(create=create)))
            return actual_execute(store, run_id, snapshot, key, approved, client=client, **kwargs)
        finally:
            active -= 1
    monkeypatch.setattr(lab, "execute_run", execute)
    result = matrix.execute_matrix(Path(manifest["directory"]) / "manifest.json")
    assert result["status"] == "completed"
    assert len(actual_requests) == count and max(peaks) == 1
    assert [request["reasoning_effort"] for request in actual_requests] == (["high", "high"] if variant != "high_recall" else [
        effort for effort in ("low", "medium", "high") for _ in range(4)
    ])
    for item in result["experiments"]:
        snapshot = json.loads((Path(manifest["directory"]) / item["label"] / "snapshot.json").read_text("utf-8"))
        assert snapshot["prompt_bundle"] == lab.load_prompt_bundle(variant=variant)
    assert all(request["max_tokens"] == 32768 for request in actual_requests)
    for item in result["experiments"]:
        assert item["summary"]["requested_efforts"] == [item["reasoning_effort"]]
        assert item["summary"]["reasoning_tokens_reported"] == 12
        assert item["summary"]["usage_sum_mismatch_count"] == 0
        assert asr_hash(store, item["video_id"]) == item["asr_sha256"]
    assert json.loads((Path(manifest["directory"]) / "verification.json").read_text("utf-8"))["passed"]
    review = (Path(manifest["directory"]) / "review.md").read_text("utf-8")
    for item in result["experiments"]:
        assert f"{item['label']}/snapshot.json" in review
