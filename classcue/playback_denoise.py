"""GTCRN streaming inference for playback, independent of transcription."""

from __future__ import annotations

from classcue.paths import PROJECT_ROOT

import hashlib
from pathlib import Path
from threading import Lock

ASSET_ROOT = PROJECT_ROOT / "assets" / "gtcrn"
MODEL_PATH = ASSET_ROOT / "gtcrn_simple.onnx"
LICENSE_PATH = ASSET_ROOT / "LICENSE.txt"
MODEL_COMMIT = "502ebfab64da7c4a9af78dcb9c6ceef1ebb01c73"
MODEL_GIT_BLOB = "015454eef64bd4328e6ff4946c231d4a4a632e93"
SAMPLE_RATE = 16000
HOP_SIZE = 256
FFT_SIZE = 512
_session = None
_session_lock = Lock()


def verify_model(data: bytes):
    digest = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
    if digest != MODEL_GIT_BLOB:
        raise ValueError("GTCRN checkpoint hash mismatch")


def verify_model_assets() -> Path:
    """Validate the release resources without downloading or writing anything."""
    verify_model(MODEL_PATH.read_bytes())
    if not LICENSE_PATH.is_file() or not LICENSE_PATH.read_text(encoding="utf-8").strip():
        raise RuntimeError("Bundled GTCRN license is missing")
    return MODEL_PATH


def model_session():
    global _session
    with _session_lock:
        if _session is None:
            import onnxruntime as ort

            model_path = verify_model_assets()
            options = ort.SessionOptions()
            options.intra_op_num_threads = 1
            options.inter_op_num_threads = 1
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            options.add_session_config_entry("session.intra_op.allow_spinning", "0")
            options.add_session_config_entry("session.inter_op.allow_spinning", "0")
            _session = ort.InferenceSession(
                str(model_path), sess_options=options, providers=["CPUExecutionProvider"],
            )
        return _session


class PlaybackDenoiser:
    """Shared read-only weights; STFT and recurrent caches belong to one stream."""

    def __init__(self, session=None):
        import numpy as np

        self.np = np
        self.session = session if session is not None else model_session()
        self.window = np.sin(np.pi * np.arange(FFT_SIZE) / FFT_SIZE).astype(np.float32)
        self.reset()

    def reset(self):
        np = self.np
        self.previous = np.zeros(HOP_SIZE, np.float32)
        self.overlap = np.zeros(HOP_SIZE, np.float32)
        self.caches = {
            "conv_cache": np.zeros((2, 1, 16, 16, 33), np.float32),
            "tra_cache": np.zeros((2, 3, 1, 1, 16), np.float32),
            "inter_cache": np.zeros((2, 1, 33, 16), np.float32),
        }

    def process(self, pcm: bytes) -> bytes:
        np = self.np
        if len(pcm) != HOP_SIZE * 4:
            raise ValueError("Expected 256 float32 samples")
        samples = np.frombuffer(pcm, dtype="<f4")
        if not np.isfinite(samples).all():
            raise ValueError("Non-finite audio")
        spectrum = np.fft.rfft(np.concatenate((self.previous, samples)) * self.window)
        inputs = dict(self.caches)
        inputs["mix"] = np.stack((spectrum.real, spectrum.imag), axis=-1).astype(np.float32)[None, :, None, :]
        enhanced, conv, tra, inter = self.session.run(None, inputs)
        self.caches = {"conv_cache": conv, "tra_cache": tra, "inter_cache": inter}
        spectrum = enhanced[0, :, 0, 0] + 1j * enhanced[0, :, 0, 1]
        frame = np.fft.irfft(spectrum, n=FFT_SIZE).astype(np.float32) * self.window
        result = self.overlap + frame[:HOP_SIZE]
        self.overlap = frame[HOP_SIZE:].copy()
        self.previous = samples.copy()
        if not np.isfinite(result).all():
            raise RuntimeError("Non-finite model output")
        return result.astype("<f4").tobytes()
