"""Prepare an isolated native-desktop QA installation; never copies user data."""

import json
import os
from pathlib import Path
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main():
    import av
    import numpy as np
    from video_store import VideoStore

    project = ROOT / ".runtime" / f"desktop-qa-{time.time_ns()}"
    project.mkdir(parents=True)
    for source in ROOT.glob("*.py"):
        shutil.copy2(source, project / source.name)
    for name in ("pyproject.toml", "release_config.json", "uv.lock"):
        shutil.copy2(ROOT / name, project / name)
    for folder in ("api", "scripts"):
        shutil.copytree(ROOT / folder, project / folder, ignore=shutil.ignore_patterns(
            "__pycache__", "*.pyc", "*.ps1", "*.vbs", "*.cjs",
        ))
    shutil.copytree(ROOT / "assets", project / "assets")
    # Exercise the real shortcut writer, but only create .lnk files inside this
    # disposable installation. Never register keys on the user's desktop/menu.
    shortcut_script = (ROOT / "scripts" / "install_desktop_shortcuts.ps1").read_text(encoding="utf-8-sig")
    for folder in ("Programs", "Desktop"):
        directory = project / "qa-shortcuts" / folder
        directory.mkdir(parents=True)
        shortcut_script = shortcut_script.replace(
            f'$Shell.SpecialFolders.Item("{folder}")', f'(Join-Path $ProjectRoot "qa-shortcuts\\{folder}")',
        )
    assert "SpecialFolders" not in shortcut_script
    (project / "scripts" / "install_desktop_shortcuts.ps1").write_text(shortcut_script, encoding="utf-8-sig")
    shutil.copy2(ROOT / "scripts" / "desktop_launcher.vbs", project / "scripts" / "desktop_launcher.vbs")
    shutil.copytree(ROOT / "frontend" / "dist", project / "frontend" / "dist")
    (project / ".runtime").mkdir()
    (project / "data").mkdir()
    (project / "data" / "desktop_settings.json").write_text(
        '{"shortcuts_installed":true,"hotkey":""}', encoding="utf-8",
    )
    data = project / "data" / "library"
    data.mkdir()
    media = project / "lesson.mp4"
    with av.open(str(media), mode="w") as container:
        stream = container.add_stream("libx264", rate=24)
        stream.width, stream.height, stream.pix_fmt = 320, 180, "yuv420p"
        for index in range(72):
            pixels = np.zeros((180, 320, 3), dtype=np.uint8)
            pixels[:] = (253, 228, 76)
            pixels[30:150, 30 + index:150 + index] = (133, 203, 114)
            frame = av.VideoFrame.from_ndarray(pixels, format="rgb24")
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    store = VideoStore(data / "project.db")
    store.create_video("desktop-qa", media.name, str(media), str(project / "audio.wav"), 3000)
    api_path = project / "api" / "main.py"
    text = api_path.read_text(encoding="utf-8")
    qa_route = '''
    @api.post("/api/desktop-qa-task")
    def desktop_qa_task():
        def operation(update, should_cancel):
            while not should_cancel():
                time.sleep(.05)
            raise ProcessingCancelled("Desktop QA task stopped safely")
        return {"job_id": jobs.submit("desktop_qa", None, operation)}

'''
    text = text.replace("    from frontend_host import mount_frontend", qa_route + "    from frontend_host import mount_frontend")
    api_path.write_text(text, encoding="utf-8")
    print(json.dumps({"project": str(project), "data": str(data)}, ensure_ascii=True))


if __name__ == "__main__":
    main()
