"""Application-owned native picker lifecycle without a global process registry."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock
import time
import uuid

from diagnostics import get_logger, log_exception


LOGGER = get_logger(__name__)


@dataclass
class _Picker:
    state: dict[str, object]
    created_at: float


class PickerManager:
    def __init__(
        self, *, start: Callable, read: Callable, discard: Callable,
        ttl_seconds: float = 15 * 60, clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._start = start
        self._read = read
        self._discard = discard
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = RLock()
        self._pickers: dict[str, _Picker] = {}
        self._closed = False

    def create(self, kind: str) -> dict[str, str]:
        with self._lock:
            if self._closed:
                raise RuntimeError("Path picker manager is shutting down")
            state = self._start(kind)
            picker_id = uuid.uuid4().hex
            self._pickers[picker_id] = _Picker(state, self._clock())
            return {"picker_id": picker_id, "status": "pending"}

    def get(self, picker_id: str) -> dict[str, object]:
        with self._lock:
            picker = self._pickers[picker_id]
            if self._clock() - picker.created_at >= self._ttl_seconds:
                self._remove(picker_id)
                return self._payload(picker_id, "cancelled")
            try:
                finished, selected = self._read(picker.state)
            except Exception as exc:
                log_exception(LOGGER, "Unable to poll path picker", exc=exc,
                              phase="picker_poll", picker_id=picker_id)
                self._remove(picker_id)
                return self._payload(picker_id, "cancelled")
            if not finished:
                return self._payload(picker_id, "pending")
            self._remove(picker_id)
            return self._payload(picker_id, "completed" if selected else "cancelled", selected)

    def cancel(self, picker_id: str) -> dict[str, object]:
        with self._lock:
            self._remove(picker_id)
            return self._payload(picker_id, "cancelled")

    def reap_expired(self) -> int:
        with self._lock:
            now = self._clock()
            expired = [picker_id for picker_id, picker in self._pickers.items()
                       if now - picker.created_at >= self._ttl_seconds]
            for picker_id in expired:
                self._remove(picker_id)
            return len(expired)

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            for picker_id in list(self._pickers):
                self._remove(picker_id)

    def _remove(self, picker_id: str) -> None:
        picker = self._pickers.pop(picker_id)
        try:
            self._discard(picker.state)
        except Exception as exc:
            log_exception(LOGGER, "Unable to discard path picker", exc=exc,
                          phase="picker_cleanup", picker_id=picker_id)

    @staticmethod
    def _payload(picker_id: str, status: str, path: str = "") -> dict[str, object]:
        return {"picker_id": picker_id, "status": status, "path": path}
