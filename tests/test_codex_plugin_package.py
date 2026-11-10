"""Optional companion distribution through the real host, in an isolated home."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from classcue.codex_client import CodexClient


@pytest.mark.skipif(not os.getenv("CLASSCUE_TEST_CODEX_EXECUTABLE"), reason="isolated native Codex opt-in")
def test_repository_marketplace_installs_without_npm_and_connects_later(tmp_path, monkeypatch):
    project = Path(__file__).resolve().parents[1]
    repository = tmp_path / "source"
    plugin = repository / "integrations/codex-desktop"
    shutil.copytree(project / "integrations/codex-desktop", plugin,
                    ignore=shutil.ignore_patterns("node_modules", "test"))
    marketplace = repository / ".agents/plugins/marketplace.json"
    marketplace.parent.mkdir(parents=True)
    shutil.copyfile(project / ".agents/plugins/marketplace.json", marketplace)
    home = tmp_path / "isolated-codex"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = os.environ["CLASSCUE_TEST_CODEX_EXECUTABLE"]
    monkeypatch.setenv("RUST_LOG", "info")
    original_popen = subprocess.Popen
    log = tmp_path / "isolated-host.log"
    with log.open("ab") as output:
        monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: original_popen(
            *args, **{**kwargs, "stderr": output}))
        _exercise_host(executable, home, workspace, marketplace, tmp_path, log)


def _exercise_host(executable, home, workspace, marketplace, tmp_path, log):
    with CodexClient(executable, home) as client:
        detail = client.call("plugin/read", {
            "pluginName": "classcue", "marketplacePath": str(marketplace),
        })["plugin"]
        assert detail["mcpServers"] == ["classcue"]
        assert len(detail["skills"]) == 2
        result = client.call("plugin/install", {
            "pluginName": "classcue", "marketplacePath": str(marketplace),
        })
        assert result["appsNeedingAuth"] == []
    assert not list(home.rglob("node_modules"))
    with CodexClient(executable, home) as client:
        thread = client.call("thread/start", {
            "cwd": str(workspace), "approvalPolicy": "never",
        })["thread"]
        servers = client.call("mcpServerStatus/list", {"threadId": thread["id"]})["data"]
        server = next(row for row in servers if row.get("pluginId") == "classcue@classcue")
        assert server["toolsError"] is None, "\n".join(
            line for line in log.read_text(encoding="utf-8", errors="replace").splitlines()
            if "MCP server stderr" in line)[-10000:]
        assert len(server["tools"]) == 23
        assert "search_library" in server["tools"]
        assert "search_bookmarks" not in server["tools"]

        def call_tool(name, arguments=None):
            return client.call("mcpServer/tool/call", {
                "threadId": thread["id"], "server": server["name"],
                "tool": name, "arguments": arguments or {},
            })

        missing = call_tool("list_courses")
        assert missing["isError"] is True
        assert "尚未连接" in missing["content"][0]["text"]
        # A synthetic Python bridge proves the installed copy discovers the connection.
        # No real library, service, model or credential is used.
        application = tmp_path / "synthetic-app"
        (application / "scripts").mkdir(parents=True)
        (application / "scripts/codex_course_tools.py").write_text(
            "import json, sys\n"
            "assert sys.argv[1:] == ['--tool', 'list_courses']\n"
            "assert json.load(sys.stdin) == {}\n"
            "print(json.dumps({'result': {'courses': [], 'total': 0, 'limit': 20, 'offset': 0}}))\n",
            encoding="utf-8",
        )
        connection = home / "classcue/connection.json"
        connection.parent.mkdir()
        connection.write_text(json.dumps({
            "format": "classcue-codex-connection", "version": 1, "roots": [str(workspace)],
            "application": {"project_root": str(application), "python": sys.executable},
        }), encoding="utf-8")
        connected = call_tool("list_courses")
        assert not connected.get("isError"), connected
        assert connected["structuredContent"]["courses"] == []
