import base64
import json
from pathlib import Path
import subprocess

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from classcue.app_updates import write_json
from classcue.app_version import VERSION
from classcue.release_security import digest, sign
from scripts.release import build
from scripts.release_gate import verify_candidate


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    key = Ed25519PrivateKey.generate()
    private = base64.b64encode(key.private_bytes_raw()).decode()
    monkeypatch.setenv("RELEASE_SIGNING_KEY", private)
    write_json(root / "release_config.json", {
        "repository": "owner/project",
        "public_key": base64.b64encode(key.public_key().public_bytes_raw()).decode(),
        "data_schema": 1,
    })
    files = [
        "pyproject.toml", "uv.lock", "release_config.json", "scripts/desktop_launcher.vbs",
        "classcue/app_updates.py", "classcue/app_update_install.py", "classcue/app_version.py",
        "classcue/release_security.py", "api/updates.py", "scripts/app_update_worker.py",
    ]
    for name in files:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("# fixture", encoding="utf-8")
    (root / "pyproject.toml").write_text(f'[project]\nversion = "{VERSION}"\n', encoding="utf-8")
    (root / "uv.lock").write_text(
        f'[[package]]\nname="fixture"\nversion="{VERSION}"\nsource={{virtual="."}}\n',
        encoding="utf-8",
    )
    (root / "release_manifest.toml").write_text(
        "schema = 1\nfiles = " + json.dumps(files), encoding="utf-8",
    )
    (root / "frontend/dist").mkdir(parents=True)
    (root / "frontend/dist/index.html").write_text("<h1>Fixture</h1>", encoding="utf-8")
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=root, text=True).strip()
    git("init")
    git("add", "--", *files, "release_manifest.toml")
    git("-c", "user.name=Release Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "-m", "Fixture")
    tag = "v" + VERSION
    git("tag", tag)
    directory = tmp_path / "dist"
    package = build(root, directory, tag=tag)
    commit = git("rev-parse", "HEAD")
    release = {"tagName": tag, "targetCommitish": commit, "isDraft": True,
               "isPrerelease": False, "assets": [
                   {"name": package.name}, {"name": "app-release.json"},
               ]}
    options = dict(tag=tag, commit=commit, checksum=digest(package), release=release)
    return root, directory, options, private


def test_reviewed_draft_and_idempotent_public_retry_pass(candidate):
    root, directory, options, _ = candidate
    assert verify_candidate(root, directory, **options)["commit"] == options["commit"]
    options["release"]["isDraft"] = False
    assert verify_candidate(root, directory, **options)["sha256"] == options["checksum"]


@pytest.mark.parametrize("field,value", [
    ("tag", "../main"), ("tag", "v01.2.3"), ("commit", "a" * 40),
    ("checksum", "b" * 64), ("checksum", "short"),
    ("latest", "v99.0.0"),
])
def test_wrong_identity_or_unreviewed_assets_are_rejected(candidate, field, value):
    root, directory, options, _ = candidate
    options[field] = value
    with pytest.raises(ValueError):
        verify_candidate(root, directory, **options)


@pytest.mark.parametrize("field,value", [
    ("targetCommitish", "main"), ("tagName", "v99.0.0"), ("isPrerelease", True),
    ("assets", [{"name": "unexpected.zip"}]),
])
def test_wrong_draft_metadata_is_rejected(candidate, field, value):
    root, directory, options, _ = candidate
    options["release"][field] = value
    with pytest.raises(ValueError):
        verify_candidate(root, directory, **options)


def test_even_validly_signed_descriptor_cannot_change_download_identity(candidate):
    root, directory, options, private = candidate
    path = directory / "app-release.json"
    value = json.loads(path.read_text(encoding="utf-8"))["payload"]
    value["release"]["url"] = "https://untrusted.example/package.zip"
    write_json(path, sign(value, private))
    with pytest.raises(ValueError, match="download URL"):
        verify_candidate(root, directory, **options)


def test_packaged_source_must_match_the_reviewed_checkout(candidate):
    root, directory, options, _ = candidate
    (root / "classcue/app_updates.py").write_text("# different source", encoding="utf-8")
    with pytest.raises(ValueError, match="source differs"):
        verify_candidate(root, directory, **options)


def test_tag_build_cannot_publish_or_refresh_feed():
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/release.yml").read_text()
    assert "Create Reviewed Draft" in workflow
    assert "--target \"$Commit\" --draft" in workflow
    assert "Verify Reviewed Assets And Finalize" in workflow
    assert "inputs.action == 'finalize'" in workflow
    assert 'if: "!(github.event_name == \'push\' && startsWith' in workflow
    assert "--sha256" in workflow and "--commit" in workflow
