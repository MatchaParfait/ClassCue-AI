"""Persistent machine-local directories for ASR assets, audio caches and logs."""

from __future__ import annotations

import json
from functools import lru_cache
from contextlib import nullcontext
from pathlib import Path
from tempfile import NamedTemporaryFile


def read_configuration(project_root: Path) -> dict:
    configuration = project_root / "data" / "asr_storage.json"
    if not configuration.is_file():
        return {}
    try:
        value = json.loads(configuration.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Expected a directory configuration object")
        return value
    except (OSError, ValueError) as exc:
        raise ValueError(f"Invalid local ASR storage configuration: {configuration}") from exc


def application_config_directory(project_root: Path) -> Path:
    value = read_configuration(project_root).get("application_data_root")
    return validate_directory(value) / "config" if value is not None else project_root / "data"


def library_identity(project_root: Path, database: Path) -> str:
    import hashlib
    database = database.resolve()
    saved = read_configuration(project_root).get("library_identity", {})
    if (isinstance(saved, dict) and isinstance(saved.get("database"), str)
            and Path(saved["database"]).resolve() == database
            and isinstance(saved.get("id"), str) and len(saved["id"]) == 12
            and all(char in "0123456789abcdef" for char in saved["id"])):
        return saved["id"]
    return hashlib.sha256(str(database).encode("utf-8")).hexdigest()[:12]


def library_scope(project_root: Path, database: Path) -> str:
    saved = read_configuration(project_root).get("library_identity", {})
    if (isinstance(saved, dict) and isinstance(saved.get("database"), str)
            and Path(saved["database"]).resolve() == database.resolve()):
        return str(saved.get("scope") or database.parent)
    return str(database.parent)


def validate_directory(value: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("目录路径不能为空")
    path = Path(value.strip()).expanduser()
    if not path.is_absolute() or path.resolve() != path.absolute() or path == Path(path.anchor):
        raise ValueError("请选择绝对目录路径，不能使用磁盘根目录、符号链接或目录联接")
    if path.exists() and not path.is_dir():
        raise ValueError("所选路径不是目录")
    return path


def storage_root(project_root: Path) -> Path:
    settings = read_configuration(project_root)
    if "storage_root" not in settings:
        return project_root
    try:
        return validate_directory(settings["storage_root"])
    except (ValueError, KeyError, TypeError) as exc:
        configuration = project_root / "data" / "asr_storage.json"
        raise ValueError(f"Invalid local ASR storage configuration: {configuration}") from exc


@lru_cache(maxsize=None)
def active_storage_root(project_root: Path) -> Path:
    """Keep model operations on their startup paths until this process restarts."""
    return storage_root(project_root)


def configured_audio_directory(project_root: Path, data_root: Path) -> Path | None:
    if root := configured_media_root(project_root):
        return root / "audio"
    value = read_configuration(project_root).get("audio_cache_dir")
    return validate_directory(value) if value is not None else None


def audio_directory(project_root: Path, data_root: Path) -> Path:
    return configured_audio_directory(project_root, data_root) or data_root / "temporary_audio"


def video_directory(project_root: Path, data_root: Path) -> Path:
    if root := configured_media_root(project_root):
        return root / "videos"
    value = read_configuration(project_root).get("video_library_dir")
    return validate_directory(value) if value is not None else data_root / "videos"


def configured_media_root(project_root: Path) -> Path | None:
    value = read_configuration(project_root).get("media_storage_root")
    return validate_directory(value) if value is not None else None


def resolve_media_directories(project_root: Path, data_root: Path, *,
                              media_storage_root: str | None = None,
                              video_library_dir: str | None = None,
                              audio_cache_dir: str | None = None) -> tuple[Path | None, Path, Path]:
    """A bound library always moves videos/audio together; old settings stay readable."""
    root = validate_directory(media_storage_root) if media_storage_root is not None else configured_media_root(project_root)
    if root is not None:
        videos, audio = root / "videos", root / "audio"
        for supplied, expected in ((video_library_dir, videos), (audio_cache_dir, audio)):
            if supplied is not None and validate_directory(supplied) != expected:
                raise ValueError("视频与音频目录已绑定，请选择统一存储目录，不能单独修改子目录")
        return root, videos, audio
    videos = validate_directory(video_library_dir) if video_library_dir is not None else video_directory(project_root, data_root)
    audio = validate_directory(audio_cache_dir) if audio_cache_dir is not None else data_root / "temporary_audio"
    return None, videos, audio


def delete_source_after_import(project_root: Path, data_root: Path) -> bool:
    value = read_configuration(project_root).get("delete_source_after_import", True)
    if not isinstance(value, bool):
        raise ValueError("视频导入设置必须为布尔值")
    return value


def validate_video_directory(videos: Path, *temporary_directories: Path) -> None:
    for temporary in temporary_directories:
        if videos == temporary or videos.is_relative_to(temporary) or temporary.is_relative_to(videos):
            raise ValueError("视频库目录必须与缓存、模型、运行环境和日志目录分开")


def log_directory(project_root: Path) -> Path:
    value = read_configuration(project_root).get("log_dir")
    return validate_directory(value) if value is not None else project_root / ".runtime" / "logs"


def save_directories(project_root: Path, data_root: Path, *,
                     asset_root: str, audio_cache_dir: str | None, log_dir: str,
                     video_library_dir: str | None = None,
                     media_storage_root: str | None = None,
                     delete_source_after_import: bool | None = None,
                     _already_locked: bool = False) -> None:
    from filelock import FileLock

    assets = validate_directory(asset_root)
    media, videos, audio = resolve_media_directories(
        project_root, data_root, media_storage_root=media_storage_root,
        video_library_dir=video_library_dir, audio_cache_dir=audio_cache_dir,
    )
    logs = validate_directory(log_dir)
    validate_video_directory(videos, audio, logs, assets / "models" / "asr", assets / ".runtime",
                             project_root / ".runtime")
    for directory in (assets, audio, logs, videos):
        directory.mkdir(parents=True, exist_ok=True)
        # Probe actual write access, including ACLs; leave no probe files behind.
        with NamedTemporaryFile(dir=directory):
            pass
    configuration = project_root / "data" / "asr_storage.json"
    configuration.parent.mkdir(parents=True, exist_ok=True)
    with nullcontext() if _already_locked else FileLock(str(configuration.with_suffix(".lock")), timeout=10):
        settings = read_configuration(project_root)
        settings.update(storage_root=str(assets), log_dir=str(logs), audio_cache_dir=str(audio),
                        video_library_dir=str(videos))
        if media is not None:
            settings["media_storage_root"] = str(media)
        if delete_source_after_import is not None:
            if not isinstance(delete_source_after_import, bool):
                raise ValueError("视频导入设置必须为布尔值")
            settings["delete_source_after_import"] = delete_source_after_import
        temporary = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=configuration.parent,
                                    prefix="storage-", suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                json.dump(settings, output, ensure_ascii=False)
            temporary.replace(configuration)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
