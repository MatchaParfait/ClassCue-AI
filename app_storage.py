"""Paths for the application's single video library and local preferences."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from asr_storage import application_config_directory, audio_directory, read_configuration, validate_directory


ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")
DATA_DIR = application_config_directory(ROOT)
TRIGGER_LIBRARY_PATH = DATA_DIR / "video_trigger_library.json"
DEEPSEEK_API_KEY_PATH = DATA_DIR / "deepseek_api_key.dpapi"
DEFAULT_LIBRARY_ROOT = ROOT / "data" / "library"


def load_data_root() -> Path:
    # The environment override isolates development/test installations. There is
    # no runtime API, project registry or UI for selecting another library.
    override = os.getenv("CLASSROOM_DATA_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    configured = read_configuration(ROOT).get("library_data_dir")
    return validate_directory(configured).resolve() if configured is not None else DEFAULT_LIBRARY_ROOT.resolve()


def storage_paths(data_root: Path) -> tuple[Path, Path]:
    return data_root / "project.db", audio_directory(ROOT, data_root)


def active_storage_paths() -> tuple[Path, Path, Path]:
    root = load_data_root()
    database, audio = storage_paths(root)
    audio.mkdir(parents=True, exist_ok=True)
    return root, database, audio
