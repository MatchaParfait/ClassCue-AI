import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_provider import ProviderProfile
from core import TranscriptSegment
import llm_experiments as lab
from qwen_resilience import atomic_json
from scripts.llm_lab_batch import asr_hash, export_run
from scripts.rematch_llm_evidence import rematch_batch
from scripts import repair_llm_evidence as replay
from video_store import VideoStore


def response(candidate):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(
            {"candidates": [candidate]}, ensure_ascii=False)), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=30, total_tokens=130),
    )


@pytest.fixture
def saved_batch(tmp_path):
    store = VideoStore(tmp_path / "project.db")
    profile = ProviderProfile(base_url="https://not-called.example/v1", model="test")
    video_id = store.create_video("hash", "lesson.mp4", "lesson.mp4", "", 60000)
    text = "这个定理很重要，必须掌握。"
    store.mark_transcribed(video_id, [TranscriptSegment(1, 0, 1000, text)], "firered")
    bad = {
        "level": "confirmed", "signal_type": "emphasis", "title": "定理", "reason": "老师强调",
        "start_sentence_id": 1, "end_sentence_id": 1, "evidence_sentence_ids": [1],
        "evidence_quote": "这个定理非常重要，必须掌握。",
    }
    snapshot = lab.prepare_snapshot(
        store, video_id, lab.LabConfig(scheme="few_shot", max_evidence_repairs=0), profile,
    )
    run_id = lab.register_run(store, "source", snapshot, snapshot["snapshot_hash"])
    client = SimpleNamespace(max_retries=0, chat=SimpleNamespace(
        completions=SimpleNamespace(create=lambda **_kwargs: response(bad))))
    lab.execute_run(store, run_id, snapshot, "secret", snapshot["snapshot_hash"], client=client)
    directory = tmp_path / "source"
    item = {"video_id": video_id, "source": "lesson.mp4", "experiment_id": run_id,
            "asr_sha256": asr_hash(store, video_id)}
    export_run(store, item, snapshot, directory / f"video-{video_id}")
    manifest = {
        "status": "completed", "batch_id": "source", "directory": str(directory),
        "database": str(store.path), "profile": profile.model_dump(), "videos": [item],
    }
    path = directory / "manifest.json"
    atomic_json(path, manifest)
    return store, path, manifest, profile, {**bad, "evidence_quote": text}


def test_offline_rematch_and_repair_replay_preserve_every_old_row(saved_batch, monkeypatch):
    store, path, original, profile, good = saved_batch
    before = replay.preservation_state(store, original)
    offline = rematch_batch(path)
    assert offline["unresolved_count"] == 1
    assert offline["llm_requests"] == 0
    assert replay.verify_preservation(store, before)["passed"]
    offline_path = Path(offline["directory"]) / "manifest.json"
    before_offline = replay.preservation_state(store, offline)
    monkeypatch.setattr(replay, "load_provider_settings", lambda: SimpleNamespace(active_profile=profile))
    monkeypatch.setattr(replay, "resolve_interface_key", lambda *_args: "secret")
    actual_execute = lab.execute_run
    requests = []

    def execute(store, run_id, snapshot, key, approved):
        def create(**kwargs):
            requests.append(kwargs)
            return response(good)
        client = SimpleNamespace(max_retries=0, chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)))
        return actual_execute(store, run_id, snapshot, key, approved, client=client)
    monkeypatch.setattr(lab, "execute_run", execute)
    repaired = replay.run_batch(offline_path, execute=True, concurrency=2)
    assert repaired["status"] == "completed"
    assert repaired["planned_candidates"] == 1 and len(requests) == 1
    summary = repaired["videos"][0]["summary"]
    assert summary["model_recovered_count"] == 1 and summary["rejected_count"] == 0
    assert summary["call_count"] == 1 and summary["candidate_count"] == 1
    calls = store.list_experiment_calls(repaired["videos"][0]["experiment_id"])
    assert calls[0]["stage"] == "evidence_repair" and calls[0]["evidence_round"] == 1
    assert replay.verify_preservation(store, before_offline)["passed"]
    assert replay.verify_preservation(store, before)["passed"]
    assert (Path(repaired["directory"]) / "verification.json").is_file()
    assert not (store.path.parent / "llm-batch-latest.json").exists()


def test_repair_preview_does_not_read_key_or_make_calls(saved_batch, monkeypatch):
    store, path, _original, _profile, _good = saved_batch
    offline = rematch_batch(path)
    monkeypatch.setattr(replay, "load_provider_settings", lambda: pytest.fail("preview read key"))
    with store.connect() as connection:
        count = connection.execute("SELECT COUNT(*) FROM video_experiments").fetchone()[0]
    preview = replay.run_batch(Path(offline["directory"]) / "manifest.json")
    assert preview["status"] == "preview_only"
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM video_experiments").fetchone()[0] == count
