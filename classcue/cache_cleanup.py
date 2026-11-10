"""Bounded maintenance of rebuildable application caches, never learning assets."""

from contextlib import ExitStack
from dataclasses import dataclass
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from filelock import FileLock, Timeout

from classcue import asr_storage
from classcue.cleanup_progress import CleanupProgress
from classcue.windows_recycle import write_recycle_record


DAY = 86400
AUTOMATIC = ("transcription", "previews", "logs", "updates")
LABELS = {
    "transcription": "已完成转写缓存", "previews": "视频封面与搜索预览",
    "logs": "历史运行日志", "updates": "旧更新下载与解压文件",
    "downloads": "依赖下载缓存", "ai": "AI 分块结果缓存", "backups": "旧更新回退备份",
}
DEFAULTS = {"enabled": True, "interval_days": 7, "retention_days": 30,
            "preview_limit_mb": 512, "categories": list(AUTOMATIC)}
_HEX = re.compile(r"[a-f0-9]{64}")
_LOG = re.compile(r"(?:backend-\d+|api|launcher|[\w-]+-worker|[\w-]+-install)\.log(?:\.\d+)?")
_TASK_FILES = {".json", ".wav", ".log", ".tmp"}
LOGGER = logging.getLogger(__name__)


def safe(path: Path, root: Path) -> bool:
    """Reject links in every ancestor, including directory junctions."""
    path, root = path.absolute(), root.absolute()
    try:
        if root == Path(root.anchor) or path.resolve() != path or not path.is_relative_to(root):
            return False
        current = path
        while True:
            if current.exists():
                info = current.lstat()
                if current.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                    return False
            if current == root:
                return root.resolve() == root
            current = current.parent
    except OSError:
        return False


def document(path):
    try:
        if not safe(path, path.parent):
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def files_under(root):
    if not safe(root, root) or not root.is_dir():
        return
    for directory, children, files in os.walk(root, followlinks=False):
        children[:] = [name for name in children if safe(Path(directory) / name, root)]
        for name in files:
            path = Path(directory) / name
            if safe(path, root) and path.is_file():
                yield path


def completed_task(directory, content_hash=None):
    if not _HEX.fullmatch(directory.name) or not safe(directory, directory.parent):
        return False
    manifest, state = document(directory / "manifest.json"), document(directory / "state.json")
    identity = manifest.get("identity", {})
    return (isinstance(identity, dict) and state.get("status") == "completed"
            and (identity.get("input_sha256") or identity.get("source_sha256")) == content_hash)


def cleanup_saved_transcript(store, video_id):
    """Run only after a successful transcript commit; cleanup cannot fail that commit."""
    try:
        row = store.get_video(video_id)
        if (row is None or not store.has_transcript(video_id)
                or not Path(row["video_path"]).is_file()):
            return
        audio = Path(row["audio_path"]).absolute()
        root = audio.parent
        content_hash = row["content_hash"]
        if (_HEX.fullmatch(content_hash) and audio.name == f"{content_hash}.wav"
                and safe(audio, root)):
            audio.unlink(missing_ok=True)
        for engine in ("qwen", "firered"):
            jobs = root / f"{engine}_jobs"
            if not safe(jobs, root) or not jobs.is_dir():
                continue
            with FileLock(str(jobs / "prepare.lock"), timeout=0):
                for directory in jobs.iterdir():
                    if (not safe(directory, jobs) or not directory.is_dir()
                            or not completed_task(directory, content_hash)):
                        continue
                    with FileLock(str(directory / "run.lock"), timeout=0):
                        if not completed_task(directory, content_hash):
                            continue
                        # The marker distinguishes persisted subtitles from a worker
                        # result that has not yet committed to the library database.
                        write_recycle_record(directory / "transcript-saved.json", {
                            "database": str(store.path.absolute()), "video_id": video_id,
                            "content_hash": content_hash, "saved_at": time.time(),
                        })
                        for path in files_under(directory):
                            if path.suffix.lower() == ".wav":
                                path.unlink(missing_ok=True)
    except Exception:
        LOGGER.warning("Transcript cache cleanup deferred", exc_info=True)


@dataclass(frozen=True)
class Candidate:
    category: str
    path: Path
    root: Path
    size: int
    modified: float
    inode: int
    device: int
    lock: Path | None = None


