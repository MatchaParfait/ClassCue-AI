"""Bounded local PCM transport for the player's optional GTCRN filter."""

from __future__ import annotations

import asyncio
import json
import logging
import struct
from threading import BoundedSemaphore
from urllib.parse import urlsplit

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from playback_denoise import HOP_SIZE, SAMPLE_RATE, PlaybackDenoiser

router = APIRouter()
LOGGER = logging.getLogger(f"classroom.{__name__}")
STREAM_SLOTS = BoundedSemaphore(4)


@router.websocket("/api/audio/denoise")
async def denoise_audio(ws: WebSocket):
    try:
        origin = urlsplit(ws.headers.get("origin", ""))
    except ValueError:
        await ws.close(code=1008)
        return
    if origin.scheme not in ("http", "https") or origin.hostname not in ("localhost", "127.0.0.1", "::1"):
        await ws.close(code=1008)
        return
    await ws.accept()
    if not STREAM_SLOTS.acquire(blocking=False):
        await ws.send_json({"type": "error", "message": "降噪会话已满，请关闭其他播放器的降噪"})
        await ws.close(code=1013)
        return
    pipeline = None
    epoch = None
    expected = 0
    try:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break
            if message.get("text") is not None:
                text = message["text"]
                if len(text) > 1024:
                    raise ValueError("Oversized configuration")
                config = json.loads(text)
                if not isinstance(config, dict) or config.get("type") != "reset":
                    raise ValueError("Expected stream reset")
                epoch = config.get("epoch")
                if type(epoch) is not int or not 0 <= epoch <= 0xFFFFFFFF:
                    raise ValueError("Invalid epoch")
                if pipeline is None:
                    pipeline = await asyncio.to_thread(PlaybackDenoiser)
                else:
                    await asyncio.to_thread(pipeline.reset)
                expected = 0
                await ws.send_json({"type": "ready", "epoch": epoch, "sample_rate": SAMPLE_RATE})
                continue
            data = message.get("bytes", b"")
            if pipeline is None or len(data) != 8 + HOP_SIZE * 4:
                raise ValueError("Invalid PCM packet")
            packet_epoch, sequence = struct.unpack_from("<II", data)
            if packet_epoch != epoch:
                continue
            if sequence != expected:
                raise ValueError("Out-of-order PCM packet")
            expected += 1
            output = await asyncio.to_thread(pipeline.process, data[8:])
            await ws.send_bytes(data[:8] + output)
    except WebSocketDisconnect:
        pass
    except Exception:
        LOGGER.exception("Playback denoise failed")
        try:
            await ws.send_json({"type": "error", "message": "降噪服务不可用，已恢复原声，请重试"})
            await ws.close(code=1011)
        except (WebSocketDisconnect, RuntimeError):
            pass
    finally:
        STREAM_SLOTS.release()
