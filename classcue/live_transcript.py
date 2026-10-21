"""Bounded, transient UI previews; never replaces durable transcripts."""

from collections import OrderedDict
from threading import RLock


class LiveTranscriptBuffer:
    def __init__(self, max_jobs=8, max_chars=12000, max_segments=240):
        self.jobs = OrderedDict()
        self.lock = RLock()
        self.max_jobs = max_jobs
        self.max_chars = max_chars
        self.max_segments = max_segments

    def publish(self, job_id, video_id, segments, settled_until_ms):
        with self.lock:
            item = self.jobs.setdefault(job_id, {
                "video_id": video_id, "revision": 0, "settled_until_ms": 0, "subtitles": [],
            })
            indexed = {row["id"]: row for row in item["subtitles"]}
            for segment in segments:
                if not segment.text.strip():
                    continue
                indexed[segment.id] = {
                    "id": segment.id, "start_ms": segment.start_ms,
                    "end_ms": segment.end_ms, "text": segment.text[:self.max_chars],
                }
            rows = sorted(indexed.values(), key=lambda row: (row["start_ms"], row["id"]))[-self.max_segments:]
            size = sum(len(row["text"]) for row in rows)
            while len(rows) > 1 and size > self.max_chars:
                size -= len(rows.pop(0)["text"])
            item.update(subtitles=rows, revision=item["revision"] + 1,
                        settled_until_ms=max(item["settled_until_ms"], settled_until_ms))
            self.jobs.move_to_end(job_id)
            while len(self.jobs) > self.max_jobs:
                self.jobs.popitem(last=False)

    def snapshot(self, job_id, video_id):
        with self.lock:
            item = self.jobs.get(job_id)
            if not item or item["video_id"] != video_id:
                return {"video_id": video_id, "revision": 0, "settled_until_ms": 0, "subtitles": []}
            return {**item, "subtitles": [dict(row) for row in item["subtitles"]]}
