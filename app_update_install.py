"""Transactional replacement of signed program files; user data is never replaced."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tomllib
from uuid import uuid4
import zipfile

from filelock import FileLock

from app_updates import read_json, updates_dir, write_json
from database_lease import DatabaseLease
from release_security import (
    MARKER, MAX_METADATA, MAX_UNPACKED, config, digest, local_path, runtime_hash, safe_target,
    validate_feed, validate_inventory, validate_zip_entry, verify, version,
)


def inventory(root, public_key):
    record = verify(read_json(safe_target(root, MARKER)), public_key)
    validate_inventory(record)
    return record


def check_current(root, cfg):
    if (Path(root) / ".git").exists():
        raise ValueError("源码工作区不能原地更新")
    current = inventory(root, cfg["public_key"])
    if current.get("repository") != cfg["repository"]:
        raise ValueError("当前发行包不属于此仓库")
    for name, checksum in current["files"].items():
        path = safe_target(root, name)
        if not path.is_file() or digest(path) != checksum:
            raise ValueError(f"程序文件已被修改，请手动升级：{name}")
    return current


def stage(root, plan):
    """Verify the entire archive before the desktop shuts down."""
    root = Path(root)
    cfg = config(root)
    if not re.fullmatch(r"[a-f0-9]{32}", plan["id"]):
        raise ValueError("无效的安装计划")
    old = check_current(root, cfg)
    release = plan["release"]
    import time
    feed = validate_feed(verify(plan["feed"], cfg["public_key"]), cfg["repository"], now=time.time(),
                         minimum_revision=read_json(updates_dir(root) / "state.json", {}).get("revision", 0))
    if feed.get("release") != release:
        raise ValueError("安装计划与签名源不符")
    if (plan["repository"] != cfg["repository"] or release["version"] != plan["version"]
            or version(release["version"]) <= version(old["version"])
            or release["runtime_hash"] != runtime_hash(root / "uv.lock")
            or release["data_schema"] != cfg["data_schema"]):
        raise ValueError("版本、依赖或数据格式不兼容，请手动升级")
    directory = updates_dir(root)
    archive = local_path(directory, f"{release['version']}-{release['sha256']}.zip")
    if archive.stat().st_size != release["size"] or digest(archive) != release["sha256"]:
        raise ValueError("更新包大小或校验值不符")
    destination = local_path(directory, Path("staging") / plan["id"])
    destination.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(archive) as package:
        entries = package.infolist()
        if len(entries) > 5001 or sum(info.file_size for info in entries) > MAX_UNPACKED:
            raise ValueError("更新包展开后过大")
        names = set()
        for info in entries:
            validate_zip_entry(info)
            if info.filename.casefold() in names:
                raise ValueError("更新包文件名重复")
            names.add(info.filename.casefold())
        marker_info = package.getinfo(MARKER)
        if marker_info.file_size > MAX_METADATA:
            raise ValueError("发行文件清单过大")
        import json
        envelope = json.loads(package.read(MARKER))
        new = verify(envelope, cfg["public_key"])
        files = validate_inventory(new)
        if (new.get("repository") != cfg["repository"] or new["version"] != plan["version"]
                or new["data_schema"] != old["data_schema"]
                or set(info.filename for info in entries) != {*files, MARKER}):
            raise ValueError("更新包身份或文件清单不符")
        for info in entries:
            target = safe_target(destination, info.filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            with package.open(info) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            if info.filename != MARKER and digest(target) != files[info.filename]:
                raise ValueError(f"更新包文件校验失败：{info.filename}")
        new_cfg = config(destination)
        if runtime_hash(destination / "uv.lock") != runtime_hash(root / "uv.lock"):
            raise ValueError("此升级需要重新安装依赖")
        metadata = tomllib.loads((destination / "pyproject.toml").read_text(encoding="utf-8"))
        if new_cfg != cfg or metadata["project"]["version"] != new["version"]:
            raise ValueError("更新包修改了信任配置或版本不一致，请手动升级")
    # Existing, untracked program paths must never be overwritten.
    for name in files:
        if name not in old["files"] and safe_target(root, name).exists():
            raise ValueError(f"新程序文件与本地文件冲突：{name}")
        if any(local_path(root, parent).is_file() for parent in Path(name).parents if str(parent) != "."):
            raise ValueError("程序目录结构变化，请手动升级")
    return destination, old, new


def replace_file(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".updating-" + uuid4().hex)
    created = False
    try:
        with temporary.open("xb") as dst:
            created = True
            with source.open("rb") as src:
                shutil.copyfileobj(src, dst)
                dst.flush()
                os.fsync(dst.fileno())
        temporary.replace(target)
    finally:
        if created:
            temporary.unlink(missing_ok=True)


def rollback(root, record):
    directory = updates_dir(root)
    if not re.fullmatch(r"[a-f0-9]{32}", record["id"]):
        raise ValueError("恢复记录无效")
    backup = local_path(directory, Path("backups") / record["id"])
    # Backups are authenticated again, including when recovering after a crash.
    old = inventory(backup, config(root)["public_key"])
    for name, checksum in old["files"].items():
        if digest(safe_target(backup, name)) != checksum:
            raise ValueError("备份已损坏，未尝试恢复")
    for name in reversed(record["touched"]):
        target = safe_target(root, name)
        if name in old["files"] or name == MARKER:
            replace_file(safe_target(backup, name), target)
        else:
            target.unlink(missing_ok=True)
    record.update(status="rolled_back", message="程序文件已恢复到升级前版本；用户数据未回退")
    write_json(directory / "transaction.json", record)


def apply(root, plan, staged, old, new):
    root = Path(root)
    cfg = config(root)
    if check_current(root, cfg) != old:
        raise ValueError("准备期间程序版本发生变化")
    for name in new["files"]:
        if name not in old["files"] and safe_target(root, name).exists():
            raise ValueError("准备期间出现文件冲突")
    directory = updates_dir(root)
    backup = local_path(directory, Path("backups") / plan["id"])
    backup.mkdir(parents=True, exist_ok=False)
    for name in [*old["files"], MARKER]:
        source = safe_target(root, name)
        destination = safe_target(backup, name)
        replace_file(source, destination)
    if inventory(backup, cfg["public_key"]) != old or any(
        digest(safe_target(backup, name)) != checksum for name, checksum in old["files"].items()
    ):
        raise ValueError("程序备份校验未通过，原程序未修改")
    # The API is stopped and its database lease must be free before backup.
    lease = DatabaseLease()
    try:
        if plan.get("database_path"):
            database = Path(plan["database_path"])
            lease.acquire(database)
            if database.is_file():
                with sqlite3.connect(database) as source, sqlite3.connect(backup / "library-backup.sqlite3") as output:
                    source.backup(output)
                    if output.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise ValueError("数据库备份校验未通过")
        record = {"id": plan["id"], "status": "replacing", "version": new["version"],
                  "previous_version": old["version"], "touched": [], "message": "正在替换程序文件"}
        write_json(directory / "transaction.json", record)
        try:
            for name in sorted(set(old["files"]) | set(new["files"])) + [MARKER]:
                record["touched"].append(name)
                write_json(directory / "transaction.json", record)
                if name in new["files"] or name == MARKER:
                    source = safe_target(staged, name)
                    expected = new["files"].get(name)
                    if expected and digest(source) != expected:
                        raise ValueError("暂存程序文件已发生变化")
                    replace_file(source, safe_target(root, name))
                else:
                    safe_target(root, name).unlink()
            record.update(status="installed", message="已安装，等待新版本启动")
            write_json(directory / "transaction.json", record)
        except Exception:
            rollback(root, record)
            raise
    finally:
        lease.release()


def recover(root):
    directory = updates_dir(root)
    record = read_json(directory / "transaction.json", {})
    if record.get("status") == "replacing":
        rollback(root, record)


def recover_at_launch(root):
    if (Path(root) / ".git").exists():
        return
    directory = updates_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    with FileLock(str(directory / "install.lock"), timeout=120):
        recover(root)


def prepare_helper(root, plan):
    directory = updates_dir(root)
    helper = local_path(directory, Path("helper") / plan["id"])
    helper.mkdir(parents=True, exist_ok=False)
    for name in ["app_update_install.py", "app_updates.py", "app_version.py", "release_security.py",
                 "database_lease.py", "pyproject.toml"]:
        shutil.copy2(Path(root) / name, helper / name)
    shutil.copy2(Path(root) / "scripts" / "app_update_worker.py", helper / "worker.py")
    return helper


def spawn_helper(root, plan, helper):
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    return subprocess.Popen(
        [sys.executable, str(helper / "worker.py"), "--root", str(root), "--plan", plan["id"]],
        cwd=helper, creationflags=flags, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
    )


def launch(root):
    subprocess.Popen(["wscript.exe", str(Path(root) / "scripts" / "desktop_launcher.vbs")],
                     cwd=root, creationflags=subprocess.CREATE_NO_WINDOW)
