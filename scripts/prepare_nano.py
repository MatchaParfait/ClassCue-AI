"""Prepare official ModelScope Nano weights, requiring the trained CTC branch."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CHECKPOINT_SHA256 = "81fec8616083c69377f3ceef36aba3655660ee0ca69a5d4a1e9810cd340ca499"


def main():
    import torch
    from preparation_progress import component_manifest, stage
    from funasr.models.fun_asr_nano.checkpoint_utils import normalize_checkpoint_state
    from asr_models import NANO_REQUIRED_FILES, component_paths, get_model, model_directory

    model = get_model("fun-asr-nano-2512")
    paths = component_paths(model.id)
    manifest = component_manifest(model.id)
    stage("verifying", "检查 Nano 官方权重指纹与 CTC 分支", current_file="model.pt")
    checkpoint = paths["recognizer"] / "model.pt"
    with checkpoint.open("rb") as source:
        sha = hashlib.file_digest(source, "sha256").hexdigest()
    if sha != CHECKPOINT_SHA256:
        raise ValueError("Nano checkpoint differs from the verified official CTC checkpoint")
    state = normalize_checkpoint_state(torch.load(checkpoint, map_location="cpu", weights_only=True))
    ctc_keys = sorted(key for key in state if key.startswith(("ctc_decoder.", "ctc.")))
    if len(ctc_keys) != 86:
        raise ValueError("Nano checkpoint must include all 86 trained CTC tensors")
    del state
    directory = model_directory(model.id)
    files = {}
    for component, names in NANO_REQUIRED_FILES.items():
        for name in names:
            file = paths[component] / name
            if not file.is_file() or not file.stat().st_size:
                raise ValueError(f"Missing Nano component: {file}")
            files[file.relative_to(directory).as_posix()] = file.stat().st_size
    marker = {"repositories": dict(model.repositories), "files": files,
              "hub": "modelscope", "checkpoint_sha256": sha, "ctc_tensor_count": len(ctc_keys),
              "sha256": manifest["sha256"]}
    stage("finalizing", "保存 Nano 模型清单")
    temporary = directory / "ready.tmp"
    temporary.write_text(json.dumps(marker, indent=2), encoding="utf-8")
    temporary.replace(directory / "ready.json")
    (directory / "download.pending.json").unlink(missing_ok=True)
    print(json.dumps(marker), flush=True)


if __name__ == "__main__":
    main()
