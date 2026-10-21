from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier

from fastapi.testclient import TestClient
import pytest

import api.main as api_main
from api.main import create_app
from classcue.video_store import VideoStore
from classcue.database_lease import DatabaseBusyError, DatabaseLease


def test_repeated_start_cannot_recover_live_servers_job(tmp_path):
    store = VideoStore(tmp_path / "workspace.db")
    with TestClient(create_app(store)) as first:
        store.create_job("live", "test", None)
        store.update_job("live", status="running")
        for _ in range(10):
            with pytest.raises(DatabaseBusyError):
                with TestClient(create_app(store)):
                    pass
            assert store.get_job("live")["status"] == "running"
        health = first.get("/api/health").json()
        assert health["service"] == "classroom-video-study"
        assert len(health["instance_id"]) == 32
    # Releasing the instance lock permits real restart recovery.
    with TestClient(create_app(store)):
        assert store.get_job("live")["status"] == "interrupted"


def test_ten_concurrent_owners_only_one_acquires(tmp_path):
    database = tmp_path / "project.db"
    barrier = Barrier(10)
    leases = [DatabaseLease() for _ in range(10)]

    def acquire(lease):
        barrier.wait(timeout=10)
        try:
            lease.acquire(database)
            return True
        except DatabaseBusyError:
            return False

    try:
        with ThreadPoolExecutor(max_workers=10) as pool:
            assert sum(pool.map(acquire, leases)) == 1
    finally:
        for lease in leases:
            lease.release()


def test_workspace_lock_is_cross_process(tmp_path):
    lease = DatabaseLease()
    database = tmp_path / "project.db"
    lease.acquire(database)
    try:
        result = subprocess.run(
            [sys.executable, "-c",
             "from pathlib import Path; import sys;"
             "from classcue.database_lease import DatabaseLease; DatabaseLease().acquire(Path(sys.argv[1]))",
             str(database)],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        assert result.returncode != 0
        assert "DatabaseBusyError" in result.stderr
    finally:
        lease.release()


def test_database_lease_cannot_change_database_until_released(tmp_path):
    first, second = DatabaseLease(), DatabaseLease()
    source, target = tmp_path / "a.db", tmp_path / "b.db"
    try:
        first.acquire(source)
        with pytest.raises(RuntimeError, match="不能更换数据库"):
            first.acquire(target)
        assert first.database == source.resolve()
        with pytest.raises(DatabaseBusyError):
            second.acquire(source)
        first.release()
        first.acquire(target)
        second.acquire(source)
    finally:
        first.release()
        second.release()


def test_removed_workspace_routes_cannot_change_database(tmp_path):
    source = VideoStore(tmp_path / "source" / "project.db")
    target = tmp_path / "target"
    with TestClient(create_app(source)) as client:
        initial = client.get("/api/library/status").json()
        assert client.get("/api/settings/workspace").status_code == 404
        for action in ("create", "switch"):
            assert client.post(f"/api/settings/workspace/{action}", json={"path": str(target)}).status_code in {404, 405}
        assert client.get("/api/library/status").json() == initial
        assert not target.exists()


def test_running_database_stays_pinned_when_startup_settings_change(tmp_path, monkeypatch):
    source, target = tmp_path / "source", tmp_path / "target"
    monkeypatch.setattr(api_main, "load_data_root", lambda: source)
    contender = DatabaseLease()
    try:
        with TestClient(create_app()) as client:
            initial = client.get("/api/health").json()
            monkeypatch.setattr(api_main, "load_data_root", lambda: target)
            assert client.get("/api/health").json() == initial
            assert client.get("/api/library/status").json()["data_root"] == str(source.resolve())
            with pytest.raises(DatabaseBusyError):
                contender.acquire(source / "project.db")
        contender.acquire(source / "project.db")
    finally:
        contender.release()


def test_app_picker_ownership_and_shutdown(tmp_path, monkeypatch):
    discarded = []
    monkeypatch.setattr(api_main, "start_path_picker", lambda kind: {"kind": kind})
    monkeypatch.setattr(api_main, "read_path_picker_result", lambda state: (False, ""))
    monkeypatch.setattr(api_main, "discard_path_picker", discarded.append)
    with TestClient(create_app(VideoStore(tmp_path / "first.db"))) as first:
        with TestClient(create_app(VideoStore(tmp_path / "second.db"))) as second:
            picker_id = first.post("/api/pickers", json={"kind": "file"}).json()["picker_id"]
            assert second.get(f"/api/pickers/{picker_id}").status_code == 404
            assert first.get(f"/api/pickers/{picker_id}").json()["status"] == "pending"
        assert not discarded
    assert discarded == [{"kind": "file"}]


def test_native_ten_sequential_and_concurrent_api_starts_are_rejected(tmp_path, monkeypatch):
    from scripts.start_frontend import start_process
    from scripts.launcher_process import terminate_owned_tree

    root = Path(__file__).resolve().parents[1]
    workspace = tmp_path / "isolated"
    store = VideoStore(workspace / "project.db")
    store.create_job("live-server-job", "test", None)
    store.update_job("live-server-job", status="running")
    monkeypatch.setenv("CLASSROOM_DATA_ROOT", str(workspace))
    lease = DatabaseLease()
    lease.acquire(workspace / "project.db")

    def rejected_start(index):
        command = [
            sys.executable, str(root / "scripts" / "api_service.py"), "--port", "0",
            "--log-dir", str(tmp_path / f"startup-log-{index}"),
        ]
        process = start_process(command, root)
        try:
            assert process.wait(timeout=30) != 0
            text = (tmp_path / f"startup-log-{index}" / "api.log").read_text(encoding="utf-8")
            assert "DatabaseBusyError" in text
        finally:
            terminate_owned_tree(process)

    try:
        for index in range(10):
            rejected_start(index)
        with ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(rejected_start, range(10, 20)))
        assert store.get_job("live-server-job")["status"] == "running"
    finally:
        lease.release()
