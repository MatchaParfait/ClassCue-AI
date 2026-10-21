import ast
import json
from pathlib import Path
import subprocess
import sys

from classcue import app_storage, asr_models, llm_experiments, playback_denoise
from classcue.paths import PROJECT_ROOT


def test_application_code_is_in_a_package_not_the_repository_root():
    assert not list(PROJECT_ROOT.glob("*.py"))
    assert (PROJECT_ROOT / "classcue" / "__init__.py").is_file()
    local_modules = {path.stem for path in (PROJECT_ROOT / "classcue").glob("*.py")}
    for folder in ("classcue", "api", "scripts"):
        for source in (PROJECT_ROOT / folder).rglob("*.py"):
            if "__pycache__" in source.parts:
                continue
            for node in ast.walk(ast.parse(source.read_text(encoding="utf-8-sig"))):
                if isinstance(node, ast.ImportFrom) and node.level == 0:
                    assert node.module not in local_modules, str(source)
                elif isinstance(node, ast.Import):
                    assert not any(alias.name in local_modules for alias in node.names), str(source)


def test_package_move_does_not_move_resources_or_default_user_data():
    assert app_storage.ROOT == PROJECT_ROOT
    assert asr_models.ROOT == PROJECT_ROOT
    assert app_storage.DEFAULT_LIBRARY_ROOT == PROJECT_ROOT / "data" / "library"
    assert llm_experiments.PROMPT_DIRECTORY == PROJECT_ROOT / "prompts" / "exam_lab"
    assert playback_denoise.ASSET_ROOT == PROJECT_ROOT / "assets" / "gtcrn"
    assert "asr_storage.json" in (PROJECT_ROOT / "classcue/asr_storage.py").read_text(encoding="utf-8")


def test_importing_package_does_not_initialize_the_application(tmp_path):
    result = subprocess.run(
        [sys.executable, "-c", (
            "import json, sys; import classcue; "
            "print(json.dumps(sorted(name for name in sys.modules if name.startswith('classcue.'))))"
        )],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=True,
    )
    assert json.loads(result.stdout) == []
