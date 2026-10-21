"""Best-effort, job-scoped previews of validated chunks, never published markers."""

from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import logging
from threading import RLock


_OBSERVER = ContextVar("analysis_observer", default=None)


@contextmanager
def analysis_observer(callback):
    token = _OBSERVER.set(callback)
    try:
        yield
    finally:
        _OBSERVER.reset(token)


def emit_analysis(video_id, mode, *, completed, total, current, results):
    observer = _OBSERVER.get()
    if observer is not None:
        try:
            observer(video_id, mode, completed=completed, total=total, current=current, results=results)
        except Exception:
            logging.getLogger(__name__).warning("Live analysis preview unavailable")


class LiveAnalysisBuffer:
    def __init__(self, max_jobs=8, max_markers=1000, max_chars=200000):
        self.jobs = OrderedDict()
        self.lock = RLock()
        self.max_jobs, self.max_markers, self.max_chars = max_jobs, max_markers, max_chars

    def publish(self, job_id, video_id, mode, *, completed, total, current, results):
        with self.lock:
            item = self.jobs.setdefault(job_id, {
                "video_id": video_id, "revision": 0, "active_mode": mode, "modes": {},
            })
            if item["video_id"] != video_id:
                return
            previous = item["modes"].get(mode, {})
            ids = {(m["start_ms"], m["title"]): m["id"] for m in previous.get("markers", [])}
            next_id = min([0, *(m["id"] for state in item["modes"].values() for m in state["markers"])]) - 1
            markers, size = [], 0
            for result in results:
                size += sum(len(result.get(field, "")) for field in ("title", "reason", "source_quote"))
                if len(markers) >= self.max_markers or size > self.max_chars:
                    break
                key = (result["start_ms"], result["title"])
                marker_id = ids.get(key)
                if marker_id is None:
                    marker_id, next_id = next_id, next_id - 1
                markers.append({
                    "id": marker_id, "video_id": video_id, "analysis_mode": mode,
                    "start_ms": result["start_ms"], "end_ms": result["end_ms"],
                    "title": result["title"], "source_quote": result["source_quote"],
                    "note": result.get("reason", ""), "review_status": "active", "preview": True,
                })
            item["modes"][mode] = {
                "completed_chunks": completed, "total_chunks": total, "current_chunk": current,
                "markers": markers, "candidate_count": len(results), "truncated": len(markers) < len(results),
            }
            item.update(revision=item["revision"] + 1, active_mode=mode)
            self.jobs.move_to_end(job_id)
            while len(self.jobs) > self.max_jobs:
                self.jobs.popitem(last=False)

    def snapshot(self, job_id, video_id):
        with self.lock:
            item = self.jobs.get(job_id)
            if item is None or item["video_id"] != video_id:
                return {"video_id": video_id, "revision": 0, "active_mode": None, "modes": {}}
            return deepcopy(item)
