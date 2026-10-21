"""Slice supplied transparent artwork into fixed ends and an extensible neck."""

import argparse
from pathlib import Path

from PIL import Image


def prepare(source: Path, destination: Path, name: str) -> None:
    image = Image.open(source).convert("RGBA")
    alpha = image.getchannel("A").point(lambda value: value if value > 8 else 0)
    image.putalpha(alpha)
    bounds = alpha.getbbox()
    if not bounds:
        raise ValueError(f"Artwork has no visible pixels: {source}")
    image = image.crop(bounds)
    seam = 480 - bounds[1]
    if not 0 < seam < image.height:
        raise ValueError(f"Artwork does not contain the expected neck seam: {source}")
    destination.mkdir(parents=True, exist_ok=True)
    parts = {
        "head": (0, 0, image.width, seam),
        "neck": (0, seam, image.width, seam + 1),
        "body": (0, seam, image.width, image.height),
    }
    for part, box in parts.items():
        tile = image.crop(box)
        tile.save(destination / f"{name}-{part}.webp", lossless=True, exact=True)
        print(f"{name}-{part}: {tile.width} x {tile.height}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--left", type=Path, required=True)
    parser.add_argument("--right", type=Path, required=True)
    parser.add_argument(
        "--output", type=Path,
        default=Path(__file__).resolve().parents[1] / "frontend/public/art/settings-herons",
    )
    args = parser.parse_args()
    prepare(args.left, args.output, "left")
    prepare(args.right, args.output, "right")


if __name__ == "__main__":
    main()
