"""Download explicit official BiCIF/FSMN components into the managed cache."""

from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> None:
    from preparation_progress import component_manifest, stage
    from asr_models import (
        PARAFORMER_REQUIRED_FILES, _managed_path, component_paths, get_model,
        model_directory,
    )
    from paraformer_runtime import _validate_weights

    model = get_model("paraformer-large-cpu")
    directory = model_directory(model.id)
    paths = component_paths(model.id)
    manifest = component_manifest(model.id)
    stage("verifying", "检查 Paraformer BiCIF 与 FSMN-VAD 权重结构")
    _validate_weights(paths)
    files = {}
    for component, names in PARAFORMER_REQUIRED_FILES.items():
        for name in names:
            file = _managed_path(paths[component] / name)
            if not file.is_file() or not file.stat().st_size:
                raise RuntimeError(f"Official model download incomplete: {file}")
            files[file.relative_to(directory).as_posix()] = file.stat().st_size
    stage("finalizing", "保存 Paraformer 模型清单")
    manifest.update(files=files, revision="v2.0.4")
    temporary = directory / "ready.tmp"
    temporary.write_text(json.dumps(manifest), encoding="utf-8")
    temporary.replace(directory / "ready.json")
    (directory / "download.pending.json").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
