"""Export every skipped interval, context audio and source-time transcript pairs."""

import argparse
import csv
import html
import json
from pathlib import Path
import sys
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.compare_asr import _stamp


def nearby_transcript(payload, left, right, sample_start):
    result = []
    for segment in payload.get("segments", []):
        start, end = segment["start_ms"] / 1000 + sample_start, segment["end_ms"] / 1000 + sample_start
        if end > left and start < right:
            result.append({"start": start, "end": end, "text": segment["text"]})
    return result


def build_review(report, context_seconds=2):
    import numpy as np

    report = Path(report).resolve()
    meta = json.loads((report / "inference_plan.json").read_text(encoding="utf-8"))
    full = json.loads((report / "silence_full_plan.json").read_text(encoding="utf-8"))
    output = report / "review"
    output.mkdir(parents=True, exist_ok=True)
    payloads = {}
    for mode in ("no-skip", "skip"):
        path = report / f"qwen-{mode}.json"
        payloads[mode] = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    rows, entries = [], []
    sample_start = meta["sample_start_seconds"]
    timing_path = report / "qwen_timings.json"
    timings = json.loads(timing_path.read_text(encoding="utf-8")) if timing_path.is_file() else {}
    warnings = [
        w for e in timings.get("modes", []) if e["mode"] == "skip" for w in e.get("mapping_warnings", [])
    ]
    with wave.open(meta["source_audio"], "rb") as source:
        rate, total = source.getframerate(), source.getnframes()
        for index, gap in enumerate(full["silence"]["deleted_regions"], 1):
            a, b = gap["start_sample"], gap["end_sample"]
            related_warnings = [
                {**w, "source_start_seconds": w["source_start_seconds"] + sample_start,
                 "source_end_seconds": w["source_end_seconds"] + sample_start}
                for w in warnings if w["source_end_seconds"] + sample_start > a / rate
                and w["source_start_seconds"] + sample_start < b / rate
            ]
            left = max(0, a - round(context_seconds * rate))
            right = min(total, b + round(context_seconds * rate))
            if related_warnings:
                left = max(0, min(left, round(
                    (min(w["source_start_seconds"] for w in related_warnings) - context_seconds) * rate,
                )))
                right = min(total, max(right, round(
                    (max(w["source_end_seconds"] for w in related_warnings) + context_seconds) * rate,
                )))
            source.setpos(left)
            pcm = source.readframes(right - left)
            files = {
                "original": f"original-{index:03d}.wav",
                "deleted": f"deleted-{index:03d}.wav",
                "joined": f"joined-{index:03d}.wav",
            }
            removed_pcm = pcm[(a - left) * 2:(b - left) * 2]
            joined_pcm = pcm[:(a - left) * 2] + pcm[(b - left) * 2:]
            for key, samples in (("original", pcm), ("deleted", removed_pcm), ("joined", joined_pcm)):
                if not samples:
                    files[key] = None
                    continue
                with wave.open(str(output / files[key]), "wb") as clip:
                    clip.setparams((1, 2, rate, 0, "NONE", "not compressed"))
                    clip.writeframes(samples)
            audio = np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768
            bins = np.array_split(audio, min(400, len(audio)))
            envelope = [[round(float(s.min()), 5), round(float(s.max()), 5)] for s in bins]
            row = {
                "index": index, "start_seconds": a / rate, "end_seconds": b / rate,
                "start_time": _stamp(round(a / rate * 1000)),
                "end_time": _stamp(round(b / rate * 1000)),
                "duration_seconds": (b - a) / rate, "start_sample": a, "end_sample": b,
                "original_audio": files["original"], "deleted_audio": files["deleted"],
                "joined_audio": files["joined"], "review": "pending", "notes": "",
            }
            words = [
                {**w, "start": w["start"] + sample_start, "end": w["end"] + sample_start}
                for s in payloads["no-skip"].get("segments", []) for w in (s.get("words") or [])
                if w["end"] + sample_start > a / rate and w["start"] + sample_start < b / rate
            ]
            rows.append(row)
            entries.append({
                **row, "clip_start_seconds": left / rate, "clip_end_seconds": right / rate,
                "deleted_start_in_clip": (a - left) / rate,
                "deleted_end_in_clip": (b - left) / rate, "envelope": envelope,
                "baseline_words_in_removed_region": words,
                "baseline": nearby_transcript(payloads["no-skip"], left / rate, right / rate, sample_start),
                "filtered": nearby_transcript(payloads["skip"], left / rate, right / rate, sample_start),
                "splice_warnings": related_warnings,
            })
    columns = [
        "index", "start_seconds", "end_seconds", "start_time", "end_time",
        "duration_seconds", "start_sample", "end_sample", "original_audio",
        "deleted_audio", "joined_audio", "review", "notes",
    ]
    with (output / "deleted_regions.csv").open("w", encoding="utf-8-sig", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    (output / "review_data.json").write_text(
        json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    template = (ROOT / "scripts/templates/qwen_silence_review.html").read_text(encoding="utf-8")
    timing_labels = []
    round_path = report / "two_round_summary.json"
    means = json.loads(round_path.read_text(encoding="utf-8"))["mean_seconds"] if round_path.is_file() else {}
    for mode, label in (("no-skip", "保留无人声"), ("skip", "跳过无人声")):
        entry = next((e for e in timings.get("modes", []) if e["mode"] == mode), None)
        if mode in means:
            timing_labels.append(f"{label}均值：{means[mode]:.2f} s")
        elif entry and entry["status"] == "completed":
            timing_labels.append(f"{label}：{entry['wall_seconds']:.2f} s")
    summary = {
        "source": Path(meta["video"]).name,
        "removed_seconds": full["silence"]["removed_samples"] / meta["sample_rate"],
        "source_seconds": meta["full_audio_seconds"],
        "padding_seconds": full["silence"]["parameters"]["padding_seconds"],
        "minimum_seconds": full["silence"]["parameters"]["min_removed_seconds"],
        "timings": timing_labels,
        "sample_start_seconds": meta["sample_start_seconds"],
        "sample_end_seconds": meta["sample_start_seconds"] + meta["sample_audio_seconds"],
    }
    markup = template.replace("__SOURCE__", html.escape(summary["source"]))
    markup = markup.replace("__DATA__", json.dumps(entries, ensure_ascii=True).replace("</", "<\\/"))
    markup = markup.replace("__SUMMARY__", json.dumps(summary, ensure_ascii=True).replace("</", "<\\/"))
    (output / "index.html").write_text(markup, encoding="utf-8")
    print(f"Review: {output / 'index.html'}; {len(entries)} removed intervals", flush=True)
    return entries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--context-seconds", type=float, default=2)
    args = parser.parse_args()
    if args.context_seconds <= 0:
        parser.error("Context must be positive")
    build_review(args.report, args.context_seconds)


if __name__ == "__main__":
    main()
