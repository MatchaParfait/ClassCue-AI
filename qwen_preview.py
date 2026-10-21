"""Real Qwen chunk text for display, never an aligned or durable transcript."""

from core import TranscriptSegment


def chunk_preview(chunk, text, rate):
    if not isinstance(text, str) or not text.strip():
        return None
    # Source sample offsets survive batch retries and splitting unfinished chunks.
    # Negative IDs keep preview rows separate from final aligned subtitle IDs.
    return TranscriptSegment(
        -(chunk["start_sample"] + 1),
        round(chunk["start_sample"] * 1000 / rate),
        round(chunk["end_sample"] * 1000 / rate), text,
    )


def publish_cached_preview(checkpoints, chunks, rate):
    from transcript_events import emit_transcript_preview, transcript_preview_requested
    if not transcript_preview_requested():
        return
    for chunk in chunks:
        segment = chunk_preview(chunk, checkpoints.load("asr", chunk), rate)
        if segment is not None:
            emit_transcript_preview([segment])
