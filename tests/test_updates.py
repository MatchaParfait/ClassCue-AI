import base64
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
from threading import Event, RLock
import time
from types import SimpleNamespace
from uuid import uuid4
import zipfile
import sqlite3
import shutil
import subprocess

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from classcue.app_update_install import apply, inventory, recover, stage
from classcue.app_updates import UpdateCenter, read_json, write_json
from classcue.app_version import VERSION
from classcue.release_security import (
    MARKER, asset_url, canonical, digest, managed_name, runtime_hash, sign,
    validate_feed, validate_inventory, verify,
)


@pytest.fixture
def distribution(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    key = Ed25519PrivateKey.generate()
    private = base64.b64encode(key.private_bytes_raw()).decode()
    public = base64.b64encode(key.public_key().public_bytes_raw()).decode()
    cfg = {"repository": "owner/project", "public_key": public, "data_schema": 1}
    write_json(root / "release_config.json", cfg)
    files = {
        "pyproject.toml": f'[project]\nversion = "{VERSION}"\n',
        "uv.lock": f'[[package]]\nname = "classroom-video-study"\nversion = "{VERSION}"\nsource = {{virtual = "."}}\n',
        "scripts/desktop_launcher.vbs": "' launcher",
        "frontend/dist/index.html": "<h1>old</h1>",
        "main.py": "old code",
        "obsolete.py": "removed by update",
    }
    for name, value in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value, encoding="utf-8")
    old = {"schema": 1, "repository": cfg["repository"], "version": VERSION, "data_schema": 1,
           "files": {name: digest(root / name) for name in [*files, "release_config.json"]}}
    write_json(root / MARKER, sign(old, private))
    new_version = "99.0.0"
    new_files = {name: (root / name).read_bytes() for name in old["files"] if name != "obsolete.py"}
    new_files["main.py"] = b"new code"
    new_files["added.py"] = b"new module"
    new_files["pyproject.toml"] = f'[project]\nversion = "{new_version}"\n'.encode()
    new_files["uv.lock"] = files["uv.lock"].replace(VERSION, new_version).encode()
    import hashlib
    new = {**old, "version": new_version, "files": {name: hashlib.sha256(data).hexdigest() for name, data in new_files.items()}}
    directory = root / ".runtime/updates"
    directory.mkdir(parents=True)
    archive = directory / "package.zip"
    with zipfile.ZipFile(archive, "w") as package:
        for name, data in new_files.items():
            package.writestr(name, data)
        package.writestr(MARKER, canonical(sign(new, private)))
    now = datetime.now(timezone.utc)
    payload = {"schema": 1, "repository": cfg["repository"], "revision": 10,
               "issued_at": now.isoformat(), "expires_at": (now + timedelta(days=10)).isoformat(),
               "release": {"version": new_version, "asset": "update.zip", "url": asset_url(cfg["repository"], "v" + new_version, "update.zip"),
                           "size": archive.stat().st_size, "sha256": digest(archive),
                           "runtime_hash": runtime_hash(root / "uv.lock"), "data_schema": 1, "notes": "New release"},
               "announcements": [{"id": "notice-1", "title": "Notice", "body": "<script>is text</script>",
                                  "published_at": now.isoformat(), "level": "important"}]}
    feed = sign(payload, private)
    write_json(directory / "feed.json", feed)
    center = UpdateCenter(root)
    archive.rename(center.package_path(payload["release"]))
    center.state["download_sha256"] = payload["release"]["sha256"]
    center._save()
    return SimpleNamespace(root=root, directory=directory, center=center, cfg=cfg, old=old,
                           private=private, public=public, payload=payload, feed=feed, new=new)


def test_signed_update_and_announcements_are_read_without_network(distribution):
    d = distribution
    d.center.fetch_json = lambda *_: pytest.fail("GET must not connect")
    status = d.center.status()
    assert status["portable"] and status["ready"] and status["compatible"]
    assert status["release"]["version"] == "99.0.0"
    assert not status["announcements"][0]["read"]
    assert d.center.mark_read(["notice-1", "unknown"])["announcements"][0]["read"]
    assert UpdateCenter(d.root).status()["announcements"][0]["read"]
    d.center.preferences({"auto_check": False, "show_announcements": False})
    assert not UpdateCenter(d.root).status()["preferences"]["auto_check"]


