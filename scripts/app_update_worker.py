"""Copied with trusted updater modules before any installed code is replaced."""

import argparse
from pathlib import Path
import re
import sys

from filelock import FileLock

from app_update_install import apply, launch, recover, recover_at_launch, stage
from app_updates import read_json, updates_dir, write_json


def main(root, plan_id):
    if not re.fullmatch(r"[a-f0-9]{32}", plan_id):
        raise ValueError("Invalid update plan")
    directory = updates_dir(root)
    plan = read_json(directory / "install-plan.json", {})
    if plan.get("id") != plan_id:
        raise ValueError("Update plan changed")
    with FileLock(str(directory / "install.lock"), timeout=0):
        staged, old, new = stage(root, plan)
        write_json(directory / "helper-ready.json", {"id": plan_id})
        # Waiting for this lock proves the native host has exited. Never kill it.
        with FileLock(str(Path(root) / ".runtime" / "desktop_instance.lock"), timeout=120):
            try:
                apply(root, plan, staged, old, new)
            except Exception:
                recover(root)
                raise
    launch(root)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--plan")
    choice.add_argument("--recover", action="store_true")
    args = parser.parse_args()
    try:
        if args.recover:
            recover_at_launch(Path(args.root))
        else:
            main(Path(args.root), args.plan)
    except Exception as error:
        write_json(updates_dir(args.root) / "worker-error.json", {"message": str(error)[:500]})
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, f"升级未完成：{error}\n程序备份保留在 .runtime/updates/backups。请重新启动或查看维护文档。",
                                            "课堂重点复习", 0x10)
        raise
