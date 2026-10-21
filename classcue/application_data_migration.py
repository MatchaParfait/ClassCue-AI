"""Relocate small application data only while the backend is stopped.

Keep the tiny machine-local locator in project/data. Never copy open SQLite
files, environments, arbitrary contents of data/, or decrypted credentials.
"""
from contextlib import ExitStack, closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time
from uuid import uuid4

from filelock import FileLock

import classcue.asr_storage as asr_storage
from classcue.database_lease import DatabaseLease
from classcue.storage_migration import _hash, _identity, _overlaps, _safe, _write_json


CONFIG_FILES = (
    "ai_providers.json", "asr_settings.json", "video_trigger_library.json",
    "deepseek_api_key.dpapi", "openai_compatible_api_key.dpapi",
    "desktop_settings.json",
)


def journal_path(project):
    return project / "data" / "application_data_migration.json"


def read_journal(project):
    path = journal_path(project)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def managed_configs(directory):
    files = [directory / name for name in CONFIG_FILES if (directory / name).exists()]
    keys = directory / "ai_keys"
    _safe(keys)
    if keys.exists():
        files.extend(keys.glob("*.dpapi"))
    for path in sorted(files):
        _safe(path)
        _identity(path)
        yield path


def status(project, database):
    config = asr_storage.application_config_directory(project)
    saved = asr_storage.read_configuration(project)
    return dict(
        root=saved.get("application_data_root"), database_dir=str(database.parent),
        config_dir=str(config), database_bytes=sum(
            p.stat().st_size for p in (database, database.with_name(database.name + "-wal")) if p.exists()),
        config_bytes=sum(p.stat().st_size for p in managed_configs(config)),
        migration=read_journal(project),
    )


def plan(project, database, destination):
    target = asr_storage.validate_directory(destination)
    source_config = asr_storage.application_config_directory(project)
    target_database, target_config = target / "library" / "project.db", target / "config"
    if database.resolve() == target_database.resolve() and source_config == target_config:
        raise ValueError("已经在使用这个应用数据目录")
    # No hot replacement or partial in-place relocation of config files.
    if source_config == target_config:
        raise ValueError("新配置目录必须与当前配置目录不同")
    if database.resolve() == target_database.resolve():
        raise ValueError("目标数据库与当前数据库相同，请另选应用数据目录")
    roots = [target_database.parent, target_config]
    assets = asr_storage.storage_root(project)
    protected = [
        asr_storage.video_directory(project, database.parent),
        asr_storage.audio_directory(project, database.parent),
        assets / "models", assets / ".runtime", project / ".venv", project / ".git",
        project / "frontend", project / "scripts", asr_storage.log_directory(project),
        project / "data" / "asr_storage.json",
    ]
    for directory in roots:
        _safe(directory)
        if directory.exists() and not directory.is_dir():
            raise ValueError("目标子文件夹位置已有同名文件")
        if any(_overlaps(directory, item) for item in protected):
            raise ValueError("应用数据不能放进视频、缓存、模型、运行环境或程序目录")
    files = []
    for path in managed_configs(source_config):
        target_path = _safe(target_config / path.relative_to(source_config))
        if target_path.exists() and (not target_path.is_file() or _hash(path) != _hash(target_path)):
            raise ValueError(f"目标配置文件已存在且内容不同，未覆盖：{target_path.name}")
        files.append(dict(source=str(path), target=str(target_path), identity=_identity(path), bytes=path.stat().st_size))
    # Include WAL pages in the conservative free-space estimate.
    size = status(project, database)["database_bytes"] + sum(f["bytes"] for f in files)
    location = target
    while not location.exists():
        location = location.parent
    free = shutil.disk_usage(_safe(location)).free
    if free < size:
        raise ValueError("目标磁盘空间不足")
    result = dict(destination=str(target), source_database=str(database.resolve()), source_config=str(source_config),
                  database_dir=str(target_database.parent), config_dir=str(target_config),
                  files=files, bytes_total=size, files_total=len(files) + 1,
                  library_id=asr_storage.library_identity(project, database))
    result["plan_id"] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    result["free_bytes"] = free
    return result


def public_plan(value):
    return {k: value[k] for k in ("destination", "database_dir", "config_dir", "bytes_total",
                                  "files_total", "free_bytes", "plan_id")}


