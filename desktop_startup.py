"""Bounded, read-only library previews for the native window's first paint."""

from __future__ import annotations

from html import escape
from html.parser import HTMLParser
import base64
from io import BytesIO
import json
import logging
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from threading import BoundedSemaphore
import time


MAX_ITEMS = 160
MAX_BYTES = 256 * 1024
MAX_COVERS = 24
MAX_COVER_BYTES = 96 * 1024
MAX_COVER_TOTAL_BYTES = 1536 * 1024
IDENTITY = re.compile(r"[0-9a-f]{12}")
_COVER_RESIZERS = BoundedSemaphore(1)


def _path(project: Path, library_id: str) -> Path:
    if not isinstance(library_id, str) or not IDENTITY.fullmatch(library_id):
        raise ValueError("Invalid library identity")
    return project / ".runtime" / "library-startup" / f"{library_id}.json"


def _items(values: list) -> list[dict]:
    items = []
    for item in values[:MAX_ITEMS]:
        if not isinstance(item, dict) or item.get("type") not in {"video", "folder"}:
            continue
        kind = item["type"]
        source = item.get(kind)
        if not isinstance(source, dict) or type(source.get("id")) is not int or source["id"] <= 0:
            continue
        name = source.get("name") or source.get("display_name") or source.get("filename")
        if not isinstance(name, str) or not name.strip():
            continue
        value = {"id": source["id"], "name": name[:240]}
        if kind == "folder":
            count = source.get("video_count", 0)
            value["video_count"] = max(0, count) if type(count) is int else 0
        items.append({"type": kind, kind: value})
    return items


def _cover_path(project: Path, library_id: str, video_id: int) -> Path:
    if type(video_id) is not int or video_id <= 0:
        raise ValueError("Invalid video identity")
    return _path(project, library_id).with_suffix("") / "covers" / f"{video_id}.jpg"


def _atomic_bytes(path: Path, data: bytes) -> None:
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_startup_cover(project: Path, library_id: str, video_id: int, cached_cover: Path) -> None:
    """Resize an already generated JPEG off the startup path; never read a video."""
    try:
        output = _cover_path(project, library_id, video_id)
        marker = output.with_suffix(".json")
        stat = cached_cover.stat()
        identity = {"version": 1, "name": cached_cover.name, "size": stat.st_size, "mtime": stat.st_mtime_ns}
        if output.is_file() and marker.is_file() and marker.stat().st_size < 1024:
            try:
                if json.loads(marker.read_text(encoding="utf-8")) == identity:
                    return
            except ValueError:
                pass
        from PIL import Image
        try:
            with _COVER_RESIZERS, Image.open(cached_cover) as image:
                if image.format != "JPEG" or image.width * image.height > 40_000_000:
                    return
                image.thumbnail((480, 270))
                buffer = BytesIO()
                image.convert("RGB").save(buffer, format="JPEG", quality=74, optimize=True)
        except Image.DecompressionBombError:
            return
        data = buffer.getvalue()
        if len(data) > MAX_COVER_BYTES:
            return
        _atomic_bytes(output, data)
        _atomic_bytes(marker, json.dumps(identity).encode("utf-8"))
    except (OSError, ValueError, TypeError, ImportError):
        logging.getLogger(__name__).debug("Startup cover unavailable")


def save_library_snapshot(project: Path, library_id: str, listing: dict,
                          thumbnail_directory: Path | None = None) -> None:
    """Best effort only: a preview must never delay or fail a library response."""
    temporary = None
    try:
        if listing.get("parent_id") is not None or not isinstance(listing.get("items"), list):
            return
        path = _path(project, library_id)
        record = {"version": 1, "library_id": library_id, "saved_at": time.time(),
                  "items": _items(listing["items"])}
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(record, handle, ensure_ascii=False)
        temporary.replace(path)
        if thumbnail_directory is not None:
            from video_thumbnail import cached_video_thumbnail
            videos = [item for item in listing["items"][:MAX_ITEMS]
                      if isinstance(item, dict) and item.get("type") == "video"][:MAX_COVERS]
            for item in videos:
                value = item.get("video", {})
                if not isinstance(value, dict):
                    continue
                try:
                    source = value.get("video_path")
                    cached = cached_video_thumbnail(Path(source), thumbnail_directory) if isinstance(source, str) else None
                    if cached is not None:
                        save_startup_cover(project, library_id, value.get("id"), cached)
                except (OSError, ValueError, TypeError):
                    continue
    except (OSError, ValueError, TypeError):
        logging.getLogger(__name__).warning("Library startup preview could not be saved")
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def load_library_snapshot(project: Path, library_id: str) -> dict | None:
    try:
        path = _path(project, library_id)
        if path.stat().st_size > MAX_BYTES:
            return None
        record = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(record, dict) or record.get("version") != 1
                or record.get("library_id") != library_id or not isinstance(record.get("items"), list)):
            return None
        return {**record, "items": _items(record["items"])}
    except (OSError, ValueError, TypeError):
        return None


