"""The Python project metadata is the release version's single source of truth."""

from pathlib import Path
import tomllib


VERSION = tomllib.loads((Path(__file__).parent / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
