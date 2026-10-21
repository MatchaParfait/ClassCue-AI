"""Maintainer tooling. No private key or user data is included in releases."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from classcue.app_updates import write_json
from classcue.app_version import VERSION
from classcue.release_security import (
    MARKER, MAX_METADATA, MAX_PACKAGE, MAX_UNPACKED, asset_url, canonical, config, digest, managed_name,
    runtime_hash, safe_target, sign, validate_feed, validate_inventory, validate_zip_entry, verify, version,
)


def private_key(root):
    value = os.environ.get("RELEASE_SIGNING_KEY", "").strip()
    if not value:
        value = (root / ".runtime/release-signing/private-key.txt").read_text(encoding="ascii").strip()
    return value


def signed(root, payload):
    envelope = sign(payload, private_key(root))
    verify(envelope, config(root)["public_key"])
    return envelope


def verify_package(root, path):
    cfg = config(root)
    if not 0 < path.stat().st_size <= MAX_PACKAGE:
        raise ValueError("发行包大小无效")
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > 5001 or sum(info.file_size for info in entries) > MAX_UNPACKED:
            raise ValueError("发行包展开后过大")
        for info in entries:
            validate_zip_entry(info)
        if archive.getinfo(MARKER).file_size > MAX_METADATA:
            raise ValueError("发行文件清单过大")
        payload = verify(json.loads(archive.read(MARKER)), cfg["public_key"])
        files = validate_inventory(payload)
        if (payload.get("repository") != cfg["repository"] or len(entries) != len(files) + 1
                or {info.filename for info in entries} != {*files, MARKER}):
            raise ValueError("发行文件身份/清单不符")
        for name, checksum in files.items():
            with archive.open(name) as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != checksum:
                    raise ValueError(f"程序文件校验失败：{name}")
    return payload


def keygen(root):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    cfg = config(root)
    path = root / ".runtime/release-signing/private-key.txt"
    if cfg["public_key"] or path.exists():
        raise ValueError("签名身份已存在，拒绝覆盖。密钥轮换请参阅 docs/releases.md")
    key = Ed25519PrivateKey.generate()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="ascii") as output:
        output.write(base64.b64encode(key.private_bytes_raw()).decode("ascii"))
    path.chmod(0o600)
    cfg["public_key"] = base64.b64encode(key.public_key().public_bytes_raw()).decode("ascii")
    write_json(root / "release_config.json", cfg)
    print("已生成签名身份。私钥仅保存在忽略的 .runtime/release-signing/private-key.txt；请离线备份。")
    print("将该文件内容设置为 GitHub Actions Secret: RELEASE_SIGNING_KEY。不要上传私钥。")


def source_files(root, *, include_untracked=False):
    manifest = tomllib.loads((root / "release_manifest.toml").read_text(encoding="utf-8"))
    entries = manifest.get("files")
    if (manifest.get("schema") != 1 or not isinstance(entries, list) or not entries
            or any(not isinstance(name, str) for name in entries)):
        raise ValueError("Invalid release source manifest")
    if len({name.casefold() for name in entries}) != len(entries):
        raise ValueError("Duplicate release source manifest entry")
    names = subprocess.check_output(
        ["git", "-c", "core.quotepath=false", "ls-files", "-z", "--cached",
         *(["--others", "--exclude-standard"] if include_untracked else [])],
        cwd=root,
    ).decode("utf-8").split("\0")
    available = set(names)
    if "release_manifest.toml" not in available:
        raise ValueError("Release source manifest must be staged")
    for name in entries:
        managed_name(name)
        if name == MARKER or name.startswith("frontend/dist/"):
            raise ValueError("Generated files cannot be listed in the source manifest")
        if name not in available or not safe_target(root, name).is_file():
            raise ValueError(f"Release source is missing or not staged: {name}")
    return set(entries)


def build(root, output, tag=None, notes="", include_untracked=False):
    version(VERSION)
    if len(notes) > 30000:
        raise ValueError("更新日志不能超过 30000 字")
    if tag and tag != "v" + VERSION:
        raise ValueError("Git tag 必须与 pyproject.toml 版本完全一致")
    if not (root / "frontend/dist/index.html").is_file():
        raise ValueError("请先构建 frontend")
    files = source_files(root, include_untracked=include_untracked)
    files.update(path.relative_to(root).as_posix() for path in (root / "frontend/dist").rglob("*") if path.is_file())
    if not {"classcue/app_updates.py", "classcue/app_update_install.py", "classcue/app_version.py", "classcue/release_security.py",
            "api/updates.py", "scripts/app_update_worker.py"}.issubset(files):
        raise ValueError("缺少已跟踪的更新模块，请先提交代码。本地未提交验收可显式使用 --include-untracked")
    for name in files:
        safe_target(root, name)
    cfg = config(root)
    inventory = {"schema": 1, "repository": cfg["repository"], "version": VERSION,
                 "data_schema": cfg["data_schema"], "files": {name: digest(root / name) for name in sorted(files)}}
    validate_inventory(inventory)
    marker = signed(root, inventory)
    if sum((root / name).stat().st_size for name in files) > MAX_UNPACKED:
        raise ValueError("发行包展开后过大")
    output.mkdir(parents=True, exist_ok=True)
    asset = f"ClassroomVideoStudy-{VERSION}-windows-x64.zip"
    package = output / asset
    if package.exists():
        raise ValueError("发行包已存在，拒绝覆盖")
    with zipfile.ZipFile(package, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name in sorted(files):
            archive.write(root / name, name)
        archive.writestr(MARKER, canonical(marker))
    if package.stat().st_size > MAX_PACKAGE:
        raise ValueError("发行包超过客户端下载上限")
    verify_package(root, package)
    release = {"version": VERSION, "asset": asset, "url": asset_url(cfg["repository"], "v" + VERSION, asset),
               "size": package.stat().st_size, "sha256": digest(package), "runtime_hash": runtime_hash(root / "uv.lock"),
               "data_schema": cfg["data_schema"], "notes": notes}
    write_json(output / "app-release.json", signed(root, {"schema": 1, "repository": cfg["repository"], "release": release}))
    print(f"发行包：{package}\nSHA256: {release['sha256']}")
    return package


def feed(root, output, descriptor=None):
    cfg = config(root)
    release = None
    if descriptor:
        value = verify(json.loads(descriptor.read_text(encoding="utf-8")), cfg["public_key"])
        if value.get("repository") != cfg["repository"]:
            raise ValueError("发行说明不属于此仓库")
        release = value["release"]
    notices = json.loads((root / "release/announcements.json").read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc)
    payload = {"schema": 1, "repository": cfg["repository"], "revision": time.time_ns(),
               "issued_at": now.isoformat(), "expires_at": (now + timedelta(days=30)).isoformat(),
               "release": release, "announcements": notices}
    validate_feed(payload, cfg["repository"], now=time.time())
    write_json(output, signed(root, payload))
    print(f"签名公告源：{output}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("keygen")
    verification = sub.add_parser("verify")
    verification.add_argument("package", type=Path)
    package = sub.add_parser("build")
    package.add_argument("--output", type=Path, default=ROOT / ".runtime/dist")
    package.add_argument("--tag")
    package.add_argument("--notes", type=Path)
    package.add_argument("--include-untracked", action="store_true", help="仅用于未提交代码的本地验收")
    source = sub.add_parser("feed")
    source.add_argument("--output", type=Path, default=ROOT / ".runtime/feed.json")
    source.add_argument("--release", type=Path)
    args = parser.parse_args()
    if args.command == "keygen":
        keygen(ROOT)
    elif args.command == "verify":
        payload = verify_package(ROOT, args.package)
        print(f"Verified {payload['version']}: {len(payload['files'])} files; SHA256 {digest(args.package)}")
    elif args.command == "build":
        build(ROOT, args.output, args.tag, args.notes.read_text(encoding="utf-8") if args.notes else "", args.include_untracked)
    else:
        feed(ROOT, args.output, args.release)


if __name__ == "__main__":
    main()
