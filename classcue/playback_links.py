"""Playback destinations shared by course results and desktop handoff."""

from urllib.parse import urlencode, urlsplit, urlunsplit


def playback_url(origin, video_id, start_ms, library_id, *, desktop=True):
    prefix = "/open/review" if desktop else "/review"
    return f"{origin.rstrip('/')}{prefix}/{video_id}?" + urlencode({
        "start_ms": start_ms, "library_id": library_id,
    })


def desktop_review_url(url):
    """Keep old review links usable while removing the browser handoff route."""
    parsed = urlsplit(url)
    if parsed.path.startswith("/open/review/"):
        parsed = parsed._replace(path=parsed.path.removeprefix("/open"))
    return urlunsplit(parsed)
