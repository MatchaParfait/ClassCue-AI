from __future__ import annotations

import struct
from contextlib import ExitStack

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import api.denoise as transport
from classcue.playback_denoise import HOP_SIZE, MODEL_PATH, PlaybackDenoiser
from scripts.prepare_gtcrn import verify


class IdentitySession:
    def run(self, _names, inputs):
        return inputs["mix"], inputs["conv_cache"], inputs["tra_cache"], inputs["inter_cache"]


def test_overlap_add_preserves_audio_with_identity_mask():
    pipeline = PlaybackDenoiser(IdentitySession())
    audio = np.random.default_rng(42).normal(0, 0.1, HOP_SIZE * 30).astype("<f4")
    result = np.concatenate([
        np.frombuffer(pipeline.process(frame.tobytes()), dtype="<f4")
        for frame in audio.reshape(-1, HOP_SIZE)
    ])
    np.testing.assert_allclose(result[HOP_SIZE:], audio[:-HOP_SIZE], atol=1e-7)


@pytest.mark.parametrize("pcm", [b"x", np.full(HOP_SIZE, np.nan, "<f4").tobytes(), np.full(HOP_SIZE, np.inf, "<f4").tobytes()])
def test_rejects_invalid_audio(pcm):
    with pytest.raises(ValueError):
        PlaybackDenoiser(IdentitySession()).process(pcm)


def test_bundled_model_is_verified_and_state_is_independent():
    verify(MODEL_PATH.read_bytes())
    first, second = PlaybackDenoiser(), PlaybackDenoiser()
    assert first.session is second.session
    assert first.caches["inter_cache"] is not second.caches["inter_cache"]
    pcm = np.random.default_rng(2).normal(0, 0.1, (20, HOP_SIZE)).astype("<f4")
    initial = [first.process(frame.tobytes()) for frame in pcm]
    assert initial == [second.process(frame.tobytes()) for frame in pcm]
    first.reset()
    assert initial == [first.process(frame.tobytes()) for frame in pcm]
    audio = np.frombuffer(b"".join(initial), dtype="<f4")
    assert np.isfinite(audio).all() and np.max(np.abs(audio)) > 0.001
    with pytest.raises(ValueError):
        verify(b"wrong checkpoint")


@pytest.fixture
def client(monkeypatch):
    class FakePipeline:
        def reset(self):
            pass

        def process(self, pcm):
            return pcm

    monkeypatch.setattr(transport, "PlaybackDenoiser", FakePipeline)
    app = FastAPI()
    app.include_router(transport.router)
    return TestClient(app)


def connect(client):
    return client.websocket_connect("/api/audio/denoise", headers={"origin": "http://127.0.0.1:5175"})


def packet(epoch=1, sequence=0):
    return struct.pack("<II", epoch, sequence) + bytes(HOP_SIZE * 4)


@pytest.mark.parametrize("origin", ["https://evil.example", "null", "file://localhost", "", "http://[broken"])
def test_websocket_rejects_non_local_browser_origins(client, origin):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/audio/denoise", headers={"origin": origin}):
            pass


def test_stream_reset_ignores_old_packets_and_restarts_sequence(client):
    with connect(client) as ws:
        ws.send_json({"type": "reset", "epoch": 1})
        assert ws.receive_json()["sample_rate"] == 16000
        ws.send_bytes(packet())
        assert ws.receive_bytes() == packet()
        ws.send_json({"type": "reset", "epoch": 2})
        assert ws.receive_json()["epoch"] == 2
        ws.send_bytes(packet(1, 1))
        ws.send_bytes(packet(2))
        assert ws.receive_bytes() == packet(2)


@pytest.mark.parametrize("config", [
    {"type": "reset", "epoch": True}, {"type": "reset", "epoch": -1},
    {"type": "reset", "epoch": 2**32}, {"type": "other", "epoch": 1}, [],
])
def test_invalid_configuration_fails_with_recoverable_message(client, config):
    with connect(client) as ws:
        ws.send_json(config)
        response = ws.receive_json()
        assert response["type"] == "error" and "原声" in response["message"]


def test_bad_packet_and_sequence_are_rejected(client):
    for data in [b"x", packet(1, 3)]:
        with connect(client) as ws:
            ws.send_json({"type": "reset", "epoch": 1})
            ws.receive_json()
            ws.send_bytes(data)
            assert ws.receive_json()["type"] == "error"


def test_initialization_error_does_not_expose_local_paths_and_releases_slot(client, monkeypatch):
    def broken():
        raise RuntimeError("private/path/model.onnx")
    monkeypatch.setattr(transport, "PlaybackDenoiser", broken)
    with connect(client) as ws:
        ws.send_json({"type": "reset", "epoch": 1})
        response = ws.receive_json()
        assert "private" not in response["message"]
    assert transport.STREAM_SLOTS.acquire(blocking=False)
    transport.STREAM_SLOTS.release()


def test_connection_limit_releases_slots_after_tabs_close(client):
    with ExitStack() as stack:
        for _ in range(4):
            stack.enter_context(connect(client))
        with connect(client) as ws:
            assert "会话已满" in ws.receive_json()["message"]
    with connect(client) as ws:
        ws.send_json({"type": "reset", "epoch": 1})
        assert ws.receive_json()["type"] == "ready"
