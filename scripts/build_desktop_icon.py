"""Build desktop and browser icons from the shared app artwork."""

from pathlib import Path
from PIL import Image


ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)


def build_icons(project: Path) -> None:
    with Image.open(project / "assets" / "app-icon.png") as source:
        image = source.convert("RGBA")
    if image.width != image.height:
        raise ValueError("App icon artwork must be square")
    image.save(project / "assets" / "desktop.ico", sizes=[
        (size, size) for size in ICON_SIZES
    ])
    image.resize((64, 64), Image.Resampling.LANCZOS).save(
        project / "frontend" / "public" / "favicon.png", optimize=True,
    )


def main():
    build_icons(Path(__file__).resolve().parents[1])


if __name__ == "__main__":
    main()
