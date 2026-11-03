import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

from scripts.prepare_desktop_host import prepare_host


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows desktop host")


@pytest.fixture
def installation(tmp_path):
    project = tmp_path / "installation with spaces \u8bfe\u5802"
    (project / "scripts").mkdir(parents=True)
    (project / "assets").mkdir()
    for name in ("scripts/desktop_host.cs", "assets/desktop.ico", "pyproject.toml"):
        shutil.copy2(ROOT / name, project / name)
    return project


def test_host_embeds_python_and_preserves_worker_interpreter(installation):
    project = installation
    (project / "scripts/desktop_app.py").write_text(
        '''
import ctypes
import json
from pathlib import Path
import subprocess
import sys
import webview
import clr

image = ctypes.create_unicode_buffer(32768)
ctypes.windll.kernel32.GetModuleFileNameW(None, image, len(image))
child = subprocess.check_output(
    [sys.executable, "-c", "import sys; print(sys.prefix)"],
    text=True, creationflags=subprocess.CREATE_NO_WINDOW,
).strip()
Path("result.json").write_text(json.dumps({
    "image": image.value, "executable": sys.executable, "prefix": sys.prefix,
    "arguments": sys.argv[1:], "child_prefix": child,
}), encoding="utf-8")
sys.exit(17)
''', encoding="ascii",
    )
    host = prepare_host(project)
    first_modified = host.stat().st_mtime_ns
    assert prepare_host(project) == host
    assert host.stat().st_mtime_ns == first_modified
    result = subprocess.run(
        [str(host), "--desktop", "--action", "restart"],
        cwd=project.parent, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 17
    captured = json.loads((project / "result.json").read_text(encoding="utf-8"))
    assert Path(captured["image"]) == host
    assert Path(captured["executable"]).resolve() == Path(sys.executable).with_name("pythonw.exe").resolve()
    assert Path(captured["prefix"]).resolve() == Path(sys.prefix).resolve()
    assert captured["child_prefix"] == captured["prefix"]
    assert captured["arguments"] == ["--action", "restart"]


def test_rebuild_repairs_binary_and_updates_source(installation):
    host = prepare_host(installation)
    host.write_bytes(b"damaged executable")
    prepare_host(installation)
    assert host.read_bytes().startswith(b"MZ")
    source = installation / "scripts/desktop_host.cs"
    source.write_text(source.read_text(encoding="utf-8") + "\n// build change\n", encoding="utf-8")
    previous = host.stat().st_mtime_ns
    time.sleep(.02)
    prepare_host(installation)
    assert host.stat().st_mtime_ns != previous
    (host.parent / "build.json").write_text("null", encoding="utf-8")
    prepare_host(installation)
    assert json.loads((host.parent / "build.json").read_text(encoding="utf-8"))["exe"]


def test_invalid_host_arguments_do_not_run_application(installation):
    host = prepare_host(installation)
    result = subprocess.run(
        [str(host), "--desktop", "--action", "unknown"],
        timeout=10, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 2


@pytest.mark.parametrize("action", ["activate", "restart"])
def test_existing_launcher_routes_to_host(installation, action):
    project = installation
    host = prepare_host(project)
    shutil.copy2(ROOT / "scripts/desktop_launcher.vbs", project / "scripts/desktop_launcher.vbs")
    (project / ".runtime/desktop_instance.json").write_text("{}", encoding="ascii")
    script = "desktop_activate.py" if action == "activate" else "desktop_app.py"
    (project / "scripts" / script).write_text(
        'import ctypes, json, sys\n'
        'from pathlib import Path\n'
        'image = ctypes.create_unicode_buffer(32768)\n'
        'ctypes.windll.kernel32.GetModuleFileNameW(None, image, len(image))\n'
        'Path("launch.json").write_text(json.dumps([image.value, sys.argv[1:]]))\n',
        encoding="ascii",
    )
    subprocess.run(
        ["cscript.exe", "//nologo", str(project / "scripts/desktop_launcher.vbs"), action],
        cwd=project.parent, capture_output=True, check=True, timeout=10,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    deadline = time.monotonic() + 10
    while not (project / "launch.json").exists() and time.monotonic() < deadline:
        time.sleep(.05)
    image, arguments = json.loads((project / "launch.json").read_text())
    assert Path(image) == host
    assert arguments == ([] if action == "activate" else ["--action", "restart"])


@pytest.mark.parametrize("arguments", [[], ["--activate"]])
@pytest.mark.parametrize("broken_runtime", ["missing_record", "missing_interpreter", "missing_library"])
def test_activation_recovers_stale_runtime_through_preparation(installation, arguments, broken_runtime):
    host = prepare_host(installation)
    runtime = host.parent / "python-runtime.txt"
    if broken_runtime == "missing_record":
        runtime.unlink()
    else:
        paths = runtime.read_text(encoding="utf-8").splitlines()
        paths[0 if broken_runtime == "missing_interpreter" else 1] = str(installation / "missing.exe")
        runtime.write_text("\n".join(paths) + "\n", encoding="utf-8")
    (installation / ".runtime/desktop_instance.json").write_text("{}", encoding="ascii")
    (installation / "scripts/desktop_bootstrap.ps1").write_text(
        '[IO.File]::WriteAllText((Join-Path $PSScriptRoot "../prepared.json"), \'{"prepared":true}\')',
        encoding="ascii",
    )
    result = subprocess.run([str(host), *arguments], timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0
    prepared = installation / "prepared.json"
    deadline = time.monotonic() + 10
    while not prepared.exists() and time.monotonic() < deadline:
        time.sleep(.05)
    assert json.loads(prepared.read_text()) == {"prepared": True}


@pytest.mark.parametrize("action", ["exit", "restart"])
def test_stale_host_runtime_preserves_legacy_control_action(installation, action):
    host = prepare_host(installation)
    (host.parent / "python-runtime.txt").unlink()
    environment = installation / ".venv"
    (environment / "Scripts").mkdir(parents=True)
    shutil.copy2(Path(sys.executable).with_name("pythonw.exe"), environment / "Scripts/pythonw.exe")
    (environment / "pyvenv.cfg").write_text(f"home = {sys.base_prefix}\n", encoding="utf-8")
    (installation / "scripts/desktop_app.py").write_text(
        'import json, sys\nfrom pathlib import Path\n'
        'Path("control.json").write_text(json.dumps(sys.argv[1:]))\n',
        encoding="ascii",
    )
    result = subprocess.run(
        [str(host), "--desktop", "--action", action], timeout=10,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 0
    record = installation / "control.json"
    deadline = time.monotonic() + 10
    while not record.exists() and time.monotonic() < deadline:
        time.sleep(.05)
    assert json.loads(record.read_text()) == ["--action", action]


def test_launcher_restart_without_instance_creates_visible_host_window(installation):
    import ctypes
    from ctypes import wintypes

    host = prepare_host(installation)
    shutil.copy2(ROOT / "scripts/desktop_launcher.vbs", installation / "scripts/desktop_launcher.vbs")
    (installation / "scripts/desktop_app.py").write_text(
        '''
import ctypes, json, os
from pathlib import Path
from ctypes import wintypes
import clr
clr.AddReference("System.Windows.Forms")
from System.Windows.Forms import Application, Form

user = ctypes.windll.user32
user.IsWindowVisible.argtypes = [wintypes.HWND]
form = Form()
state = {}
def shown(sender, event):
    state.update(pid=os.getpid(), visible=bool(user.IsWindowVisible(form.Handle.ToInt64())))
    form.Close()
form.Shown += shown
Application.Run(form)
Path("visibility.json").write_text(json.dumps(state))
''', encoding="ascii",
    )
    subprocess.run(
        ["cscript.exe", "//nologo", str(installation / "scripts/desktop_launcher.vbs"), "restart"],
        capture_output=True, check=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    record = installation / "visibility.json"
    deadline = time.monotonic() + 15
    while not record.exists() and time.monotonic() < deadline:
        time.sleep(.05)
    state = json.loads(record.read_text())
    kernel = ctypes.windll.kernel32
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x00100000, False, state["pid"])
    if handle:
        try:
            assert kernel.WaitForSingleObject(handle, 10000) == 0
        finally:
            kernel.CloseHandle(handle)
    assert state["visible"]
