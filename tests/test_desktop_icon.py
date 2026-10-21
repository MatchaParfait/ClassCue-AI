from pathlib import Path

from PIL import Image, ImageChops

from scripts.build_desktop_icon import ICON_SIZES, build_icons


ROOT = Path(__file__).resolve().parents[1]


def test_generated_icons_match_shared_artwork(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    public = tmp_path / "frontend" / "public"
    public.mkdir(parents=True)
    artwork = Image.new("RGBA", (512, 512), (253, 228, 76, 255))
    artwork.putpixel((0, 0), (0, 0, 0, 0))
    artwork.save(assets / "app-icon.png")

    build_icons(tmp_path)

    with Image.open(assets / "desktop.ico") as icon:
        assert icon.ico.sizes() == {(size, size) for size in ICON_SIZES}
        assert icon.convert("RGBA").getextrema()[3][0] < 255
    with Image.open(public / "favicon.png") as favicon:
        assert favicon.size == (64, 64)
        assert favicon.mode == "RGBA"


def test_shipped_icons_match_user_artwork():
    with Image.open(ROOT / "assets" / "app-icon.png") as source:
        artwork = source.convert("RGBA")
    assert artwork.width == artwork.height
    with Image.open(ROOT / "assets" / "desktop.ico") as icon:
        assert icon.ico.sizes() == {(size, size) for size in ICON_SIZES}
        expected = artwork.resize((256, 256), Image.Resampling.LANCZOS)
        assert ImageChops.difference(icon.convert("RGBA"), expected).getbbox(alpha_only=False) is None
    with Image.open(ROOT / "frontend" / "public" / "favicon.png") as favicon:
        expected = artwork.resize((64, 64), Image.Resampling.LANCZOS)
        assert ImageChops.difference(favicon.convert("RGBA"), expected).getbbox(alpha_only=False) is None
    assert 'href="/favicon.png"' in (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