def test_tampered_expired_and_replayed_feeds_are_not_accepted(distribution):
    d = distribution
    d.feed["payload"]["announcements"][0]["title"] = "tampered"
    with pytest.raises(ValueError):
        verify(d.feed, d.public)
    for changes in [
        {"repository": "attacker/repo"}, {"revision": 0},
        {"expires_at": "2000-01-01T00:00:00Z"}, {"issued_at": "2099-01-01T00:00:00Z"},
    ]:
        payload = {**d.payload, **changes}
        with pytest.raises(ValueError):
            validate_feed(payload, d.cfg["repository"], now=time.time(), minimum_revision=10)


@pytest.mark.parametrize("name", [
    "../secret.py", "/absolute.py", "C:/a.py", "api/../../data/x.py", "api\\x.py",
    ".runtime/secret.py", "models/x.py", "api/NUL.py", "api/x.py:evil", "api/x. /a.py",
    "api/CON", "frontend/src/x.ts", "api//x.py", "api/./x.py",
])
def test_reserved_and_traversal_paths_are_rejected(name):
    with pytest.raises(ValueError):
        managed_name(name)


def test_inventory_case_collisions_are_rejected(distribution):
    value = distribution.old
    value["files"]["MAIN.py"] = value["files"]["main.py"]
    with pytest.raises(ValueError):
        validate_inventory(value)


def test_source_installs_and_edited_programs_cannot_update(distribution):
    d = distribution
    (d.root / ".git").mkdir()
    assert not d.center.status()["portable"]
    with pytest.raises(ValueError):
        d.center.reserve_install()
    (d.root / ".git").rmdir()
    (d.root / "main.py").write_text("local modifications")
    with pytest.raises(ValueError, match="main.py"):
        stage(d.root, d.center.reserve_install())


def test_update_transaction_preserves_data_and_removes_only_managed_files(distribution):
    d = distribution
    write_json(d.root / "data/settings.json", {"private": "user data"})
    (d.root / "models").mkdir()
    (d.root / "models/model.bin").write_bytes(b"weights")
    (d.root / "local_notes.py").write_text("untracked")
    plan = d.center.reserve_install()
    staged, old, new = stage(d.root, plan)
    apply(d.root, plan, staged, old, new)
    assert (d.root / "main.py").read_text() == "new code"
    assert not (d.root / "obsolete.py").exists()
    assert (d.root / "added.py").is_file()
    assert (d.root / "local_notes.py").read_text() == "untracked"
    assert read_json(d.root / "data/settings.json") == {"private": "user data"}
    assert (d.root / "models/model.bin").read_bytes() == b"weights"
    assert inventory(d.root, d.public)["version"] == "99.0.0"
    assert read_json(d.directory / "transaction.json")["status"] == "installed"
    assert (d.directory / "backups" / plan["id"] / "main.py").read_text() == "old code"


def test_sqlite_snapshot_is_consistent_and_never_restored_over_user_data(distribution):
    d = distribution
    database = d.root / "data/project.db"
    database.parent.mkdir()
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE lessons(title TEXT)")
        connection.execute("INSERT INTO lessons VALUES ('user lesson')")
    plan = d.center.reserve_install()
    plan["database_path"] = str(database)
    staged, old, new = stage(d.root, plan)
    apply(d.root, plan, staged, old, new)
    snapshot = d.directory / "backups" / plan["id"] / "library-backup.sqlite3"
    with sqlite3.connect(snapshot) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT title FROM lessons").fetchone()[0] == "user lesson"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT title FROM lessons").fetchone()[0] == "user lesson"


def test_busy_database_is_not_touched_by_installer(distribution):
    from classcue.database_lease import DatabaseLease, DatabaseBusyError
    d = distribution
    database = d.root / "data/project.db"
    plan = d.center.reserve_install()
    plan["database_path"] = str(database)
    staged, old, new = stage(d.root, plan)
    owner = DatabaseLease()
    owner.acquire(database)
    try:
        with pytest.raises(DatabaseBusyError):
            apply(d.root, plan, staged, old, new)
        assert digest(d.root / "main.py") == old["files"]["main.py"]
    finally:
        owner.release()


