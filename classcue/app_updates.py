"""Opt-in downloads and a small, signed public update/announcement feed."""

from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Event, RLock, Thread
import time
from uuid import uuid4

from filelock import FileLock

from classcue.app_version import VERSION
from classcue.release_security import (
    FEED_TAG, MARKER, asset_url, config, digest, local_path, now_iso, open_download,
    read_remote_json, runtime_hash, timestamp, validate_feed, validate_inventory, verify, version,
)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as output:
            json.dump(value, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def updates_dir(root):
    return local_path(root, Path(".runtime") / "updates")


def pending_install(root):
    value = read_json(updates_dir(root) / "transaction.json", {})
    return value.get("status") in {"replacing", "installed"}


class UpdateCenter:
    def __init__(self, root, *, fetch_json=read_remote_json, download=open_download, clock=time.time):
        self.root = Path(root).absolute()
        try:
            self.config = config(self.root)
            self.configuration_error = None
        except (OSError, ValueError, TypeError):
            # A damaged release configuration must not disable local study.
            self.config = {"repository": "MatchaParfait/ClassCue-AI", "public_key": "", "data_schema": 1}
            self.configuration_error = "发行配置缺失或损坏，更新功能已禁用"
        self.directory = updates_dir(self.root)
        self.fetch_json, self.download, self.clock = fetch_json, download, clock
        self.guard = RLock()
        self.stop_event = Event()
        self.download_stop = Event()
        self.working = False
        self.downloading = False
        self.downloaded_bytes = 0
        self.state = read_json(self.directory / "state.json", {})
        if not isinstance(self.state, dict):
            self.state = {}
        self.thread = None
        self.feed = None
        self._load_feed()

    def _save(self):
        with FileLock(str(self.directory / "state.lock")):
            write_json(self.directory / "state.json", self.state)

    def _load_feed(self):
        self.feed = None
        envelope = read_json(self.directory / "feed.json")
        if envelope and self.config["public_key"]:
            try:
                self.feed = validate_feed(verify(envelope, self.config["public_key"]), self.config["repository"],
                    now=self.clock(), minimum_revision=self.state.get("revision", 0))
            except (ValueError, KeyError, TypeError):
                pass

    def portable(self):
        if (self.root / ".git").exists() or not (self.root / MARKER).is_file():
            return False
        try:
            payload = verify(read_json(self.root / MARKER), self.config["public_key"])
            validate_inventory(payload)
            return payload["version"] == VERSION and payload["repository"] == self.config["repository"]
        except (ValueError, TypeError, KeyError):
            return False

    def package_path(self, release):
        return self.directory / f"{release['version']}-{release['sha256']}.zip"

    def _release(self):
        self._load_feed()
        release = self.feed.get("release") if self.feed else None
        return release if release and version(release["version"]) > version(VERSION) else None

    def status(self):
        with self.guard:
            release = self._release()
            compatible = bool(release and release["runtime_hash"] == runtime_hash(self.root / "uv.lock")
                              and release["data_schema"] == self.config["data_schema"])
            announcements = []
            seen = self.state.get("seen", [])
            for item in (self.feed or {}).get("announcements", []):
                if timestamp(item["published_at"]) > self.clock():
                    continue
                if item.get("expires_at") and timestamp(item["expires_at"]) <= self.clock():
                    continue
                if item.get("min_version") and version(VERSION) < version(item["min_version"]):
                    continue
                if item.get("max_version") and version(VERSION) > version(item["max_version"]):
                    continue
                announcements.append({**item, "read": item["id"] in seen})
            announcements.sort(key=lambda item: timestamp(item["published_at"]), reverse=True)
            ready = bool(release and self.package_path(release).is_file()
                         and self.state.get("download_sha256") == release["sha256"])
            return {
                "version": VERSION, "repository": self.config["repository"],
                "configured": bool(self.config["public_key"]), "portable": self.portable(),
                "preferences": {key: bool(self.state.get(key, default)) for key, default in
                                [("auto_check", True), ("auto_download", False), ("show_announcements", True)]},
                "checking": self.working, "downloading": self.downloading, "downloaded_bytes": self.downloaded_bytes,
                "last_checked": self.state.get("last_checked"), "error": self.configuration_error or self.state.get("error"),
                "download_error": self.state.get("download_error"), "release": release,
                "compatible": compatible, "ready": ready, "announcements": announcements,
                "install": read_json(self.directory / "transaction.json"),
            }

    def preferences(self, changes):
        with self.guard:
            self.directory.mkdir(parents=True, exist_ok=True)
            self.state.update(changes)
            self._save()
        return self.status()

    def mark_read(self, ids):
        with self.guard:
            allowed = {item["id"] for item in self.status()["announcements"]}
            self.state["seen"] = list(dict.fromkeys([*self.state.get("seen", []), *(item for item in ids if item in allowed)]))[-500:]
            self.directory.mkdir(parents=True, exist_ok=True)
            self._save()
        return self.status()

    def start_check(self, *, automatic=False):
        with self.guard:
            if self.stop_event.is_set() or self.working or self.downloading:
                return self.status()
            now = self.clock()
            if automatic and (not self.state.get("auto_check", True) or now - self.state.get("last_attempt", 0) < 86400):
                return self.status()
            if not self.config["public_key"]:
                self.state["error"] = self.configuration_error or "尚未配置发行签名公钥"
                return self.status()
            self.working = True
            self.directory.mkdir(parents=True, exist_ok=True)
            self.state["last_attempt"] = now
            self._save()
            Thread(target=self._check, name="release-check", daemon=True).start()
        return self.status()

    def _check(self):
        try:
            envelope = self.fetch_json(asset_url(self.config["repository"], FEED_TAG, "feed.json"))
            payload = validate_feed(verify(envelope, self.config["public_key"]), self.config["repository"],
                now=self.clock(), minimum_revision=self.state.get("revision", 0))
            with self.guard:
                write_json(self.directory / "feed.json", envelope)
                self.state.update(revision=payload["revision"], last_checked=now_iso(), error=None)
                self._save()
        except Exception:
            with self.guard:
                self.state["error"] = "检查失败：网络不可达、尚未发布公告源，或发行签名/有效期校验未通过。"
                self._save()
        finally:
            with self.guard:
                self.working = False
        if not self.stop_event.is_set() and self.state.get("auto_download", False) and self.portable():
            result = self.status()
            if result["release"] and result["compatible"] and not result["ready"]:
                self.start_download()

    def start_download(self):
        with self.guard:
            if self.stop_event.is_set() or self.downloading or self.working:
                return self.status()
            status = self.status()
            if not status["release"]:
                raise ValueError("没有可下载的新版本")
            if not status["portable"] or not status["compatible"]:
                raise ValueError("当前安装或此版本需要手动下载发行包")
            if status["ready"]:
                return status
            self.downloading = True
            self.download_stop.clear()
            self.state["download_error"] = None
            Thread(target=self._download, args=(status["release"],), name="release-download", daemon=True).start()
        return self.status()

    def pause_download(self):
        self.download_stop.set()
        return self.status()

    def _download(self, release):
        target = self.package_path(release)
        partial = target.with_suffix(".part")
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            offset = partial.stat().st_size if partial.exists() else 0
            if offset > release["size"]:
                partial.unlink()
                offset = 0
            if offset < release["size"]:
                headers = {"Range": f"bytes={offset}-"} if offset else {}
                with self.download(release["url"], headers=headers) as response:
                    resumed = getattr(response, "status", 200) == 206
                    if resumed and response.headers.get("Content-Range") != f"bytes {offset}-{release['size'] - 1}/{release['size']}":
                        raise ValueError("下载续传范围无效")
                    if not resumed:
                        offset = 0
                    self.downloaded_bytes = offset
                    deadline = time.monotonic() + 1800
                    with partial.open("ab" if resumed else "wb") as output:
                        while True:
                            if self.stop_event.is_set() or self.download_stop.is_set():
                                raise InterruptedError("下载已暂停")
                            if time.monotonic() > deadline:
                                raise TimeoutError("下载超时")
                            chunk = response.read(256 * 1024)
                            if not chunk:
                                break
                            self.downloaded_bytes += len(chunk)
                            if self.downloaded_bytes > release["size"]:
                                raise ValueError("下载超出已签名的文件大小")
                            output.write(chunk)
            if partial.stat().st_size < release["size"]:
                raise OSError("下载中断，已保留文件，可重试续传")
            if partial.stat().st_size != release["size"] or digest(partial) != release["sha256"]:
                partial.unlink(missing_ok=True)
                raise ValueError("下载不完整或哈希校验失败")
            partial.replace(target)
            with self.guard:
                self.state.update(download_sha256=release["sha256"], download_error=None)
                self._save()
        except InterruptedError:
            with self.guard:
                self.state["download_error"] = None
                self._save()
        except Exception as error:
            with self.guard:
                self.state["download_error"] = str(error)[:200]
                self._save()
        finally:
            with self.guard:
                self.downloading = False

    def reserve_install(self):
        with self.guard:
            state = self.status()
            if not state["ready"] or not state["portable"] or not state["compatible"] or self.downloading or self.working:
                raise ValueError("更新尚未就绪，或当前安装不支持原地升级")
            if pending_install(self.root):
                raise ValueError("已有安装正在进行")
            plan = {"id": uuid4().hex, "version": state["release"]["version"], "previous_version": VERSION,
                    "release": state["release"], "repository": self.config["repository"],
                    "feed": read_json(self.directory / "feed.json")}
            write_json(self.directory / "install-plan.json", plan)
            return plan

    def start_background(self):
        if self.thread is not None:
            return
        def run():
            if self.stop_event.wait(12):
                return
            while not self.stop_event.is_set():
                self.start_check(automatic=True)
                self.stop_event.wait(3600)
        self.thread = Thread(target=run, name="release-scheduler", daemon=True)
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=1)
