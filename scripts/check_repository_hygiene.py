"""Check the Git index for private/generated files and recognizable credentials.

Run after staging a public commit: python scripts/check_repository_hygiene.py.
Only indexed content is inspected, so unstaged edits and local backups are not
published or scanned accidentally. This is a lightweight guard, not a complete
secret scanner or a review of historical commits.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys


LOCAL_ROOTS = frozenset({
    ".runtime", ".venv", "data", "models", "output", "outputs", "projects",
    "recovered-images",
})
CACHE_DIRECTORIES = frozenset({"__pycache__", ".pytest_cache", "node_modules"})
EXAMPLE_ENV_FILES = frozenset({".env.example", ".env.sample", ".env.template"})
PRIVATE_INSTRUCTIONS = frozenset({"agent.md", "agents.md", "前端开发.md"})
SECRET_PATTERNS = (
    ("OpenAI API key", re.compile(
        rb"(?<![A-Za-z0-9_-])sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}"
        rb"(?![A-Za-z0-9_-])")),
    ("GitHub token", re.compile(
        rb"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{30,}"
        rb"|github_pat_[A-Za-z0-9_]{60,})(?![A-Za-z0-9_])")),
    ("AWS access key ID", re.compile(
        rb"(?<![A-Za-z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Za-z0-9])")),
    ("PEM private key", re.compile(
        rb"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"
        rb"(?:[ \t\r\n]|\\[rn])+[A-Za-z0-9+/=]{32,}")),
)


class CheckError(Exception):
    """An index cannot be checked reliably."""


def path_violations(path: str) -> list[str]:
    parts = PurePosixPath(path.lower()).parts
    name = parts[-1]
    suffix = PurePosixPath(name).suffix
    reasons = []
    if name in PRIVATE_INSTRUCTIONS:
        reasons.append("internal collaboration instructions")
    if parts[0] in LOCAL_ROOTS or any(part in CACHE_DIRECTORIES for part in parts):
        reasons.append("local runtime, backup, or dependency directory")
    if parts[:2] == ("frontend", "dist"):
        reasons.append("generated frontend build")
    if name not in EXAMPLE_ENV_FILES and (name == ".env" or name.startswith(".env.")):
        reasons.append("local environment credentials")
    if suffix in {".pyc", ".db", ".sqlite", ".sqlite3", ".dpapi", ".p12", ".pfx", ".key"}:
        reasons.append("local database, bytecode, or private credential file")
    if parts[0] == "experiments" and parts != ("experiments", "readme.md"):
        scene_source = (
            parts[:3] == ("experiments", "subtitle-cyberspace", "src")
            and suffix in {".js", ".ts"} and name != "main.js"
        )
        if not scene_source:
            reasons.append("private experiment material outside the shared scene source")
    return reasons


def is_placeholder(value: bytes) -> bool:
    lowered = value.lower()
    if lowered == b"AKIAIOSFODNN7EXAMPLE".lower():
        return True
    for prefix in (b"sk-proj-", b"sk-svcacct-", b"sk-", b"github_pat_", b"ghp_"):
        if lowered.startswith(prefix):
            lowered = lowered[len(prefix):]
            break
    words = set(re.split(rb"[-_]", lowered))
    if words & {b"example", b"placeholder", b"dummy"}:
        return True
    return len(set(lowered)) == 1


def secret_violations(content: bytes) -> list[str]:
    reasons = []
    for kind, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(content):
            if kind == "PEM private key" or not is_placeholder(match.group()):
                reasons.append(f"possible {kind}")
                break
    return reasons


def indexed_files(root: Path) -> list[tuple[str, str, str]]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--stage", "-z"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
    )
    if result.returncode:
        raise CheckError("Cannot read the Git index.")
    entries = []
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        metadata, path = record.split(b"\t", 1)
        mode, oid, stage = metadata.decode("ascii").split()
        if stage != "0":
            raise CheckError("Resolve unmerged index entries before running this check.")
        entries.append((path.decode("utf-8", errors="surrogateescape"), oid, mode))
    return entries


def inspect_index(root: Path) -> tuple[int, list[tuple[str, str]]]:
    entries = indexed_files(root)
    findings = []
    content_findings = {}
    with subprocess.Popen(
        ["git", "-C", str(root), "cat-file", "--batch"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    ) as process:
        try:
            for path, oid, mode in entries:
                reasons = path_violations(path)
                # Gitlinks refer to another repository, not readable file blobs.
                if mode != "160000" and oid not in content_findings:
                    process.stdin.write(oid.encode("ascii") + b"\n")
                    process.stdin.flush()
                    header = process.stdout.readline().split()
                    if len(header) != 3 or header[1] != b"blob":
                        raise CheckError("Cannot read an indexed file blob.")
                    size = int(header[2])
                    content = process.stdout.read(size)
                    if len(content) != size or process.stdout.read(1) != b"\n":
                        raise CheckError("An indexed file blob was truncated.")
                    content_findings[oid] = secret_violations(content)
                reasons.extend(content_findings.get(oid, []))
                findings.extend((path, reason) for reason in reasons)
        finally:
            process.stdin.close()
            process.wait()
    return len(entries), findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    try:
        count, findings = inspect_index(args.root)
    except (CheckError, OSError, ValueError) as error:
        detail = str(error) if isinstance(error, CheckError) else type(error).__name__
        print(f"Repository hygiene check could not complete: {detail}", file=sys.stderr)
        return 2
    if findings:
        print("Repository hygiene check failed (file contents and credential values omitted):")
        for path, reason in findings:
            print(f"  {json.dumps(path, ensure_ascii=True)}: {reason}")
        print("Keep private/generated files locally and out of the public index.")
        return 1
    print(f"Repository hygiene check passed: {count} indexed files checked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
