from pathlib import Path
import subprocess

import pytest

from scripts import check_repository_hygiene as hygiene


@pytest.mark.parametrize("path", [
    "output/generated.png", "outputs/generated.png", "recovered-images/image.png",
    ".runtime/snapshot.zip",
    "frontend/node_modules/package/index.js", "frontend/dist/assets/index.js",
    "nested/__pycache__/cached.pyc", ".env.local", "nested/.env.production",
    "data/classroom.db", "private.pem.key", "api_key.dpapi",
    "Agent.md", "agent.md", "AGENTS.md", "nested/AGENTS.md", "前端开发.md",
    "experiments/new-run/jobs/id/request.json", "experiments/new-run/worker.log",
    "experiments/new-run/transcript.txt", "experiments/new-run/subtitles.json",
    "experiments/new-run/firered.txt", "experiments/new-run/qwen3-asr-1.7b.json",
    "experiments/new-run/raw_outputs.json", "experiments/new-run/test.srt",
    "experiments/firered-bs8-20261011/summary.json",
    "experiments/qwen-resilience-20261011/qwen3-asr-1.7b.txt",
    "experiments/llm-lab/seed.json", "experiments/new-run/acceptance-workspace/data.json",
    "experiments/new-run/summarize.py", "experiments/new-run/report.md",
    "experiments/subtitle-cyberspace/package-lock.json",
    "experiments/subtitle-cyberspace/src/main.js",
    "experiments/subtitle-cyberspace/src/style.css",
])
def test_rejects_local_and_generated_paths(path):
    assert hygiene.path_violations(path)


@pytest.mark.parametrize("path", [
    ".env.example", ".env.sample", ".env.template", "docs/.env.example",
    "frontend/package-lock.json", "uv.lock", "assets/gtcrn/gtcrn_simple.onnx",
    "docs/images/library.png", "tests/fixtures/synthetic.json",
    "experiments/subtitle-cyberspace/src/scene.js",
    "experiments/subtitle-cyberspace/src/fixture.d.ts", "experiments/README.md",
])
def test_keeps_public_code_fixtures_dependencies_and_shared_scene(path):
    assert not hygiene.path_violations(path)


@pytest.mark.parametrize(("token", "kind"), [
    ("sk-proj-" + "Ab3cDe9f" * 10, "OpenAI"),
    ("sk-" + "Ab3cDe9f" * 6, "OpenAI"),
    ("sk-" + "0123456789abcdef" * 2, "OpenAI"),
    ("ghp_" + "Ab3cDe9f" * 5, "GitHub"),
    ("github_pat_" + "Ab3cDe9f" * 10, "GitHub"),
    ("AKIA" + "A1B2C3D4E5F6G7H8", "AWS"),
    ("-----BEGIN RSA PRIVATE KEY-----\n" + "Ab3cDe9f" * 8, "PEM"),
    ("-----BEGIN OPENSSH PRIVATE KEY-----\\n" + "Ab3cDe9f" * 8, "PEM"),
])
def test_detects_synthetic_credential_formats_without_returning_values(token, kind):
    reasons = hygiene.secret_violations(token.encode())
    assert any(kind in reason for reason in reasons)
    assert all(token not in reason for reason in reasons)


@pytest.mark.parametrize("placeholder", [
    "sk-your-api-key", "sk-" + "x" * 40,
    "sk-proj-example-" + "placeholder-" * 6,
    "ghp_" + "x" * 40, "AKIAIOSFODNN7EXAMPLE",
    "-----BEGIN PRIVATE KEY-----",
    "-----BEGIN PUBLIC KEY-----",
])
def test_accepts_explicit_documentation_placeholders_and_public_keys(placeholder):
    assert not hygiene.secret_violations(placeholder.encode())


@pytest.fixture
def repository(tmp_path):
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    return tmp_path


def stage(root: Path, name: str, content: str):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "--", name], check=True)


