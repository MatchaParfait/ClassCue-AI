"""Verify image delivery to the real host using only a local Responses stub."""

import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from threading import Thread

from PIL import Image
import pytest


@pytest.mark.skipif(not os.getenv("CLASSCUE_TEST_CODEX_EXECUTABLE"), reason="isolated native Codex opt-in")
def test_native_codex_receives_slide_and_frame_pixels_in_model_input(tmp_path):
    node = os.getenv("CODEX_MCP_NODE_PATH") or shutil.which("node")
    if not node:
        pytest.skip("Node is required")
    project = Path(__file__).resolve().parents[1]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "codex-home"
    home.mkdir()
    application = tmp_path / "synthetic-app"
    (application / "scripts").mkdir(parents=True)
    images = {}
    for name, color in [("read_slide", (20, 170, 50)), ("read_video_frame", (200, 20, 10))]:
        output = BytesIO()
        Image.new("RGB", (160, 96), color).save(output, format="JPEG")
        images[name] = base64.b64encode(output.getvalue()).decode("ascii")
    (application / "images.json").write_text(json.dumps(images), encoding="utf-8")
    (application / "scripts/codex_course_tools.py").write_text(
        "import json, sys\nfrom pathlib import Path\n"
        "name = sys.argv[2]\n"
        "args = json.load(sys.stdin)\n"
        "assert args == ({'video_id': 1, 'slide_id': 2} if name == 'read_slide' else "
        "{'video_id': 1, 'start_seconds': 0.2})\n"
        "images = json.loads((Path(__file__).resolve().parents[1] / 'images.json').read_text())\n"
        "result = {'library_id': 'synthetic', 'video_id': 1, 'image': "
        "{'mime_type': 'image/jpeg', 'data': images[name], 'width': 160, 'height': 96}}\n"
        "result.update({'slide_id': 2, 'evidence_type': 'extracted_slide'} if name == 'read_slide' "
        "else {'time_seconds': 0.2, 'evidence_type': 'video_frame', 'play_url': 'http://127.0.0.1/'})\n"
        "print(json.dumps({'result': result}))\n",
        encoding="utf-8",
    )
    connection = tmp_path / "connection.json"
    connection.write_text(json.dumps({
        "format": "classcue-codex-connection", "version": 1, "roots": [str(workspace)],
        "application": {"project_root": str(application), "python": sys.executable},
    }), encoding="utf-8")
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(payload)
            if len(requests) == 1:
                items = []
                for name, args in [("read_slide", {"video_id": 1, "slide_id": 2}),
                                   ("read_video_frame", {"video_id": 1, "start_seconds": 0.2})]:
                    namespace = next((tool["name"] for tool in payload["tools"]
                                      if tool["type"] == "namespace"
                                      and any(child["name"] == name for child in tool["tools"])), None)
                    qualified = name if namespace else next(
                        tool["name"] for tool in payload["tools"] if tool.get("name", "").endswith("__" + name))
                    item = {"type": "function_call", "id": "fc_" + name,
                            "call_id": "call_" + name, "name": qualified,
                            "arguments": json.dumps(args), "status": "completed"}
                    if namespace:
                        item["namespace"] = namespace
                    items.append(item)
            else:
                items = [{"type": "message", "id": "msg_done", "role": "assistant",
                          "status": "completed", "content": [
                              {"type": "output_text", "text": "Synthetic image transport checked.",
                               "annotations": []}]}]
            events = [("response.created", {"response": {"id": "resp_synthetic", "status": "in_progress"}})]
            for index, item in enumerate(items):
                events.extend([
                    ("response.output_item.added", {"output_index": index, "item": item}),
                    ("response.output_item.done", {"output_index": index, "item": item}),
                ])
            events.append(("response.completed", {"response": {
                "id": "resp_synthetic", "status": "completed", "output": items,
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            }}))
            body = "".join(f"event: {name}\ndata: {json.dumps({'type': name, **data})}\n\n"
                           for name, data in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = "\n".join([
        'model = "gpt-5.4"',
        'model_provider = "synthetic"',
        'approval_policy = "never"',
        'features.remote_plugin = false',
        '[model_providers.synthetic]',
        'name = "Local synthetic transport test"',
        f'base_url = "http://127.0.0.1:{server.server_port}/v1"',
        'wire_api = "responses"',
        'requires_openai_auth = false',
        '[mcp_servers.classcue]',
        f'command = {json.dumps(node)}',
        f'args = [{json.dumps(str(project / "integrations/codex-desktop/server.bundle.mjs"))}]',
        '[mcp_servers.classcue.env]',
        f'CLASSCUE_CODEX_CONNECTION_FILE = {json.dumps(str(connection))}',
    ])
    (home / "config.toml").write_text(config, encoding="utf-8")
    env = {**os.environ, "CODEX_HOME": str(home)}
    for key in ["OPENAI_API_KEY", "CODEX_API_KEY"]:
        env.pop(key, None)
    try:
        result = subprocess.run([
            os.environ["CLASSCUE_TEST_CODEX_EXECUTABLE"], "exec", "--json",
            "--skip-git-repo-check", "-C", str(workspace),
            "Read the synthetic slide and frame with the ClassCue tools.",
        ], env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=90)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(requests) == 2, result.stdout + result.stderr
    # Inspect what the host actually sends onward, not just the raw MCP response.
    inputs = requests[-1]["input"]
    outputs = [item for item in inputs if item.get("type") == "function_call_output"]
    assert len(outputs) == 2, result.stdout + result.stderr
    for name, expected in images.items():
        output = next(item["output"] for item in outputs if item["call_id"] == "call_" + name)
        assert isinstance(output, list), f"{name} lost image content: {output}"
        actual = [part["image_url"] for part in output if part.get("type") == "input_image"]
        assert actual == ["data:image/jpeg;base64," + expected], f"{name} lost image bytes"
        text = [part["text"] for part in output if part["type"] == "input_text"]
        assert all(expected not in part for part in text)
        metadata_lines = [line for part in text for line in part.splitlines() if line.startswith("{")]
        assert len(metadata_lines) == 1
        metadata = json.loads(metadata_lines[0])
        assert metadata["image"] == {"mime_type": "image/jpeg", "width": 160, "height": 96}
