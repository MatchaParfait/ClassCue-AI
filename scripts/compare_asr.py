"""Run all locally ready ASR models on one video and save comparable outputs."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import traceback

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from asr_models import MODELS, get_model, model_is_downloaded
from media import extract_audio
from transcriber import transcribe_audio, unload_model


def _stamp(milliseconds: int) -> str:
    milliseconds = max(0, int(milliseconds))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def _write_result(output_dir: Path, model_id: str, label: str, segments, duration_ms: int) -> None:
    payload = {
        "model_id": model_id,
        "model_label": label,
        "duration_ms": duration_ms,
        "segments": [asdict(segment) for segment in segments],
    }
    (output_dir / f"{model_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    timestamped = "\n".join(
        f"[{_stamp(segment.start_ms)} --> {_stamp(segment.end_ms)}] {segment.text}"
        for segment in segments
    )
    (output_dir / f"{model_id}.txt").write_text(
        timestamped + ("\n" if timestamped else ""),
        encoding="utf-8",
    )

    srt_blocks = []
    for index, segment in enumerate(segments, start=1):
        start = _stamp(segment.start_ms).replace(".", ",")
        end = _stamp(segment.end_ms).replace(".", ",")
        srt_blocks.append(f"{index}\n{start} --> {end}\n{segment.text}")
    (output_dir / f"{model_id}.srt").write_text(
        "\n\n".join(srt_blocks) + ("\n" if srt_blocks else ""),
        encoding="utf-8",
    )


def _progress(model_id: str, fraction: float, message: str) -> None:
    percent = max(0, min(100, round(float(fraction) * 100)))
    print(f"[{model_id}] {percent:3d}% {message}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--models",
        nargs="+",
        choices=list(MODELS),
        default=list(MODELS),
        help="模型 ID；默认尝试全部模型，但只运行已准备好的模型。",
    )
    parser.add_argument(
        "--allow-download",
        action="store_true",
        help="允许为未准备好的模型下载权重；默认跳过未准备模型。",
    )
    args = parser.parse_args()

    video = args.video.expanduser().resolve()
    if not video.is_file():
        raise SystemExit(f"视频不存在：{video}")
    output_dir = (
        args.output.expanduser().resolve()
        if args.output
        else video.with_name(video.stem + "_asr_compare")
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    statuses: list[dict] = []
    manifest = {
        "video": str(video),
        "output_dir": str(output_dir),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "models_requested": args.models,
    }

    with tempfile.TemporaryDirectory(prefix="asr-compare-") as temporary:
        audio_path = Path(temporary) / "audio.wav"
        print(f"抽取音频：{video}", flush=True)
        source_duration_ms = extract_audio(video, audio_path)
        manifest["source_duration_ms"] = source_duration_ms
        print(f"音频已准备，时长 {_stamp(source_duration_ms)}", flush=True)

        for model_id in args.models:
            model = get_model(model_id)
            status = {
                "model_id": model.id,
                "label": model.label,
                "engine": model.engine,
                "status": "pending",
            }
            print(f"\n=== {model.label} ({model.id}) ===", flush=True)
            if not model_is_downloaded(model.id) and not args.allow_download:
                status.update({
                    "status": "skipped",
                    "reason": "模型未准备，未启用 --allow-download",
                })
                statuses.append(status)
                print("跳过：模型权重未准备。", flush=True)
                continue
            try:
                segments, duration_ms = transcribe_audio(
                    audio_path,
                    model_id=model.id,
                    progress=lambda fraction, message, mid=model.id: _progress(
                        mid, fraction, message
                    ),
                )
                _write_result(output_dir, model.id, model.label, segments, duration_ms)
                status.update({
                    "status": "completed",
                    "duration_ms": duration_ms,
                    "segment_count": len(segments),
                    "text_file": f"{model.id}.txt",
                    "srt_file": f"{model.id}.srt",
                    "json_file": f"{model.id}.json",
                })
                print(f"完成：{len(segments)} 段", flush=True)
            except Exception as exc:
                status.update({
                    "status": "failed",
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                })
                print(f"失败：{exc}", flush=True)
            finally:
                unload_model(model.id)
            statuses.append(status)

    manifest["models"] = statuses
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    completed = [item for item in statuses if item["status"] == "completed"]
    failed = [item for item in statuses if item["status"] == "failed"]
    skipped = [item for item in statuses if item["status"] == "skipped"]
    lines = [
        f"# ASR 对比结果",
        "",
        f"- 视频：`{video}`",
        f"- 时长：`{_stamp(source_duration_ms)}`",
        f"- 完成：{len(completed)}，失败：{len(failed)}，跳过：{len(skipped)}",
        "",
        "| 模型 | 引擎 | 状态 | 段数 | 文本 | 字幕 | JSON |",
        "|---|---|---|---:|---|---|---|",
    ]
    for item in statuses:
        if item["status"] == "completed":
            lines.append(
                f"| {item['label']} | {item['engine']} | 完成 | "
                f"{item['segment_count']} | `{item['text_file']}` | "
                f"`{item['srt_file']}` | `{item['json_file']}` |"
            )
        else:
            lines.append(
                f"| {item['label']} | {item['engine']} | {item['status']} | - | - | - | "
                f"{item.get('reason', item.get('error', ''))} |"
            )
    (output_dir / "comparison.md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )
    print(f"\n结果目录：{output_dir}", flush=True)
    return 0 if completed else 1


if __name__ == "__main__":
    raise SystemExit(main())
