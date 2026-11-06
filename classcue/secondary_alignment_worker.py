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
    Matcher, anchors, search_ranges, MAX_SEGMENTS, MAX_CALLS, MAX_ASR_SECONDS,
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

    def snapshot():
        value = {**result, "segments_checked": count, "asr_calls": calls,
                 "asr_seconds": round(asr_seconds, 3), "decoded_seconds": round(decoded, 3)}
        checkpoint(value)
        return value

    snapshot()
    with asr_session():
        for begin, finish in search_ranges(primary, request["original_offset_ms"], request["duration_ms"]):
            cursor = begin
            while cursor < finish:
                if should_stop():
                    raise ProcessingCancelled("已停止自动对齐")
                if count >= MAX_SEGMENTS or calls >= MAX_CALLS or asr_seconds >= MAX_ASR_SECONDS:
                    return snapshot()
                remaining = MAX_DECODE_SECONDS - decoded
                if remaining <= 0:
                    return snapshot()
                stop = min(cursor + SCAN_SECONDS, finish)
                report("finding_speech", f"正在寻找语音，已检查 {count}/10 段")
                audio, charged, exhausted = read_audio(
                    request["secondary_path"], cursor, stop, decoded_budget=remaining,
                    should_stop=should_stop,
                )
                decoded += charged
                if exhausted:
                    return snapshot()
                for speech in detect(audio):
                    start_sample, end_sample = int(speech["start"]), int(speech["end"])
                    start_sample = max(0, start_sample)
                    end_sample = min(len(audio), end_sample)
                    while start_sample < end_sample:
                        if should_stop():
                            raise ProcessingCancelled("已停止自动对齐")
                        if count >= MAX_SEGMENTS or calls >= MAX_CALLS or asr_seconds >= MAX_ASR_SECONDS:
                            return snapshot()
                        last = min(end_sample, start_sample + WINDOW_SECONDS * 16000,
                                   start_sample + round((MAX_ASR_SECONDS - asr_seconds) * 16000))
                        if last <= start_sample:
                            return snapshot()
                        clip = audio[start_sample:last]
                        path = Path(directory) / "window.wav"
                        with wave.open(str(path), "wb") as output:
                            output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                            output.writeframes((np.clip(clip, -1, 1) * 32767).astype("<i2").tobytes())
                        calls += 1
                        duration = len(clip) / 16000
                        asr_seconds += duration
                        report("recognizing", f"正在识别语音，已检查 {count}/10 段")
                        try:
                            output = recognize(path)
                        finally:
                            path.unlink(missing_ok=True)
                        shift_ms = round((cursor + start_sample / 16000) * 1000)
                        for segment in output[:MAX_SEGMENTS - count]:
                            count += 1
                            if not 0 <= segment.start_ms < segment.end_ms <= round(duration * 1000) + 100:
                                continue
                            words = [{**word, "start": word["start"] + shift_ms / 1000,
                                      "end": word["end"] + shift_ms / 1000}
                                     for word in segment.words or []
                                     if isinstance(word.get("start"), (int, float))
                                     and isinstance(word.get("end"), (int, float))]
                            recognized.append(replace(segment, id=count,
                                                      start_ms=segment.start_ms + shift_ms,
                                                      end_ms=segment.end_ms + shift_ms, words=words))
                            windows[count] = calls
                        report("matching", f"正在匹配字幕，已检查 {count}/10 段")
                        result = matcher.evaluate(recognized, windows)
                        saved = snapshot()
                        if result["outcome"] == "ready":
                            return saved
                        start_sample = last
                cursor = stop
    return snapshot()


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
