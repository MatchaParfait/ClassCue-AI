"""Prepare and inspect real signed portable installs with synthetic data only."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import venv
import zipfile

SOURCE = Path(__file__).resolve().parents[1]
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from classcue.app_updates import read_json, write_json
from classcue.release_security import MARKER, asset_url, config, digest, verify
from scripts.release import feed, verify_package


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def fixture_video(path):
    import av
    import numpy as np
    with av.open(str(path), "w") as output:
        stream = output.add_stream("libx264", rate=12)
        stream.width, stream.height, stream.pix_fmt = 320, 180, "yuv420p"
        for index in range(72):
            image = np.zeros((180, 320, 3), dtype=np.uint8)
            image[:, :, 1] = 80
            image[40:140, 20 + index * 2:80 + index * 2, 0] = 220
            frame = av.VideoFrame.from_ndarray(image, format="rgb24")
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)


def seed(root, external=False, empty=False):
    from classcue.core import TranscriptSegment
    from classcue.video_store import VideoStore
    base = root.parent / (root.name + "-user-data") if external else root / "data"
    library = base / "library"
    settings = base / "config" if external else base
    media = base / "media"
    models = base / "asr"
    for directory in (library, settings, media / "videos", media / "audio", models):
        directory.mkdir(parents=True, exist_ok=True)
    storage = {"library_data_dir": str(library), "media_storage_root": str(media),
               "storage_root": str(models), "log_dir": str(root / ".runtime/logs")}
    if external:
        storage["application_data_root"] = str(base)
    write_json(root / "data/asr_storage.json", storage)
    write_json(settings / "desktop_settings.json", {"shortcuts_installed": True, "hotkey": ""})
    write_json(settings / "release-qa-preference.json", {"fixture": True, "preserve": "settings"})
    (models / "fixture-model.bin").write_bytes(b"synthetic model sentinel, not executable")
    store = VideoStore(library / "project.db")
    if not empty:
        path = media / "videos/fixture.mp4"
        fixture_video(path)
        stat = path.stat()
        video_id = store.create_video(digest(path), path.name, str(path),
                                     str(media / "audio/fixture.wav"), 6000,
                                     source_size=stat.st_size, source_mtime_ns=stat.st_mtime_ns)
        store.mark_transcribed(video_id, [TranscriptSegment(1, 0, 3000, "Synthetic lesson for release acceptance.")])
        marker = store.add_marker(video_id, 1000, 3000, "Fixture point", "Preserve this note",
                                  "Synthetic lesson for release acceptance.", "fixture", .9, "medium_recall")
        store.set_marker_review_status(marker, "mastered")
        store.set_marker_jump_time(marker, 1200)
        store.set_status(video_id, "completed")
    return {"database": str(library / "project.db"), "config": str(settings),
            "media": str(media), "models": str(models), "empty": empty}


def snapshot(record):
    result = {"files": {}, "rows": {}}
    base_files = [Path(record["config"]) / "release-qa-preference.json",
                  Path(record["models"]) / "fixture-model.bin"]
    base_files.extend((Path(record["media"]) / "videos").glob("*"))
    for path in base_files:
        result["files"][str(path)] = digest(path)
    with sqlite3.connect(record["database"]) as db:
        for table in ("videos", "video_transcript_segments", "video_markers"):
            result["rows"][table] = db.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()
    return result


def prepare(args):
    root = args.root.absolute()
    if root.exists():
        raise ValueError("Choose a new, empty test installation path")
    if ".runtime" not in root.parts:
        raise ValueError("Acceptance installs must be inside an ignored .runtime directory")
    inventory = verify_package(SOURCE, args.package)
    root.mkdir(parents=True)
    with zipfile.ZipFile(args.package) as package:
        package.extractall(root)
    environment = root / ".runtime/venv"
    venv.EnvBuilder(with_pip=False).create(environment)
    site = environment / "Lib/site-packages"
    shared = Path(sys.prefix) / "Lib/site-packages"
    (site / "qa-dependencies.pth").write_text(str(shared) + "\n" + str(SOURCE / "tests") + "\n", encoding="utf-8")
    (site / "sitecustomize.py").write_text("from update_transport import install\ninstall()\n", encoding="utf-8")
    record = {"root": str(root), "initial_version": inventory["version"],
              "api_port": free_port(), "debug_port": free_port(), **seed(root, args.external, args.empty)}
    if args.descriptor:
        signed_descriptor = verify(read_json(args.descriptor), config(root)["public_key"])
        candidate = signed_descriptor["release"]
        candidate_path = args.descriptor.parent / candidate["asset"]
        if digest(candidate_path) != candidate["sha256"]:
            raise ValueError("Candidate descriptor and package differ")
        verify_package(root, candidate_path)
        local_feed = root.parent / (root.name + "-feed.json")
        feed(SOURCE, local_feed, args.descriptor)
        transport = root.parent / (root.name + "-transport.json")
        write_json(transport, {
            asset_url(config(root)["repository"], "update-feed", "feed.json"): {"path": str(local_feed)},
            candidate["url"]: {"path": str(candidate_path.absolute()), "delay": .12},
        })
        record["transport"] = str(transport)
    write_json(root / ".runtime/updates/state.json", {
        "auto_check": False, "auto_download": False, "show_announcements": False,
    })
    record["before"] = snapshot(record)
    write_json(root / ".runtime/release-qa.json", record)
    print(json.dumps({k: v for k, v in record.items() if k != "before"}, indent=2))


def checked_record(root):
    root = root.absolute()
    record = read_json(root / ".runtime/release-qa.json")
    if not record or record["root"] != str(root) or ".runtime" not in root.parts:
        raise ValueError("Not an isolated release acceptance installation")
    return record


def start(args):
    record = checked_record(args.root)
    root = args.root.absolute()
    environment = {**os.environ, "PYTHONUTF8": "1",
                   "CLASSROOM_DATA_ROOT": str(Path(record["database"]).parent),
                   "CLASSROOM_API_PORT": str(record["api_port"]),
                   "CLASSROOM_DESKTOP_DEBUG_PORT": str(record["debug_port"])}
    for key in ("RELEASE_SIGNING_KEY", "GH_TOKEN", "GITHUB_TOKEN", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
        environment.pop(key, None)
    environment.pop("CLASSCUE_QA_TRANSPORT", None)
    if record.get("transport") and not args.online:
        environment["CLASSCUE_QA_TRANSPORT"] = record["transport"]
    process = subprocess.Popen(
        [str(root / ".runtime/venv/Scripts/pythonw.exe"), str(root / "scripts/desktop_app.py")],
        cwd=root, env=environment, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    print(json.dumps({"pid": process.pid, "debug_port": record["debug_port"], "online": args.online}))


def inspect(args):
    root = args.root.absolute()
    record = checked_record(root)
    inventory = verify(read_json(root / MARKER), config(root)["public_key"])
    mismatches = [name for name, checksum in inventory["files"].items()
                  if not (root / name).is_file() or digest(root / name) != checksum]
    current = snapshot(record)
    # New versions may append schema columns, but must preserve every existing value.
    before = record["before"]
    preserved = before["files"] == current["files"]
    for table, rows in before["rows"].items():
        actual = current["rows"][table]
        preserved = preserved and len(rows) == len(actual) and all(
            list(new[:len(old)]) == old for old, new in zip(rows, actual)
        )
    transaction = read_json(root / ".runtime/updates/transaction.json", {})
    result = {"version": inventory["version"], "program_mismatches": mismatches,
              "fixture_preserved": preserved, "transaction": transaction.get("status"),
              "backups": len(list((root / ".runtime/updates/backups").glob("*/library-backup.sqlite3")))}
    write_json(root / ".runtime/release-qa-result.json", result)
    print(json.dumps(result, indent=2))
    upgraded = record["initial_version"] != args.expected_version
    if (inventory["version"] != args.expected_version or mismatches or not preserved
            or upgraded and (transaction.get("status") != "completed" or result["backups"] < 1)):
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("--root", type=Path, required=True)
    prep.add_argument("--package", type=Path, required=True)
    prep.add_argument("--descriptor", type=Path)
    prep.add_argument("--external", action="store_true")
    prep.add_argument("--empty", action="store_true")
    launch = commands.add_parser("start")
    launch.add_argument("--root", type=Path, required=True)
    launch.add_argument("--online", action="store_true")
    check = commands.add_parser("inspect")
    check.add_argument("--root", type=Path, required=True)
    check.add_argument("--expected-version", required=True)
    args = parser.parse_args()
    {"prepare": prepare, "start": start, "inspect": inspect}[args.command](args)


if __name__ == "__main__":
    main()