def schedule(project, database, destination, expected_plan):
    existing = read_journal(project)
    if existing and existing["status"] in {"pending", "copying", "committed"}:
        raise ValueError("应用数据正在迁移并重启，请等待完成")
    value = plan(project, database, destination)
    if value["plan_id"] != expected_plan:
        raise ValueError("数据或配置已变化，请重新检查迁移范围")
    # Never put configuration contents or credentials into the migration record.
    record = {k: value[k] for k in ("destination", "source_database", "source_config", "library_id")}
    record.update(status="pending", message="正在关闭后端，准备迁移并立即重启", id=uuid4().hex)
    _write_json(journal_path(project), record)
    return record


def cancel(project):
    value = read_journal(project)
    if value and value["status"] == "pending":
        value.update(status="cancelled", message="已取消待执行的迁移")
        _write_json(journal_path(project), value)


def _database_fingerprint(connection):
    digest = hashlib.sha256()
    for row in connection.execute("SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"):
        name, sql = row
        digest.update(repr((name, sql)).encode())
        quoted = '"' + name.replace('"', '""') + '"'
        for value in connection.execute(f"SELECT * FROM {quoted}"):
            digest.update(repr(value).encode())
            digest.update(b"\0")
    return digest.hexdigest()


def _publish(temporary, target):
    _safe(target)
    if os.name == "nt":
        temporary.rename(target)
    else:
        os.link(temporary, target)
        temporary.unlink()


