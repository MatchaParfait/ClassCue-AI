"""Private pipe protocol for an ephemeral browser; never writes credentials."""

from __future__ import annotations

import base64
import json
import sys

from classcue.bilibili import BilibiliError, video_url
from classcue.bilibili_frames import EdgeFrameSession


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def read():
    raw = sys.stdin.readline(65537)
    if not raw or len(raw) > 65536:
        raise ValueError("无效的取帧命令")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("无效的取帧命令")
    return value


class Bridge:
    def __init__(self):
        self.paused = False

    def acknowledge(self, event, **value):
        emit({"event": event, **value})
        if read().get("ok") is not True:
            raise BilibiliError("capture_failed", "取帧已被主进程停止")

    def control(self, _):
        return self.paused

    def set_control(self, _, value):
        self.paused = True
        self.acknowledge("pause", reason=value.get("reason", "browser_challenge"))

    def checkpoint(self):
        self.acknowledge("checkpoint")

    def consume(self, key, amount=1):
        self.acknowledge("consume", key=key, amount=amount)


def main():
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        initial = read()
        if initial.get("action") != "init":
            return
        bridge = Bridge()
        with EdgeFrameSession(bridge, initial.get("cookie", ""), checkpoint=bridge.checkpoint,
                              consume=bridge.consume) as session:
            session.stage_callback = lambda stage: emit({"event": "stage", "stage": stage})
            emit({"event": "ready"})
            while True:
                command = read()
                if command.get("action") == "close":
                    return
                unit, requested = command.get("unit", {}), command.get("requested_ms")
                if (command.get("action") != "capture" or type(requested) is not int or requested < 0
                        or type(unit.get("cid")) is not int or unit["cid"] <= 0
                        or type(unit.get("page")) is not int or unit["page"] <= 0):
                    raise ValueError("无效的取帧命令")
                video_url(unit.get("bvid", ""), unit["page"])
                captured = session.capture(unit, requested)
                emit({"event": "frame", "captured_ms": captured["captured_ms"],
                      "time_source": captured["time_source"], "image": base64.b64encode(captured["data"]).decode("ascii")})
    except BilibiliError as error:
        emit({"event": "error", "code": error.code})
    except Exception:
        emit({"event": "error", "code": "capture_failed"})


if __name__ == "__main__":
    main()
