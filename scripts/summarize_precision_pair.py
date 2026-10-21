"""Offline completion audit of two precision experiments, including a saved retry."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from qwen_resilience import atomic_json
from scripts.analyze_reasoning_matrix import audit_calls
from scripts.llm_lab_batch import asr_hash, write_review
from scripts.llm_reasoning_matrix import summarize
from video_store import VideoStore


def summarize_pair(directory: Path, baseline: Path, retry_label: str) -> dict:
    directory = directory.resolve()
    manifest = json.loads((directory / "manifest.json").read_text("utf-8"))
    store = VideoStore(Path(manifest["database"]))
    retry_status = json.loads((directory / retry_label / "status.json").read_text("utf-8"))
    retry_source = next(item for item in manifest["experiments"] if item["video_id"] == retry_status["video_id"])
    retry = {
        **retry_source, "label": retry_label, "experiment_id": retry_status["id"],
        "status": retry_status["status"], "summary": summarize(store, retry_status["id"]),
        "retry_of_experiment_id": retry_source["experiment_id"],
    }
    selected = [retry if item is retry_source else item for item in manifest["experiments"]]
    if not all(item["summary"]["complete"] for item in selected):
        raise ValueError("Both selected experiments must be complete")
    audit = audit_calls({**manifest, "experiments": manifest["experiments"] + [retry]})
    saved_checks = [(directory / item["label"], item["experiment_id"])
                    for item in manifest["experiments"] + [retry]]
    unchanged = []
    for item in selected:
        old_folder = baseline / f"video-{item['video_id']}-high-600"
        old_status = json.loads((old_folder / "status.json").read_text("utf-8"))
        saved_checks.append((old_folder, old_status["id"]))
        old = json.loads((old_folder / "snapshot.json").read_text("utf-8"))
        new = json.loads((directory / item["label"] / "snapshot.json").read_text("utf-8"))
        unchanged.append({
            "video_id": item["video_id"], "transcript": old["transcript_hash"] == new["transcript_hash"],
            "input_view": old["input_view_hash"] == new["input_view_hash"],
            "config": old["config"] == new["config"], "profile": old["profile"] == new["profile"],
            "stored_asr": asr_hash(store, item["video_id"]) == item["asr_sha256"],
        })
    for folder, run_id in saved_checks:
        for filename, actual in (
            ("calls", store.list_experiment_calls(run_id)),
            ("steps", store.list_experiment_steps(run_id)),
            ("report", store.get_experiment_results(run_id)),
            ("status", dict(store.get_experiment(run_id))),
        ):
            if json.loads((folder / f"{filename}.json").read_text("utf-8")) != actual:
                raise ValueError(f"Archived log differs from database: {folder / filename}")
    if not all(all(value for key, value in item.items() if key != "video_id") for item in unchanged):
        raise ValueError("A setting other than the prompt or the ASR changed")
    result = {
        **manifest, "status": "completed_after_retry", "experiments": selected,
        "source_attempt_manifest": str(directory / "manifest.json"),
        "baseline_manifest": str(baseline / "manifest.json"),
        "audit_including_failed_attempts": audit,
        "historical_and_failed_logs_match_database": True, "unchanged_inputs": unchanged,
    }
    atomic_json(directory / "completion.json", result)
    write_review(store, {**result, "videos": selected}, directory)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--retry-label", required=True)
    args = parser.parse_args()
    result = summarize_pair(args.directory, args.baseline, args.retry_label)
    print(json.dumps({
        "status": result["status"], "audit": result["audit_including_failed_attempts"],
        "unchanged_inputs": result["unchanged_inputs"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
