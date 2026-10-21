"""Run Qwen3-ASR on bounded audio chunks and merge timestamped results."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import tempfile
import wave
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from asr_models import component_paths
from core import TranscriptSegment
from media import extract_audio
from transcriber import QwenWorker, prepare_asr_model
from scripts.compare_asr import _write_result, _stamp
from qwen_boundaries import plan_spans
from qwen_vad import detect_speech
from qwen_silence import (
    QWEN_AUDIO_PROFILES, build_silence_plan, compact_audio, resolve_silence_skipping,
    restore_segments,
)
from qwen_memory import ensure_memory_headroom


MODEL_ID = "qwen3-asr-1.7b"


def _progress(fraction: float, message: str) -> None:
    print(f"[qwen3-asr-1.7b] {fraction * 100:5.1f}% {message}", flush=True)


def _offset_segment(segment: TranscriptSegment, offset_ms: int, index: int) -> TranscriptSegment:
    words = []
    for word in segment.words or []:
        item = dict(word)
        if item.get("start") is not None:
            item["start"] = round(float(item["start"]) + offset_ms / 1000, 3)
        if item.get("end") is not None:
            item["end"] = round(float(item["end"]) + offset_ms / 1000, 3)
        words.append(item)
    return TranscriptSegment(
        index,
        segment.start_ms + offset_ms,
        segment.end_ms + offset_ms,
        segment.text,
        words,
    )


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--chunk-seconds", type=int, default=300)
    parser.add_argument("--max-chunks", type=int)
    parser.add_argument("--boundary-mode", choices=["fixed", "energy", "firered"], default="fixed")
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--vad-model-path")
    parser.add_argument("--vad-python")
    parser.add_argument(
        "--audio-profile", choices=QWEN_AUDIO_PROFILES, default="general",
        help="Select mostly-silent only when the user confirms most of the audio has no dialogue",
    )
    silence_switch = parser.add_mutually_exclusive_group()
    silence_switch.add_argument(
        "--skip-non-speech", action="store_true", default=None,
        help="Explicit silence-skipping override for experiments",
    )
    silence_switch.add_argument(
        "--keep-non-speech", action="store_false", dest="skip_non_speech",
        help="Keep all audio even for the mostly-silent profile",
    )
    parser.add_argument("--speech-padding-seconds", type=float, default=.3)
    parser.add_argument("--min-removed-seconds", type=float, default=1.5)
    parser.add_argument("--staged", action="store_true", help="Separate GPU ASR and GPU alignment processes")
    parser.add_argument("--resource-mode", choices=["auto", "low-resource", "manual"], default="auto")
    parser.add_argument("--align-batch-size", type=int, default=2)
    parser.add_argument("--load-profile", choices=["standard", "low-memory"], default="standard")
    parser.add_argument("--no-auto-recovery", action="store_true")
    args = parser.parse_args(argv)
    args.skip_non_speech = resolve_silence_skipping(
        args.audio_profile, skip_non_speech=args.skip_non_speech,
    )
    if args.chunk_seconds < 30:
        raise SystemExit("--chunk-seconds 至少为 30")
    if args.batch_size < 1:
        raise SystemExit("--batch-size must be positive")
    if args.max_chunks is not None and args.max_chunks < 1:
        raise SystemExit("--max-chunks must be positive")
    if args.staged:
        if args.boundary_mode != "firered" or args.max_chunks is not None:
            parser.error("--staged currently requires --boundary-mode firered and no --max-chunks")
        if args.vad_python or args.vad_model_path:
            parser.error("--staged currently uses the configured FireRed runtime/model")
    return args


def main() -> int:
    args = _parse_args()
    if args.staged:
        from qwen_resilience import QwenOptions, run_qwen
        from processing_control import ResourcePaused
        args.output.mkdir(parents=True, exist_ok=True)
        try:
            segments, duration = run_qwen(
                args.video, args.output / "jobs", component_paths(MODEL_ID),
                options=QwenOptions(
                    mode=args.resource_mode, batch_size=args.batch_size,
                    chunk_seconds=args.chunk_seconds, align_batch_size=args.align_batch_size,
                    load_profile=args.load_profile, auto_recovery=not args.no_auto_recovery,
                    audio_profile=args.audio_profile, skip_non_speech=args.skip_non_speech,
                    speech_padding_seconds=args.speech_padding_seconds,
                    min_removed_seconds=args.min_removed_seconds,
                ), progress=_progress, report_path=args.output / "execution.json",
            )
        except ResourcePaused as exc:
            print(str(exc), flush=True)
            return 2
        _write_result(args.output, MODEL_ID, "Qwen staged GPU", segments, duration)
        return 0

    video = args.video.expanduser().resolve()
    output_dir = args.output.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if not video.is_file():
        raise SystemExit(f"视频不存在：{video}")

    prepare_asr_model(MODEL_ID, lambda fraction, message: _progress(fraction * 0.05, message))
    worker = None
    segments: list[TranscriptSegment] = []
    duration_ms = 0
    completed_chunks = 0
    try:
        with tempfile.TemporaryDirectory(prefix="qwen-chunks-") as temporary:
            audio_path = Path(temporary) / "timeline.wav"
            duration_ms = extract_audio(video, audio_path)
            with wave.open(str(audio_path), "rb") as source:
                rate = source.getframerate()
                channels = source.getnchannels()
                width = source.getsampwidth()
                total_frames = source.getnframes()
                # Use the extracted WAV timeline for model outputs. PyAV's
                # resampling can add a small tail beyond the container duration.
                duration_ms = round(total_frames * 1000 / rate)
                if rate != 16000 or channels != 1 or width != 2:
                    raise ValueError("Expected mono 16 kHz PCM16")
                import numpy as np
                audio = np.frombuffer(source.readframes(total_frames), dtype="<i2").astype("float32") / 32768
                vad = None
                if args.boundary_mode == "firered" or args.skip_non_speech:
                    vad = detect_speech(
                        audio_path, "firered", model_path=args.vad_model_path,
                        python_path=args.vad_python,
                    )
                spans = plan_spans(
                    audio, rate, args.chunk_seconds, args.boundary_mode,
                    vad["speech_intervals"] if vad else None,
                )
                silence = build_silence_plan(
                    spans, total_frames, rate, vad["speech_intervals"] if vad else None,
                    enabled=args.skip_non_speech, padding_seconds=args.speech_padding_seconds,
                    min_removed_seconds=args.min_removed_seconds,
                )
                (output_dir / "silence_plan.json").write_text(
                    json.dumps(silence, ensure_ascii=False, indent=2), encoding="utf-8",
                )
                metadata = {
                    "boundary_mode": args.boundary_mode, "batch_size": args.batch_size,
                    "chunk_seconds": args.chunk_seconds, "duration_ms": duration_ms,
                    "vad": vad, "spans": [asdict(span) for span in spans],
                    "selected_chunk_count": min(len(spans), args.max_chunks) if args.max_chunks else len(spans),
                    "is_partial": args.max_chunks is not None and args.max_chunks < len(spans),
                    "skip_non_speech": args.skip_non_speech,
                    "audio_profile": args.audio_profile,
                }
                (output_dir / "chunk_plan.json").write_text(
                    json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8",
                )
                total_chunks = len(spans)
                if args.max_chunks is not None:
                    spans = spans[:args.max_chunks]
                chunks = [c for c in silence["chunks"][:len(spans)] if c["input_samples"]]
                mapping_warnings = []
                paths = component_paths(MODEL_ID)
                if chunks:
                    worker = QwenWorker()

                for chunk_index in range(0, len(chunks), args.batch_size):
                    ensure_memory_headroom()
                    group = chunks[chunk_index:chunk_index + args.batch_size]
                    chunk_paths = []
                    for position, chunk_map in enumerate(group, chunk_index):
                        if args.skip_non_speech:
                            raw = (compact_audio(audio, chunk_map) * 32768).astype("<i2").tobytes()
                        else:
                            source.setpos(chunk_map["start_sample"])
                            raw = source.readframes(chunk_map["end_sample"] - chunk_map["start_sample"])
                        chunk_path = Path(temporary) / f"chunk-{position:04d}.wav"
                        with wave.open(str(chunk_path), "wb") as chunk:
                            chunk.setnchannels(channels)
                            chunk.setsampwidth(width)
                            chunk.setframerate(rate)
                            chunk.writeframes(raw)
                        chunk_paths.append(chunk_path)
                    _progress(
                        0.05 + 0.9 * chunk_index / max(1, total_chunks),
                        f"转写分段 {chunk_index + 1}/{total_chunks} BS={args.batch_size}",
                    )
                    results = worker.transcribe_batch(
                        chunk_paths, paths,
                        lambda fraction, message, ci=chunk_index: _progress(
                            0.05 + 0.9 * (ci + min(0.99, fraction)) / max(1, total_chunks),
                            message,
                        ),
                        batch_size=args.batch_size,
                    )
                    if len(results) != len(group):
                        raise ValueError("Unexpected Qwen batch output count")
                    for chunk_map, (chunk_segments, _chunk_duration) in zip(group, results):
                        if args.skip_non_speech:
                            mapped = restore_segments(
                                chunk_segments, chunk_map, rate, total_frames,
                                warnings=mapping_warnings,
                            )
                            offset_ms = 0
                        else:
                            mapped = chunk_segments
                            offset_ms = round(chunk_map["start_sample"] * 1000 / rate)
                        for segment in mapped:
                            segments.append(_offset_segment(segment, offset_ms, len(segments) + 1))
                        completed_chunks += 1
                (output_dir / "mapping_warnings.json").write_text(
                    json.dumps(mapping_warnings, ensure_ascii=False, indent=2), encoding="utf-8",
                )
    finally:
        if worker:
            worker.close()

    _write_result(output_dir, MODEL_ID, "Qwen3-ASR 1.7B（分段对齐）", segments, duration_ms)
    result_path = output_dir / f"{MODEL_ID}.json"
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    payload.update({
        "boundary_mode": args.boundary_mode, "batch_size": args.batch_size,
        "is_partial": metadata["is_partial"],
        "processed_duration_ms": round(spans[-1].end_sample * 1000 / rate) if spans else 0,
        "skip_non_speech": args.skip_non_speech,
        "audio_profile": args.audio_profile,
        "asr_input_duration_ms": round(sum(c["input_samples"] for c in chunks) * 1000 / rate),
        "timestamp_axis": "original_source",
    })
    result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"完成：{completed_chunks} 段音频，{len(segments)} 个字幕段；"
        f"结果目录：{output_dir}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
