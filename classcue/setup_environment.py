"""Install the video-study app and report the actual faster-whisper backend."""

from __future__ import annotations

from classcue.paths import PROJECT_ROOT

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = PROJECT_ROOT
def install(*args: str) -> None:
    subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", *args], check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Install and verify the classroom video study app.")
    parser.add_argument(
        "--require-gpu",
        action="store_true",
        help="Return a non-zero exit code when CTranslate2 cannot use CUDA.",
    )
    args = parser.parse_args()

    install("-r", str(ROOT / "requirements.txt"))
    from classcue.runtime import whisper_backend

    backend = whisper_backend()
    print(f"Whisper backend: {backend.device.upper()} / {backend.compute_type}")
    print(backend.detail)
    if args.require_gpu and not backend.is_gpu:
        print("GPU 验证失败；请检查 NVIDIA 驱动、CUDA/cuDNN 运行库和显卡兼容性。")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
