"""Video probing and audio extraction using PyAV; no ffmpeg executable required."""

from __future__ import annotations

import wave
from pathlib import Path


def video_duration_ms(video_path: str | Path) -> int:
    import av

    with av.open(str(video_path)) as container:
        if container.duration is not None:
            return int(container.duration / av.time_base * 1000)
        video_stream = next(iter(container.streams.video), None)
        if video_stream is None:
            raise ValueError("文件中没有可读取的视频轨道")
        if video_stream.duration is not None and video_stream.time_base is not None:
            return int(float(video_stream.duration * video_stream.time_base) * 1000)
    return 0


def extract_audio(video_path: str | Path, audio_path: str | Path, *, should_stop=None) -> int:
    """Extract mono 16 kHz PCM audio and return the source duration in ms."""
    import av

    source_duration = video_duration_ms(video_path)
    output = Path(audio_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with av.open(str(video_path)) as container:
        audio_stream = next(iter(container.streams.audio), None)
        if audio_stream is None:
            raise ValueError("视频没有音频轨道")
        resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=16000)
        with wave.open(str(output), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(16000)
            # PyAV's audio selector is an index within audio streams, not the
            # stream's global index (often 1 when the video stream is index 0).
            for frame in container.decode(audio=0):
                if should_stop and should_stop():
                    from classcue.processing_control import ProcessingCancelled
                    raise ProcessingCancelled("用户已请求停止处理")
                converted = resampler.resample(frame)
                frames = converted if isinstance(converted, list) else [converted]
                for resampled in frames:
                    wav_file.writeframes(resampled.to_ndarray().tobytes())
    return source_duration


def read_audio_window(video_path, start: float, end: float, *, decoded_budget: float, should_stop=None):
    """Read a bounded mono window, preserving media time and audio-track gaps."""
    import av
    import numpy as np
    from classcue.processing_control import ProcessingCancelled

    samples = np.zeros(max(0, round((end - start) * 16000)), dtype=np.float32)
    charged = 0.
    exhausted = False
    with av.open(str(video_path)) as container:
        stream = next(iter(container.streams.audio), None)
        if stream is None:
            raise ValueError("副视频没有可读取的音轨")
        origin = float(container.start_time or 0) / av.time_base
        container.seek(max(0, int((start + origin) / stream.time_base)), stream=stream, backward=True)
        resampler = av.AudioResampler(format="fltp", layout="mono", rate=16000)
        for frame in container.decode(stream):
            if should_stop and should_stop():
                raise ProcessingCancelled("已停止自动对齐")
            charged += frame.samples / frame.sample_rate
            if charged > decoded_budget:
                exhausted = True
                break
            if frame.pts is None:
                raise ValueError("副视频音轨缺少可靠时间戳")
            if float(frame.pts * frame.time_base) - origin >= end:
                break
            for converted in resampler.resample(frame):
                if converted.pts is None:
                    raise ValueError("副视频音轨缺少可靠时间戳")
                absolute = float(converted.pts * converted.time_base) - origin
                left = round((absolute - start) * 16000)
                data = converted.to_ndarray().reshape(-1)
                source = max(0, -left)
                target = max(0, left)
                count = min(len(data) - source, len(samples) - target)
                if count > 0:
                    samples[target:target + count] = data[source:source + count]
    return samples, charged, exhausted
