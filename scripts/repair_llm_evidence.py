"""Replay only unresolved citations after offline rematching, preserving source experiments."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from threading import RLock
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ai_provider import ProviderProfile, load_provider_settings, resolve_interface_key
from core import TranscriptSegment
import llm_experiments as lab
from qwen_resilience import atomic_json
from scripts.llm_lab_batch import asr_hash, export_run, write_review
from video_store import VideoStore


def preservation_state(store: VideoStore, source: dict) -> dict:
    """Hash immutable source artifacts and every preexisting experiment row."""
    with store.connect() as connection:
        tables = {
            table: [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in ("video_experiments", "llm_experiment_calls",
                          "llm_experiment_steps", "llm_experiment_results")
        }
        marker_count = connection.execute("SELECT COUNT(*) FROM video_markers").fetchone()[0]
    files = {
        str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(Path(source["directory"]).rglob("*")) if path.is_file()
    }
    return {"rows": tables, "files": files, "formal_markers": marker_count,
            "asr_hashes": {str(v["video_id"]): asr_hash(store, v["video_id"]) for v in source["videos"]}}


def verify_preservation(store: VideoStore, before: dict) -> dict:
    checked = {}
    with store.connect() as connection:
        for table, rows in before["rows"].items():
            keys = ["experiment_id", "step_id"] if table == "llm_experiment_steps" else [
                "experiment_id" if table == "llm_experiment_results" else
                "id"
            ]
            for row in rows:
                current = connection.execute(
                    f"SELECT * FROM {table} WHERE " + " AND ".join(f"{key} = ?" for key in keys),
                    [row[key] for key in keys],
                ).fetchone()
                if current is None or dict(current) != row:
                    raise ValueError(f"Preexisting row changed: {table}")
            checked[table] = len(rows)
        markers = connection.execute("SELECT COUNT(*) FROM video_markers").fetchone()[0]
    if markers != before["formal_markers"]:
        raise ValueError("Formal markers changed")
    for path, value in before["files"].items():
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != value:
            raise ValueError(f"Source file changed: {path}")
    for video_id, value in before["asr_hashes"].items():
        if asr_hash(store, int(video_id)) != value:
            raise ValueError(f"ASR changed: {video_id}")
    return {"passed": True, "unchanged_rows": checked, "unchanged_source_files": len(before["files"]),
            "unchanged_asr_videos": len(before["asr_hashes"]), "formal_markers": markers}


def run_batch(source_path: Path, *, execute: bool = False, concurrency: int = 2) -> dict:
    if concurrency not in (1, 2):
        raise ValueError("Correction replay concurrency must be 1 or 2")
    source = json.loads(source_path.read_text(encoding="utf-8"))
    if source.get("operation") != "offline_citation_rematch" or source["status"] != "completed":
        raise ValueError("Expected a completed offline citation rematch manifest")
    store = VideoStore(Path(source["database"]))
    before = preservation_state(store, source)
    batch_id = "citation-repair-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    directory = store.path.parent / "batches" / batch_id
    snapshots, entries = {}, []
    for source_item in source["videos"]:
        run_id = source_item["experiment_id"]
        rejections = [step for step in store.list_experiment_steps(run_id) if step.get("rejected")]
        if not rejections:
            continue
        video_id = source_item["video_id"]
        parent = json.loads((Path(source["directory"]) / f"video-{video_id}" / "snapshot.json").read_text("utf-8"))
        if lab.digest({k: v for k, v in parent.items() if k != "snapshot_hash"}) != parent["snapshot_hash"]:
            raise ValueError("Source snapshot changed")
        if json.loads(store.get_experiment(run_id)["options_json"])["snapshot_hash"] != parent["snapshot_hash"]:
            raise ValueError("Source database snapshot differs")
        config = lab.LabConfig.model_validate({**parent["config"], "max_evidence_repairs": 2})
        bundle = {**parent["prompt_bundle"], "evidence_repair": lab.load_prompt_bundle()["evidence_repair"]}
        bundle["version"] = lab.digest({k: v for k, v in bundle.items() if k != "version"})
        profile = ProviderProfile.model_validate(parent["profile"])
        snapshot = lab.prepare_snapshot(store, video_id, config, profile, bundle=bundle)
        if (snapshot["transcript_hash"] != parent["transcript_hash"]
                or snapshot["input_view_hash"] != parent["input_view_hash"]):
            raise ValueError("Transcript/input view changed")
        by_id = {x["id"]: TranscriptSegment(x["id"], x["start_ms"], x["end_ms"], x["text"])
                 for x in snapshot["input_segments"]}
        requests = {r["chunk_id"]: r for r in parent["requests"]}
        replay_requests = []
        for step in rejections:
            original_request = requests[step["step_id"]]
            supplied = [by_id[i] for i in original_request["sentence_ids"]]
            replay_requests.append({
                "chunk_id": step["step_id"], "sentence_ids": original_request["sentence_ids"],
                "source_call_id": step["source_call_id"], "rejected": step["rejected"],
                "request": lab.request_body(profile, config, lab.render_evidence_repair(
                    bundle, config, supplied, step["rejected"][0],
                    backend_audit=next((a for a in step["citation_matches"]
                                        if a["original_candidate"] == step["rejected"][0]["candidate"]), None),
                )),
            })
        snapshot.update(
            operation="evidence_repair_replay", source_experiment_id=run_id,
            source_snapshot_hash=parent["snapshot_hash"], requests=replay_requests,
            source_pre_merge_candidates=store.get_experiment_results(run_id)["pre_merge_candidates"],
        )
        snapshot.pop("snapshot_hash")
        snapshot["snapshot_hash"] = lab.digest(snapshot)
        snapshots[video_id] = snapshot
        entries.append({
            "video_id": video_id, "source": source_item["source"], "status": "prepared",
            "source_experiment_id": run_id, "asr_sha256": source_item["asr_sha256"],
            "snapshot_hash": snapshot["snapshot_hash"],
            "unresolved_count": sum(len(step["rejected"]) for step in rejections),
        })
        atomic_json(directory / f"video-{video_id}" / "snapshot.json", snapshot)
    if not entries:
        raise ValueError("No unresolved citations")
    manifest = {
        "batch_id": batch_id, "operation": "evidence_repair_replay",
        "source_batch_id": source["batch_id"], "database": str(store.path),
        "directory": str(directory), "profile": source["profile"],
        "concurrency": concurrency, "total": len(entries),
        "planned_candidates": sum(e["unresolved_count"] for e in entries),
        "status": "running" if execute else "preview_only", "videos": entries,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    lock = RLock()

    def persist() -> None:
        with lock:
            atomic_json(directory / "manifest.json", manifest)

    persist()
    if not execute:
        return manifest
    settings = load_provider_settings()
    profile = ProviderProfile.model_validate(source["profile"])
    if (settings.active_profile.base_url != profile.base_url
            or settings.active_profile.model != profile.model):
        raise ValueError("Active interface differs from source")
    key = resolve_interface_key(profile, settings)
    profile.require_ready(key)

    def process(item: dict) -> None:
        video_id = item["video_id"]
        snapshot = snapshots[video_id]
        run_id = lab.register_run(store, f"Citation-only repair {batch_id} video {video_id}",
                                  snapshot, snapshot["snapshot_hash"])
        with lock:
            item.update(experiment_id=run_id, status="processing")
            persist()
        try:
            lab.execute_run(store, run_id, snapshot, key, snapshot["snapshot_hash"])
        except Exception:
            item["status"] = "failed"
        finally:
            report = store.get_experiment_results(run_id)
            if report:
                item.update(status=report["status"], summary={
                    **{k: report[k] for k in ("candidate_count", "call_count", "format_repair_count", "usage")},
                    "rejected_count": report["citation_metrics"]["unresolved_count"],
                    "model_recovered_count": report["citation_metrics"]["model_recovered_count"],
                })
            export_run(store, item, snapshot, directory / f"video-{video_id}")
            persist()
            print(f"video={video_id} status={item['status']} summary={item.get('summary')}", flush=True)
    try:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(process, item) for item in entries]
            for future in as_completed(futures):
                future.result()
        manifest["status"] = "completed" if all(
            e["status"] in ("completed", "completed_with_validation_errors") for e in entries
        ) else "completed_with_failures"
    finally:
        if manifest["status"] == "running":
            manifest["status"] = "interrupted"
        atomic_json(directory / "verification.json", verify_preservation(store, before))
        manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        persist()
        write_review(store, manifest, directory)
    return manifest


def main() -> None:
    from filelock import FileLock
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-manifest", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--concurrency", type=int, default=2)
    args = parser.parse_args()
    source = json.loads(args.source_manifest.read_text("utf-8"))
    with FileLock(str(Path(source["database"]).parent / "llm-batch.lock"), timeout=0):
        result = run_batch(args.source_manifest, execute=args.execute, concurrency=args.concurrency)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
