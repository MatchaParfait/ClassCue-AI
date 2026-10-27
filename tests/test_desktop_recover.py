import json
import os
from types import SimpleNamespace

import pytest
from filelock import FileLock

from scripts import desktop_recover as recovery


def record(project, pid=101, *, parent=0, script="desktop_bootstrap.ps1"):
    return {"pid": pid, "parent": parent, "created": str(1000 + pid),
            "command": json.dumps(["powershell.exe", "-NoProfile", "-File",
                                   str(project / "scripts" / script)])}


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    project = tmp_path / "installation with spaces"
    project.mkdir()
    monkeypatch.setattr(recovery.webui_process, "command_args", json.loads)
    monkeypatch.setattr(recovery.SingleInstance, "send",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("not running")))
    monkeypatch.setattr(recovery.start_frontend, "read_state", lambda: {})
    monkeypatch.setattr(recovery, "wait_ready", lambda _instance: None)
    records = {101: record(project)}
    monkeypatch.setattr(recovery.webui_process, "snapshot", lambda: records)
    events = []
    monkeypatch.setattr(recovery.start_frontend, "main", lambda action: events.append(action) or 0)
    monkeypatch.setattr(recovery.webui_process, "stop_verified", lambda rows: events.append(rows))
    monkeypatch.setattr(recovery.subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    monkeypatch.setattr(recovery.subprocess, "Popen",
                        lambda command, **kwargs: events.append((command, kwargs)))
    return SimpleNamespace(project=project, records=records, events=events)


def test_recovery_only_selects_this_installations_script(runtime):
    other = runtime.project.parent / "another installation"
    runtime.records[102] = record(other, 102)
    runtime.records[103] = record(runtime.project, 103, script="other.ps1")
    runtime.records[104] = {**record(runtime.project, 104),
                            "command": json.dumps(["powershell.exe", "-Command",
                                                   str(runtime.project / "scripts/desktop_bootstrap.ps1")])}
    assert [row["pid"] for row in recovery.bootstrap_records(runtime.project)] == [101]


def test_idle_preparation_is_removed_after_safe_service_stop_then_relaunched(runtime):
    recovery.recover(runtime.project)
    assert runtime.events[0] == "stop"
    assert runtime.events[1] == [runtime.records[101]]
    command, options = runtime.events[2]
    assert command == ["wscript.exe", str(runtime.project / "scripts/desktop_launcher.vbs")]
    assert options["cwd"] == runtime.project
    assert not (runtime.project / "data").exists()


def test_recovery_refuses_to_interrupt_active_installation(runtime):
    runtime.records[102] = record(runtime.project, 102, parent=101, script="prepare_desktop.ps1")
    with pytest.raises(RuntimeError, match="安装或构建"):
        recovery.recover(runtime.project)
    assert runtime.events == []


def test_windows_console_host_is_not_mistaken_for_active_installation(runtime):
    console = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "conhost.exe")
    runtime.records[102] = {**record(runtime.project, 102, parent=101),
                            "command": json.dumps(["\\??\\" + console, "0x4"])}
    assert [row["pid"] for row in recovery.bootstrap_records(runtime.project)] == [101, 102]


def test_untrusted_console_host_path_still_blocks_cleanup(runtime):
    runtime.records[102] = {**record(runtime.project, 102, parent=101),
                            "command": json.dumps([str(runtime.project / "conhost.exe"), "0x4"])}
    with pytest.raises(RuntimeError, match="安装或构建"):
        recovery.bootstrap_records(runtime.project)


def test_failed_task_guard_never_kills_preparation_or_relaunches(runtime, monkeypatch):
    monkeypatch.setattr(recovery.start_frontend, "main", lambda _action: 1)
    with pytest.raises(RuntimeError, match="安全停止"):
        recovery.recover(runtime.project)
    assert runtime.events == []


