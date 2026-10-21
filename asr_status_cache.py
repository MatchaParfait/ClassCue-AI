"""Cached UI observations, separate from mandatory inference-time validation."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from threading import RLock, Semaphore
from time import perf_counter_ns, time
from typing import Callable

from filelock import FileLock


class AsrStatusCache:
    def __init__(self, path: Path | None = None):
        self.path = path
        self.lock = RLock()
        self.locks: dict[str, RLock] = {}
        self.entries: dict[str, dict] = {}
        self.sizes: dict[str, int] = {}
        self.generations: dict[str, int] = {}
        self.completed: dict[str, int] = {}
        self.order = 0
        self.full_checks = Semaphore(2)
        if path is not None:
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                if saved.get("version") == 1:
                    self.entries = {
                        key: value for key, value in saved.get("entries", {}).items()
                        if isinstance(value, dict) and isinstance(value.get("data"), dict)
                        and isinstance(value.get("checked_at"), (int, float))
                        and isinstance(value.get("order", 0), int)
                    }
                    self.sizes = {
                        key: value for key, value in saved.get("sizes", {}).items()
                        if isinstance(value, int) and value >= 0
                    }
                    self.order = max((item.get("order", 0) for item in self.entries.values()), default=0)
            except (OSError, ValueError, AttributeError, TypeError):
                pass

    def _key_lock(self, key: str) -> RLock:
        with self.lock:
            return self.locks.setdefault(key, RLock())

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with FileLock(str(self.path.with_suffix(".lock")), timeout=1):
                temporary = self.path.with_suffix(".tmp")
                temporary.write_text(json.dumps({
                    "version": 1, "entries": self.entries, "sizes": self.sizes,
                }), encoding="utf-8")
                temporary.replace(self.path)
        except (OSError, TimeoutError):
            logging.getLogger(__name__).warning("Could not persist ASR status cache", exc_info=True)

    def peek(self, key: str) -> dict | None:
        with self.lock:
            candidates = [
                self.entries[scope] for scope in (key + "|basic", key + "|full")
                if scope in self.entries
            ]
            if not candidates:
                return None
            # Preserve full-only fields when a newer basic observation arrives.
            data = {}
            for entry in sorted(candidates, key=lambda item: (item["checked_at"], item.get("order", 0))):
                data.update(entry["data"])
            return {
                "data": data, "checked_at": max(item["checked_at"] for item in candidates),
                "revision": max(item.get("order", 0) for item in candidates),
            }

    def inspect(self, key: str, check: Callable[[], dict], *, full: bool, refresh: bool = False) -> dict:
        started = time()
        started_tick = perf_counter_ns()
        scope = key + ("|full" if full else "|basic")
        with self._key_lock(scope):
            with self.lock:
                previous = self.entries.get(scope)
                if previous and (
                    self.completed.get(scope, -1) >= started_tick
                    or (not refresh and 0 <= started - previous["checked_at"] < (300 if full else 60))
                ):
                    return dict(previous["data"])
                generation = self.generations.setdefault(key, 0)
            if full:
                with self.full_checks:
                    data = check()
            else:
                data = check()
            with self.lock:
                if generation == self.generations.get(key, 0):
                    self.order += 1
                    self.entries[scope] = {"data": dict(data), "checked_at": time(), "order": self.order}
                    self.completed[scope] = perf_counter_ns()
                    self._save()
            return data

    def size(self, key: str, scan: Callable[[], int]) -> int:
        with self._key_lock(key + "|sizes"):
            with self.lock:
                if key in self.sizes:
                    return self.sizes[key]
                generation = self.generations.setdefault(key, 0)
            result = scan()
            with self.lock:
                if generation == self.generations.get(key, 0):
                    self.sizes[key] = result
            return result

    def clear_sizes(self, prefix: str) -> None:
        with self.lock:
            for key in set(self.sizes) | set(self.generations):
                if key.startswith(prefix) and key.endswith("-size"):
                    self.generations[key] = self.generations.get(key, 0) + 1
                    self.sizes.pop(key, None)

    def invalidate(self, prefix: str, *, sizes: bool = True) -> None:
        with self.lock:
            keys = set(self.generations) | set(self.sizes)
            keys.update(key.rsplit("|", 1)[0] for key in self.entries)
            for key in keys:
                if key.startswith(prefix):
                    self.generations[key] = self.generations.get(key, 0) + 1
            self.entries = {key: value for key, value in self.entries.items() if not key.startswith(prefix)}
            self.completed = {key: value for key, value in self.completed.items() if not key.startswith(prefix)}
            if sizes:
                self.sizes = {key: value for key, value in self.sizes.items() if not key.startswith(prefix)}
            self._save()
