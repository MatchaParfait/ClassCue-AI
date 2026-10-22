"""Verify the exact, already-reviewed draft before making it public."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from classcue.release_security import config, digest, runtime_hash, verify, version
from scripts.release import source_files, verify_package


def verify_candidate(root, directory, *, tag, commit, checksum, release, latest=None):
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise ValueError("Expected a stable version tag")
    version(tag[1:])
    if not re.fullmatch(r"[a-f0-9]{40}", commit) or not re.fullmatch(r"[a-f0-9]{64}", checksum):
        raise ValueError("Expected a full commit SHA and ZIP SHA-256")
    for ref in ("HEAD", f"refs/tags/{tag}^{{commit}}"):
        actual = subprocess.check_output(["git", "rev-parse", ref], cwd=root, text=True).strip()
        if actual != commit:
            raise ValueError("Checkout or tag does not match the reviewed commit")
    if (release.get("tagName") != tag or release.get("isPrerelease") is not False
            or type(release.get("isDraft")) is not bool
            or release.get("targetCommitish") != commit):
        raise ValueError("Draft identity does not match the reviewed commit")
    if latest and version(latest.removeprefix("v")) > version(tag[1:]):
        raise ValueError("Refusing to make an older version latest")
    cfg = config(root)
    descriptor = verify(json.loads((directory / "app-release.json").read_text(encoding="utf-8")),
                        cfg["public_key"])
    value = descriptor.get("release", {})
    expected_name = f"ClassroomVideoStudy-{tag[1:]}-windows-x64.zip"
    if (descriptor.get("repository") != cfg["repository"] or value.get("version") != tag[1:]
            or value.get("asset") != expected_name or value.get("sha256") != checksum
            or value.get("runtime_hash") != runtime_hash(root / "uv.lock")
            or value.get("data_schema") != cfg["data_schema"]):
        raise ValueError("Signed descriptor does not match the reviewed release")
    from classcue.release_security import asset_url
    if value.get("url") != asset_url(cfg["repository"], tag, expected_name):
        raise ValueError("Signed download URL does not match the version tag")
    assets = release.get("assets", [])
    if sorted(item["name"] for item in assets) != sorted([expected_name, "app-release.json"]):
        raise ValueError("Draft must have exactly the two expected release assets")
    package = directory / expected_name
    if digest(package) != checksum or package.stat().st_size != value.get("size"):
        raise ValueError("Downloaded ZIP does not match the reviewed SHA-256 or size")
    inventory = verify_package(root, package)
    if inventory["version"] != tag[1:] or inventory["data_schema"] != cfg["data_schema"]:
        raise ValueError("Package version or data compatibility does not match")
    expected = source_files(root)
    packaged = {name for name in inventory["files"] if not name.startswith("frontend/dist/")}
    if packaged != expected:
        raise ValueError("Package does not match the tagged release source manifest")
    for name in expected:
        if digest(root / name) != inventory["files"][name]:
            raise ValueError(f"Package source differs from the reviewed checkout: {name}")
    with zipfile.ZipFile(package) as archive:
        import tomllib
        metadata = tomllib.loads(archive.read("pyproject.toml").decode("utf-8"))
        if metadata["project"]["version"] != tag[1:]:
            raise ValueError("Packaged project version does not match the tag")
    return {"tag": tag, "commit": commit, "sha256": checksum, "files": len(inventory["files"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--release-info", type=Path, required=True)
    parser.add_argument("--latest")
    args = parser.parse_args()
    result = verify_candidate(
        ROOT, args.directory, tag=args.tag, commit=args.commit, checksum=args.sha256,
        release=json.loads(args.release_info.read_text(encoding="utf-8-sig")), latest=args.latest,
    )
    print(json.dumps(result))


if __name__ == "__main__":
    main()
