import os
from pathlib import Path
import stat
import subprocess

import pytest

import classcue.cleanup_progress as cleanup
from classcue.preparation_progress import preparation_events
from classcue.processing_control import ProcessingCancelled


def make_files(root, count=24):
    root.mkdir(parents=True)
    (root / "ready.json").write_bytes(b"ready")
    for index in range(count):
        directory = root / str(index % 6)
        directory.mkdir(exist_ok=True)
        (directory / f"{index}.py").write_bytes(b"package")
    return 5 + count * 7


def test_cleanup_reports_live_counts_and_consumes_one_inventory(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    target = root / "dolphin-cpu"
    size = make_files(target)
    events, scans, resolves = [], [], []
    scandir, resolve = cleanup.os.scandir, Path.resolve
    clock = [0.]
    monkeypatch.setattr(cleanup.time, "monotonic", lambda: clock[0])

    def scan(path):
        if Path(path).is_relative_to(target):
            scans.append(str(path))
        return scandir(path)

    def resolving(path, *args, **kwargs):
        if path == root or path.is_relative_to(root):
            resolves.append(str(path))
        return resolve(path, *args, **kwargs)

    remove = cleanup._remove_file

    def remove_slow(path):
        clock[0] += .4
        remove(path)

    monkeypatch.setattr(cleanup.os, "scandir", scan)
    monkeypatch.setattr(Path, "resolve", resolving)
    monkeypatch.setattr(cleanup, "_remove_file", remove_slow)
    with cleanup.cleanup_events(events.append):
        assert cleanup.cleanup_directories(root, [(target, "Dolphin CPU")], scope="环境") == size
    assert len(scans) == len(set(scans)) == 7
    assert len(resolves) == 2, "Path resolution must scale with targets, not 25 files"
    assert not target.exists()
    deleting = [event for event in events if event["phase"] == "deleting"]
    assert len(deleting) > 2
    assert all(event["files_total"] == 25 and event["directories_total"] == 7 for event in deleting)
    assert [event["files_deleted"] for event in deleting] == sorted(event["files_deleted"] for event in deleting)
    assert any(event["eta_seconds"] is not None for event in deleting)
    assert events[-1]["phase"] == "completed"
    assert events[-1]["files_deleted"] == 25
    assert events[-1]["directories_deleted"] == 7
    assert events[-1]["bytes_deleted"] == size


def test_outside_target_rejected_before_any_deletion(tmp_path):
    root = tmp_path / "runtime"
    owned, outside = root / "safe", tmp_path / "outside"
    make_files(owned)
    make_files(outside)
    with pytest.raises(ValueError):
        cleanup.cleanup_directories(root, [(owned, "safe"), (outside, "outside")], scope="test")
    assert (owned / "ready.json").exists()
    assert (outside / "ready.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction safety")
def test_nested_junction_rejected_before_deleting_siblings(tmp_path):
    root = tmp_path / "runtime"
    target, outside = root / "owned", tmp_path / "outside"
    make_files(target)
    make_files(outside)
    link = target / "escape"
    subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(link), str(outside)],
                   capture_output=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        with pytest.raises(ValueError, match="目录联接"):
            cleanup.cleanup_directories(root, [(target, "owned")], scope="test")
        assert (target / "ready.json").read_bytes() == b"ready"
        assert (outside / "ready.json").read_bytes() == b"ready"
    finally:
        link.rmdir()


