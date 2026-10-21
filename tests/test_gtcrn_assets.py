from pathlib import Path
import shutil
import subprocess
import sys

import pytest

import classcue.playback_denoise as playback_denoise
from scripts import prepare_gtcrn
from classcue.paths import PROJECT_ROOT


def test_fresh_distribution_prepares_and_loads_offline_without_runtime_data(tmp_path):
    """Only release source/assets are present; no cached weights or experiment."""
    release = tmp_path / "release"
    (release / "scripts").mkdir(parents=True)
    root = PROJECT_ROOT
    package = release / "classcue"
    package.mkdir()
    for name in ("__init__.py", "paths.py", "playback_denoise.py"):
        shutil.copyfile(root / "classcue" / name, package / name)
    shutil.copyfile(root / "scripts" / "prepare_gtcrn.py", release / "scripts" / "prepare_gtcrn.py")
    shutil.copytree(root / "assets" / "gtcrn", release / "assets" / "gtcrn")
    code = """
import runpy, socket, sys, urllib.request
from pathlib import Path
def forbidden(*args, **kwargs):
    raise AssertionError("Bundled model must work without network access")
socket.create_connection = forbidden
urllib.request.urlopen = forbidden
script = Path(sys.argv[1])
runpy.run_path(str(script), run_name="__main__")
from classcue import playback_denoise
import numpy as np
denoiser = playback_denoise.PlaybackDenoiser()
pcm = np.random.default_rng(42).normal(0, .1, (20, 256)).astype("<f4")
output = np.frombuffer(b"".join(denoiser.process(frame.tobytes()) for frame in pcm), dtype="<f4")
assert np.isfinite(output).all() and np.max(np.abs(output)) > .001
assert denoiser.session.get_providers() == ["CPUExecutionProvider"]
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(release / "scripts" / "prepare_gtcrn.py")],
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Bundled GTCRN playback model ready:" in result.stdout
    assert not (release / "data").exists()
    assert not (release / "gtcrn-realtime-experiment").exists()


@pytest.fixture
def isolated_assets(tmp_path, monkeypatch):
    model = tmp_path / "gtcrn_simple.onnx"
    license = tmp_path / "LICENSE.txt"
    shutil.copyfile(playback_denoise.MODEL_PATH, model)
    shutil.copyfile(playback_denoise.LICENSE_PATH, license)
    monkeypatch.setattr(playback_denoise, "MODEL_PATH", model)
    monkeypatch.setattr(playback_denoise, "LICENSE_PATH", license)
    monkeypatch.setattr(playback_denoise, "_session", None)
    return model, license


@pytest.mark.parametrize("missing", ["model", "license"])
def test_incomplete_distribution_fails_in_preparation_and_loading(isolated_assets, missing):
    model, license = isolated_assets
    (model if missing == "model" else license).unlink()
    for action in (prepare_gtcrn.main, playback_denoise.model_session):
        with pytest.raises((FileNotFoundError, RuntimeError)):
            action()
    assert playback_denoise._session is None


def test_corrupt_bundle_is_rejected_before_inference(isolated_assets):
    model, _license = isolated_assets
    model.write_bytes(b"corrupted checkpoint")
    for action in (prepare_gtcrn.main, playback_denoise.model_session):
        with pytest.raises(ValueError, match="hash mismatch"):
            action()
    assert playback_denoise._session is None
