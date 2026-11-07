"""One owned, offline ASR process for bounded secondary-view alignment."""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import time
import wave

from classcue.core import TranscriptSegment
from classcue.media import read_audio_window
from classcue.secondary_alignment import (
    Matcher, anchors, search_windows, FAST_SEGMENTS, FAST_CALLS, FAST_ASR_SECONDS,
    MAX_SEGMENTS, MAX_CALLS, MAX_ASR_SECONDS,
    MAX_DECODE_SECONDS, MAX_RUNTIME_SECONDS, SCAN_SECONDS, WINDOW_SECONDS,
)

PREFIX = "CLASSCUE_ALIGNMENT:"


def run_alignment(request, directory, *, read_audio=read_audio_window, recognize=None, detect=None,
                  checkpoint=lambda result: None, report=lambda stage, message: None,
                  should_stop=lambda: False):
    import numpy as np
    from classcue.processing_control import ProcessingCancelled
    from classcue.transcriber import asr_session, transcribe_audio

    if recognize is None:
        recognize = lambda path: transcribe_audio(
            path, model_id=request["model"]["id"], prepared_only=True,
            cache_directory=directory, should_stop=should_stop,
        )[0]
    if detect is None:
        from faster_whisper.vad import VadOptions, get_speech_timestamps
        detect = lambda audio: get_speech_timestamps(audio, VadOptions(
            min_speech_duration_ms=300, min_silence_duration_ms=500,
            max_speech_duration_s=11, speech_pad_ms=500,
        ))
    primary = [TranscriptSegment(**s) for s in request["segments"]]
    if not anchors(primary):
        raise ValueError("主字幕缺少可对齐的长句")
    matcher = Matcher(primary)
    recognized, windows = [], {}
    calls = count = 0
    asr_seconds = decoded = 0.
    result = matcher.evaluate([], {})
    phase = "fast"
    attempted: list[tuple[int, int]] = []
    deferred = []
    skipped_short = 0

    def inspect(segment, shift_ms, duration, window_id):
        nonlocal count
        count += 1
        if not 0 <= segment.start_ms < segment.end_ms <= round(duration * 1000) + 100:
            return
        words = [{**word, "start": word["start"] + shift_ms / 1000,
                  "end": word["end"] + shift_ms / 1000}
                 for word in segment.words or []
                 if isinstance(word.get("start"), (int, float))
                 and isinstance(word.get("end"), (int, float))]
        recognized.append(replace(segment, id=count, start_ms=segment.start_ms + shift_ms,
                                  end_ms=segment.end_ms + shift_ms, words=words))
        windows[count] = window_id

    def snapshot(reason=None):
        value = {**result, "segments_checked": count, "asr_calls": calls,
                 "asr_seconds": round(asr_seconds, 3), "decoded_seconds": round(decoded, 3),
                 "phase": phase, "model_id": request["model"]["id"],
                 "primary_model_id": request.get("primary_model_id", ""),
                 "skipped_short": skipped_short, "stop_reason": reason or "searching"}
        if reason and result["outcome"] != "ready":
            explanations = {
                "asr_budget": "已达到 15 分钟 ASR 音频上限",
                "call_budget": "已达到识别调用上限",
                "segment_budget": "已达到字幕检查上限",
                "decode_budget": "已达到音频扫描上限",
                "search_exhausted": "快速匹配及扩大搜索均已结束",
            }
            value["message"] = f"{result['message']}；{explanations.get(reason, reason)}"
        checkpoint(value)
        return value

    snapshot()
    with asr_session():
        for phase in ("fast", "fallback"):
            fallback = phase == "fallback"
            label = "扩大搜索" if fallback else "快速匹配"
            call_limit = MAX_CALLS if fallback else FAST_CALLS
            segment_limit = MAX_SEGMENTS if fallback else FAST_SEGMENTS
            audio_limit = MAX_ASR_SECONDS if fallback else FAST_ASR_SECONDS
            per_anchor_calls = 32 if fallback else 4
            per_anchor_audio = 300 if fallback else 48
            per_anchor_segments = 64 if fallback else 4
            anchor_calls, anchor_audio, anchor_segments = {}, {}, {}
            scanned: list[tuple[float, float]] = []
            report(phase, f"{label}：使用 {request['model']['id']}")
            if fallback:
                for owner, segment, shift_ms, duration, window_id in deferred:
                    if should_stop():
                        raise ProcessingCancelled("已停止自动对齐")
                    if count >= segment_limit:
                        break
                    if anchor_segments.get(owner, 0) >= per_anchor_segments:
                        continue
                    inspect(segment, shift_ms, duration, window_id)
                    anchor_segments[owner] = anchor_segments.get(owner, 0) + 1
                deferred.clear()
                result = matcher.evaluate(recognized, windows)
                if result["outcome"] == "ready":
                    return snapshot("matched")
            offset = result["offset_ms"] if result["outcome"] == "candidate" else request["original_offset_ms"]
            snapshot()
            for anchor_id, begin, finish in search_windows(
                    primary, offset, request["duration_ms"], fallback=fallback):
                if should_stop():
                    raise ProcessingCancelled("已停止自动对齐")
                if count >= segment_limit or calls >= call_limit or asr_seconds >= audio_limit:
                    break
                if (anchor_calls.get(anchor_id, 0) >= per_anchor_calls
                        or anchor_audio.get(anchor_id, 0) >= per_anchor_audio
                        or anchor_segments.get(anchor_id, 0) >= per_anchor_segments):
                    continue
                remaining = MAX_DECODE_SECONDS - decoded
                if remaining <= 0:
                    return snapshot("decode_budget")
                # Overlapping anchor searches must not repeatedly decode the same interval in a phase.
                pieces = [(begin, finish)]
                for used_start, used_end in scanned:
                    pieces = [(a, b) for start, end in pieces
                              for a, b in ((start, min(end, used_start)), (max(start, used_end), end))
                              if b > a]
                for cursor, stop in pieces:
                    if should_stop():
                        raise ProcessingCancelled("已停止自动对齐")
                    remaining = MAX_DECODE_SECONDS - decoded
                    if remaining <= 0:
                        return snapshot("decode_budget")
                    report(phase, f"{label}：寻找语音，累计 ASR {asr_seconds:.0f}/900 秒")
                    audio, charged, exhausted = read_audio(
                        request["secondary_path"], cursor, stop, decoded_budget=remaining,
                        should_stop=should_stop,
                    )
                    decoded += charged
                    scanned.append((cursor, stop))
                    if exhausted:
                        return snapshot("decode_budget")
                    speech_ranges = []
                    for speech in detect(audio):
                        start = max(0, int(speech["start"]))
                        end = min(len(audio), int(speech["end"]))
                        if end <= start:
                            continue
                        if fallback and speech_ranges and start - speech_ranges[-1][1] <= 2 * 16000:
                            previous_start, previous_end = speech_ranges[-1]
                            if end - previous_start <= WINDOW_SECONDS * 16000:
                                speech_ranges[-1] = (previous_start, max(previous_end, end))
                                continue
                        speech_ranges.append((start, end))
                    for start_sample, end_sample in speech_ranges:
                        if (end_sample - start_sample) / 16000 < (1 if fallback else 2.5):
                            skipped_short += 1
                            continue
                        shift = round(cursor * 16000)
                        pending = [(start_sample + shift, end_sample + shift)]
                        for used_start, used_end in attempted:
                            pending = [(a, b) for start, end in pending
                                       for a, b in ((start, min(end, used_start)), (max(start, used_end), end))
                                       if b > a]
                        for absolute_start, absolute_end in pending:
                            start_sample, end_sample = absolute_start - shift, absolute_end - shift
                            while start_sample < end_sample:
                                if should_stop():
                                    raise ProcessingCancelled("已停止自动对齐")
                                if count >= segment_limit or calls >= call_limit or asr_seconds >= audio_limit:
                                    break
                                used_audio = anchor_audio.get(anchor_id, 0)
                                if (anchor_calls.get(anchor_id, 0) >= per_anchor_calls
                                        or used_audio >= per_anchor_audio
                                        or anchor_segments.get(anchor_id, 0) >= per_anchor_segments):
                                    break
                                last = min(end_sample, start_sample + WINDOW_SECONDS * 16000,
                                           start_sample + int(min(audio_limit - asr_seconds,
                                                                  per_anchor_audio - used_audio) * 16000))
                                if last <= start_sample:
                                    break
                                clip = audio[start_sample:last]
                                path = Path(directory) / "window.wav"
                                with wave.open(str(path), "wb") as output:
                                    output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                                    output.writeframes((np.clip(clip, -1, 1) * 32767).astype("<i2").tobytes())
                                calls += 1
                                anchor_calls[anchor_id] = anchor_calls.get(anchor_id, 0) + 1
                                duration = len(clip) / 16000
                                asr_seconds += duration
                                anchor_audio[anchor_id] = used_audio + duration
                                attempted.append((start_sample + shift, last + shift))
                                report(phase, f"{label}：识别语音，累计 ASR {asr_seconds:.0f}/900 秒")
                                snapshot()
                                try:
                                    output = recognize(path)
                                finally:
                                    path.unlink(missing_ok=True)
                                shift_ms = round((cursor + start_sample / 16000) * 1000)
                                remaining_segments = min(segment_limit - count,
                                                         per_anchor_segments - anchor_segments.get(anchor_id, 0))
                                for segment in output[:remaining_segments]:
                                    inspect(segment, shift_ms, duration, calls)
                                    anchor_segments[anchor_id] = anchor_segments.get(anchor_id, 0) + 1
                                if not fallback:
                                    room = max(0, MAX_SEGMENTS - count - len(deferred))
                                    deferred.extend((anchor_id, segment, shift_ms, duration, calls)
                                                    for segment in output[remaining_segments:remaining_segments + room])
                                result = matcher.evaluate(recognized, windows)
                                snapshot()
                                if result["outcome"] == "ready":
                                    return snapshot("matched")
                                start_sample = last
    reason = ("asr_budget" if asr_seconds >= MAX_ASR_SECONDS else
              "call_budget" if calls >= MAX_CALLS else
              "segment_budget" if count >= MAX_SEGMENTS else "search_exhausted")
    return snapshot(reason)


def main():
    from classcue.processing_control import ProcessingCancelled
    request_path, result_path = map(Path, sys.argv[1:3])
    request = json.loads(request_path.read_text(encoding="utf-8"))
    started = time.monotonic()

    def save(result):
        temporary = result_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, result_path)

    def report(stage, message):
        print(PREFIX + json.dumps({"stage": stage, "message": message}), flush=True)

    try:
        result = run_alignment(
            request, request_path.parent, checkpoint=save, report=report,
            should_stop=lambda: time.monotonic() - started >= MAX_RUNTIME_SECONDS,
        )
        save(result)
    except ProcessingCancelled:
        if result_path.exists():
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if result.get("outcome") != "ready":
                result.update(stop_reason="runtime_limit",
                              message=f"{result['message']}；达到运行时限，已停止扩大搜索")
                save(result)
            return 0
        save({"outcome": "no_match", "offset_ms": None, "evidence": [],
              "message": "达到运行时限，原偏移已保留"})
        return 0
    except Exception:
        save({"error": "自动对齐识别失败，请检查副视频音轨和已准备的 ASR 环境"})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