class CacheCleanup:
    def __init__(self, project, store, *, clock=time.time, update_center=None):
        self.project, self.store, self.clock = Path(project).absolute(), store, clock
        self.update_center = update_center

    @property
    def settings_path(self):
        return asr_storage.application_config_directory(self.project) / "cache_cleanup.json"

    def state(self):
        value = document(self.settings_path)
        preferences = {**DEFAULTS, **value.get("preferences", {})}
        self.validate(preferences)
        return {"preferences": preferences, "last_run": value.get("last_run")}

    @staticmethod
    def validate(value):
        if (type(value.get("enabled")) is not bool
                or type(value.get("interval_days")) is not int or not 1 <= value["interval_days"] <= 30
                or type(value.get("retention_days")) is not int or not 1 <= value["retention_days"] <= 365
                or type(value.get("preview_limit_mb")) is not int or not 64 <= value["preview_limit_mb"] <= 10240
                or not isinstance(value.get("categories"), list)
                or any(key not in AUTOMATIC for key in value["categories"])):
            raise ValueError("缓存清理设置无效")

    def save(self, preferences):
        self.validate(preferences)
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.settings_path) + ".lock", timeout=5):
            value = document(self.settings_path)
            write_recycle_record(self.settings_path, {**value, "preferences": preferences})
        return self.state()

    def due(self):
        state = self.state()
        last = state["last_run"] or {}
        if last.get("cancelled"):
            last = {}
        return (state["preferences"]["enabled"]
                and bool(state["preferences"]["categories"])
                and self.clock() - last.get("finished_at", 0) >= state["preferences"]["interval_days"] * DAY)

    def _task_is_saved(self, directory):
        marker = document(directory / "transcript-saved.json")
        if not (directory / "transcript-saved.json").exists():
            identity = document(directory / "manifest.json").get("identity", {})
            if not isinstance(identity, dict):
                return False
            content_hash = identity.get("input_sha256") or identity.get("source_sha256")
            if not isinstance(content_hash, str) or not _HEX.fullmatch(content_hash):
                return False
            row = self.store.get_video_by_hash(content_hash)
            return (row is not None and row["status"] in {"transcribed", "review_ready"}
                    and Path(row["video_path"]).is_file() and self.store.has_transcript(row["id"])
                    and completed_task(directory, content_hash))
        if marker.get("database") != str(self.store.path.absolute()):
            return False
        if type(marker.get("video_id")) is not int:
            return False
        row = self.store.get_video(marker["video_id"])
        return (row is not None and marker.get("content_hash") == row["content_hash"]
                and row["status"] in {"transcribed", "review_ready"}
                and Path(row["video_path"]).is_file()
                and self.store.has_transcript(row["id"])
                and completed_task(directory, row["content_hash"]))

    def _saved_video(self, content_hash):
        row = self.store.get_video_by_hash(content_hash)
        return (row is not None and row["status"] in {"transcribed", "review_ready"}
                and Path(row["video_path"]).is_file() and self.store.has_transcript(row["id"]))

    def _protected_work(self):
        return (self.store.list_paused_jobs()
                or any(row["job_type"] != "cache_cleanup" for row in self.store.list_active_jobs()))

    @staticmethod
    def _backup_record(directory):
        record = document(directory / "backup-state.json")
        stamp = record.get("completed_at")
        if (record.get("id") != directory.name or record.get("status") not in {"complete", "cleaning"}
                or type(stamp) not in (int, float) or not math.isfinite(stamp)
                or type(record.get("database_backed_up")) is not bool):
            return None
        return record

    @staticmethod
    def _backup_complete(directory, record):
        return (record is not None and record["status"] == "complete"
                and (not record["database_backed_up"]
                     or (safe(directory / "library-backup.sqlite3", directory)
                         and (directory / "library-backup.sqlite3").is_file())))

    def _inventory(self, mode, preferences=None):
        preferences = preferences or self.state()["preferences"]
        cutoff = self.clock() - preferences["retention_days"] * DAY
        audio = asr_storage.audio_directory(self.project, self.store.path.parent).absolute()
        logs = asr_storage.log_directory(self.project).absolute()
        updates = self.project / ".runtime" / "updates"
        candidates, totals = [], {key: 0 for key in LABELS}

        def add(category, path, root, *, eligible=None, lock=None):
            if not safe(path, root):
                return
            try:
                info = path.stat()
                totals[category] += info.st_size
                if category == "previews" and info.st_mtime > self.clock() - 300:
                    eligible = False
                if eligible is True or (eligible is None and (mode == "all" or info.st_mtime <= cutoff)):
                    candidates.append(Candidate(category, path, root, info.st_size, info.st_mtime,
                                                info.st_ino, info.st_dev, lock))
                return info
            except OSError:
                pass

        previews = []
        if safe(audio, audio) and audio.is_dir():
            for path in audio.iterdir():
                if path.suffix != ".wav" or not _HEX.fullmatch(path.stem) or not safe(path, audio):
                    continue
                if self._saved_video(path.stem):
                    add("transcription", path, audio)
        for name in ("thumbnails", "search-previews"):
            root = audio / name
            for path in files_under(root):
                if path.suffix == ".jpg" and _HEX.fullmatch(path.stem):
                    if info := add("previews", path, root, lock=path.with_suffix(".lock")):
                        previews.append((path, info))
        # Keep recently used images under a bounded budget, even before expiry.
        excess = totals["previews"] - preferences["preview_limit_mb"] * 1024 ** 2
        chosen = {item.path for item in candidates}
        for path, info in sorted(previews, key=lambda item: item[1].st_mtime):
            if excess <= 0:
                break
            if info.st_mtime > self.clock() - 300:
                continue
            size = info.st_size
            if path not in chosen:
                # Its bytes were already included in the category total.
                totals["previews"] -= size
                add("previews", path, path.parent, eligible=True, lock=path.with_suffix(".lock"))
            excess -= size
        for engine in ("qwen", "firered"):
            root = audio / f"{engine}_jobs"
            if not safe(root, audio) or not root.is_dir():
                continue
            for directory in root.iterdir():
                if not safe(directory, root) or not directory.is_dir() or not self._task_is_saved(directory):
                    continue
                marker = document(directory / "transcript-saved.json")
                saved_at = marker.get("saved_at")
                if (directory / "transcript-saved.json").exists():
                    if type(saved_at) not in (int, float) or not math.isfinite(saved_at):
                        continue
                else:
                    try:
                        saved_at = (directory / "state.json").stat().st_mtime
                    except OSError:
                        continue
                expired = mode == "all" or saved_at <= cutoff
                for path in files_under(directory):
                    if (path.suffix.lower() in _TASK_FILES
                            and path.name not in {"manifest.json", "state.json", "transcript-saved.json"}):
                        add("transcription", path, root, eligible=expired, lock=directory / "run.lock")
        for path in files_under(logs):
            if not _LOG.fullmatch(path.name) or path.name.startswith(f"backend-{os.getpid()}.log"):
                continue
            # Fixed-name base logs can be owned by a live launcher or worker.
            if path.name in {"api.log", "launcher.log"}:
                continue
            add("logs", path, logs)
        transaction = document(updates / "transaction.json")
        if transaction.get("status") not in {"replacing", "installed"}:
            current = document(updates / "state.json")
            protected_hash = current.get("download_sha256")
            for path in files_under(updates):
                relative = path.relative_to(updates)
                if relative.parts[0] in {"backups", "staging"}:
                    continue
                if (re.fullmatch(r"[\w.+-]+-[a-f0-9]{64}\.(zip|part)", path.name)
                        and (not protected_hash or protected_hash not in path.name)):
                    add("updates", path, updates)
            staging = updates / "staging"
            for path in files_under(staging):
                if transaction.get("id") not in path.relative_to(staging).parts:
                    add("updates", path, updates)
            backup_root = updates / "backups"
            if safe(backup_root, updates) and backup_root.is_dir():
                directories = [
                    item for item in backup_root.iterdir() if safe(item, backup_root) and item.is_dir()
                     and re.fullmatch(r"[a-f0-9]{32}", item.name)
                     and (item / ".release-install.json").is_file()]
                records = {item: self._backup_record(item) for item in directories}
                complete = sorted(
                    (item for item in directories if self._backup_complete(item, records[item])),
                    key=lambda item: records[item]["completed_at"], reverse=True)
                for directory in directories:
                    record = records[directory]
                    eligible = (record is not None and directory not in complete[:2]
                                and (record["status"] == "cleaning" or directory in complete)
                                and directory.name != transaction.get("id"))
                    for path in files_under(directory):
                        add("backups", path, backup_root,
                            eligible=(eligible and path.name not in {".release-install.json", "backup-state.json"}))
        caches = self._download_caches()
        # uv owns this directory's structure and lock protocol. Never unlink its
        # entries ourselves: archive files may be hard-linked to environments.
        for path in {path for _kind, cache in caches for path in files_under(cache)}:
            try:
                totals["downloads"] += path.stat().st_size
            except OSError:
                pass
        where = "" if mode == "all" else "WHERE julianday(updated_at) <= julianday('now', ?)"
        args = () if mode == "all" else (f"-{preferences['retention_days']} days",)
        with self.store.connect() as connection:
            total = connection.execute(
                "SELECT COALESCE(SUM(length(CAST(selection_json AS BLOB))), 0) FROM video_ai_chunks").fetchone()[0]
            eligible = connection.execute(
                f"SELECT COALESCE(SUM(length(CAST(selection_json AS BLOB))), 0) FROM video_ai_chunks {where}",
                args).fetchone()[0]
        totals["ai"] = total
        return candidates, totals, eligible

    def preview(self, mode="expired"):
        preferences = self.state()["preferences"]
        candidates, totals, ai_bytes = self._inventory(mode, preferences)
        return {"categories": [
            {"id": key, "label": label, "bytes": totals[key],
             "eligible_bytes": (ai_bytes if key == "ai" else totals[key] if key == "downloads"
                                else sum(item.size for item in candidates if item.category == key)),
             "automatic": key in AUTOMATIC}
            for key, label in LABELS.items()]}

    def _download_caches(self):
        cache = asr_storage.storage_root(self.project) / ".runtime" / "cache"
        if not safe(cache, cache):
            return []
        roots = []
        for root in (cache, cache / "uv"):
            marker = root / "CACHEDIR.TAG"
            if safe(marker, cache) and marker.is_file():
                roots.append(("uv", root))
                break
        pip = cache / "pip"
        if safe(pip, cache) and pip.is_dir():
            roots.append(("pip", pip))
        return roots

    def _clean_downloads(self):
        caches = self._download_caches()
        if not caches:
            return 0, 0, []
        executable = next((path for path in (
            self.project / ".runtime" / "uv" / "uv.exe",
            self.project / ".runtime" / "bin" / "uv.exe",
            self.project / ".venv" / "Scripts" / "uv.exe",
            Path(shutil.which("uv") or "__missing_uv__"),
        ) if path.is_file()), None)
        runtime = asr_storage.storage_root(self.project) / ".runtime"
        python = next((path for path in (
            runtime / "dolphin-cpu" / "Scripts/python.exe",
            runtime / "fun-asr-nano-cpu" / "Scripts/python.exe", Path(sys.executable),
        ) if path.is_file()), None)
        released, removed, warnings = 0, 0, []
        for kind, cache in sorted(caches, key=lambda item: item[0]):
            command = ([str(executable), "cache", "clean"] if kind == "uv" and executable else
                       [str(python), "-m", "pip", "cache", "purge"] if kind == "pip" and python else None)
            if command is None:
                warnings.append(f"{kind} 下载缓存未清理：未找到可用的清理工具")
                continue
            before_paths = {path: path.stat().st_size for path in files_under(cache)}
            try:
                subprocess.run([*command, "--cache-dir", str(cache)],
                               check=True, capture_output=True, timeout=120,
                               env={**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK": "1"},
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                after_paths = {path: path.stat().st_size for path in files_under(cache)}
                released += max(0, sum(before_paths.values()) - sum(after_paths.values()))
                removed += len(before_paths.keys() - after_paths.keys())
            except (OSError, subprocess.SubprocessError):
                warnings.append(f"{kind} 下载缓存清理未完成，可稍后重试")
        return released, removed, warnings

    def clean(self, categories, *, mode="expired", automatic=False, should_cancel=lambda: False):
        with FileLock(str(self.store.path.parent / "cache-cleanup.lock"), timeout=0):
            return self._clean(categories, mode=mode, automatic=automatic, should_cancel=should_cancel)

    def _clean(self, categories, *, mode, automatic, should_cancel):
        if not categories or any(key not in LABELS for key in categories):
            raise ValueError("请选择有效的清理类别")
        if automatic and any(key not in AUTOMATIC for key in categories):
            raise ValueError("此类别仅支持手动清理")
        if mode not in {"expired", "all"}:
            raise ValueError("清理模式无效")
        if self._protected_work():
            raise RuntimeError("请先完成或停止运行及暂停中的任务，再清理缓存")
        progress = CleanupProgress("应用缓存")
        progress.report(force=True, phase="scanning", message="检查过期缓存与任务保护")
        preferences = self.state()["preferences"]
        candidates, totals, ai_bytes = self._inventory(mode, preferences)
        selected = [item for item in candidates if item.category in categories]
        released = count = skipped = download_files = 0
        failures = []
        progress.report(force=True, phase="deleting", message="清理所选缓存",
                        files_total=len(selected), directories_total=0,
                        bytes_total=sum(item.size for item in selected))
        for item in selected:
            if should_cancel():
                break
            try:
                with ExitStack() as stack:
                    if item.category in {"updates", "backups"} and self.update_center is not None:
                        stack.enter_context(self.update_center.guard)
                        if self.update_center.downloading or self.update_center.working:
                            skipped += 1
                            continue
                    if item.lock is not None:
                        stack.enter_context(FileLock(str(item.lock), timeout=0))
                        if item.category == "transcription" and not self._task_is_saved(item.lock.parent):
                            skipped += 1
                            continue
                    elif item.category == "transcription" and (
                            self._protected_work() or not self._saved_video(item.path.stem)):
                        skipped += 1
                        continue
                    if not safe(item.path, item.root):
                        skipped += 1
                        continue
                    info = item.path.stat()
                    if (info.st_size, info.st_mtime, info.st_ino, info.st_dev) != (
                            item.size, item.modified, item.inode, item.device):
                        skipped += 1
                        continue
                    if item.category == "backups":
                        directory = item.root / item.path.relative_to(item.root).parts[0]
                        record = self._backup_record(directory)
                        if record is None or (record["status"] == "complete"
                                              and not self._backup_complete(directory, record)):
                            skipped += 1
                            continue
                        if record["status"] == "complete":
                            write_recycle_record(directory / "backup-state.json", {**record, "status": "cleaning"})
                    item.path.unlink()
                    released += item.size
                    count += 1
            except (OSError, Timeout):
                skipped += 1
            progress.report(files_deleted=count, bytes_deleted=released, current_path=item.path.name)
        if not should_cancel() and "ai" in categories:
            where = "" if mode == "all" else "WHERE julianday(updated_at) <= julianday('now', ?)"
            args = () if mode == "all" else (f"-{preferences['retention_days']} days",)
            with self.store.connect() as connection:
                connection.execute(f"DELETE FROM video_ai_chunks {where}", args)
            # SQLite reuses freed pages; do not claim this immediately shrinks the DB.
        if not should_cancel() and "downloads" in categories:
            progress.report(force=True, phase="checking", message="清理依赖下载缓存，保留模型与已安装环境")
            download_bytes, download_files, warnings = self._clean_downloads()
            released += download_bytes
            count += download_files
            failures.extend(warnings)
        result = {"finished_at": self.clock(), "released_bytes": released, "files_deleted": count,
                  "skipped": skipped, "automatic": automatic, "categories": list(categories),
                  "cancelled": should_cancel(), "warnings": failures,
                  "ai_cache_bytes_removed": ai_bytes if "ai" in categories else 0}
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.settings_path) + ".lock", timeout=5):
            value = document(self.settings_path)
            write_recycle_record(self.settings_path, {**value, "last_run": result})
        progress.report(force=True, phase="deleting" if result["cancelled"] else "completed",
                        message="缓存清理已停止" if result["cancelled"] else "缓存清理已结束",
                        files_deleted=count, files_total=len(selected) + download_files,
                        bytes_deleted=released, phase_progress=None if result["cancelled"] else 1,
                        eta_seconds=None)
        return {**result, "message": f"缓存清理已结束，删除 {count} 个文件，跳过 {skipped} 个文件"}
