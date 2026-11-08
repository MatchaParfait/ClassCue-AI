"""One-shot bridge process; stdout is reserved for a single JSON response."""

import argparse
import json
from pathlib import Path
import sys
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--register")
    parser.add_argument("--tool")
    args = parser.parse_args()
    from classcue.course_connection import call_tool, register_application
    if args.register:
        register_application(args.register)
        return
    try:
        raw = sys.stdin.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            raise ValueError("工具参数过长")
        result = call_tool(args.tool, json.loads(raw))
        print(json.dumps({"result": result}, ensure_ascii=False))
    except Exception as error:
        if isinstance(error, HTTPError):
            try:
                message = json.load(error).get("detail", "本地工具调用失败")
            except (OSError, ValueError):
                message = "本地工具调用失败"
        else:
            from classcue.diagnostics import redact_text
            message = redact_text(error)
        print(json.dumps({"error": str(message)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
