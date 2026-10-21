import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

import classcue.preparation_progress as telemetry


def test_transfer_eta_uses_new_bytes_and_expires_when_stalled(monkeypatch):
    clock = [0.]
    monkeypatch.setattr(telemetry.time, "monotonic", lambda: clock[0])
    events = []
    with telemetry.preparation_events(events.append):
        tracker = telemetry.DownloadProgress(1000)
        tracker.file("recognizer", "official/repo", "model.bin", 1000)
        tracker.report(600)  # Resumed cache: not 600 bytes received just now.
        assert events[-1]["eta_seconds"] is None
        clock[0] = 3
        tracker.report(660)
        assert events[-1]["speed_bytes_per_second"] == 20
        assert events[-1]["eta_seconds"] == 17
        clock[0] = 14
        tracker.report(660)
        assert events[-1]["waiting_for_network"] is True
        assert events[-1]["eta_seconds"] is None
        tracker.verifying("model.bin", checked=500, total=1000)
        assert events[-1]["phase"] == "verifying"
        assert events[-1]["eta_seconds"] is None
        assert events[-1]["phase_progress"] == .5


def test_file_switch_and_ignored_resume_range_reset_speed(monkeypatch):
    clock = [0.]
    monkeypatch.setattr(telemetry.time, "monotonic", lambda: clock[0])
    events = []
    with telemetry.preparation_events(events.append):
        tracker = telemetry.DownloadProgress(2000)
        tracker.file("recognizer", "repo", "first.bin", 1000)
        tracker.report(400)
        clock[0] = 3
        tracker.report(700)
        assert events[-1]["eta_seconds"] is not None
        tracker.report(100)  # Range ignored/restarted
        assert events[-1]["eta_seconds"] is None
        tracker.completed = 1000
        tracker.file("aligner", "repo2", "second.bin", 1000)
        assert events[-1]["bytes_completed"] == 1000
        assert events[-1]["current_file"] == "second.bin"
        assert events[-1]["eta_seconds"] is None


def test_child_events_are_delivered_before_child_finishes_and_do_not_leak_context(tmp_path):
    events = []
    payload = {"phase": "verifying", "label": "check", "message": "model.pt",
               "phase_progress": .5, "eta_seconds": None}
    child = f"import json,time; print({telemetry.PREFIX!r}+json.dumps({payload!r}),flush=True); time.sleep(1.4)"
    started = time.monotonic()
    with (tmp_path / "log.txt").open("w", encoding="utf-8") as log:
        with telemetry.preparation_events(lambda event: events.append((time.monotonic() - started, event))):
            result = telemetry.run_logged([sys.executable, "-u", "-c", child], relay=True,
                                          stdout=log, timeout=5)
    assert result.returncode == 0
    assert any(at < 1 and event["message"] == "model.pt" for at, event in events)
    assert telemetry._SINK.get() is None
    assert "model.pt" in (tmp_path / "log.txt").read_text(encoding="utf-8")


def test_installation_reports_step_and_package_without_publishing_urls():
    events = []
    child = "import time; print('Downloading torch from https://secret:password@example.test',flush=True); time.sleep(1.3)"
    with telemetry.preparation_events(events.append):
        result = telemetry.run_logged([sys.executable, "-u", "-c", child], step=2, steps=4, timeout=5)
    assert result.returncode == 0
    assert any(event.get("current_package") == "torch" for event in events)
    assert all(event["eta_seconds"] is None for event in events)
    assert events[-1]["step"] == 2
    assert "password" not in json.dumps(events)


def test_timeout_stops_silent_owned_child():
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        telemetry.run_logged([sys.executable, "-c", "import time; time.sleep(30)"], timeout=.3)
    assert time.monotonic() - started < 8


def test_callback_cancellation_stops_child_and_propagates():
    class Cancelled(Exception):
        pass
    def callback(event):
        if event.get("elapsed_seconds"):
            raise Cancelled()
    with telemetry.preparation_events(callback):
        with pytest.raises(Cancelled):
            telemetry.run_logged([sys.executable, "-c", "import time; time.sleep(30)"], timeout=5)


def test_validator_does_not_need_host_network_dependencies(tmp_path, monkeypatch):
    import classcue.asr_models as asr_models

    monkeypatch.setattr(asr_models, "MODEL_ROOT", tmp_path)
    directory = asr_models.model_directory("paraformer-large-cpu")
    directory.mkdir()
    manifest = {"repositories": dict(asr_models.get_model("paraformer-large-cpu").repositories),
                "files": {"recognizer/model.pt": 7}, "sha256": {}}
    (directory / "download.pending.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(sys, "argv", ["prepare_paraformer.py", "--validate-only"])
    assert telemetry.component_manifest("paraformer-large-cpu") == manifest


@pytest.mark.parametrize("model_id", [
    "whisper-turbo", "whisper-large-v3", "qwen3-asr-1.7b", "fireredasr2-aed",
    "paraformer-large-cpu", "dolphin-small-cn",
])
def test_preparation_finishes_its_telemetry_before_transcription(monkeypatch, tmp_path, model_id):
    import classcue.asr_models as asr_models
    import classcue.transcriber as transcriber
    monkeypatch.setattr(asr_models, "MODEL_ROOT", tmp_path / "models")
    for name in ("install_runtime", "install_firered_runtime", "install_paraformer_runtime", "install_dolphin_runtime"):
        monkeypatch.setattr(transcriber, name, lambda *_a, **_kw: telemetry.stage(
            "installing", "fixture dependency step", step=1, steps=1))
    monkeypatch.setattr(transcriber, "require_firered_gpu", lambda *_a: None)
    monkeypatch.setattr(transcriber, "download_model", lambda *_a, **_kw:
                        telemetry.stage("downloading", "fixture weights", phase_progress=.5))
    events = []
    with telemetry.preparation_events(events.append):
        transcriber.prepare_asr_model(model_id)
    assert events[0]["phase"] == "checking"
    assert any(event["phase"] == "downloading" for event in events)
    assert events[-1]["active"] is False
    assert events[-1]["phase"] == "finalizing"


def test_real_aria_rpc_reports_received_bytes_and_exits(tmp_path):
    """A throttled local transfer catches RPC-idle hangs and sparse-file false progress."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from scripts import prepare_asr_modelscope as downloader

    aria = downloader.ROOT / ".runtime/bin/aria2/aria2-1.37.0-win-64bit-build1/aria2c.exe"
    if not aria.is_file():
        pytest.skip("Bundled aria2 not present")
    content = b"x" * (4 * 1024 * 1024)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            try:
                for offset in range(0, len(content), 64 * 1024):
                    self.wfile.write(content[offset:offset + 64 * 1024])
                    self.wfile.flush()
                    time.sleep(.04)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    values = []
    try:
        downloader._aria_download([
            str(aria), f"--dir={tmp_path}", "--out=model.part", "--file-allocation=none",
            "--max-connection-per-server=1", "--split=1", "--console-log-level=warn",
            f"http://127.0.0.1:{server.server_port}/model",
        ], values.append, len(content), timeout=15)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert any(0 < value < len(content) for value in values)
    assert values[-1] == len(content)
    assert (tmp_path / "model.part").read_bytes() == content
