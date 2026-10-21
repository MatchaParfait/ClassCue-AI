"""Optional, append-only transcript notifications; ASR scheduling stays unchanged."""

from contextlib import contextmanager
from contextvars import ContextVar
import logging
from core import TranscriptSegment


_LISTENER = ContextVar("transcript_listener", default=None)
_OBSERVER = ContextVar("transcript_observer", default=None)


@contextmanager
def transcript_observer(callback):
    """A best-effort UI tap, independent of the analysis pipeline's listener."""
    token = _OBSERVER.set(callback)
    try:
        yield
    finally:
        _OBSERVER.reset(token)


@contextmanager
def transcript_listener(callback):
    token = _LISTENER.set(callback)
    try:
        yield
    finally:
        _LISTENER.reset(token)


def transcript_requested():
    return _LISTENER.get() is not None or _OBSERVER.get() is not None


def transcript_preview_requested():
    return _OBSERVER.get() is not None


def emit_transcript_preview(segments, settled_until_ms=0):
    """UI-only text; unaligned previews must never reach the extraction listener."""
    observer = _OBSERVER.get()
    if observer is not None:
        try:
            observer(list(segments), settled_until_ms)
        except Exception:
            logging.getLogger(__name__).warning("Live transcript preview unavailable")


def emit_transcript(segments, settled_until_ms):
    segments = list(segments)
    emit_transcript_preview(segments, settled_until_ms)
    listener = _LISTENER.get()
    if listener is not None:
        listener(list(segments), settled_until_ms)


def receive_transcript(event):
    emit_transcript(
        [TranscriptSegment(**item) for item in event["segments"]],
        event["settled_until_ms"],
    )


def worker_transcript_callback(request, emit):
    if not request.get("stream_transcript"):
        return None
    from dataclasses import asdict

    def publish(segments, settled_until_ms):
        emit({
            "event": "transcript", "segments": [asdict(segment) for segment in segments],
            "settled_until_ms": settled_until_ms,
        })
    return publish


def worker_preview_callback(request, emit):
    if not request.get("stream_preview"):
        return None
    from dataclasses import asdict

    def publish(segments):
        emit({"event": "transcript_preview", "segments": [asdict(segment) for segment in segments]})
    return publish