def test_failed_replacement_rolls_back_all_touched_program_files(distribution, monkeypatch):
    import classcue.app_update_install as installer
    d = distribution
    plan = d.center.reserve_install()
    staged, old, new = stage(d.root, plan)
    original = installer.replace_file
    failed = False

    def fail_once(source, target):
        nonlocal failed
        if target == d.root / "main.py" and not failed:
            failed = True
            raise OSError("simulated disk failure")
        original(source, target)
    monkeypatch.setattr(installer, "replace_file", fail_once)
    with pytest.raises(OSError):
        apply(d.root, plan, staged, old, new)
    for name, checksum in old["files"].items():
        assert digest(d.root / name) == checksum
    assert not (d.root / "added.py").exists()
    assert read_json(d.directory / "transaction.json")["status"] == "rolled_back"


def test_interrupted_replacement_is_recoverable(distribution, monkeypatch):
    import classcue.app_update_install as installer
    d = distribution
    plan = d.center.reserve_install()
    staged, old, new = stage(d.root, plan)
    original = installer.replace_file
    def power_loss(source, target):
        if target == d.root / "main.py":
            raise KeyboardInterrupt()
        original(source, target)
    monkeypatch.setattr(installer, "replace_file", power_loss)
    with pytest.raises(KeyboardInterrupt):
        apply(d.root, plan, staged, old, new)
    assert read_json(d.directory / "transaction.json")["status"] == "replacing"
    monkeypatch.setattr(installer, "replace_file", original)
    recover(d.root)
    assert read_json(d.directory / "transaction.json")["status"] == "rolled_back"
    assert digest(d.root / "main.py") == old["files"]["main.py"]


def test_unknown_file_collision_and_download_tampering_are_rejected(distribution):
    d = distribution
    (d.root / "added.py").write_text("user file")
    with pytest.raises(ValueError, match="冲突"):
        stage(d.root, d.center.reserve_install())
    (d.root / "added.py").unlink()
    package = d.center.package_path(d.payload["release"])
    package.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="校验"):
        stage(d.root, d.center.reserve_install())


@pytest.mark.parametrize("entry", ["../escaped.py", "api/NUL.py", "api\\evil.py", "MAIN.py"])
def test_even_signed_zip_with_unexpected_or_unsafe_entries_is_rejected(distribution, entry):
    d = distribution
    release = d.payload["release"]
    archive = d.center.package_path(release)
    with zipfile.ZipFile(archive, "a") as package:
        package.writestr(entry, b"evil")
    release.update(size=archive.stat().st_size, sha256=digest(archive))
    archive.rename(d.center.package_path(release))
    write_json(d.directory / "feed.json", sign(d.payload, d.private))
    d.center.state["download_sha256"] = release["sha256"]
    with pytest.raises(ValueError):
        stage(d.root, d.center.reserve_install())
    assert not (d.root.parent / "escaped.py").exists()


def test_dependencies_are_compared_without_project_version_only(distribution):
    d = distribution
    original = runtime_hash(d.root / "uv.lock")
    value = (d.root / "uv.lock").read_text()
    (d.root / "uv.lock").write_text(value.replace(VERSION, "5.4.3"))
    assert runtime_hash(d.root / "uv.lock") == original
    (d.root / "uv.lock").write_text(value + '\n[[package]]\nname="changed-dependency"\nversion="1.0.0"\n')
    assert runtime_hash(d.root / "uv.lock") != original
    assert not d.center.status()["compatible"]


