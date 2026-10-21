"""Native-resolution, on-demand covers using the existing PyAV decoder."""

from __future__ import annotations

import hashlib
import json
from fractions import Fraction
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import BoundedSemaphore

from filelock import FileLock


_DECODERS = BoundedSemaphore(2)
FRAME_NUMBER = 24
THUMBNAIL_VERSION = "native-jpeg444-q2-v2"
PREVIEW_VERSION = "seek-jpeg444-960-v1"


class ThumbnailUnavailable(ValueError):
    pass


def _source_identity(source: Path) -> tuple:
    stat = source.stat()
    return (str(source.resolve()), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _encode_frame(source: Path) -> bytes:
    import av

    with av.open(str(source)) as container:
        if not container.streams.video:
            raise ThumbnailUnavailable("文件没有视频轨道")
        for number, frame in enumerate(container.decode(video=0), start=1):
            if number != FRAME_NUMBER:
                continue
            # Keep native pixels and full chroma detail for small course text.
            image = frame.reformat(format="yuvj444p")
            image.pts = 0
            image.time_base = Fraction(1, 25)
            encoder = av.CodecContext.create("mjpeg", "w")
            encoder.width, encoder.height = frame.width, frame.height
            encoder.pix_fmt = "yuvj444p"
            encoder.qmin = encoder.qmax = 2
            encoder.time_base = Fraction(1, 25)
            return b"".join(bytes(packet) for packet in [*encoder.encode(image), *encoder.encode(None)])
    raise ThumbnailUnavailable("视频不足 24 帧")


def _thumbnail_path(source: Path, cache_directory: Path) -> tuple[Path, tuple]:
    source = source.expanduser()
    identity = _source_identity(source)
    key = hashlib.sha256(json.dumps([THUMBNAIL_VERSION, FRAME_NUMBER, *identity]).encode("utf-8")).hexdigest()
    return cache_directory / f"{key}.jpg", identity


def cached_video_thumbnail(source: Path, cache_directory: Path) -> Path | None:
    """Look up an existing cover without importing a decoder or generating it."""
    output, _identity = _thumbnail_path(source, cache_directory)
    return output if output.is_file() else None


def video_thumbnail(source: Path, cache_directory: Path) -> Path:
    """Cache frame 24 atomically; never modify the source video."""
    import av

    source = source.expanduser()
    output, identity = _thumbnail_path(source, cache_directory)
    cache_directory.mkdir(parents=True, exist_ok=True)
    if output.is_file():
        return output
    temporary = None
    # A per-file lock prevents duplicate work; the semaphore bounds decoding
    # across different cards without serializing cache hits.
    with FileLock(str(output.with_suffix(".lock")), timeout=60):
        if output.is_file():
            return output
        try:
            with _DECODERS:
                data = _encode_frame(source)
            if not data or _source_identity(source) != identity:
                raise ThumbnailUnavailable("截图时视频文件已变化")
            with NamedTemporaryFile(dir=cache_directory, suffix=".tmp", delete=False) as file:
                temporary = Path(file.name)
                file.write(data)
            temporary.replace(output)
            return output
        except av.error.FFmpegError as exc:
            raise ThumbnailUnavailable("无法读取视频截图") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def _encode_preview(source: Path, time_ms: int) -> bytes:
    """Seek from the preceding keyframe and retain the frame at the requested time."""
    import av

    with av.open(str(source)) as container:
        if not container.streams.video:
            raise ThumbnailUnavailable("文件没有视频轨道")
        stream = container.streams.video[0]
        origin = stream.start_time or 0
        target = origin + int(Fraction(time_ms, 1000) / stream.time_base)
        if stream.duration is not None:
            target = min(target, origin + max(0, stream.duration - 1))
        container.seek(target, stream=stream, backward=True)
        chosen = None
        for frame in container.decode(stream):
            chosen = frame
            if frame.pts is not None and frame.pts >= target:
                break
        if chosen is None:
            raise ThumbnailUnavailable("该时间点没有可读取的视频画面")
        scale = min(1, 960 / max(chosen.width, chosen.height))
        image = chosen.reformat(width=max(1, round(chosen.width * scale)),
                                height=max(1, round(chosen.height * scale)), format="yuvj444p")
        image.pts = 0
        image.time_base = Fraction(1, 25)
        encoder = av.CodecContext.create("mjpeg", "w")
        encoder.width, encoder.height = image.width, image.height
        encoder.pix_fmt = "yuvj444p"
        encoder.qmin = encoder.qmax = 2
        encoder.time_base = Fraction(1, 25)
        return b"".join(bytes(packet) for packet in [*encoder.encode(image), *encoder.encode(None)])


def video_preview_frame(source: Path, cache_directory: Path, time_ms: int) -> Path:
    """Cache a requested still without audio playback or changes to the original video."""
    import av

    if time_ms < 0:
        raise ValueError("预览时间不能为负数")
    source = source.expanduser()
    identity = _source_identity(source)
    key = hashlib.sha256(json.dumps([PREVIEW_VERSION, time_ms, *identity]).encode("utf-8")).hexdigest()
    cache_directory.mkdir(parents=True, exist_ok=True)
    output = cache_directory / f"{key}.jpg"
    if output.is_file():
        return output
    temporary = None
    with FileLock(str(output.with_suffix(".lock")), timeout=60):
        if output.is_file():
            return output
        try:
            with _DECODERS:
                data = _encode_preview(source, time_ms)
            if not data or _source_identity(source) != identity:
                raise ThumbnailUnavailable("截图时视频文件已变化")
            with NamedTemporaryFile(dir=cache_directory, suffix=".tmp", delete=False) as file:
                temporary = Path(file.name)
                file.write(data)
            temporary.replace(output)
            return output
        except av.error.FFmpegError as exc:
            raise ThumbnailUnavailable("无法读取该时间点的视频画面") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
