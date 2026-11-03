"""Import videos into durable storage without deleting an unverified source."""

from __future__ import annotations

import os
import json
from pathlib import Path
import re
import shutil
from tempfile import NamedTemporaryFile
from uuid import uuid4
from typing import Callable

from filelock import FileLock

import classcue.asr_storage as asr_storage
import classcue.video_pipeline as video_pipeline
from classcue.video_store import VideoStore


def video_filename(value: str | None, source: Path) -> str:
    if value is None:
        return source.name
    name = value.strip()
    if not name or name in {".", ".."} or len(name) > 200:
        raise ValueError("文件名不能为空，且不能超过 200 个字符")
    if re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or name.endswith((".", " ")):
        raise ValueError("文件名不能包含路径、特殊字符或末尾的点与空格")
    if name.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(
        f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
    )}:
        raise ValueError("不能使用系统保留的文件名")
    suffix = source.suffix
    if not name.lower().endswith(suffix.lower()):
        if Path(name).suffix.lower() in video_pipeline.SUPPORTED_VIDEO_SUFFIXES:
            raise ValueError("修改文件名时不能更改视频格式")
        name += suffix
    return name


def hash_video_source(store: VideoStore, source: Path) -> tuple[str, os.stat_result]:
    before = source.stat()
    digest = store.file_hash(source)
    if video_pipeline._file_identity(before) != video_pipeline._file_identity(source.stat()):
        raise ValueError("视频在读取期间发生变化，请重新导入，原文件已保留")
    return digest, before


def _rename_journal(store: VideoStore) -> Path:
    return Path(store.path).parent / "video-library-rename.json"


