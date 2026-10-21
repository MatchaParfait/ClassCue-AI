"""Conservative, verified relocation of managed files, never databases or venvs."""

from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
from tempfile import NamedTemporaryFile
from uuid import uuid4

from filelock import FileLock

import classcue.asr_storage as asr_storage


CHUNK = 4 * 1024 * 1024


def _safe(path: Path) -> Path:
    path = path.absolute()
    if path.resolve() != path or path.is_symlink():
        raise ValueError(f"迁移路径不能经过符号链接或目录联接：{path}")
    if path.exists() and getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
        raise ValueError(f"迁移路径不能是目录联接：{path}")
    return path


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def _identity(path: Path) -> tuple:
    value = _safe(path).stat()
    if not stat.S_ISREG(value.st_mode):
        raise ValueError(f"只能迁移普通文件：{path}")
    return value.st_size, value.st_mtime_ns, value.st_dev, value.st_ino


def _hash(path: Path, checkpoint=lambda: None) -> str:
    digest = hashlib.sha256()
    with _safe(path).open("rb") as stream:
        while chunk := stream.read(CHUNK):
            checkpoint()
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def _nearest_existing(path: Path) -> Path:
    while not path.exists():
        path = path.parent
    return _safe(path)


def _tree(root: Path):
    if not root.exists():
        return
    pending = [_safe(root)]
    while pending:
        directory = pending.pop()
        for path in directory.iterdir():
            _safe(path)
            if path.is_dir():
                pending.append(path)
            elif path.is_file():
                yield path
            else:
                raise ValueError(f"迁移目录包含非普通文件：{path}")