def test_running_task_is_rejected_before_service_stop(runtime, monkeypatch):
    monkeypatch.setattr(recovery.start_frontend, "read_state", lambda: {"api_port": 12345})
    def active(*args, **kwargs):
        assert kwargs["require_idle"] is True
        raise RuntimeError("A processing task is running")
    monkeypatch.setattr(recovery.start_frontend, "live_data_root", active)
    with pytest.raises(RuntimeError, match="processing task"):
        recovery.recover(runtime.project)
    assert runtime.events == []


def test_unresponsive_live_service_is_not_force_stopped(runtime, monkeypatch):
    monkeypatch.setattr(recovery.start_frontend, "read_state", lambda: {"api_port": 12345})
    monkeypatch.setattr(recovery.start_frontend, "live_data_root", lambda *args, **kwargs: None)
    monkeypatch.setattr(recovery.start_frontend, "port_available", lambda *args: True)
    with pytest.raises(RuntimeError, match="无法确认任务状态"):
        recovery.recover(runtime.project)
    assert runtime.events == []


def test_responsive_desktop_receives_normal_restart_without_process_cleanup(runtime, monkeypatch):
    sent = []
    monkeypatch.setattr(recovery.SingleInstance, "send", lambda self, action, **kwargs: sent.append(action))
    recovery.recover(runtime.project)
    assert sent == ["activate", "restart"] and runtime.events == []


def test_unresponsive_desktop_with_held_lock_is_not_killed(runtime):
    (runtime.project / ".runtime").mkdir(exist_ok=True)
    with FileLock(str(runtime.project / ".runtime/desktop_instance.lock")):
        with pytest.raises(RuntimeError, match="不会强杀"):
            recovery.recover(runtime.project)
    assert runtime.events == []


def test_new_command_uses_local_python_and_preserves_failure_message():
    text = (recovery.PROJECT / "scripts/一键恢复并重启.cmd").read_text(encoding="utf-8")
    assert 'cd /d "%~dp0.."' in text
    assert '"%PYTHON%" "%~dp0desktop_recover.py"' in text
    assert "if errorlevel 1 (" in text and "pause" in text
    assert "taskkill" not in text.lower()


def test_readiness_requires_a_visible_ready_window(monkeypatch):
    states = iter([{"ready": False, "visible": True}, {"ready": True, "visible": False},
                   {"ready": True, "visible": True}])
    instance = SimpleNamespace(send=lambda *args, **kwargs: next(states))
    monkeypatch.setattr(recovery.time, "sleep", lambda _seconds: None)
    recovery.wait_ready(instance)
    with pytest.raises(StopIteration):
        next(states)


def test_readiness_timeout_is_reported_not_silently_successful():
    with pytest.raises(RuntimeError, match="尚未确认"):
        recovery.wait_ready(SimpleNamespace(), timeout=0)


@pytest.mark.skipif(os.name != "nt", reason="Windows command entry point")
@pytest.mark.parametrize("environment", [".venv", ".runtime/venv"], ids=["development", "portable"])
def test_recovery_command_runs_from_an_unrelated_directory(tmp_path, environment):
    import shutil
    import subprocess
    project = tmp_path / "installation with spaces"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    target = scripts / "一键恢复并重启.cmd"
    shutil.copyfile(recovery.PROJECT / "scripts/一键恢复并重启.cmd", target)
    subprocess.run([os.sys.executable, "-m", "venv", "--without-pip", str(project / environment)],
                   check=True, capture_output=True, timeout=30)
    if environment == ".runtime/venv":
        assert not (project / ".venv").exists()
    (scripts / "desktop_recover.py").write_text(
        "import json,sys\nfrom pathlib import Path\n"
        "Path('result.json').write_text(json.dumps({'cwd':str(Path.cwd()),'python':sys.executable}), encoding='utf-8')\n",
        encoding="utf-8")
    result = subprocess.run(["cmd.exe", "/d", "/c", str(target)], cwd=tmp_path,
                            capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    result = json.loads((project / "result.json").read_text(encoding="utf-8"))
    assert result["cwd"] == str(project)
    assert os.path.normcase(result["python"]) == os.path.normcase(str(project / environment / "Scripts/python.exe"))