def test_native_reservation_blocks_tasks_and_rejects_web_or_cross_origin(distribution, monkeypatch):
    from api.updates import router
    d = distribution
    monkeypatch.setenv("CLASSROOM_UPDATE_TOKEN", "test-native-secret")
    busy = {"running": False, "paused": []}
    jobs = SimpleNamespace(_lock=RLock(), is_running=lambda: busy["running"])
    provider = SimpleNamespace(get=lambda: SimpleNamespace(list_paused_jobs=lambda: busy["paused"]),
                               database_path=lambda: d.root / "data/project.db")
    migrating, updating = Event(), Event()
    app = FastAPI()
    app.include_router(router(d.center, jobs, provider, migrating, updating))
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        assert client.get("/api/updates").status_code == 200
        assert client.post("/api/updates/check", headers={"Origin": "https://evil.example"}).status_code == 403
        assert client.post("/api/updates/check", headers={"Host": "evil.example"}).status_code == 403
        assert client.post("/api/updates/prepare-install").status_code == 403
        headers = {"Authorization": "Bearer test-native-secret"}
        assert client.post("/api/updates/prepare-install", headers={**headers, "Origin": "http://127.0.0.1:8765"}).status_code == 403
        busy["paused"] = [{}]
        assert client.post("/api/updates/prepare-install", headers=headers).status_code == 409
        busy["paused"] = []
        busy["running"] = True
        assert client.post("/api/updates/prepare-install", headers=headers).status_code == 409
        busy["running"] = False
        migrating.set()
        assert client.post("/api/updates/prepare-install", headers=headers).status_code == 409
        migrating.clear()
        assert client.post("/api/updates/prepare-install", headers=headers).status_code == 200
        assert updating.is_set()
        assert client.post("/api/updates/cancel-install", headers=headers).status_code == 200
        assert not updating.is_set()


def test_download_resumes_and_validates_signed_hash(distribution):
    d = distribution
    release = d.payload["release"]
    target = d.center.package_path(release)
    contents = target.read_bytes()
    target.unlink()
    target.with_suffix(".part").write_bytes(contents[:100])
    class Response(io.BytesIO):
        status = 206
        headers = {"Content-Range": f"bytes 100-{len(contents)-1}/{len(contents)}"}
    def download(url, *, headers):
        assert url == release["url"] and headers == {"Range": "bytes=100-"}
        return Response(contents[100:])
    d.center.download = download
    d.center._download(release)
    assert target.read_bytes() == contents
    assert d.center.status()["ready"]


def test_failed_network_check_keeps_valid_cache(distribution):
    d = distribution
    def offline(_url):
        raise OSError("offline")
    d.center.fetch_json = offline
    d.center._check()
    state = d.center.status()
    assert state["error"] and state["release"] and state["announcements"]


def test_announcement_schedule_expiry_and_version_filter(distribution):
    d = distribution
    notice = d.payload["announcements"][0]
    d.payload["announcements"] = [
        {**notice, "id": "future", "published_at": "2099-01-01T00:00:00Z"},
        {**notice, "id": "expired", "published_at": "2000-01-01T00:00:00Z", "expires_at": "2001-01-01T00:00:00Z"},
        {**notice, "id": "wrong-version", "min_version": "50.0.0"},
        notice,
    ]
    write_json(d.directory / "feed.json", sign(d.payload, d.private))
    assert [item["id"] for item in d.center.status()["announcements"]] == ["notice-1"]


def test_missing_or_damaged_configuration_disables_updates_without_breaking_local_api(tmp_path):
    center = UpdateCenter(tmp_path)
    assert not center.status()["configured"] and center.status()["error"]
    write_json(tmp_path / "release_config.json", [])
    assert not UpdateCenter(tmp_path).status()["configured"]


def test_native_install_rejects_unsaved_busy_and_foreign_pages():
    from classcue.desktop_runtime import Lifecycle
    from scripts.desktop_app import NativeAPI
    ui = SimpleNamespace(window=SimpleNamespace(get_current_url=lambda: "http://127.0.0.1:8765/"))
    services = SimpleNamespace(origin="http://127.0.0.1:8765")
    lifecycle = Lifecycle(services, ui)
    lifecycle.ready = True
    native = NativeAPI(lifecycle, ui)
    for field in ["unsaved_changes", "starting", "exiting"]:
        setattr(lifecycle, field, True)
        with pytest.raises(RuntimeError):
            native.install_update()
        setattr(lifecycle, field, False)
    ui.window.get_current_url = lambda: "https://evil.example"
    with pytest.raises(PermissionError):
        native.install_update()


