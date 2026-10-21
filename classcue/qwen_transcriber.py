"""Qwen word alignment to the application's existing subtitle data contract."""

from __future__ import annotations

from contextlib import contextmanager
from functools import partial
import math
import os
import gc
import sys
from pathlib import Path
import re
from typing import Callable
import unicodedata

from classcue.core import TranscriptSegment


def aligned_segments(
    text: str,
    items: list,
    offset_seconds: float,
    duration_seconds: float,
    first_id: int = 1,
) -> list[TranscriptSegment]:
    """Keep original punctuation while grouping aligned characters into readable cues."""
    if not text.strip():
        return []
    if not items:
        raise ValueError("Qwen 返回了文本但没有时间对齐结果，不能生成可靠字幕。")
    # The aligner drops internal punctuation (e.g. "1,234" -> "1234").
    # Map its normalized tokens back to the original text instead of losing punctuation.
    text_positions = [
        index for index, character in enumerate(text)
        if character == "'" or unicodedata.category(character)[0] in {"L", "N"}
    ]
    normalized = "".join(text[index] for index in text_positions)
    matched = []
    cursor = 0
    previous_start = 0.0
    for item in items:
        token = item.text.strip()
        start, end = float(item.start_time), float(item.end_time)
        if not token or not math.isfinite(start) or not math.isfinite(end):
            raise ValueError("Qwen 时间对齐包含无效字词或时间。")
        if start < -0.05 or end < start or start < previous_start - 0.05 or end > duration_seconds + 0.25:
            raise ValueError("Qwen 时间对齐超出音频范围或顺序错误。")
        if not normalized.startswith(token, cursor):
            raise ValueError("Qwen 时间对齐字词与转写文本不一致。")
        position = text_positions[cursor]
        matched.append((position, token, min(duration_seconds, max(0.0, start)),
                        min(duration_seconds, max(0.0, end))))
        cursor += len(token)
        previous_start = start
    if cursor != len(normalized):
        raise ValueError("Qwen 时间对齐未覆盖完整转写文本。")

    result: list[TranscriptSegment] = []
    fragments: list[str] = []
    words: list[dict] = []
    cue_start = cue_end = 0.0

    def finish() -> None:
        nonlocal fragments, words
        if not words:
            return
        start_ms = round((offset_seconds + cue_start) * 1000)
        end_ms = max(start_ms + 1, round((offset_seconds + cue_end) * 1000))
        result.append(TranscriptSegment(
            first_id + len(result), start_ms, end_ms, "".join(fragments).strip(), words
        ))
        fragments, words = [], []

    for index, (position, token, start, end) in enumerate(matched):
        next_position = matched[index + 1][0] if index + 1 < len(matched) else len(text)
        fragment = text[0 if index == 0 else position:next_position]
        if words and (start - cue_end >= 1.2 or end - cue_start > 15 or sum(map(len, fragments)) >= 80):
            finish()
        if not words:
            cue_start = start
        cue_end = max(cue_end if words else end, end)
        fragments.append(fragment)
        words.append({"word": token, "start": round(offset_seconds + start, 3),
                      "end": round(offset_seconds + end, 3), "probability": None})
        if re.search(r"[。！？.!?；;\n]\s*$", fragment):
            finish()
    finish()
    return result


def decode_audio(path: str | Path):
    """Decode to mono 16 kHz with PyAV, preserving silence on the original timeline."""
    import av
    import numpy as np

    arrays = []
    written = 0
    with av.open(str(path)) as container:
        if not container.streams.audio:
            raise ValueError("文件没有音频轨道。")
        origin = (container.start_time or 0) / av.time_base
        resampler = av.AudioResampler(format="fltp", layout="mono", rate=16000)

        def append_frame(converted) -> None:
            nonlocal written
            samples = converted.to_ndarray().reshape(-1)
            position = written
            if converted.pts is not None and converted.time_base is not None:
                position = round((float(converted.pts * converted.time_base) - origin) * 16000)
            if position > written:
                arrays.append(np.zeros(position - written, dtype=np.float32))
                written = position
            if position < written:
                samples = samples[written - position:]
            if len(samples):
                arrays.append(samples)
                written += len(samples)

        for frame in container.decode(audio=0):
            for converted in resampler.resample(frame):
                append_frame(converted)
        for converted in resampler.resample(None):
            append_frame(converted)
    if not arrays:
        raise ValueError("音频轨道为空。")
    return np.concatenate(arrays).astype(np.float32, copy=False)


