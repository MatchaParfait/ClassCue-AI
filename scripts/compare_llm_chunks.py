"""Run the 600-second B variant against preserved, citation-corrected 300-second results."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ai_provider import ProviderProfile
import llm_experiments as lab
from qwen_resilience import atomic_json
from scripts.llm_lab_batch import run_batch
from scripts.repair_llm_evidence import preservation_state, verify_preservation
from video_store import VideoStore


def compare_run(workspace: Path, *, execute: bool = False) -> dict:
    original_path = workspace / "batches/few-shot-20261012T151954Z-772190d5/manifest.json"
    matched_path = workspace / "batches/citation-rematch-20261012T162407Z-38d4ec4e/manifest.json"
    repaired_path = workspace / "batches/citation-repair-20261012T162436Z-aff5c17a/manifest.json"
    manifests = [json.loads(path.read_text("utf-8")) for path in (original_path, matched_path, repaired_path)]
    original, matched, repaired = manifests
    if any(m["status"] != "completed" for m in manifests):
        raise ValueError("Baseline batches must be completed")
    store = VideoStore(Path(original["database"]))
    before = preservation_state(store, original)
    baseline_items = {item["video_id"]: {**item, "directory": matched["directory"]} for item in matched["videos"]}
    baseline_items.update({item["video_id"]: {**item, "directory": repaired["directory"]} for item in repaired["videos"]})
    directory = workspace / "comparisons" / ("chunks-300-vs-600-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    baseline = []
    for video_id, item in sorted(baseline_items.items()):
        snapshot = json.loads((Path(item["directory"]) / f"video-{video_id}/snapshot.json").read_text("utf-8"))
        if lab.digest({k: v for k, v in snapshot.items() if k != "snapshot_hash"}) != snapshot["snapshot_hash"]:
            raise ValueError("Baseline snapshot changed")
        if json.loads(store.get_experiment(item["experiment_id"])["options_json"])["snapshot_hash"] != snapshot["snapshot_hash"]:
            raise ValueError("Database snapshot differs")
        baseline.append({
            "video_id": video_id, "source": item["source"], "experiment_id": item["experiment_id"],
            "report": store.get_experiment_results(item["experiment_id"]),
            "snapshot_path": str(Path(item["directory"]) / f"video-{video_id}/snapshot.json"),
        })
    seed_path = Path(repaired["directory"]) / "video-5/snapshot.json"
    seed = json.loads(seed_path.read_text("utf-8"))
    config = lab.LabConfig.model_validate({**seed["config"], "chunk_seconds": 600})
    profile = ProviderProfile.model_validate(seed["profile"])
    for item in original["videos"]:
        previous = json.loads((Path(original["directory"]) / f"video-{item['video_id']}/snapshot.json").read_text("utf-8"))
        new = lab.prepare_snapshot(store, item["video_id"], config, profile, bundle=seed["prompt_bundle"])
        if previous["input_view_hash"] != new["input_view_hash"] or previous["transcript_hash"] != new["transcript_hash"]:
            raise ValueError("ASR or merged input changed")
        for name in ("system", "rules", "review", "repair", "task", "examples"):
            if previous["prompt_bundle"][name] != new["prompt_bundle"][name]:
                raise ValueError(f"Extraction prompt changed: {name}")
    frozen = lab.prepare_snapshot(store, 5, config, profile, bundle=seed["prompt_bundle"])
    atomic_json(directory / "baseline-300.json", baseline)
    atomic_json(directory / "seed-600.json", frozen)
    meta = {
        "status": "running" if execute else "preview_only", "directory": str(directory),
        "source_batches": [str(path) for path in (original_path, matched_path, repaired_path)],
        "changed_extraction_setting": {"chunk_seconds": [300, 600]},
        "baseline_candidate_count": sum(x["report"]["candidate_count"] for x in baseline),
        "concurrency": 2, "location_tolerance_merged_segments": 3,
        "comparison_note": "Same knowledge object and nearby evidence required; both range boundaries within three merged segments count as almost unchanged. Human semantic alignment is saved separately.",
    }
    atomic_json(directory / "comparison.json", meta)
    try:
        batch = run_batch(workspace / "asr-batch-latest.json", directory / "seed-600.json",
                          execute=execute, concurrency=2, publish_latest=False)
        meta.update(status=batch["status"], variant_manifest=str(Path(batch["directory"]) / "manifest.json"))
        atomic_json(directory / "variant-600.json", batch)
        return meta
    finally:
        if meta["status"] == "running":
            meta["status"] = "interrupted"
        atomic_json(directory / "verification.json", verify_preservation(store, before))
        atomic_json(directory / "comparison.json", meta)


def main() -> None:
    from filelock import FileLock
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    workspace = ROOT / "experiments/llm-lab"
    with FileLock(str(workspace / "llm-batch.lock"), timeout=0):
        result = compare_run(workspace, execute=args.execute)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