def test_checks_index_not_unstaged_or_untracked_contents(repository):
    stage(repository, "app.py", "print('public')\n")
    token = "sk-proj-" + "Ab3cDe9f" * 10
    (repository / "app.py").write_text(token)
    (repository / "untracked.txt").write_text(token)
    count, findings = hygiene.inspect_index(repository)
    assert count == 1
    assert not findings


def test_staged_secret_and_generated_file_fail_without_disclosing_content(repository, capsys):
    token = "ghp_" + "Ab3cDe9f" * 5
    stage(repository, "app.py", token)
    stage(repository, "experiments/new-run/worker.log", "private classroom material")
    assert hygiene.main(["--root", str(repository)]) == 1
    output = capsys.readouterr().out
    assert "app.py" in output and "GitHub" in output and "worker.log" in output
    assert token not in output and "private classroom material" not in output


def test_removed_candidate_and_public_scene_pass(repository, capsys):
    stage(repository, "experiments/new-run/worker.log", "private")
    subprocess.run(
        ["git", "-C", str(repository), "rm", "--cached", "--",
         "experiments/new-run/worker.log"], check=True, capture_output=True,
    )
    stage(repository, "experiments/subtitle-cyberspace/src/scene.js", "export const scene = {};")
    assert hygiene.main(["--root", str(repository)]) == 0
    assert (repository / "experiments/new-run/worker.log").exists()
    assert "passed" in capsys.readouterr().out


def test_identical_blobs_under_multiple_paths_pass(repository):
    stage(repository, "normal.py", "# public\n")
    stage(repository, "with space.py", "# public\n")
    assert hygiene.inspect_index(repository) == (2, [])


def test_credentials_are_rejected_even_in_documentation_and_sample_env(repository):
    token = "sk-proj-" + "Ab3cDe9f" * 10
    stage(repository, ".env.example", token)
    stage(repository, "docs/example with space.md", token)
    count, findings = hygiene.inspect_index(repository)
    assert count == 2
    assert {path for path, reason in findings} == {".env.example", "docs/example with space.md"}
    assert all("OpenAI" in reason for path, reason in findings)


def test_git_errors_fail_closed(tmp_path, capsys):
    assert hygiene.main(["--root", str(tmp_path)]) == 2
    assert "could not complete" in capsys.readouterr().err


def test_gitignore_covers_local_artifacts_without_hiding_scene_code(repository):
    source_root = Path(__file__).resolve().parents[1]
    (repository / ".gitignore").write_bytes((source_root / ".gitignore").read_bytes())
    ignored = [
        "output/image.png", "outputs/image.png", "recovered-images/image.png", ".env.local",
        "Agent.md", "agent.md", "AGENTS.md", "前端开发.md",
        "experiments/new-run/jobs/id/state.json", "experiments/new-run/worker.log",
        "experiments/firered-bs8-20261011/summary.json",
        "experiments/firered-routing-20261011/verified-telemetry/subtitles.json",
        "experiments/new-run/summarize.py", "experiments/new-run/report.md",
        "experiments/subtitle-cyberspace/src/main.js",
        "experiments/subtitle-cyberspace/src/style.css",
    ]
    result = subprocess.run(
        ["git", "-c", "core.quotepath=false", "-C", str(repository), "check-ignore", "--stdin"],
        input="\n".join(ignored).encode(), capture_output=True, check=False,
    )
    assert set(result.stdout.decode().splitlines()) == set(ignored)
    allowed = [
        ".env.example", "experiments/subtitle-cyberspace/src/scene.js",
        "experiments/subtitle-cyberspace/src/fixture.d.ts", "experiments/README.md",
    ]
    result = subprocess.run(
        ["git", "-c", "core.quotepath=false", "-C", str(repository), "check-ignore", "--stdin"],
        input="\n".join(allowed).encode(), capture_output=True, check=False,
    )
    assert not result.stdout