def _save_rename_journal(store: VideoStore, value: dict) -> None:
    journal = _rename_journal(store)
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=journal.parent, delete=False) as output:
            temporary = Path(output.name)
            json.dump(value, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(journal)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def _clear_rename_journal(store: VideoStore) -> None:
    try:
        _rename_journal(store).unlink(missing_ok=True)
    except OSError:
        try:
            # Completed records must not replay after a later import, deletion or migration.
            _save_rename_journal(store, {"completed": True})
        except OSError:
            pass


def _recover_managed_rename(store: VideoStore) -> None:
    journal = _rename_journal(store)
    if not journal.is_file():
        return
    record = json.loads(journal.read_text(encoding="utf-8"))
    if not isinstance(record, dict):
        raise ValueError("视频改名恢复记录格式无效，未移动任何文件")
    if record.get("completed") is True:
        _clear_rename_journal(store)
        return
    source, target, intermediate = (Path(record[key]) for key in ("source", "target", "intermediate"))
    video = store.get_video(record["video_id"])
    if (video is None or video["content_hash"] != record["digest"]
            or video["video_path"] not in {str(source), str(target)}
            or not source.is_absolute() or source.parent != target.parent or source.parent != intermediate.parent
            or source.parent.resolve() != source.parent
            or target.name != video_filename(target.name, source)
            or not intermediate.name.startswith(".rename-") or intermediate.suffix != ".tmp"):
        raise ValueError("视频改名恢复记录不一致，未移动任何文件")
    names = {entry.name for entry in source.parent.iterdir()}
    candidates = [path for path in (source, target, intermediate) if path.name in names]
    if len(candidates) != 1:
        raise ValueError("视频改名恢复时发现文件缺失或名称冲突，未覆盖任何文件")
    actual = candidates[0]
    if actual.is_symlink() or not actual.is_file() or hash_video_source(store, actual)[0] != record["digest"]:
        raise ValueError("视频改名恢复时内容已变化，原文件已保留")
    expected = Path(video["video_path"])
    if actual.name != expected.name:
        actual.rename(expected)
    _clear_rename_journal(store)


def recover_managed_rename(store: VideoStore) -> None:
    with FileLock(str(Path(store.path).parent / "video-library.lock"), timeout=30):
        _recover_managed_rename(store)


def rename_managed_video(store: VideoStore, video_id: int, filename: str, library_dir: Path) -> Path | None:
    """Rename a managed recording in place, compensating if the database commit fails."""
    library_dir = asr_storage.validate_directory(str(library_dir))
    with FileLock(str(Path(store.path).parent / "video-library.lock"), timeout=30):
        _recover_managed_rename(store)
        video = store.get_video(video_id)
        if video is None:
            raise ValueError("视频不存在")
        source = Path(video["video_path"]).absolute()
        if source.parent != library_dir / video["content_hash"]:
            return None
        if source.resolve() != source or source.is_symlink() or not source.is_file():
            raise ValueError("请使用真实视频文件，视频源文件不存在或位置已变化")
        name = video_filename(filename, source)
        digest, before = hash_video_source(store, source)
        if digest != video["content_hash"]:
            raise ValueError("视频内容已变化，请先重新导入，未修改文件名")
        target = source.with_name(name)
        if name == source.name:
            return source
        if target != source and target.exists():
            raise ValueError("视频库中已存在同名文件，未覆盖任何文件")
        intermediate = source.with_name(f".rename-{uuid4().hex}.tmp")
        _save_rename_journal(store, {
            "video_id": video_id, "digest": digest, "source": str(source),
            "target": str(target), "intermediate": str(intermediate),
        })
        moved = False
        try:
            with store.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
                if current is None or current["video_path"] != video["video_path"] or current["content_hash"] != digest:
                    raise ValueError("视频记录已变化，请重新读取后再改名")
                if target == source:
                    source.rename(intermediate)
                    try:
                        intermediate.rename(target)
                    except BaseException:
                        intermediate.rename(source)
                        raise
                else:
                    source.rename(target)
                moved = True
                after = target.stat()
                if video_pipeline._file_identity(before)[:-1] != video_pipeline._file_identity(after)[:-1]:
                    raise ValueError("视频在改名期间发生变化，请重新导入")
                connection.execute(
                    """UPDATE videos SET video_path = ?, filename = ?, display_name = ?,
                       source_size = ?, source_mtime_ns = ? WHERE id = ?""",
                    (str(target), target.name, target.stem, after.st_size, after.st_mtime_ns, video_id),
                )
        except BaseException:
            if moved:
                target.rename(source)
            elif intermediate.exists():
                intermediate.rename(source)
            _clear_rename_journal(store)
            raise
        _clear_rename_journal(store)
        return target


def register_managed_video(
    store: VideoStore, raw_path: str | Path, audio_dir: Path, library_dir: Path,
    filename: str | None = None,
    *,
    on_registered: Callable[[int], None] | None = None,
    delete_source_after_import: bool = True,
    source_folder: str | None = None,
    secondary_only: bool = False,
    validate_content: Callable[[str], None] | None = None,
    _verified_source: tuple[str, os.stat_result] | None = None,
    _consume_upload: bool = False,
) -> tuple[int, Path, bool, str | None]:
    source = Path(raw_path).expanduser().absolute()
    if source.resolve() != source or source.is_symlink():
        raise ValueError("请使用真实视频文件，不能移动符号链接或目录联接中的文件")
    if not source.is_file():
        raise ValueError("找不到该本机视频文件")
    if source.suffix.lower() not in video_pipeline.SUPPORTED_VIDEO_SUFFIXES:
        raise ValueError("不支持该视频文件格式")
    before = source.stat()
    if before.st_size > video_pipeline.MAX_VIDEO_BYTES:
        raise ValueError("视频超过 1GB，请压缩后再识别")
    name = video_filename(filename, source)
    library_dir = asr_storage.validate_directory(str(library_dir))
    library_dir.mkdir(parents=True, exist_ok=True)

    # Serialize registration and renaming, including clients in another process.
    with FileLock(str(Path(store.path).parent / "video-library.lock"), timeout=30):
        current = store.get_video_by_path(source)
        if current and source.is_relative_to(library_dir) and name == source.name and validate_content is None and _verified_source is None:
            video_id, path, existed = video_pipeline.register_local_video(
                store, source, audio_dir, secondary_only=secondary_only,
            )
            if on_registered:
                on_registered(video_id)
            return video_id, path, existed, None

        if _verified_source is None:
            digest, before = hash_video_source(store, source)
        else:
            digest, verified_source_stat = _verified_source
            before = source.stat()
            if video_pipeline._file_identity(before) != video_pipeline._file_identity(verified_source_stat):
                raise ValueError("视频在校验后发生变化，请重新导入，原文件已保留")
        if validate_content:
            validate_content(digest)
        existing = store.get_video_by_hash(digest)
        destination = library_dir / digest / name
        verified_stat = before
        if destination.parent.resolve() != destination.parent:
            raise ValueError("视频库中的目标目录不能是符号链接或目录联接")
        destination.parent.mkdir(parents=True, exist_ok=True)
        created = False
        if destination == source and destination.name != source.name:
            intermediate = source.with_name(f".rename-{uuid4().hex}.tmp")
            source.rename(intermediate)
            try:
                intermediate.rename(destination)
            except Exception:
                intermediate.rename(source)
                raise
            verified_stat = destination.stat()
        if destination != source:
            if destination.exists():
                verified_stat = destination.stat()
                if destination.is_symlink() or not destination.is_file() or store.file_hash(destination) != digest:
                    raise ValueError("视频库中存在不同内容的同名文件，未覆盖任何文件")
                if video_pipeline._file_identity(verified_stat) != video_pipeline._file_identity(destination.stat()):
                    raise ValueError("视频库文件在校验期间发生变化，请重新导入")
            elif _consume_upload and before.st_dev == destination.parent.stat().st_dev:
                # Only server-owned upload temporaries may be consumed without a second copy.
                if video_pipeline._file_identity(before) != video_pipeline._file_identity(source.stat()):
                    raise ValueError("视频在校验后发生变化，请重新上传")
                source.rename(destination)
                created = True
                try:
                    verified_stat = destination.stat()
                    if video_pipeline._file_identity(before)[:-1] != video_pipeline._file_identity(verified_stat)[:-1]:
                        raise ValueError("视频在归档期间发生变化，请重新上传")
                except BaseException:
                    destination.unlink(missing_ok=True)
                    raise
            else:
                if shutil.disk_usage(library_dir).free < before.st_size:
                    raise ValueError("视频库所在磁盘空间不足，原文件未移动")
                temporary = None
                try:
                    with NamedTemporaryFile(dir=destination.parent, suffix=".part", delete=False) as output:
                        temporary = Path(output.name)
                        with source.open("rb") as input_file:
                            shutil.copyfileobj(input_file, output, length=4 * 1024 * 1024)
                        output.flush()
                        os.fsync(output.fileno())
                    copied_stat = temporary.stat()
                    if store.file_hash(temporary) != digest:
                        raise ValueError("视频复制校验失败，原文件未移动")
                    if video_pipeline._file_identity(copied_stat) != video_pipeline._file_identity(temporary.stat()):
                        raise ValueError("视频副本在校验期间发生变化，原文件未移动")
                    # Exclusive creation prevents overwriting a file that appeared during copying.
                    if os.name == "nt":
                        # Windows rename is atomic and refuses an existing destination, including on exFAT.
                        temporary.rename(destination)
                    else:
                        os.link(temporary, destination)
                    created = True
                    verified_stat = destination.stat()
                    # Renaming may change ctime; the copied file itself must stay identical.
                    if video_pipeline._file_identity(copied_stat)[:-1] != video_pipeline._file_identity(verified_stat)[:-1]:
                        destination.unlink(missing_ok=True)
                        raise ValueError("视频副本在校验后发生变化，原文件未移动")
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)

        registered = False
        try:
            video_id, path, existed = video_pipeline.register_local_video(
                store, destination, audio_dir, secondary_only=secondary_only,
                _verified_content=(digest, verified_stat),
            )
            registered = True
            original_folder = str(source.parent) if not source.is_relative_to(library_dir) else None
            if source_folder is not None:
                original_folder = existing["source_folder"] if existing else source_folder
            if existing and source == Path(existing["video_path"]):
                original_folder = existing["source_folder"]
            if original_folder is None and existing:
                original_folder = existing["source_folder"]
            store.update_video_file(video_id, path, source_folder=original_folder)
            if on_registered:
                on_registered(video_id)
        except Exception:
            # Once registration commits, retain the valid library copy even if metadata fails.
            if created and not registered:
                destination.unlink(missing_ok=True)
            raise

        warning = None
        if destination != source and delete_source_after_import and not (_consume_upload and not source.exists()):
            try:
                after = source.stat()
                if (before.st_size, before.st_mtime_ns, before.st_ino) != (
                    after.st_size, after.st_mtime_ns, after.st_ino
                ):
                    warning = "视频已存入视频库，但原文件在导入期间发生变化，已保留原文件"
                else:
                    source.unlink()
            except OSError:
                warning = "视频已存入视频库，但原文件无法删除，仍保留在原位置"
        return video_id, path, existed, warning
