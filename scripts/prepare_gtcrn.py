"""Verify the bundled playback model and license; no network access is needed."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from classcue.playback_denoise import verify_model as verify, verify_model_assets


def main():
    target = verify_model_assets()
    print(f"Bundled GTCRN playback model ready: {target}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
