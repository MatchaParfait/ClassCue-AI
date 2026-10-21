"""Actual bundled downloader transfers against local HTTP; no user weights."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import subprocess
import signal
import sys
import threading
import time

import pytest

import asr_models
from model_downloads import PreparationPaused
from scripts import prepare_asr_modelscope as source


@pytest.fixture
def transfer(tmp_path, monkeypatch):
    aria = source.ROOT / ".runtime/bin/aria2/aria2-1.37.0-win-64bit-build1/aria2c.exe"
    if not aria.exists():
        pytest.skip("Bundled aria2 unavailable")
    monkeypatch.setattr(asr_models, "MODEL_ROOT", tmp_path / "models")
    content = b"0123456789abcdef" * (512 * 1024)
    requests = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            header = self.headers.get("Range")
            requests.append(header)
            first, last = 0, len(content) - 1
            if header:
                interval = header.removeprefix("bytes=").split("-", 1)
                first = int(interval[0])
                last = int(interval[1]) if interval[1] else last
            self.send_response(206 if header else 200)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(last - first + 1))
            if header:
                self.send_header("Content-Range", f"bytes {first}-{last}/{len(content)}")
            self.end_headers()
            try:
                for offset in range(first, last + 1, 64 * 1024):
                    self.wfile.write(content[offset:min(offset + 64 * 1024, last + 1)])
                    self.wfile.flush()
                    time.sleep(.035)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    destination = asr_models.component_paths("whisper-turbo")["recognizer"] / "model.bin"
    item = {"Size": len(content), "Sha256": hashlib.sha256(content).hexdigest()}
    try:
        yield destination, item, f"http://127.0.0.1:{server.server_port}/model", content, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_real_pause_saves_piece_map_and_continues_remaining_ranges(transfer):
    path, item, url, content, requests = transfer

    def pause(size):
        if len(content) // 3 <= size < len(content):
            raise PreparationPaused("pause local transfer")

    with pytest.raises(PreparationPaused):
        source._download(path, item, url, pause)
    assert not path.exists()
    assert path.with_name(path.name + ".part.aria2").exists()
    boundary = len(requests)
    values = []
    source._download(path, item, url, values.append)
    assert path.read_bytes() == content
    assert any(header and not header.startswith("bytes=0-") for header in requests[boundary:])
    assert values[-1] == len(content)
    assert not list(path.parent.glob("*.part*"))


def test_real_process_death_recovers_without_publishing_a_partial_model(transfer, tmp_path):
    from scripts.launcher_process import terminate_owned_tree, windows_process_is_alive
    path, item, url, content, requests = transfer
    record = tmp_path / "received.txt"
    script = """
import json, pathlib, sys, os
sys.path.insert(0, sys.argv[1])
import asr_models
from scripts import prepare_asr_modelscope as source
asr_models.MODEL_ROOT = pathlib.Path(sys.argv[2])
launch = source.subprocess.Popen
child_pid = None
def popen(*args, **kwargs):
    global child_pid
    process = launch(*args, **kwargs)
    child_pid = process.pid
    return process
source.subprocess.Popen = popen
def report(size):
    pathlib.Path(sys.argv[6]).write_text(json.dumps({"bytes": size, "owner": os.getpid(), "child": child_pid}))
source._download(pathlib.Path(sys.argv[3]), json.loads(sys.argv[4]), sys.argv[5], report)
"""
    process = subprocess.Popen([
        sys.executable, "-c", script, str(source.ROOT), str(asr_models.MODEL_ROOT),
        str(path), json.dumps(item), url, str(record),
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        start_new_session=os.name != "nt")
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                progress = json.loads(record.read_text())
                size = progress["bytes"]
            except (OSError, ValueError):
                size = 0
            if len(content) // 4 <= size < len(content) and path.with_name(path.name + ".part.aria2").exists():
                break
            assert process.poll() is None, "Transfer exited before interruption"
            time.sleep(.1)
        else:
            pytest.fail("No persisted partial download")
        if os.name == "nt":
            # Kill just the actual Python owner, not its whole tree. Its private
            # Windows job must reap aria2 even when finally blocks never run.
            os.kill(progress["owner"], signal.SIGTERM)
            process.wait(timeout=5)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and windows_process_is_alive(progress["child"]):
                time.sleep(.05)
            assert not windows_process_is_alive(progress["child"])
    finally:
        terminate_owned_tree(process)
    assert not path.exists()
    boundary = len(requests)
    source._download(path, item, url, lambda _: None)
    assert path.read_bytes() == content
    assert any(header and not header.startswith("bytes=0-") for header in requests[boundary:])
