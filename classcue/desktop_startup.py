"""Local startup artwork, selected once and carried across the first navigation."""

from __future__ import annotations

import base64
from html import escape
from html.parser import HTMLParser
import json
import logging
from pathlib import Path
import re
import secrets
from tempfile import NamedTemporaryFile


MAX_ARTWORK_BYTES = 1024 * 1024
MAX_MANIFEST_BYTES = 32 * 1024
SCENE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
SCENE_FILE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\.jpg")
COLOR = re.compile(r"#[0-9a-fA-F]{6}")


def _read_json(path: Path, limit: int) -> object:
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Startup metadata exceeds its size limit")
    return json.loads(data)


def load_startup_scenes(project: Path) -> list[dict]:
    try:
        record = _read_json(project / "assets" / "startup" / "scenes.json", MAX_MANIFEST_BYTES)
        if not isinstance(record, dict) or record.get("version") != 1:
            return []
        scenes = record.get("scenes")
        if not isinstance(scenes, list):
            return []
        result, identities = [], set()
        for scene in scenes[:24]:
            if not isinstance(scene, dict):
                continue
            identity, filename = scene.get("id"), scene.get("file")
            caption, background = scene.get("caption"), scene.get("background")
            if (not isinstance(identity, str) or not SCENE_ID.fullmatch(identity)
                    or identity in identities or not isinstance(filename, str)
                    or not SCENE_FILE.fullmatch(filename) or not isinstance(caption, str)
                    or not caption.strip() or not isinstance(background, str)
                    or not COLOR.fullmatch(background)):
                continue
            result.append({"id": identity, "file": filename, "caption": caption[:120],
                           "background": background})
            identities.add(identity)
        return result
    except (OSError, ValueError, TypeError):
        return []


def _remember_scene(project: Path, identity: str) -> None:
    temporary = None
    try:
        path = project / ".runtime" / "startup-scene.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump({"version": 1, "id": identity}, handle)
        temporary.replace(path)
    except OSError:
        logging.getLogger(__name__).debug("Startup artwork preference could not be saved")
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def select_startup_scene(project: Path, preferred_id: str | None = None) -> dict | None:
    """Read one bounded shipped JPEG; never open a library or a source video."""
    scenes = load_startup_scenes(project)
    try:
        previous = _read_json(project / ".runtime" / "startup-scene.json", 1024)
        previous_id = previous.get("id") if isinstance(previous, dict) else None
    except (OSError, ValueError, TypeError):
        previous_id = None
    preferred = next((scene for scene in scenes if scene["id"] == preferred_id), None)
    candidates = scenes[:]
    while candidates:
        alternatives = [scene for scene in candidates if scene["id"] != previous_id]
        scene = preferred or secrets.choice(alternatives or candidates)
        preferred = None
        candidates = [candidate for candidate in candidates if candidate["id"] != scene["id"]]
        try:
            with (project / "assets" / "startup" / scene["file"]).open("rb") as handle:
                data = handle.read(MAX_ARTWORK_BYTES + 1)
            if not (0 < len(data) <= MAX_ARTWORK_BYTES
                    and data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9")):
                continue
        except OSError:
            continue
        _remember_scene(project, scene["id"])
        return {**scene, "image": "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")}
    return None


def startup_html(project: Path, *, scene_id: str | None = None) -> str:
    template = (project / "assets" / "desktop-startup.html").read_text(encoding="utf-8")
    scene = select_startup_scene(project, scene_id)
    artwork = (
        f'<img class="startup-art" src="{scene["image"]}" alt="" aria-hidden="true" '
        'decoding="sync" fetchpriority="high" draggable="false">'
        if scene else ""
    )
    replacements = {
        "<!--STARTUP_ARTWORK-->": artwork,
        "<!--STARTUP_CAPTION-->": escape(scene["caption"]) if scene else "给今天的学习，留一点好心情。",
        "__STARTUP_BACKGROUND__": scene["background"] if scene else "#eef6f8",
        "__STARTUP_SCENE__": scene["id"] if scene else "",
    }
    for placeholder, value in replacements.items():
        template = template.replace(placeholder, value)
    return template


def startup_background(html: str) -> str:
    match = re.search(r"body\{background:(#[0-9a-fA-F]{6})", html)
    return match[1] if match else "#eef6f8"


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


def startup_transition_script(project: Path, html: str, origin: str, view: dict | None = None) -> str:
    preview = _ReadOnlyPreview()
    preview.feed(html)
    data = {"html": "".join(preview.parts), "origin": origin, "view": view,
            "background": startup_background(html)}
    template = (project / "assets" / "desktop-startup-transition.js").read_text(encoding="utf-8")
    return template.replace("__STARTUP_TRANSITION_DATA__", json.dumps(data))