def test_detached_helper_preflights_then_waits_for_native_host_before_replacing(distribution):
    from classcue.app_update_install import prepare_helper, spawn_helper
    from filelock import FileLock
    d = distribution
    source_root = Path(__file__).resolve().parents[1]
    package = d.root / "classcue"
    package.mkdir()
    for name in ["__init__.py", "paths.py", "app_update_install.py", "app_updates.py",
                 "app_version.py", "release_security.py", "database_lease.py"]:
        shutil.copy2(source_root / "classcue" / name, package / name)
    shutil.copy2(source_root / "scripts/app_update_worker.py", d.root / "scripts/app_update_worker.py")
    plan = d.center.reserve_install()
    helper = prepare_helper(d.root, plan)
    # The production worker runs unchanged; replace only its final GUI launch.
    installer = helper / "classcue" / "app_update_install.py"
    installer.write_text(installer.read_text(encoding="utf-8") +
                         "\n\ndef launch(root):\n    write_json(updates_dir(root) / 'qa-launched.json', {'ok': True})\n",
                         encoding="utf-8")
    process = None
    try:
        with FileLock(str(d.root / ".runtime/desktop_instance.lock"), timeout=0):
            process = spawn_helper(d.root, plan, helper)
            deadline = time.monotonic() + 15
            while read_json(d.directory / "helper-ready.json", {}).get("id") != plan["id"]:
                assert process.poll() is None
                assert time.monotonic() < deadline
                time.sleep(.05)
            assert (d.root / "main.py").read_text() == "old code"
        assert process.wait(timeout=20) == 0
        assert (d.root / "main.py").read_text() == "new code"
        assert read_json(d.directory / "qa-launched.json") == {"ok": True}
        assert read_json(d.directory / "transaction.json")["status"] == "installed"
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=10)


def test_publisher_includes_only_tracked_program_files_and_prebuilt_frontend(distribution, monkeypatch):
    from scripts.release import build, verify_package
    d = distribution
    monkeypatch.setenv("RELEASE_SIGNING_KEY", d.private)
    required = ["classcue/app_updates.py", "classcue/app_update_install.py", "classcue/app_version.py", "classcue/release_security.py",
                "api/updates.py", "scripts/app_update_worker.py"]
    for name in required:
        target = d.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# fixture source", encoding="utf-8")
    subprocess.run(["git", "init", str(d.root)], check=True, capture_output=True)
    manifest_files = [name for name in d.old["files"] if not name.startswith("frontend/dist/")]
    manifest_files += required
    (d.root / "release_manifest.toml").write_text(
        "schema = 1\nfiles = " + json.dumps(manifest_files), encoding="utf-8",
    )
    excluded = [
        "Agent.md", "docs/internal-notes.md", "docs/screenshots/review.webp",
        "scripts/benchmark_private.py", "scripts/build_desktop_icon.py", "scripts/release.py",
        "tests/fixture.py", "experiments/private-run/report.md", "internal_helper.py",
    ]
    for name in excluded:
        target = d.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("PRIVATE_DEVELOPMENT_ONLY", encoding="utf-8")
    subprocess.run(["git", "add", "--", *d.old["files"], *required, *excluded, "release_manifest.toml"],
                   cwd=d.root, check=True, capture_output=True)
    (d.root / "credentials.py").write_text("PRIVATE_UNTRACKED_VALUE")
    write_json(d.root / "data/settings.json", {"private": "USER_DATA"})
    package = build(d.root, d.directory / "dist")
    payload = verify_package(d.root, package)
    assert "frontend/dist/index.html" in payload["files"]
    with zipfile.ZipFile(package) as archive:
        assert "credentials.py" not in archive.namelist()
        assert not any(name.startswith((".runtime/", "data/", ".git/")) for name in archive.namelist())
        assert MARKER in archive.namelist()
        assert not set(excluded) & set(archive.namelist())
        assert "release_manifest.toml" not in archive.namelist()


def test_successful_api_start_acknowledges_matching_portable_install(distribution, monkeypatch):
    import api.main as api_main
    d = distribution
    monkeypatch.setattr(api_main, "DIRECTORY_PROJECT_ROOT", d.root)
    monkeypatch.setattr(api_main, "load_data_root", lambda: d.root / "data")
    monkeypatch.setattr(UpdateCenter, "start_background", lambda self: None)
    write_json(d.directory / "transaction.json", {"status": "installed", "version": VERSION})
    with TestClient(api_main.create_app(), base_url="http://127.0.0.1:8765") as client:
        assert client.get("/api/health").json()["version"] == VERSION
        assert read_json(d.directory / "transaction.json")["status"] == "completed"
