import ast
import json
from pathlib import Path
import subprocess
import tomllib

import pytest

from scripts.release import source_files


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def source_repository(tmp_path):
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    (tmp_path / "app.py").write_text("# application\n", encoding="utf-8")
    return tmp_path


def write_manifest(root, entries, *, schema=1, stage=True):
    (root / "release_manifest.toml").write_text(
        f"schema = {schema}\nfiles = " + json.dumps(entries), encoding="utf-8",
    )
    if stage:
        subprocess.run(["git", "add", "--", "release_manifest.toml", "app.py"],
                       cwd=root, check=True)


def test_source_selection_uses_manifest_not_tracked_directory_contents(source_repository):
    root = source_repository
    write_manifest(root, ["app.py"])
    for name in ("private.py", "scripts/benchmark.py", "docs/notes.md", "Agent.md"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("private", encoding="utf-8")
        subprocess.run(["git", "add", "--", name], cwd=root, check=True)
    assert source_files(root) == {"app.py"}
    assert source_files(root, include_untracked=True) == {"app.py"}


@pytest.mark.parametrize("entries", [
    [], ["app.py", "APP.py"], ["../escape.py"], ["scripts/../../secret.py"],
    [".runtime/private.py"], ["frontend/dist/index.html"], [".release-install.json"],
    ["missing.py"], [42], ["app.py", "app.py"],
])
def test_invalid_or_missing_manifest_entries_fail_closed(source_repository, entries):
    write_manifest(source_repository, entries)
    with pytest.raises(ValueError):
        source_files(source_repository)


def test_unstaged_manifest_is_rejected_by_default(source_repository):
    write_manifest(source_repository, ["app.py"], stage=False)
    with pytest.raises(ValueError, match="manifest must be staged"):
        source_files(source_repository)
    assert source_files(source_repository, include_untracked=True) == {"app.py"}


def test_unstaged_source_requires_explicit_local_acceptance(source_repository):
    root = source_repository
    write_manifest(root, ["app.py", "new_module.py"])
    (root / "new_module.py").write_text("# reviewed new module", encoding="utf-8")
    with pytest.raises(ValueError, match="new_module.py"):
        source_files(root)
    assert source_files(root, include_untracked=True) == {"app.py", "new_module.py"}
    (root / "app.py").unlink()
    with pytest.raises(ValueError, match="app.py"):
        source_files(root, include_untracked=True)


def test_unsupported_manifest_schema_is_rejected(source_repository):
    write_manifest(source_repository, ["app.py"], schema=2)
    with pytest.raises(ValueError, match="Invalid"):
        source_files(source_repository)


def test_manifest_source_symlink_is_rejected(source_repository):
    root = source_repository
    write_manifest(root, ["app.py", "linked.py"])
    try:
        (root / "linked.py").symlink_to(root / "app.py")
    except OSError:
        pytest.skip("Creating symlinks requires platform privileges")
    with pytest.raises(ValueError):
        source_files(root, include_untracked=True)


def test_application_manifest_covers_runtime_sources_and_excludes_development_material():
    entries = tomllib.loads((ROOT / "release_manifest.toml").read_text(encoding="utf-8"))["files"]
    files = set(entries)
    assert len(files) == len(entries)
    assert all((ROOT / name).is_file() for name in files)
    assert not list(ROOT.glob("*.py"))
    assert {path.relative_to(ROOT).as_posix() for path in (ROOT / "classcue").rglob("*.py")} <= files
    source_only_scripts = {
        "build_desktop_icon.py", "check_repository_hygiene.py",
        "prepare_settings_herons.py", "release.py",
    }
    assert {
        path.relative_to(ROOT).as_posix() for path in (ROOT / "scripts").iterdir()
        if path.is_file() and path.name not in source_only_scripts
    } <= files
    assert {path.relative_to(ROOT).as_posix() for path in (ROOT / "prompts").rglob("*")
            if path.is_file()} <= files
    assert {path.relative_to(ROOT).as_posix() for path in (ROOT / "assets").rglob("*")
            if path.is_file()} <= files
    assert {name for name in files if name.startswith("docs/")} == {"docs/dolphin_upstream_LICENSE.txt"}
    assert not any(name.startswith(("tests/", "experiments/", ".github/")) for name in files)
    assert not {f"scripts/{name}" for name in source_only_scripts} & files
    assert {"LICENSE", "scripts/desktop_launcher.vbs", "api/main.py", "classcue/pages/review.py"} <= files

    # Follow local static imports so a new runtime dependency cannot be silently omitted.
    for name in files:
        if not name.endswith(".py"):
            continue
        tree = ast.parse((ROOT / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]
            for module in modules:
                dependency = module.replace(".", "/") + ".py"
                if (ROOT / dependency).is_file():
                    assert dependency in files, f"{name} imports unpackaged {dependency}"
