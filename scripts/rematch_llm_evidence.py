"""Offline citation rematching of saved B experiments; never creates an LLM client."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import llm_experiments as lab
from citation_matching import MATCHING_VERSION
from core import TranscriptSegment
from qwen_resilience import atomic_json
from scripts.llm_lab_batch import asr_hash, export_run, write_review
from video_store import VideoStore


def rematch_batch(source_path: Path) -> dict:
    from scripts.repair_llm_evidence import preservation_state, verify_preservation

    source = json.loads(source_path.read_text(encoding="utf-8"))
    if source["status"] != "completed":
        raise ValueError("Source extraction batch must be complete")
    store = VideoStore(Path(source["database"]))
    before = preservation_state(store, source)
    batch_id = "citation-rematch-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    directory = store.path.parent / "batches" / batch_id
    manifest = {
        "batch_id": batch_id, "source_batch_id": source["batch_id"],
        "operation": "offline_citation_rematch", "status": "running",
        "database": str(store.path), "profile": source["profile"], "total": len(source["videos"]),
        "matching_version": MATCHING_VERSION, "llm_requests": 0,
        "directory": str(directory), "videos": [], "started_at": datetime.now(timezone.utc).isoformat(),
    }
    audit_cases = []

    def persist() -> None:
        atomic_json(directory / "manifest.json", manifest)

    persist()
    try:
        for source_item in source["videos"]:
            video_id = source_item["video_id"]
            if asr_hash(store, video_id) != source_item["asr_sha256"]:
                raise ValueError(f"ASR changed: video {video_id}")
            source_run = source_item["experiment_id"]
            parent_path = Path(source["directory"]) / f"video-{video_id}" / "snapshot.json"
            parent = json.loads(parent_path.read_text(encoding="utf-8"))
            unsigned = {key: value for key, value in parent.items() if key != "snapshot_hash"}
            if lab.digest(unsigned) != parent["snapshot_hash"]:
                raise ValueError("Source snapshot was changed")
            if json.loads(store.get_experiment(source_run)["options_json"])["snapshot_hash"] != parent["snapshot_hash"]:
                raise ValueError("Source experiment does not match the saved snapshot")
            snapshot = json.loads(json.dumps(parent))
            snapshot.update(
                operation="offline_citation_rematch", source_experiment_id=source_run,
                source_snapshot_hash=parent["snapshot_hash"],
                runner_code_hash=hashlib.sha256(Path(lab.__file__).read_bytes()).hexdigest(),
                citation_matching_code_hash=hashlib.sha256((ROOT / "citation_matching.py").read_bytes()).hexdigest(),
                matching_version=MATCHING_VERSION,
            )
            snapshot["config"] = lab.LabConfig.model_validate({
                **parent["config"], "match_evidence_quotes": True,
            }).model_dump()
            snapshot.pop("snapshot_hash")
            snapshot["snapshot_hash"] = lab.digest(snapshot)
            run_id = lab.register_run(store, f"Offline citation matching {batch_id} video {video_id}",
                                      snapshot, snapshot["snapshot_hash"])
            store.update_experiment(run_id, status="processing")
            item = {
                "video_id": video_id, "source": source_item["source"],
                "source_experiment_id": source_run, "experiment_id": run_id, "status": "processing",
                "asr_sha256": source_item["asr_sha256"],
            }
            manifest["videos"].append(item)
            persist()
            full = [TranscriptSegment(x["id"], x["start_ms"], x["end_ms"], x["text"])
                    for x in parent["input_segments"]]
            by_id = {segment.id: segment for segment in full}
            requests = {request["chunk_id"]: request for request in parent["requests"]}
            original = store.get_experiment_results(source_run)
            collected = list(original["pre_merge_candidates"])
            failures = []
            matches_count = rejects_count = initial_rejected = 0
            for step in store.list_experiment_steps(source_run):
                rejected = step.get("rejected", [])
                supplied = [by_id[i] for i in requests[step["step_id"]]["sentence_ids"]]
                recovered, unresolved, audits = lab.rematch_rejections(rejected, supplied, full)
                initial_rejected += len(rejected)
                matches_count += len(recovered)
                rejects_count += len(unresolved)
                if unresolved:
                    failures.append({"step_id": step["step_id"], "rejected_candidates": len(unresolved)})
                store.save_experiment_step(run_id, step["step_id"], {
                    "step_id": step["step_id"], "stage": "citation_match", "status": "completed",
                    "source_experiment_id": source_run, "source_call_id": step["call_id"],
                    "first_rejected": rejected, "accepted": recovered, "rejected": unresolved,
                    "citation_matches": audits,
                })
                for candidate in recovered:
                    collected.append({
                        **candidate, "call_id": step["call_id"], "origin_chunk_id": step["step_id"],
                        "source_experiment_id": source_run, "backend_citation_rematched": True,
                    })
                for audit in audits:
                    audit_cases.append({
                        **audit, "video_id": video_id, "source_experiment_id": source_run,
                        "derived_experiment_id": run_id, "source_chunk_id": step["step_id"],
                    })
            results = lab._deduplicate(collected, by_id)
            for result in results:
                result["source_sentence_ids"] = [
                    source_id for segment in parent["input_segments"]
                    if result["start_sentence_id"] <= segment["id"] <= result["end_sentence_id"]
                    for source_id in segment["source_sentence_ids"]
                ]
                result["source_start_sentence_id"] = result["source_sentence_ids"][0]
                result["source_end_sentence_id"] = result["source_sentence_ids"][-1]
            status = "completed_with_validation_errors" if failures else "completed"
            report = lab._run_report(store, run_id, snapshot, collected, failures, results, status, complete=True)
            report.update(
                scope="offline_citation_rematch", source_experiment_id=source_run,
                source_candidate_count=original["candidate_count"],
                source_api_call_count=original["call_count"],
                source_format_metrics=original["format_metrics"],
                citation_metrics={
                    "initial_rejected_count": initial_rejected, "matched_count": matches_count,
                    "unresolved_count": rejects_count, "additional_api_calls": 0,
                },
            )
            store.save_experiment_results(run_id, report)
            store.update_experiment(run_id, status=status, candidate_count=len(results))
            item.update(
                status=status, summary={
                    "candidate_count": len(results), "source_candidate_count": original["candidate_count"],
                    "matched_count": matches_count, "rejected_count": rejects_count,
                    "call_count": 0, "format_repair_count": 0, "usage": {"reported_calls": 0, "totals": {}},
                },
            )
            export_run(store, item, snapshot, directory / f"video-{video_id}")
            persist()
            print(f"video={video_id} restored={matches_count} unresolved={rejects_count} "
                  f"candidates={len(results)} llm_requests=0", flush=True)
        manifest.update(
            status="completed",
            original_candidate_count=sum(item["summary"]["source_candidate_count"] for item in manifest["videos"]),
            candidate_count=sum(item["summary"]["candidate_count"] for item in manifest["videos"]),
            matched_count=sum(item["summary"]["matched_count"] for item in manifest["videos"]),
            unresolved_count=sum(item["summary"]["rejected_count"] for item in manifest["videos"]),
        )
        atomic_json(directory / "matching-audit.json", audit_cases)
        return manifest
    finally:
        if manifest["status"] == "running":
            manifest["status"] = "interrupted"
        manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        atomic_json(directory / "verification.json", verify_preservation(store, before))
        persist()
        write_review(store, manifest, directory)


def main() -> None:
    from filelock import FileLock

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-manifest", required=True, type=Path)
    args = parser.parse_args()
    source = json.loads(args.source_manifest.read_text(encoding="utf-8"))
    with FileLock(str(Path(source["database"]).parent / "llm-batch.lock"), timeout=0):
        manifest = rematch_batch(args.source_manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
