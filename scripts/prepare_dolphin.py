"""Download official pinned-registry Dolphin weights and verify their digest."""

import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CHECKPOINT_SHA256 = "1cee2b8d2133cabb36567625a832d4033569e27eaf5f98df9be1139ec6068bbb"


def main():
    from preparation_progress import component_manifest, stage
    from asr_models import DOLPHIN_REQUIRED_FILES, component_paths, get_model, model_directory
    from dolphin_runtime import SOURCE_COMMIT

    model = get_model("dolphin-small-cn")
    paths = component_paths(model.id)
    manifest = component_manifest(model.id)
    stage("verifying", "检查 Dolphin 官方权重指纹", current_file="small.cn.pt")
    with (paths["recognizer"] / "small.cn.pt").open("rb") as source:
        sha = hashlib.file_digest(source, "sha256").hexdigest()
    if sha != CHECKPOINT_SHA256:
        raise ValueError("Dolphin checkpoint differs from the official pinned registry")
    directory = model_directory(model.id)
    files = {}
    for component, required in DOLPHIN_REQUIRED_FILES.items():
        for name in required:
            file = paths[component] / name
            if not file.is_file() or not file.stat().st_size:
                raise ValueError(f"Missing Dolphin component: {file}")
            files[file.relative_to(directory).as_posix()] = file.stat().st_size
    for name in ("bpe.model", "feats_stats.npz"):
        file = paths["recognizer"] / name
        if file.is_file():
            files[file.relative_to(directory).as_posix()] = file.stat().st_size
    marker = {"repositories": dict(model.repositories), "files": files,
              "checkpoint_sha256": sha, "source_commit": SOURCE_COMMIT, "hub": "modelscope",
              "sha256": manifest["sha256"]}
    stage("finalizing", "保存 Dolphin 模型清单")
    temporary = directory / "ready.tmp"
    temporary.write_text(json.dumps(marker, indent=2), encoding="utf-8")
    temporary.replace(directory / "ready.json")
    (directory / "download.pending.json").unlink(missing_ok=True)
    print(json.dumps(marker), flush=True)


if __name__ == "__main__":
    main()