def apply_pending(project, database):
    """Called by the launcher after stopping owned services, before starting any.

    Original database and configs remain as recovery backups. A single atomic
    locator update activates all copied data together. Never relocates a live DB.
    """
    database = database.resolve()
    record = read_journal(project)
    if not record or record["status"] in {"cancelled", "failed"}:
        return database.parent
    target = Path(record["destination"])
    target_database = target / "library" / "project.db"
    source_database = Path(record["source_database"]).resolve()
    # An explicit development/test database must never be redirected to the
    # installation's library, or finalize its unrelated migration journal.
    if database not in (source_database, target_database.resolve()):
        return database.parent
    config = asr_storage.read_configuration(project)
    # This also repairs a launcher interrupted immediately after the locator commit.
    if (config.get("application_data_root") == str(target)
            and config.get("library_data_dir") == str(target_database.parent)):
        if not target_database.is_file():
            raise ValueError("迁移目标数据库缺失，未启动空视频库")
        if record["status"] != "completed":
            record.update(status="completed", message="应用数据迁移完成，原数据保留为备份")
            _write_json(journal_path(project), record)
        return target_database.parent
    if record["status"] == "completed":
        return database.parent
    if source_database != database:
        raise ValueError("待迁移的数据库与当前应用不一致，未移动数据")
    source_lease, target_lease = DatabaseLease(), DatabaseLease()
    created, temporaries = [], []
    committed = False
    with ExitStack() as stack:
        stack.enter_context(FileLock(str(project / "data" / "application-data.lock"), timeout=0))
        source_lease.acquire(database)
        stack.callback(source_lease.release)
        target_lease.acquire(target_database)
        stack.callback(target_lease.release)
        # Respect the credential/config writer lock used by the running API/CLI.
        source_config = Path(record["source_config"])
        stack.enter_context(FileLock(str(source_config / "ai_providers.json.lock"), timeout=0))
        stack.enter_context(FileLock(str(database.parent / "video-library.lock"), timeout=0))
        stack.enter_context(FileLock(str(project / "data" / "asr_storage.lock"), timeout=0))
        current = plan(project, database, str(target))
        if current["source_config"] != record["source_config"]:
            raise ValueError("配置目录已改变，请重新安排应用数据迁移")
        record.update(status="copying", message="正在备份数据库并校验配置")
        _write_json(journal_path(project), record)
        old_settings = asr_storage.read_configuration(project)
        # Pin defaults that used to be relative to the old database directory.
        new_settings = {
            **old_settings, "application_data_root": str(target),
            "library_data_dir": str(target_database.parent),
            "video_library_dir": str(asr_storage.video_directory(project, database.parent)),
            "audio_cache_dir": str(asr_storage.audio_directory(project, database.parent)),
            "library_identity": {
                "database": str(target_database), "id": current["library_id"],
                "scope": asr_storage.library_scope(project, database),
            },
        }
        try:
            # Reserve the writer slot while a separate reader makes a consistent backup.
            # All backend connections are already closed; this also excludes stray writers.
            with closing(sqlite3.connect(database, timeout=10)) as reserve:
                reserve.execute("BEGIN IMMEDIATE")
                with closing(sqlite3.connect(database, timeout=10)) as source:
                    target_database.parent.mkdir(parents=True, exist_ok=True)
                    fingerprint = _database_fingerprint(source)
                    if target_database.exists():
                        with closing(sqlite3.connect(f"{target_database.as_uri()}?mode=ro", uri=True)) as previous:
                            if previous.execute("PRAGMA quick_check").fetchone()[0] != "ok" or _database_fingerprint(previous) != fingerprint:
                                raise ValueError("目标数据库已存在且内容不同，未覆盖")
                    else:
                        temporary = target_database.with_name(f".application-data-{record['id']}.db")
                        temporaries.append(temporary)
                        if temporary.exists():
                            raise ValueError("存在上次未完成的数据库副本，请另选空目录或先保留该副本")
                        with closing(sqlite3.connect(temporary)) as destination:
                            deadline = time.monotonic() + 60
                            def progress(_status, _remaining, _total):
                                if time.monotonic() > deadline:
                                    raise TimeoutError("数据库备份超时，原数据库保留")
                            source.backup(destination, pages=256, progress=progress, sleep=.05)
                            if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok" or _database_fingerprint(destination) != fingerprint:
                                raise ValueError("数据库副本校验失败，原数据库保留")
                        _publish(temporary, target_database)
                        created.append((target_database, _identity(target_database)))
                target_config = target / "config"
                target_config.mkdir(parents=True, exist_ok=True)
                for item in current["files"]:
                    src, dst = Path(item["source"]), Path(item["target"])
                    if tuple(item["identity"]) != _identity(src):
                        raise ValueError("配置文件已变化，迁移已停止")
                    digest = _hash(src)
                    _safe(dst.parent).mkdir(parents=True, exist_ok=True)
                    if not dst.exists():
                        temporary = dst.with_name(f".application-data-{uuid4().hex}.part")
                        temporaries.append(temporary)
                        shutil.copy2(src, temporary)
                        with temporary.open("rb+") as output:
                            os.fsync(output.fileno())
                        if _hash(temporary) != digest:
                            raise ValueError("配置复制校验失败")
                        _publish(temporary, dst)
                        created.append((dst, _identity(dst)))
                    if _hash(dst) != digest or _hash(src) != digest:
                        raise ValueError("配置文件在迁移期间发生变化")
                # All new files are complete before publishing the only activation pointer.
                _write_json(project / "data" / "asr_storage.json", new_settings)
                committed = True
            record.update(status="completed", message="应用数据迁移完成，原数据库和配置保留为备份",
                          database_dir=str(target_database.parent), config_dir=str(target_config))
            _write_json(journal_path(project), record)
            return target_database.parent
        except BaseException as exc:
            if committed:
                # The locator is the commit boundary; never restore stale paths after it.
                return target_database.parent
            for path, identity in reversed(created):
                if path.exists() and _identity(path) == identity:
                    path.unlink()
            record.update(status="failed", message=str(exc))
            _write_json(journal_path(project), record)
            raise
        finally:
            for path in temporaries:
                if path.exists():
                    _safe(path).unlink()


def require_owned_launcher(project, instance_id):
    message = "当前后端不属于一键启动器，无法安全地自动重启；请使用应用启动入口打开后再迁移"
    try:
        state = json.loads((project / ".runtime" / "frontend_state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(message) from exc
    if not isinstance(state, dict):
        raise ValueError(message)
    if state.get("instance_id") != instance_id or not state.get("owned_processes"):
        raise ValueError(message)
    return state


def restart_owned_application(project, instance_id, migration_id):
    import subprocess
    import sys
    require_owned_launcher(project, instance_id)
    environment = {
        **os.environ, "CLASSROOM_APP_NO_BROWSER": "1", "CLASSROOM_EXPECT_INSTANCE_ID": instance_id,
        "CLASSROOM_DATA_MIGRATION_ID": migration_id,
    }
    return subprocess.Popen(
        [sys.executable, str(project / "scripts" / "start_frontend.py"), "restart"],
        cwd=project, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=os.name != "nt",
        creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0,
    )