def stream_decode_audio_to_wav(
    path: str | Path,
    output_path: str | Path,
    progress: Callable[[float, str], None] | None = None,
    *,
    should_stop: Callable[[], bool] | None = None,
) -> int:
    """Stream-decoder for FireRed: write the same timeline as a 16-bit WAV.

    Unlike ``decode_audio`` this never retains the complete resampled signal in
    a NumPy array. PTS gaps are written as bounded zero chunks so long videos
    do not create a second full-size buffer before VAD starts.
    """
    import av
    import numpy as np
    import wave
    import logging
    import math
    from classcue.processing_control import ProcessingCancelled

    def check_stop():
        if should_stop and should_stop():
            raise ProcessingCancelled("用户已请求停止处理")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    emitted = False
    last_reported = -1.0
    corrupt_packets = 0
    corrupt_seconds = 0.0
    consecutive_seconds = 0.0
    damaged_until = None
    last_frame_end = None
    with av.open(str(path)) as container:
        if not container.streams.audio:
            raise ValueError("文件没有音频轨道。")
        origin = (container.start_time or 0) / av.time_base
        source_duration = (getattr(container, "duration", None) or 0) / av.time_base
        resampler = av.AudioResampler(format="fltp", layout="mono", rate=16000)
        with wave.open(str(output_path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)

            def write_silence(count: int) -> None:
                chunk_samples = 4 * 16000
                zero_chunk = np.zeros(chunk_samples, dtype="<i2").tobytes()
                remaining = count
                while remaining:
                    check_stop()
                    size = min(remaining, chunk_samples)
                    output.writeframes(zero_chunk[:size * 2])
                    remaining -= size

            def append_frame(converted) -> None:
                nonlocal written, emitted, last_reported
                samples = converted.to_ndarray().reshape(-1).astype(np.float32, copy=False)
                position = written
                if converted.pts is not None and converted.time_base is not None:
                    position = round(
                        (float(converted.pts * converted.time_base) - origin) * 16000
                    )
                if position > written:
                    write_silence(position - written)
                    written = position
                if position < written:
                    samples = samples[written - position:]
                if len(samples):
                    pcm = np.clip(np.rint(samples * 32768), -32768, 32767).astype("<i2")
                    output.writeframes(pcm.tobytes())
                    written += len(samples)
                    emitted = True

                if progress and source_duration > 0 and converted.pts is not None:
                    current = max(0.0, float(converted.pts * converted.time_base) - origin)
                    fraction = min(1.0, current / source_duration)
                    if fraction - last_reported >= 0.02:
                        last_reported = fraction
                        progress(0.02 + fraction * 0.02, "正在流式准备音频")

            # Decode packets separately: container.decode() abandons its
            # generator on a single corrupt AAC packet, losing the entire
            # remaining lesson. Only small, timestamped losses are recoverable.
            for packet in container.demux(audio=0):
                check_stop()
                try:
                    frames = packet.decode()
                except av.error.InvalidDataError as exc:
                    if packet.pts is None or packet.time_base is None or not packet.duration:
                        raise ValueError("音频数据损坏且缺少时间戳，无法保留原视频时间轴。") from exc
                    start = float(packet.pts * packet.time_base) - origin
                    duration = float(packet.duration * packet.time_base)
                    corrupt_packets += 1
                    corrupt_seconds += duration
                    consecutive_seconds += duration
                    if (not math.isfinite(start) or not math.isfinite(duration)
                            or duration <= 0 or consecutive_seconds > .25
                            or corrupt_seconds > 1 or corrupt_packets > 50
                            or (source_duration > 0 and corrupt_seconds > source_duration * .01)):
                        raise ValueError("音频损坏范围过大，无法可靠转写，请重新导出或更换视频。") from exc
                    damaged_until = max(damaged_until or 0, start + duration)
                    # Drain preceding samples before resetting. Keeping the
                    # original PTS leaves silence at the damaged packet instead
                    # of shifting every later subtitle earlier.
                    for converted in resampler.resample(None):
                        append_frame(converted)
                    resampler = av.AudioResampler(format="fltp", layout="mono", rate=16000)
                    continue
                for frame in frames:
                    check_stop()
                    timestamp = (float(frame.pts * frame.time_base) - origin
                                 if frame.pts is not None and frame.time_base is not None else None)
                    if damaged_until is not None:
                        gap = timestamp - (last_frame_end or 0) if timestamp is not None else None
                        if gap is None or not math.isfinite(gap) or gap > .25 + 1 / 16000:
                            raise ValueError("损坏音频之后的时间轴不完整，无法可靠恢复字幕定位。")
                        damaged_until = None
                        consecutive_seconds = 0
                    if timestamp is not None:
                        last_frame_end = timestamp + frame.samples / frame.sample_rate
                    for converted in resampler.resample(frame):
                        append_frame(converted)
            for converted in resampler.resample(None):
                append_frame(converted)
            if damaged_until is not None and emitted:
                missing = max(0, round(damaged_until * 16000) - written)
                if missing > round(.25 * 16000):
                    raise ValueError("音频结尾损坏范围过大，无法可靠恢复。")
                write_silence(missing)
                written += missing
    if not emitted:
        raise ValueError("音频轨道为空。")
    if corrupt_packets:
        logging.getLogger(__name__).warning(
            "Recovered %d corrupt audio packets (%.3fs); original PTS preserved",
            corrupt_packets, corrupt_seconds,
        )
        if progress:
            progress(.04, f"已恢复 {corrupt_packets} 处局部音频损坏，保留原始时间轴")
    return round(written / 16000 * 1000)


@contextmanager
def _checkpoint_reader():
    if os.name != "nt":
        yield
        return
    from safetensors import safe_open
    from transformers import modeling_utils

    # Avoid the native crash observed with memory-mapped Windows checkpoint loading.
    previous = modeling_utils.safe_open
    modeling_utils.safe_open = partial(safe_open, backend="pread")
    try:
        yield
    finally:
        modeling_utils.safe_open = previous


def load_qwen_model(recognizer_path: str, aligner_path: str, *, load_aligner=True):
    os.environ.setdefault("ONEDNN_PRIMITIVE_CACHE_CAPACITY", "0")
    import torch
    from qwen_asr import Qwen3ASRModel

    cpu_threads = int(os.environ.get("QWEN_CPU_THREADS", "4"))
    if cpu_threads < 1:
        raise ValueError("QWEN_CPU_THREADS must be positive")
    torch.set_num_threads(cpu_threads)
    try:
        torch.set_num_interop_threads(1)
    except RuntimeError:
        # Inter-op configuration can only be set before parallel work starts.
        pass
    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    # An 8 GB GPU has room for ASR; keep the aligner on CPU to avoid contention.
    aligner_device = device
    max_inference_batch_size = 1
    if device != "cpu" and torch.cuda.get_device_properties(0).total_memory < 12 * 1024**3:
        aligner_device = "cpu"
    elif device != "cpu":
        max_inference_batch_size = 2
    with _checkpoint_reader():
        return Qwen3ASRModel.from_pretrained(
            recognizer_path,
            dtype=torch.float16 if device != "cpu" else torch.float32,
            device_map=device,
            attn_implementation="sdpa",
            local_files_only=True,
            max_inference_batch_size=max_inference_batch_size,
            max_new_tokens=1024,
            forced_aligner=aligner_path if load_aligner else None,
            forced_aligner_kwargs={
                "dtype": torch.float16 if aligner_device != "cpu" else torch.float32,
                "device_map": aligner_device,
                "attn_implementation": "sdpa",
                "local_files_only": True,
            },
        )


def transcribe_qwen(
    model,
    audio_path: str | Path,
    progress: Callable[[float, str], None] | None = None,
    *,
    audio_profile: str = "general",
    skip_non_speech: bool | None = None,
    chunk_seconds: float = 90,
    batch_size: int = 2,
    speech_padding_seconds: float = .3,
    min_removed_seconds: float = 1.5,
    silence_report: dict | None = None,
) -> tuple[list[TranscriptSegment], int]:
    from classcue.qwen_silence import resolve_silence_skipping

    skip_non_speech = resolve_silence_skipping(
        audio_profile, skip_non_speech=skip_non_speech,
    )
    if silence_report is not None:
        silence_report["routing"] = {
            "audio_profile": audio_profile, "skip_non_speech": skip_non_speech,
        }
    audio = decode_audio(audio_path)
    duration = len(audio) / 16000
    if skip_non_speech:
        return _transcribe_with_silence_skipping(
            model, audio, progress, chunk_seconds, batch_size,
            speech_padding_seconds, min_removed_seconds, silence_report,
        )
    if progress:
        progress(0.02, "Qwen 官方长音频转写与时间对齐")

    # Let Qwen's official wrapper split long input, batch ASR/alignment, and
    # merge timestamps back onto the original audio timeline.
    outputs = model.transcribe(
        audio=(audio, 16000),
        language="Chinese",
        return_time_stamps=True,
    )
    if len(outputs) != 1:
        raise ValueError("Qwen 返回了非预期数量的转写结果。")

    output = outputs[0]
    alignment = output.time_stamps
    items = list(alignment.items) if alignment is not None else []
    result = aligned_segments(output.text, items, 0.0, duration, 1)
    if progress:
        progress(0.98, f"Qwen 官方长音频转写完成，共 {len(result)} 段")
    return result, round(duration * 1000)


def _transcribe_with_silence_skipping(
    model, audio, progress, chunk_seconds, batch_size, padding, minimum, report,
):
    import tempfile
    import wave
    import numpy as np
    from classcue.qwen_boundaries import plan_spans
    from classcue.qwen_silence import build_silence_plan, compact_audio, restore_segments
    from classcue.qwen_vad import detect_speech

    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if progress:
        progress(.02, "FireRedVAD 检测人声与无人声区域")
    with tempfile.TemporaryDirectory(prefix="qwen-silence-") as temporary:
        wav_path = Path(temporary) / "timeline.wav"
        with wave.open(str(wav_path), "wb") as target:
            target.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            target.writeframes((np.clip(audio, -1, 32767 / 32768) * 32768).astype("<i2").tobytes())
        vad = detect_speech(wav_path, "firered")
    spans = plan_spans(audio, 16000, chunk_seconds, "firered", vad["speech_intervals"])
    plan = build_silence_plan(
        spans, len(audio), 16000, vad["speech_intervals"], enabled=True,
        padding_seconds=padding, min_removed_seconds=minimum,
    )
    warnings, segments = [], []
    if report is not None:
        report.update({"vad": vad, "silence": plan, "mapping_warnings": warnings})
    chunks = [c for c in plan["chunks"] if c["input_samples"]]
    previous = model.max_inference_batch_size
    model.max_inference_batch_size = batch_size
    try:
        for index in range(0, len(chunks), batch_size):
            from classcue.qwen_memory import ensure_memory_headroom
            ensure_memory_headroom()
            group = chunks[index:index + batch_size]
            outputs = model.transcribe(
                audio=[(compact_audio(audio, c), 16000) for c in group],
                language="Chinese", return_time_stamps=True,
            )
            if len(outputs) != len(group):
                raise ValueError("Unexpected Qwen batch output count")
            for chunk, output in zip(group, outputs):
                items = list(output.time_stamps.items) if output.time_stamps is not None else []
                cues = aligned_segments(output.text, items, 0, chunk["input_samples"] / 16000)
                for cue in restore_segments(cues, chunk, 16000, len(audio), warnings=warnings):
                    segments.append(TranscriptSegment(
                        len(segments) + 1, cue.start_ms, cue.end_ms, cue.text, cue.words,
                    ))
            if progress:
                progress(.1 + .88 * (index + len(group)) / max(1, len(chunks)),
                         f"Qwen 无人声跳过转写 {index + len(group)}/{len(chunks)}")
            del outputs, group
            _release_qwen_workspace()
    finally:
        model.max_inference_batch_size = previous
    return segments, round(len(audio) * 1000 / 16000)


def _release_qwen_workspace():
    gc.collect()
    torch = sys.modules.get("torch")
    if torch is not None and torch.cuda.is_available():
        torch.cuda.empty_cache()


def transcribe_qwen_batch(model, audio_paths, batch_size: int = 2, progress=None):
    """Batch independent timeline-preserving chunks through the official API."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    audios = [decode_audio(path) for path in audio_paths]
    if not audios:
        return []
    previous = model.max_inference_batch_size
    model.max_inference_batch_size = batch_size
    try:
        if progress:
            progress(.02, f"Qwen batch: {len(audios)} chunks, BS={batch_size}")
        outputs = model.transcribe(
            audio=[(audio, 16000) for audio in audios],
            language="Chinese", return_time_stamps=True,
        )
        if len(outputs) != len(audios):
            raise ValueError("Unexpected Qwen batch output count")
        results = []
        for audio, output in zip(audios, outputs):
            duration = len(audio) / 16000
            alignment = output.time_stamps
            items = list(alignment.items) if alignment is not None else []
            segments = aligned_segments(output.text, items, 0, duration, 1)
            results.append((segments, round(duration * 1000)))
        if progress:
            progress(1, "Qwen batch complete")
        return results
    finally:
        model.max_inference_batch_size = previous
        _release_qwen_workspace()
