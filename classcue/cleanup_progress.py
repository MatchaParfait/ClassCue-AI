"""Single-inventory, application-owned directory cleanup with real work counts."""

from collections import deque
from contextlib import contextmanager, ExitStack
from contextvars import ContextVar
from dataclasses import dataclass
import os
from pathlib import Path
import stat
import time

from classcue.preparation_progress import checkpoint


_SINK = ContextVar("cleanup_sink", default=None)


@contextmanager
def cleanup_events(callback):
    token = _SINK.set(callback)
    try:
        yield
    finally:
        _SINK.reset(token)


class CleanupProgress:
    def __init__(self, scope):
        self.scope = scope
        self.started = time.monotonic()
        self.last_report = -1
        self.samples = deque()
        self.detail = dict(scope=scope, phase="checking", files_scanned=0, directories_scanned=0,
                           files_deleted=0, directories_deleted=0, bytes_deleted=0,
                           files_total=None, directories_total=None, bytes_total=None,
                           phase_progress=None, eta_seconds=None)

    def report(self, *, force=False, **fields):
        checkpoint()
        self.detail.update(fields)
        now = time.monotonic()
        if not force and now - self.last_report < .2:
            return
        value = {**self.detail, "elapsed_seconds": now - self.started, "updated_at": time.time()}
        if value["phase"] == "deleting":
            done = value["files_deleted"] + value["directories_deleted"]
            total = value["files_total"] + value["directories_total"]
            value["phase_progress"] = done / max(1, total)
            self.samples.append((now, done))
            while len(self.samples) > 2 and self.samples[1][0] < now - 10:
                self.samples.popleft()
            elapsed = now - self.samples[0][0]
            rate = (done - self.samples[0][1]) / elapsed if elapsed >= 2 else 0
            value["eta_seconds"] = (total - done) / rate if rate > 0 and done < total else None
        if callback := _SINK.get():
            callback(value)
        self.last_report = now


def _no_link(info):
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("清理目录包含符号链接或目录联接，已停止清理")


def _identity(info):
    return info.st_dev, info.st_ino


@dataclass
class Inventory:
    files: list
    directories: list
    bytes_total: int = 0


def _scan(targets, progress):
    inventory = Inventory([], [])
    for target, label in targets:
        try:
            info = target.lstat()
        except FileNotFoundError:
            continue
        _no_link(info)
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("清理目标必须是应用管理的目录")
        pending = [(target, info)]
        while pending:
            directory, info = pending.pop()
            checkpoint()
            _no_link(info)
            inventory.directories.append((directory, _identity(info)))
            with os.scandir(directory) as entries:
                for entry in entries:
                    info = entry.stat(follow_symlinks=False)
                    _no_link(info)
                    path = Path(entry.path)
                    if stat.S_ISDIR(info.st_mode):
                        # Windows DirEntry.stat may return zero file identity.
                        # One lstat per directory supplies a usable identity;
                        # regular-file sizes still use scandir's cached metadata.
                        pending.append((path, path.lstat()))
                    elif stat.S_ISREG(info.st_mode):
                        inventory.files.append((path, info.st_size))
                        inventory.bytes_total += info.st_size
                    else:
                        raise ValueError("清理目录包含非普通文件，已停止清理")
                    progress.report(current_target=label, current_path=str(path.relative_to(target)),
                                    files_scanned=len(inventory.files),
                                    directories_scanned=len(inventory.directories) + len(pending))
    return inventory


def _remove_file(path):
    # Unlink never follows a final symlink. Check it anyway before a possible
    # read-only chmod, which must never be applied through a link.
    info = path.lstat()
    _no_link(info)
    try:
        path.unlink()
    except PermissionError:
        if os.name != "nt" or not getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_READONLY:
            raise
        path.chmod(stat.S_IWRITE | stat.S_IREAD)
        path.unlink()


def cleanup_directories(owned_root, targets, *, scope, locks=()):
    """Validate all targets before mutation, scan once, then consume that inventory.

    targets: (absolute Path, user-facing label). Inference environments and model
    caches use the same implementation while retaining their existing locks.
    """
    from filelock import FileLock

    root = Path(owned_root).absolute()
    if root.resolve() != root:
        raise ValueError("清理根目录不得经过符号链接或目录联接")
    targets = [(Path(path).absolute(), label) for path, label in targets]
    for target, _label in targets:
        if target.resolve() != target or target == root or not target.is_relative_to(root):
            raise ValueError("清理目标越出应用管理目录或经过链接")
    for index, (target, _) in enumerate(targets):
        if any(target == other or target.is_relative_to(other) or other.is_relative_to(target)
               for other, _ in targets[:index]):
            raise ValueError("清理目标不得重叠")
    progress = CleanupProgress(scope)
    progress.report(force=True, phase="checking", message="检查清理范围与运行状态")
    with ExitStack() as stack:
        if root.exists():
            for lock in locks:
                lock = Path(lock).absolute()
                if lock.parent != root or lock.resolve() != lock:
                    raise ValueError("清理锁越出应用管理目录")
                stack.enter_context(FileLock(str(lock), timeout=0))
        progress.report(force=True, phase="scanning", message="检查目录安全性并统计待删除文件")
        inventory = _scan(targets, progress)
        # No recursive resolve/stat walk is repeated during size accounting or
        # deletion. Recheck each directory identity before consuming its files.
        checked = set()
        identities = dict(inventory.directories)
        files = sorted(inventory.files, key=lambda item: item[0].name != "ready.json")
        progress.report(force=True, phase="deleting", message="正在删除文件",
                        files_total=len(files), directories_total=len(identities),
                        bytes_total=inventory.bytes_total, current_target="", current_path=None)

        def location(path):
            for target, label in targets:
                if path.is_relative_to(target):
                    return dict(current_target=label, current_path=str(path.relative_to(target)))
            raise ValueError("清理文件越出目标目录")

        files_deleted = directories_deleted = released = 0
        for path, size in files:
            if path.parent not in checked:
                info = path.parent.lstat()
                _no_link(info)
                if _identity(info) != identities[path.parent]:
                    raise ValueError("清理期间目录发生变化，已停止清理")
                checked.add(path.parent)
            checkpoint()
            try:
                _remove_file(path)
            except FileNotFoundError:
                size = 0
            except OSError as exc:
                raise OSError(f"无法删除 {path.name}，请关闭占用文件的程序后重试：{exc}") from exc
            files_deleted += 1
            released += size
            progress.report(files_deleted=files_deleted, bytes_deleted=released,
                            **location(path))
        for path, identity in reversed(inventory.directories):
            checkpoint()
            try:
                info = path.lstat()
                _no_link(info)
                if _identity(info) != identity:
                    raise ValueError("清理期间目录发生变化，已停止清理")
                path.rmdir()
            except FileNotFoundError:
                pass
            directories_deleted += 1
            progress.report(directories_deleted=directories_deleted, **location(path))
        progress.report(force=True, phase="completed", message="清理完成", phase_progress=1,
                        files_deleted=files_deleted, directories_deleted=directories_deleted,
                        bytes_deleted=released, eta_seconds=None, current_path=None)
        return released