def test_cancelled_cleanup_invalidates_ready_marker_and_can_be_retried(tmp_path):
    root = tmp_path / "runtime"
    target = root / "owned"
    make_files(target)
    events = []

    def check():
        if any(event["phase"] == "deleting" and event["files_deleted"] > 0 for event in events):
            raise ProcessingCancelled("stop")

    # Emit after every file for a deterministic cancellation boundary.
    def record(event):
        events.append(event)
        if event["phase"] == "deleting" and event["files_deleted"] == 0:
            # The ready marker must be removed before any package.
            assert (target / "ready.json").exists()

    import time
    from unittest.mock import patch
    original = cleanup._remove_file

    def remove(path):
        original(path)
        time.sleep(.21)

    with patch.object(cleanup, "_remove_file", remove):
        with pytest.raises(ProcessingCancelled):
            with preparation_events(None, checkpoint=check), cleanup.cleanup_events(record):
                cleanup.cleanup_directories(root, [(target, "owned")], scope="test")
    assert not (target / "ready.json").exists()
    assert target.exists()
    assert not any(event["phase"] == "completed" for event in events)
    cleanup.cleanup_directories(root, [(target, "owned")], scope="retry")
    assert not target.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows read-only attributes")
def test_readonly_package_can_be_removed(tmp_path):
    root = tmp_path / "runtime"
    target = root / "owned"
    target.mkdir(parents=True)
    path = target / "readonly.py"
    path.write_bytes(b"package")
    path.chmod(stat.S_IREAD)
    assert cleanup.cleanup_directories(root, [(target, "owned")], scope="test") == 7


def test_runtime_device_cleanup_preserves_other_environment_and_weights(tmp_path, monkeypatch):
    import classcue.asr_models as asr_models
    import classcue.dolphin_runtime as dolphin_runtime
    import classcue.transcriber as transcriber
    root = tmp_path / "assets"
    monkeypatch.setattr(asr_models, "MODEL_ROOT", root / "models/asr")
    monkeypatch.setattr(dolphin_runtime, "RUNTIME_ROOT", root / ".runtime")
    monkeypatch.setattr(transcriber, "unload_model", lambda *_a, **_kw: None)
    cpu, gpu = dolphin_runtime.runtime_directory("cpu"), dolphin_runtime.runtime_directory("gpu")
    make_files(cpu)
    size = make_files(gpu)
    weights = asr_models.component_paths("dolphin-small-cn")["recognizer"]
    weights.mkdir(parents=True)
    (weights / "small.cn.pt").write_bytes(b"complete")
    assert transcriber.clear_asr_runtime("dolphin-small-cn", device="gpu") == size
    assert not gpu.exists()
    assert (cpu / "ready.json").read_bytes() == b"ready"
    assert (weights / "small.cn.pt").read_bytes() == b"complete"


@pytest.mark.parametrize("module_name,dual", [
    ("qwen_runtime", False), ("firered_runtime", False), ("paraformer_runtime", True),
    ("dolphin_runtime", True), ("nano_runtime", True),
])
def test_every_runtime_uses_inventory_sizes_and_device_scope(tmp_path, monkeypatch, module_name, dual):
    import importlib
    module = importlib.import_module(f"classcue.{module_name}")
    root = tmp_path / "app"
    runtime = root / ".runtime"
    monkeypatch.setattr(module, "ROOT", root)
    if hasattr(module, "RUNTIME_ROOT"):
        monkeypatch.setattr(module, "RUNTIME_ROOT", runtime)
    if hasattr(module, "ENVIRONMENT"):
        monkeypatch.setattr(module, "ENVIRONMENT", runtime / f"{module_name}-cpu")
    if hasattr(module, "GPU_ENVIRONMENT"):
        monkeypatch.setattr(module, "GPU_ENVIRONMENT", runtime / f"{module_name}-gpu")
    selected = module.runtime_directory("cpu") if dual else module.ENVIRONMENT
    expected = make_files(selected)
    other = module.runtime_directory("gpu") if dual else runtime / "main-application"
    make_files(other)
    weights = root / "models/weights.bin"
    weights.parent.mkdir()
    weights.write_bytes(b"keep")
    monkeypatch.setattr(module, "runtime_size", lambda: pytest.fail("Capacity must come from the cleanup inventory"))
    released = module.clear_runtime(device="cpu") if dual else module.clear_runtime()
    assert released == expected
    assert not selected.exists()
    assert (other / "ready.json").read_bytes() == b"ready"
    assert weights.read_bytes() == b"keep"