def plan_migration(project: Path, store, values: dict) -> dict:
    """Read-only inventory. Only registered files are taken from the video root."""
    root = Path(store.path).absolute().parent
    old = asr_storage.read_configuration(project)
    assets = asr_storage.storage_root(project)
    videos = asr_storage.video_directory(project, root)
    audio = asr_storage.audio_directory(project, root)
    new_assets = asr_storage.validate_directory(values["asr_storage_root"])
    media, new_videos, new_audio = asr_storage.resolve_media_directories(
        project, root, media_storage_root=values.get("media_storage_root"),
        audio_cache_dir=values.get("audio_cache_dir"), video_library_dir=values.get("video_library_dir"),
    )
    new_logs = asr_storage.validate_directory(values["log_dir"])
    settings = {
        **old, "storage_root": str(new_assets), "audio_cache_dir": str(new_audio),
        "video_library_dir": str(new_videos), "log_dir": str(new_logs),
    }
    if media is not None:
        settings["media_storage_root"] = str(media)
    if values.get("delete_source_after_import") is not None:
        settings["delete_source_after_import"] = values["delete_source_after_import"]
    pairs = [
        ("临时音频与转写缓存", audio, new_audio),
        ("模型权重", assets / "models" / "asr", new_assets / "models" / "asr"),
        ("环境下载缓存", assets / ".runtime" / "cache", new_assets / ".runtime" / "cache"),
        ("视频库", videos, new_videos),
    ]
    protected = [Path(store.path).absolute(), project / "data" / "asr_storage.json",
                 project / "data" / "storage_migration.json", project / ".git",
                 project / "frontend", project / "scripts", project / ".venv"]
    for base in {assets, new_assets, project}:
        protected.extend(base / ".runtime" / name for name in (
            "qwen-asr", "firered-asr2", "paraformer-cpu", "paraformer-gpu",
            "dolphin-cpu", "dolphin-gpu", "fun-asr-nano-cpu", "fun-asr-nano-gpu",
            "sources", "python", "bin",
        ))
    all_roots = [path for _, source, target in pairs for path in (source, target)]
    # A log destination must never be a cache/model/video tree, including an old tree.
    for path in all_roots:
        _safe(path)
        if any(_overlaps(path, item) for item in protected):
            raise ValueError("迁移目录不能包含数据库、配置或应用程序文件")
        if _overlaps(path, new_logs):
            raise ValueError("日志目录必须与视频、缓存及模型目录分开")
    for index, (_, source, target) in enumerate(pairs):
        if source != target and _overlaps(source, target):
            raise ValueError("新旧目录不能互相包含，请选择独立目录")
        for _, other_source, other_target in pairs[:index]:
            if any(_overlaps(a, b) for a in (source, target) for b in (other_source, other_target)):
                raise ValueError("不同类型的新旧存储目录不能重叠或互换")
    asr_storage.validate_video_directory(new_videos, new_audio, new_logs,
                                        new_assets / "models" / "asr", new_assets / ".runtime",
                                        project / ".runtime")
    registered = store.list_videos()
    entries, sections, updates = [], [], []
    for label, source, target in pairs:
        if source == target:
            continue
        paths = (sorted({Path(row["video_path"]).absolute() for row in registered
                         if Path(row["video_path"]).absolute().is_relative_to(source)})
                 if label == "视频库" else sorted(path for path in _tree(source)
                      if not (label == "模型权重" and path.is_relative_to(source / ".locks"))))
        start = len(entries)
        for path in paths:
            info = _identity(path)  # A missing registered video must not be silently discarded.
            destination = _safe(target / path.relative_to(source))
            if destination.exists() and not destination.is_file():
                raise ValueError(f"目标位置已有同名目录：{destination}")
            entries.append(dict(source=str(path), target=str(destination), size=info[0],
                                identity=info, source_root=str(source), target_root=str(target)))
        sections.append(dict(label=label, source=str(source), target=str(target),
                             files=len(entries) - start, bytes=sum(e["size"] for e in entries[start:])))
    mapping = {entry["source"]: entry["target"] for entry in entries}
    for row in registered:
        before_video, before_audio = str(Path(row["video_path"]).absolute()), str(Path(row["audio_path"]).absolute())
        after_video = mapping.get(before_video, before_video)
        # Audio files may have been evicted already; update their future output location too.
        path = Path(before_audio)
        after_audio = str(new_audio / path.relative_to(audio)) if path.is_relative_to(audio) else before_audio
        if before_video != after_video or before_audio != after_audio:
            updates.append(dict(id=row["id"], old_video=before_video, video=after_video,
                                old_audio=before_audio, audio=after_audio))
    # Group all roots sharing a volume, otherwise several individually fitting moves can overfill it.
    volumes = {}
    for entry in entries:
        location = _nearest_existing(Path(entry["target"]).parent)
        volume = location.stat().st_dev
        bucket = volumes.setdefault(volume, {"required": 0, "location": location})
        bucket["required"] += entry["size"]  # Conservative: existing identical files still count.
    disks = []
    for bucket in volumes.values():
        free = shutil.disk_usage(bucket["location"]).free
        if free < bucket["required"]:
            raise ValueError("目标磁盘空间不足，未修改目录或移动原文件")
        disks.append(dict(path=str(bucket["location"]), required_bytes=bucket["required"], free_bytes=free))
    warnings = []
    if assets != new_assets:
        settings["migration_requires_restart"] = True
        warnings.append(f"模型权重和下载缓存会迁移；旧运行环境保留在 {assets / '.runtime'}。重启后在模型页按需重新准备运行环境。")
    if asr_storage.log_directory(project) != new_logs:
        warnings.append("历史日志保留在原目录；新日志目录重启后生效。")
    if videos != new_videos and any(not Path(row["video_path"]).absolute().is_relative_to(videos)
                                    for row in registered):
        warnings.append("原视频库以外的来源文件保留原位，继续从原路径播放。")
    payload = dict(entries=entries, sections=sections, updates=updates, old_settings=old,
                   settings=settings, disks=disks, warnings=warnings, database=str(Path(store.path).absolute()))
    signature = {key: value for key, value in payload.items() if key != "disks"}
    payload["plan_id"] = hashlib.sha256(json.dumps(signature, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return payload


def public_plan(plan: dict) -> dict:
    return {key: plan[key] for key in ("plan_id", "sections", "disks", "warnings")} | {
        "files_total": len(plan["entries"]), "bytes_total": sum(e["size"] for e in plan["entries"]),
    }


def _update_paths(store, updates, *, restore=False):
    with store.connect() as connection:
        for item in updates:
            connection.execute("UPDATE videos SET video_path = ?, audio_path = ? WHERE id = ?",
                               (item["old_video"] if restore else item["video"],
                                item["old_audio"] if restore else item["audio"], item["id"]))


def _cleanup_sources(entries, checkpoint=lambda: None):
    """Only delete inventory files whose bytes and identity still match the verified copy."""
    warnings, parents = [], set()
    for entry in entries:
        checkpoint()
        source, target = Path(entry["source"]), Path(entry["target"])
        if not source.exists():
            continue
        try:
            if (list(_identity(source)) != list(entry["identity"])
                    or _hash(source) != entry["sha256"] or _hash(target) != entry["sha256"]):
                raise ValueError("文件在迁移期间发生变化")
            # Unlink only the exact inventoried regular file, never a directory tree.
            source.unlink()
            parent, boundary = source.parent, Path(entry["source_root"])
            while parent.is_relative_to(boundary):
                parents.add(parent)
                if parent == boundary:
                    break
                parent = parent.parent
        except (OSError, ValueError) as exc:
            warnings.append(f"已切换到新目录，但原文件保留：{source}（{exc}）")
    for directory in sorted(parents, key=lambda p: len(p.parts), reverse=True):
        try:
            _safe(directory).rmdir()  # Nonempty/unrelated directories are always retained.
        except (OSError, ValueError):
            pass
    return warnings


def migrate(project: Path, store, values: dict, *, expected_plan=None,
            report=lambda detail: None, checkpoint=lambda: None) -> dict:
    """Copy -> hash verify -> publish -> persist references -> remove originals.

    The journal enables conservative recovery after process death. Python
    environments, database files, configuration and historical logs are excluded.
    """
    from classcue.asr_models import OPERATION_LOCK

    configuration = project / "data" / "asr_storage.json"
    journal_path = project / "data" / "storage_migration.json"
    created, temporaries = [], []
    committed = False
    journal = None
    with ExitStack() as stack:
        if not OPERATION_LOCK.acquire(blocking=False):
            raise ValueError("ASR 正在使用中，请等待任务结束再迁移")
        stack.callback(OPERATION_LOCK.release)
        configuration.parent.mkdir(parents=True, exist_ok=True)
        stack.enter_context(FileLock(str(configuration.with_suffix(".lock")), timeout=0))
        stack.enter_context(FileLock(str(Path(store.path).parent / "video-library.lock"), timeout=0))
        old_config_bytes = configuration.read_bytes() if configuration.exists() else None
        assets = asr_storage.storage_root(project)
        if str(assets) != values["asr_storage_root"]:
            from classcue.asr_models import MODELS, RETIRED_MODELS
            locks = assets / "models" / "asr" / ".locks"
            if locks.parent.exists():
                _safe(locks).mkdir(exist_ok=True)
                for model_id in (*MODELS, *RETIRED_MODELS):
                    stack.enter_context(FileLock(str(_safe(locks / f"{model_id}.lock")), timeout=0))
            runtime = assets / ".runtime"
            if runtime.exists():
                for lock in runtime.glob("*setup.lock"):
                    stack.enter_context(FileLock(str(_safe(lock)), timeout=0))
        plan = plan_migration(project, store, values)
        if expected_plan is not None and plan["plan_id"] != expected_plan:
            raise ValueError("目录内容或设置已变化，请重新检查迁移范围")
        checkpoint()
        journal = {**plan, "phase": "copying", "job_id": values.get("_job_id"),
                   "temporary_files": [str(Path(e["target"]).with_name(f".migration-{uuid4().hex}.part"))
                                       for e in plan["entries"]]}
        _write_json(journal_path, journal)
        total = sum(e["size"] for e in plan["entries"])
        done = 0
        detail = dict(phase="copying", files_total=len(plan["entries"]), files_completed=0,
                      bytes_total=total, bytes_completed=0, warnings=plan["warnings"])
        try:
            for index, entry in enumerate(plan["entries"]):
                checkpoint()
                source, target = Path(entry["source"]), Path(entry["target"])
                if list(_identity(source)) != list(entry["identity"]):
                    raise ValueError("原文件已变化，请重新检查迁移范围")
                _safe(target.parent).mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                if target.exists():
                    source_hash = _hash(source, checkpoint)
                    if _hash(target, checkpoint) != source_hash:
                        raise ValueError(f"目标目录已有不同内容的同名文件，未覆盖：{target}")
                    entry["sha256"] = source_hash
                else:
                    temporary = Path(journal["temporary_files"][index])
                    temporaries.append(temporary)
                    with source.open("rb") as stream, temporary.open("xb") as output:
                        while chunk := stream.read(CHUNK):
                            checkpoint()
                            output.write(chunk)
                            digest.update(chunk)
                            detail.update(current_file=source.name, bytes_completed=done + output.tell())
                            report(detail)
                        output.flush()
                        os.fsync(output.fileno())
                    detail.update(phase="verifying", current_file=source.name)
                    report(detail)
                    entry["sha256"] = digest.hexdigest()
                    if _hash(temporary, checkpoint) != entry["sha256"]:
                        raise ValueError(f"复制校验失败，原文件保留：{source}")
                    shutil.copystat(source, temporary)
                    # Publish without ever replacing a preexisting destination.
                    if os.name == "nt":
                        temporary.rename(_safe(target))
                    else:
                        os.link(temporary, _safe(target))
                        temporary.unlink()
                    created.append((target, _identity(target)))
                if list(_identity(source)) != list(entry["identity"]):
                    raise ValueError("原文件在复制过程中发生变化，迁移已停止")
                done += entry["size"]
                detail.update(phase="copying", bytes_completed=done,
                              files_completed=detail["files_completed"] + 1)
                report(detail)
            checkpoint()
            # Recheck all sources before the commit boundary, including zero-byte files.
            for entry in plan["entries"]:
                if list(_identity(Path(entry["source"]))) != list(entry["identity"]):
                    raise ValueError("原文件在迁移期间发生变化，未切换目录")
            journal.update(phase="committing", entries=plan["entries"])
            _write_json(journal_path, journal)
            detail.update(phase="committing", current_file=None)
            report(detail)
            # Retain the established ACL/write probes and configuration validation.
            asr_storage.save_directories(
                project, Path(store.path).parent, asset_root=values["asr_storage_root"],
                audio_cache_dir=values.get("audio_cache_dir"), log_dir=values["log_dir"],
                video_library_dir=values.get("video_library_dir"),
                media_storage_root=values.get("media_storage_root"),
                delete_source_after_import=values.get("delete_source_after_import"),
                _already_locked=True,
            )
            _write_json(configuration, plan["settings"])
            _update_paths(store, plan["updates"])
            journal["phase"] = "committed"
            _write_json(journal_path, journal)
            committed = True
            # From here cancellation must not claim the move failed or restore old paths.
            detail.update(phase="cleaning")
            report(detail)
            warnings = plan["warnings"] + _cleanup_sources(plan["entries"])
            journal.update(phase="completed", warnings=warnings)
            _write_json(journal_path, journal)
            detail.update(phase="completed", warnings=warnings)
            report(detail)
            return {"committed": True, "message": "目录迁移完成", "warnings": warnings}
        except BaseException:
            if committed:
                # New references are durable; keep originals if cleanup cannot finish.
                return {"committed": True, "message": "目录已迁移，部分原文件保留；重启后检查清理结果"}
            # Neither a failed copy nor failed persistence may strand the library.
            _update_paths(store, plan["updates"], restore=True)
            if old_config_bytes is None:
                configuration.unlink(missing_ok=True)
            else:
                # Preserve the original configuration bytes, including unrelated preferences.
                with NamedTemporaryFile(dir=configuration.parent, delete=False) as stream:
                    restore_path = Path(stream.name)
                    stream.write(old_config_bytes)
                    stream.flush()
                    os.fsync(stream.fileno())
                restore_path.replace(configuration)
            for path, identity in reversed(created):
                try:
                    if _identity(path) == identity:
                        path.unlink()
                except (OSError, ValueError):
                    pass
            journal["phase"] = "failed"
            _write_json(journal_path, journal)
            raise
        finally:
            for path in temporaries:
                try:
                    _safe(path).unlink(missing_ok=True)
                except OSError:
                    pass


def recover_migration(project: Path, store) -> None:
    """Run under the database lease before accepting HTTP requests."""
    path = project / "data" / "storage_migration.json"
    if not path.exists():
        return
    journal = json.loads(path.read_text(encoding="utf-8"))
    if journal["phase"] in {"completed", "failed", "interrupted"}:
        return
    if journal["database"] != str(Path(store.path).absolute()):
        raise ValueError("迁移记录属于另一个视频库，未改动文件")
    if journal["phase"] == "committed":
        warnings = journal["warnings"] + _cleanup_sources(journal["entries"])
        journal.update(phase="completed", warnings=warnings)
        message, status = "目录迁移完成，已恢复中断的清理", "completed"
    else:
        # A crash before the durable commit keeps all originals. Verified
        # destination copies may remain and can be safely reused on retry.
        if journal["phase"] == "committing":
            _write_json(project / "data" / "asr_storage.json", journal["old_settings"])
            _update_paths(store, journal["updates"], restore=True)
        journal["phase"] = "interrupted"
        message, status = "迁移被中断，原文件与原目录设置已保留；可重新检查后重试", "interrupted"
    for name in journal.get("temporary_files", []):
        temporary = Path(name)
        if (temporary.name.startswith(".migration-") and temporary.suffix == ".part"
                and any(temporary.parent == Path(e["target"]).parent for e in journal["entries"])):
            _safe(temporary).unlink(missing_ok=True)
    _write_json(path, journal)
    if job_id := journal.get("job_id"):
        store.update_job(job_id, status=status, stage=status, message=message,
                         progress=1 if status == "completed" else 0)
