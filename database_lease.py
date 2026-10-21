"""Exclusive backend ownership of the application's database."""

from pathlib import Path
from threading import RLock

from filelock import FileLock, Timeout


class DatabaseBusyError(RuntimeError):
    pass


class DatabaseLease:
    def __init__(self):
        self._lock = RLock()
        self._lease: FileLock | None = None
        self.database: Path | None = None

    def acquire(self, database: Path) -> None:
        database = database.expanduser().resolve()
        with self._lock:
            if self.database == database:
                return
            if self._lease is not None:
                raise RuntimeError("运行期间不能更换数据库")
            database.parent.mkdir(parents=True, exist_ok=True)
            candidate = FileLock(
                str(database.with_name(f".{database.name}.backend.lock")),
                timeout=0, thread_local=False,
            )
            try:
                candidate.acquire()
            except Timeout as exc:
                raise DatabaseBusyError(
                    f"该视频库已有后端实例，不能重复启动：{database.parent}"
                ) from exc
            self._lease = candidate
            self.database = database

    def release(self) -> None:
        with self._lock:
            if self._lease is not None:
                self._lease.release()
                self._lease = None
            self.database = None
