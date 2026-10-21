"""Persist ASR-only fixtures in an isolated workspace for later LLM experiments."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def prepare_video(video_path: Path, workspace: Path, model_id: str = "fireredasr2-aed",
                  *, progress=None) -> dict:
    from asr_models import get_model
    from core import format_time, transcript_to_vtt
    from firered_resilience import resource_events
    from qwen_resilience import atomic_json
    from transcriber import transcribe_audio
    from video_pipeline import register_local_video
    from video_store import VideoStore

    workspace = workspace.resolve()
    model = get_model(model_id)
    store = VideoStore(workspace / "project.db")
    video_id, source, _existing = register_local_video(
        store, video_path, workspace / "temporary_audio",
    )
    video = store.get_video(video_id)
    segments = store.get_segments(video_id)
    reused = bool(segments)
    if reused and video["transcription_model"] != model.id:
        raise ValueError("Existing transcript uses another model; choose a new workspace.")

    def report_progress(fraction: float, message: str) -> None:
        if progress:
            progress(fraction, message)
        else:
            print(f"{fraction:.3f} {message}", flush=True)

    started = time.perf_counter()
    job_id = None
    if not reused:
        job_id = uuid.uuid4().hex
        store.create_job(job_id, "video_transcribe", video_id)
        store.update_job(
            job_id, status="running", stage="transcribing",
            started_at=datetime.now(timezone.utc).isoformat(),
            request_json=json.dumps(
                {"kind": "transcribe", "video_id": video_id, "model_id": model.id},
            ),
        )
        store.set_status(video_id, "processing")

        def resources(event: dict) -> None:
            store.update_job(job_id, resource_json=json.dumps(event, ensure_ascii=False))

        try:
            with resource_events(resources):
                segments, duration_ms = transcribe_audio(
                    source, model_id=model.id,
                    cache_directory=workspace / "temporary_audio" / "firered_jobs",
                    progress=report_progress,
                )
            if not segments:
                raise ValueError("ASR produced no segments.")
            store.mark_transcribed(video_id, segments, model.id)
            store.record_transcription_metrics(video_id, time.perf_counter() - started)
            store.update_job(
                job_id, status="completed", stage="completed", progress=1.0,
                message=f"ASR saved: {len(segments)} segments; no LLM requests.",
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
        except BaseException as error:
            store.set_status(video_id, "failed")
            store.update_job(
                job_id, status="failed", error_message=str(error),
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
            raise
    else:
        duration_ms = int(video["duration_ms"])

    persisted = store.get_segments(video_id)
    if persisted != segments:
        raise AssertionError("Database transcript differs from ASR result.")
    ids = [segment.id for segment in persisted]
    if len(ids) != len(set(ids)) or any(
        segment.start_ms < 0 or segment.end_ms <= segment.start_ms
        or segment.end_ms > max(duration_ms, int(video["duration_ms"])) + 1000
        for segment in persisted
    ):
        raise AssertionError("Invalid transcript identifiers or timestamps.")

    export = workspace / "exports" / f"video-{video_id}"
    export.mkdir(parents=True, exist_ok=True)
    payload = [asdict(segment) for segment in persisted]
    atomic_json(export / "subtitles.json", payload)
    (export / "subtitles.vtt").write_text(transcript_to_vtt(persisted), encoding="utf-8")
    (export / "transcript.txt").write_text(
        "\n".join(segment.as_prompt_line() for segment in persisted) + "\n",
        encoding="utf-8",
    )
    transcript_hash = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"),
    ).hexdigest()
    summary = {
        "video_id": video_id, "job_id": job_id, "source": str(source),
        "database": str(store.path), "model_id": model.id,
        "source_content_hash": video["content_hash"],
        "duration_ms": int(video["duration_ms"]),
        "audio_duration_ms": duration_ms,
        "duration": format_time(int(video["duration_ms"])),
        "segment_count": len(persisted),
        "word_count": sum(len(segment.words or []) for segment in persisted),
        "text_characters": sum(len(segment.text) for segment in persisted),
        "transcript_sha256": transcript_hash,
        "reused_database_transcript": reused,
        "wall_seconds": round(time.perf_counter() - started, 3),
        "llm_requests": 0,
        "export_directory": str(export),
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    if not reused or not (export / "preparation.json").is_file():
        atomic_json(export / "preparation.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--model", default="fireredasr2-aed")
    args = parser.parse_args()
    summary = prepare_video(args.video, args.workspace, args.model)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
