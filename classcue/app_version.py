"""The Python project metadata is the release version's single source of truth."""

from classcue.paths import PROJECT_ROOT

from pathlib import Path
import tomllib


VERSION = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