def startup_html(project: Path, library_id: str | None = None) -> str:
    template = (project / "assets" / "desktop-startup.html").read_text(encoding="utf-8")
    record = load_library_snapshot(project, library_id) if library_id else None
    folders, videos = [], []
    cover_count, cover_bytes = 0, 0
    for item in record["items"] if record else []:
        kind = item["type"]
        value = item[kind]
        name = escape(value["name"], quote=True)
        if kind == "folder":
            folders.append(
                f'<article class="folder" data-library-key="folder-{value["id"]}"><span class="folder-icon" aria-hidden="true"></span>'
                f'<div><strong title="{name}">{name}</strong><small>{value["video_count"]} 个视频</small></div></article>'
            )
        else:
            cover_html = ""
            if cover_count < MAX_COVERS and cover_bytes < MAX_COVER_TOTAL_BYTES:
                try:
                    cover = _cover_path(project, library_id, value["id"])
                    size = cover.stat().st_size
                    if 0 < size <= min(MAX_COVER_BYTES, MAX_COVER_TOTAL_BYTES - cover_bytes):
                        data = cover.read_bytes()
                        if data.startswith(b"\xff\xd8\xff") and len(data) <= MAX_COVER_BYTES:
                            encoded = base64.b64encode(data).decode("ascii")
                            cover_html = f'<img src="data:image/jpeg;base64,{encoded}" alt="" decoding="async" draggable="false">'
                            cover_count += 1
                            cover_bytes += len(data)
                except (OSError, ValueError):
                    pass
            videos.append(
                f'<article class="video" data-library-key="video-{value["id"]}"><div class="thumbnail" aria-hidden="true">'
                f'<span class="video-icon"></span>{cover_html}</div><strong title="{name}">{name}</strong>'
                '<small>等待同步</small></article>'
            )
    content = '<p class="snapshot-notice">上次同步的列表 · 连接后更新</p>' if record else ""
    if folders:
        content += '<section><h2>文件夹</h2><div class="folders">' + "".join(folders) + "</div></section>"
    if videos:
        content += '<section><h2>视频</h2><div class="videos">' + "".join(videos) + "</div></section>"
    if not content:
        content = '<section class="empty"><span class="folder-icon" aria-hidden="true"></span><p>正在读取本地视频库…</p></section>'
    message = "上次的视频库列表 · 正在连接本地服务…" if record else "正在连接本地视频库…"
    return template.replace("<!--LIBRARY_PREVIEW-->", content).replace("<!--STARTUP_MESSAGE-->", message)


class _ReadOnlyPreview(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.parts = []
        self.skipping = False

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.skipping = True
        elif not self.skipping:
            attributes = "".join(f' {name}="{escape(value or "", quote=True)}"'
                                 for name, value in attrs if not name.lower().startswith("on"))
            self.parts.append(f"<{tag}{attributes}>")

    def handle_endtag(self, tag):
        if tag == "script":
            self.skipping = False
        elif not self.skipping:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        if not self.skipping:
            self.parts.append(data)

    def handle_entityref(self, name):
        if not self.skipping:
            self.parts.append(f"&{name};")

    def handle_charref(self, name):
        if not self.skipping:
            self.parts.append(f"&#{name};")

    def handle_decl(self, decl):
        self.parts.append(f"<!{decl}>")


def startup_transition_script(project: Path, html: str, origin: str, view: dict) -> str:
    """Carry a noninteractive copy across navigation, not a second application."""
    preview = _ReadOnlyPreview()
    preview.feed(html)
    data = {"html": "".join(preview.parts), "origin": origin, "view": view}
    template = (project / "assets" / "desktop-startup-transition.js").read_text(encoding="utf-8")
    return template.replace("__STARTUP_TRANSITION_DATA__", json.dumps(data))
