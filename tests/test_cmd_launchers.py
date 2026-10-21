import os
from pathlib import Path
import shutil
import subprocess
import time
import tomllib

import pytest


ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "\u8bfe\u5802\u91cd\u70b9\u590d\u4e60"
START = f"\u542f\u52a8{APP_NAME}.cmd"
STOP = f"\u505c\u6b62{APP_NAME}.cmd"
RESTART = f"\u91cd\u542f{APP_NAME}.cmd"
FRONTEND = "\u542f\u52a8\u672c\u5730\u524d\u7aef.cmd"
LAUNCHERS = [
    (START, "activate", r'scripts\desktop_launcher.vbs'),
    (f"scripts/{STOP}", "exit", "desktop_launcher.vbs"),
    (f"scripts/{RESTART}", "restart", "desktop_launcher.vbs"),
    (f"scripts/{FRONTEND}", "start", "webui.ps1"),
]


def test_only_primary_launcher_is_exposed_at_repository_root():
    assert {path.name for path in ROOT.glob("*.cmd")} == {START}
    assert all((ROOT / "scripts" / name).is_file() for name in (STOP, RESTART, FRONTEND))


def test_release_includes_maintenance_launchers_but_not_developer_frontend():
    files = set(tomllib.loads((ROOT / "release_manifest.toml").read_text(encoding="utf-8"))["files"])
    assert {START, f"scripts/{STOP}", f"scripts/{RESTART}"} <= files
    assert not {STOP, RESTART, FRONTEND, f"scripts/{FRONTEND}"} & files


@pytest.mark.parametrize("name,action,target", LAUNCHERS)
def test_cmd_routes_to_script_relative_to_its_own_location(name, action, target):
    text = (ROOT / name).read_text(encoding="utf-8")
    directory = "%~dp0.." if name.startswith("scripts/") else "%~dp0"
    assert f'cd /d "{directory}"' in text
    assert f'"%~dp0{target}"' in text
    if target.endswith(".vbs"):
        suffix = "" if action == "activate" else f" {action}"
        assert f'wscript.exe "%~dp0{target}"{suffix}' in text.splitlines()
    else:
        assert f'-File "%~dp0{target}" -Action {action}' in text
        assert "if errorlevel 1 (" in text
        assert "exit /b 1" in text


@pytest.mark.skipif(os.name != "nt", reason="Windows CMD entry points")
@pytest.mark.parametrize("name,action,target", LAUNCHERS)
def test_cmd_launches_from_unrelated_directory_with_spaces(tmp_path, name, action, target):
    project = tmp_path / "installation with spaces"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    unrelated = tmp_path / "unrelated working directory"
    unrelated.mkdir()
    launcher = project / name
    shutil.copyfile(ROOT / name, launcher)

    # Replace both backends: no real app, runtime, or service is touched.
    (scripts / "desktop_launcher.vbs").write_text(
        'Set fs = CreateObject("Scripting.FileSystemObject")\n'
        'Set shell = CreateObject("WScript.Shell")\n'
        'project = fs.GetParentFolderName(fs.GetParentFolderName(WScript.ScriptFullName))\n'
        'Set output = fs.CreateTextFile(project & "\\launch-result.txt", True, True)\n'
        'output.WriteLine shell.CurrentDirectory\n'
        'action = "activate"\n'
        'If WScript.Arguments.Count > 0 Then action = WScript.Arguments(0)\n'
        'output.WriteLine action\n'
        'output.Close\n',
        encoding="ascii",
    )
    (scripts / "webui.ps1").write_text(
        'param([string]$Action)\n'
        '[System.IO.File]::WriteAllLines(\n'
        '    (Join-Path $PSScriptRoot "..\\launch-result.txt"),\n'
        '    @((Get-Location).Path, $Action), [System.Text.Encoding]::Unicode)\n',
        encoding="ascii",
    )
    subprocess.run(
        ["cmd.exe", "/d", "/c", str(launcher)],
        cwd=unrelated, capture_output=True, check=True, timeout=15,
    )
    result = project / "launch-result.txt"
    deadline = time.monotonic() + 10
    lines = []
    while time.monotonic() < deadline:
        try:
            lines = result.read_text(encoding="utf-16").splitlines()
        except (OSError, UnicodeError):
            pass
        if len(lines) == 2:
            break
        time.sleep(0.05)
    assert len(lines) == 2, f"Launcher did not report its working directory and action: {lines}"
    assert Path(lines[0]).resolve() == project.resolve()
    assert lines[1] == action
