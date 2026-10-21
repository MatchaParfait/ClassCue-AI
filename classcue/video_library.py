"""Import videos into durable storage without deleting an unverified source."""

from __future__ import annotations

import os
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


def register_managed_video(
    store: VideoStore, raw_path: str | Path, audio_dir: Path, library_dir: Path,
    filename: str | None = None,
    *,
    on_registered: Callable[[int], None] | None = None,
    delete_source_after_import: bool = True,
    source_folder: str | None = None,
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
        if current and source.is_relative_to(library_dir) and name == source.name:
            video_id, path, existed = video_pipeline.register_local_video(store, source, audio_dir)
            if on_registered:
                on_registered(video_id)
            return video_id, path, existed, None

        digest = store.file_hash(source)
        existing = store.get_video_by_hash(digest)
        destination = library_dir / digest / name
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
        if destination != source:
            if destination.exists():
                if destination.is_symlink() or not destination.is_file() or store.file_hash(destination) != digest:
                    raise ValueError("视频库中存在不同内容的同名文件，未覆盖任何文件")
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
                    if store.file_hash(temporary) != digest:
                        raise ValueError("视频复制校验失败，原文件未移动")
                    # Exclusive creation prevents overwriting a file that appeared during copying.
                    if os.name == "nt":
                        # Windows rename is atomic and refuses an existing destination, including on exFAT.
                        temporary.rename(destination)
                    else:
                        os.link(temporary, destination)
                    created = True
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)

        registered = False
        try:
            video_id, path, existed = video_pipeline.register_local_video(store, destination, audio_dir)
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
        if destination != source and delete_source_after_import:
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
