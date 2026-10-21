"""Open a native Windows path dialog and write its result to JSON."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=("file", "directory"), required=True)
    parser.add_argument("--result", required=True)
    args = parser.parse_args()

    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        if args.kind == "directory":
            selected = filedialog.askdirectory(title="选择目录")
        else:
            selected = filedialog.askopenfilename(
                title="选择课堂视频",
                filetypes=[
                    ("视频文件", "*.mp4 *.mov *.mkv *.avi"),
                    ("所有文件", "*.*"),
                ],
            )
    finally:
        root.destroy()

    result_path = Path(args.result)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps({"path": selected or ""}, ensure_ascii=False),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
