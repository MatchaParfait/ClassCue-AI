import base64
import json
import os
import sqlite3
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.codex_bridge import router
from classcue.codex_bridge import CodexBridge, folder_name
from classcue.codex_client import CodexClient
from classcue.codex_drafts import connect_workspace
from classcue.dsh_pipe import BridgeFailure
from classcue.sqlite_connection import ClosingConnection


class Store:
    def __init__(self, path):
        self.path = path

    def connect(self):
        db = sqlite3.connect(self.path, factory=ClosingConnection)
        db.row_factory = sqlite3.Row
        return db


class FakeCodex:
    def __init__(self, *_):
        self.identity = {"userAgent": "synthetic-codex"}
        self.calls = []
        self.threads = {}
        self.sections = {}
        self.failure = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def call(self, method, params):
        self.calls.append((method, params))
        assert method != "turn/start"
        if self.failure == method:
            raise BridgeFailure("codex_timeout", "synthetic timeout")
        if method == "threadSection/list":
            return {"data": list(self.sections.values())}
        if method == "threadSection/create":
            section = {"id": str(uuid4()), "name": params["name"]}
            self.sections[section["id"]] = section
            return {"section": section}
        if method == "thread/start":
            thread = {"id": str(uuid4()), "cwd": params["cwd"], "name": None, "items": []}
            self.threads[thread["id"]] = thread
            return {"thread": thread}
        thread = self.threads.get(params["threadId"])
        if not thread:
            raise BridgeFailure("codex_rpc_error", "synthetic missing thread")
        if method == "thread/inject_items":
            thread["items"].extend(params["items"])
        if method == "thread/name/set":
            thread["name"] = params["name"]
        if method == "thread/section/move":
            thread["section"] = self.sections[params["sectionId"]]
        return {"thread": thread}


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    client = FakeCodex()
    client.native_calls = []
    version = {"value": 1}

    def snapshot(_store, library, question):
        return {"library_id": library, "course_id": "database", "course_name": "数据库",
                "document_id": "paper", "source": "试卷1.pdf", "title": "第1题",
                "question_id": question, "content_version": version["value"],
                "content": [{"type": "text", "text": "合成题目：2 + 3 = ?"}]}

    def native(**kwargs):
        client.native_calls.append(kwargs)
        return {"status": "attached", "image_count": len(kwargs["images"])}

    bridge = CodexBridge(Store(tmp_path / "project.db"), "library", snapshot, lambda *_: client,
                         lambda _: None, native_inserter=native)
    bridge.configure(str(tmp_path), executable=__file__)
    return bridge, client, version


def test_reopen_preserves_thread_context_and_user_title(service):
    bridge, client, _ = service
    first = bridge.prepare("q")
    thread = client.threads[first["thread_id"]]
    thread["name"] = "用户自行改名"
    thread["items"].append({"type": "user-draft", "text": "保留草稿"})
    reopened = bridge.prepare("q")
    assert reopened["thread_id"] == first["thread_id"]
    assert thread["name"] == "用户自行改名"
    assert len(client.threads) == 1
    assert not any(call[0] == "thread/inject_items" for call in client.calls)
    assert first["draft_transfer"]["status"] == "attached"
    assert first["image_count"] == 0
    assert thread["items"][-1]["text"] == "保留草稿"
    restarted = CodexBridge(bridge.store, "library", bridge.snapshot_provider, lambda *_: client, lambda _: None)
    assert restarted.resume(first["request_id"])["thread_id"] == first["thread_id"]
    assert Path(first["context_file"]).parent.parent.parent.name == ".classcue"


def test_changed_question_requires_new_discussion_and_keeps_old_version(service):
    bridge, client, version = service
    old = bridge.prepare("q")
    version["value"] = 2
    with pytest.raises(BridgeFailure, match="题目内容已修改"):
        bridge.prepare("q")
    new = bridge.prepare("q", new_discussion=True)
    assert new["thread_id"] != old["thread_id"]
    assert bridge.resume(old["request_id"])["content_version"] == 1
    assert len(bridge.history("q")) == 2
    assert len(client.threads) == 2


@pytest.mark.parametrize("method", ["thread/start"])
def test_uncertain_mutation_never_duplicates_thread_or_question(service, method):
    bridge, client, _ = service
    client.failure = method
    with pytest.raises(BridgeFailure):
        bridge.prepare("q")
    count = len(client.calls)
    client.failure = None
    with pytest.raises(BridgeFailure) as error:
        bridge.prepare("q")
    assert error.value.code == "codex_outcome_unknown"
    assert len(client.calls) == count
    assert len(bridge.history("q")) == 1


def test_recover_known_bound_thread_without_hidden_injection(service):
    bridge, client, _ = service
    receipt = bridge.prepare("q")
    receipt["status"] = "bound"
    client.threads[receipt["thread_id"]]["items"].clear()
    bridge._save(receipt)
    recovered = bridge.resume(receipt["request_id"])
    assert recovered["status"] == "ready"
    assert len(client.threads) == 1
    assert client.threads[receipt["thread_id"]]["items"] == []
    assert receipt["image_count"] == 0
    assert recovered["draft_transfer"]["status"] == "attached"


def test_missing_native_thread_never_creates_replacement(service):
    bridge, client, _ = service
    receipt = bridge.prepare("q")
    del client.threads[receipt["thread_id"]]
    with pytest.raises(BridgeFailure):
        bridge.prepare("q")
    assert len(client.threads) == 0
    assert len(bridge.history("q")) == 1


def test_course_group_is_native_and_shared_by_multiple_discussions(service):
    bridge, client, _ = service
    first = bridge.prepare("q")
    second = bridge.prepare("q2")
    assert len(client.sections) == 1
    assert first["section_id"] == second["section_id"]
    assert client.threads[first["thread_id"]]["section"]["id"] == first["section_id"]
    assert client.sections[first["section_id"]]["name"] == "ClassCue · 数据库"


def test_native_group_deletion_never_creates_replacement_groups(service):
    bridge, client, _ = service
    first = bridge.prepare("q")
    del client.sections[first["section_id"]]
    with pytest.raises(BridgeFailure) as error:
        bridge.prepare("q2")
    assert error.value.code == "codex_group_missing"
    assert not client.sections
    assert len(client.threads) == 1


def test_uncertain_group_creation_is_not_repeated(service):
    bridge, client, _ = service
    client.failure = "threadSection/create"
    with pytest.raises(BridgeFailure):
        bridge.prepare("q")
    count = len(client.calls)
    client.failure = None
    with pytest.raises(BridgeFailure) as error:
        bridge.prepare("q")
    assert error.value.code == "codex_group_unknown"
    assert len([call for call in client.calls[count:] if call[0] == "threadSection/create"]) == 0
    assert len(client.threads) == 0


def test_images_and_text_remain_in_source_order(service):
    bridge, client, _ = service
    original = bridge.snapshot_provider
    images = [image_part("white"), image_part("black")]

    def snapshot(*args):
        value = original(*args)
        value["content"] = [
            images[0],
            {"type": "text", "text": "小问一"},
            images[1],
        ]
        return value

    bridge.snapshot_provider = snapshot
    receipt = bridge.prepare("q")
    parts = json.loads(Path(receipt["context_file"]).read_text(encoding="utf-8"))["content"]
    assert [part["type"] for part in parts] == ["image", "text", "image"]
    assert parts[0]["data"] == images[0]["data"]
    assert parts[2]["data"] == images[1]["data"]
    assert client.threads[receipt["thread_id"]]["items"] == []
    assert receipt["image_count"] == 2

def image_part(color="white"):
    from PIL import Image
    output = BytesIO()
    Image.new("RGB", (160, 96), color).save(output, format="PNG")
    return {"type": "image", "media_type": "image/png",
            "data": base64.b64encode(output.getvalue()).decode("ascii")}


def test_copy_images_uses_original_snapshot_in_order_and_preserves_draft(service):
    bridge, client, _ = service
    original = bridge.snapshot_provider
    images = [image_part("white"), image_part("black")]

    def snapshot(*args):
        value = original(*args)
        value["content"] = [images[0], {"type": "text", "text": "小问"}, images[1]]
        return value

    bridge.snapshot_provider = snapshot
    first = bridge.prepare("q")
    thread = client.threads[first["thread_id"]]
    thread["items"].append({"type": "user-draft", "text": "保留草稿"})
    before = json.dumps(thread)
    copied, opened = [], []
    bridge.clipboard_writer = lambda paths: copied.append(list(paths))
    bridge.opener = opened.append
    bridge.snapshot_provider = lambda *_: pytest.fail("历史补图不应读取现题")
    recovered = bridge.resume(first["request_id"], copy_images=True)
    assert recovered["image_transfer"] == {"status": "paste_required", "image_count": 2}
    assert [path.read_bytes() for path in copied[0]] == [base64.b64decode(part["data"]) for part in images]
    assert [path.name[:11] for path in copied[0]] == ["question-01", "question-02"]
    assert opened == [first["uri"]]
    assert json.dumps(thread) == before
    assert not any(call[0] == "thread/inject_items" for call in client.calls)
    assert "image_transfer" not in bridge.history("q")[0]
    bridge.resume(first["request_id"])
    assert len(copied) == 1
    # Repair an altered generated copy from the immutable original snapshot.
    copied[0][0].write_bytes(b"damaged cache")
    bridge.resume(first["request_id"], copy_images=True)
    assert copied[1][0].read_bytes() == base64.b64decode(images[0]["data"])


def test_copy_from_primary_action_reopens_same_thread_and_does_not_claim_attached(service):
    bridge, client, _ = service
    original = bridge.snapshot_provider

    def snapshot(*args):
        return {**original(*args), "content": [image_part()]}

    bridge.snapshot_provider = snapshot
    copied = []
    bridge.clipboard_writer = copied.append
    first = bridge.prepare("q", copy_images=True)
    second = bridge.prepare("q", copy_images=True)
    assert first["thread_id"] == second["thread_id"]
    assert len(client.threads) == 1
    assert first["image_transfer"]["status"] == "paste_required"
    assert copied[0] == copied[1]


def test_text_only_questions_leave_clipboard_untouched(service):
    bridge, _, _ = service
    bridge.clipboard_writer = lambda _: pytest.fail("纯文字题不能改剪贴板")
    receipt = bridge.prepare("q", copy_images=True)
    assert receipt["image_transfer"] == {"status": "no_images", "image_count": 0}


def test_clipboard_failure_preserves_binding_and_can_retry_without_reinjection(service):
    bridge, client, _ = service
    original = bridge.snapshot_provider
    bridge.snapshot_provider = lambda *args: {**original(*args), "content": [image_part()]}

    def unavailable(_):
        raise OSError("synthetic busy clipboard")

    opened = []
    bridge.clipboard_writer = unavailable
    bridge.opener = opened.append
    with pytest.raises(BridgeFailure) as error:
        bridge.prepare("q", copy_images=True)
    assert error.value.code == "clipboard_unavailable"
    receipt = bridge.history("q")[0]
    assert receipt["status"] == "ready"
    assert not opened
    bridge.clipboard_writer = lambda _: None
    result = bridge.resume(receipt["request_id"], copy_images=True)
    assert result["thread_id"] == receipt["thread_id"]
    assert len(client.threads) == 1
    assert not any(call[0] == "thread/inject_items" for call in client.calls)


def test_corrupt_image_is_rejected_before_clipboard_or_navigation(service):
    bridge, _, _ = service
    original = bridge.snapshot_provider
    bridge.snapshot_provider = lambda *args: {**original(*args), "content": [
        {"type": "image", "media_type": "image/png", "data": base64.b64encode(b"bad PNG").decode("ascii")}]}
    bridge.clipboard_writer = lambda _: pytest.fail("损坏图片不能复制")
    bridge.opener = lambda _: pytest.fail("损坏图片不能报告跳转成功")
    with pytest.raises(OSError):
        bridge.prepare("q", copy_images=True)
    assert bridge.history("q")[0]["status"] == "ready"


def test_root_change_reuses_source_binding_and_names_do_not_identify_threads(service, tmp_path):
    bridge, _, _ = service
    first = bridge.prepare("q")
    other = tmp_path / "other"
    other.mkdir()
    bridge.configure(str(other), executable=__file__)
    second = bridge.prepare("q2")
    assert second["source_path"] == first["source_path"]
    assert second["thread_id"] != first["thread_id"]
    assert folder_name("CON", "a").startswith("_CON-")
    assert folder_name("同名", "a") != folder_name("同名", "b")


def test_history_does_not_start_codex_and_ordinary_browser_is_disabled(service):
    bridge, client, _ = service
    bridge.prepare("q")
    calls = len(client.calls)
    assert len(bridge.history("q")) == 1
    assert len(client.calls) == calls

    class Provider:
        def get(self):
            return bridge.store

        def library_id(self):
            return "library"

    app = FastAPI()
    app.include_router(router(Provider(), bridge_factory=lambda *_: bridge, desktop=lambda: True))
    api = TestClient(app)
    assert api.get("/api/codex-bridge/settings").status_code == 200
    assert api.post("/api/codex-bridge/discussions", json={"question_id": "q", "content": []}).status_code == 422
    assert api.get("/api/codex-bridge/questions/q/discussions").json()[0]["thread_id"]
    receipt = bridge.history("q")[0]
    copied = api.post(f"/api/codex-bridge/requests/{receipt['request_id']}/copy-images")
    assert copied.status_code == 200
    assert copied.json()["image_transfer"]["status"] == "no_images"
    web = FastAPI()
    web.include_router(router(Provider(), desktop=lambda: False))
    assert TestClient(web).get("/api/codex-bridge/settings").status_code == 404
    assert TestClient(web).post(f"/api/codex-bridge/requests/{receipt['request_id']}/copy-images").status_code == 404


@pytest.mark.skipif(not os.getenv("CLASSCUE_TEST_CODEX_EXECUTABLE"), reason="isolated native Codex opt-in")
def test_native_codex_restarts_and_keeps_unicode_title_and_images(tmp_path):
    executable = os.environ["CLASSCUE_TEST_CODEX_EXECUTABLE"]
    root = tmp_path / "workspace"
    root.mkdir()
    home = tmp_path / "codex-home"
    home.mkdir()
    import shutil
    import subprocess
    node = os.getenv("CODEX_MCP_NODE_PATH") or shutil.which("node")
    source = Path(__file__).resolve().parents[1] / "integrations/codex-desktop"
    destination = tmp_path / "plugins" / "classcue"
    destination.mkdir(parents=True)
    # Reproduce the old dual-manifest installation, then exercise the real upgrade.
    (destination / "plugin.json").write_text(json.dumps({"name": "classcue", "version": "0.2.1"}))
    (destination / "mcp.json").write_text("{}")
    installed = subprocess.run([
        shutil.which("pwsh"), "-NoProfile", "-File", str(source / "install.ps1"),
        "-WorkspaceRoot", str(root), "-Destination", str(destination),
        "-MarketplaceRoot", str(tmp_path), "-CodexHome", str(home),
        "-NodeExecutable", node, "-NpmExecutable", shutil.which("npm.cmd"),
    ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
    assert installed.returncode == 0, installed.stdout + installed.stderr
    assert not (destination / "plugin.json").exists()
    assert not (destination / "mcp.json").exists()
    marketplace = tmp_path / ".agents/plugins/marketplace.json"
    with CodexClient(executable, home) as client:
        detail = client.call("plugin/read", {"pluginName": "classcue", "marketplacePath": str(marketplace)})["plugin"]
        assert detail["mcpServers"] == ["classcue"]
        assert len(detail["skills"]) == 2
        result = client.call("plugin/install", {"pluginName": "classcue", "marketplacePath": str(marketplace)})
        assert result["appsNeedingAuth"] == []
    from PIL import Image
    output = BytesIO()
    Image.new("RGB", (160, 96), "white").save(output, format="PNG")
    png = base64.b64encode(output.getvalue()).decode("ascii")

    def snapshot(_store, library, question):
        return {"library_id": library, "course_id": "database", "course_name": "合成课程",
                "document_id": "paper", "source": "合成试卷.pdf", "title": "第1题",
                "question_id": question, "content_version": 1, "content": [
                    {"type": "text", "text": "合成题目：2 + 3 = ?"},
                    {"type": "image", "media_type": "image/png", "data": png},
                    {"type": "image", "media_type": "image/png", "data": png},
                ]}

    bridge = CodexBridge(Store(tmp_path / "project.db"), "synthetic", snapshot)
    bridge.configure(str(root), executable, str(home))
    first = bridge.prepare("q", open_window=False)
    second = bridge.prepare("q", open_window=False)
    assert first["thread_id"] == second["thread_id"]
    with CodexClient(executable, home) as client:
        thread = client.call("thread/read", {"threadId": first["thread_id"]})["thread"]
        assert thread["name"] == "合成课程 · 合成试卷 · 第1题"
        assert Path(thread["cwd"]) == Path(first["source_path"])
        assert thread["section"]["id"] == first["section_id"]
        sections = client.call("threadSection/list", {})["data"]
        assert any(section["id"] == first["section_id"] for section in sections)
        listed = client.call("thread/list", {"cwd": first["source_path"], "limit": 10})["data"]
        # Context-only threads have no real turn and are intentionally absent from history.
        assert all(row["id"] != first["thread_id"] for row in listed)
        client.call("thread/resume", {"threadId": first["thread_id"], "excludeTurns": True})
        servers = client.call("mcpServerStatus/list", {"threadId": first["thread_id"]})["data"]
        server = next(row for row in servers if row.get("pluginId") == "classcue@personal")
        assert server["toolsError"] is None
        assert {"preview_batch", "start_batch_processing", "retry_batch", "resume_batch"} <= set(server["tools"])
        panel_tool = server["tools"]["classcue_discussion"]
        assert panel_tool["title"] == "ClassCue 题目"
        assert panel_tool["_meta"]["openai/ui"]["entrypoints"] == [{"type": "thread"}]
        def plugin_call(tool, arguments=None):
            return client.call("mcpServer/tool/call", {
                "threadId": first["thread_id"], "server": server["name"],
                "tool": tool, "arguments": arguments or {},
            })
        panel = plugin_call("classcue_discussion")
        assert not panel.get("isError")
        discussion = panel["_meta"]["classcue"]
        assert discussion["thread_id"] == first["thread_id"]
        assert [part["type"] for part in discussion["content"]] == ["text", "image", "image"]
        assert discussion["content"][1]["data"] == png
        claim = plugin_call("claim_discussion_draft", {"request_id": first["request_id"]})["structuredContent"]
        # Native handoffs cannot be auto-claimed by a legacy MCP panel.
        assert not claim["claimed"] and claim["status"] == "blocked"
    context = json.loads(Path(first["context_file"]).read_text(encoding="utf-8"))
    assert len(context["content"]) == 3
    # Read only the synthetic rollout identified by the public API; never inspect real sessions.
    events = [json.loads(line) for line in Path(thread["path"]).read_text(encoding="utf-8").splitlines()]
    messages = [entry["payload"] for entry in events if entry["type"] == "response_item"
                and entry["payload"].get("role") == "user"]
    injected = [message for message in messages
                if any(part.get("text") == "合成题目：2 + 3 = ?" for part in message["content"])]
    assert injected == []
    assert first["draft_transfer"]["status"] == "blocked"
    assert second["draft_transfer"]["status"] == "blocked"
    assert bridge.discussion(first["request_id"])["draft_transfer"]["status"] == "blocked"


def test_plugin_receipt_survives_reopen_and_status_read_never_starts_codex(service):
    bridge, client, _ = service
    first = bridge.prepare("q")
    database = Path(first["draft_root"]) / ".classcue-codex" / "drafts.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("UPDATE drafts SET status='attached',attempt='synthetic' WHERE request_id=?",
                   (first["request_id"],))
    before = len(client.calls)
    assert bridge.discussion(first["request_id"])["draft_transfer"]["status"] == "attached"
    assert bridge.history("q")[0]["draft_transfer"]["status"] == "attached"
    assert len(client.calls) == before
    assert bridge.prepare("q")["draft_transfer"]["status"] == "attached"
    assert len(client.threads) == 1


def test_modified_snapshot_does_not_reset_original_plugin_binding(service):
    bridge, _, _ = service
    first = bridge.prepare("q")
    Path(first["context_file"]).write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="绑定已变化"):
        bridge.resume(first["request_id"])


def test_legacy_injection_with_unknown_outcome_remains_blocked(service):
    bridge, _, _ = service
    first = bridge.prepare("q")
    first["status"] = "injecting"
    bridge._save(first)
    with pytest.raises(BridgeFailure) as error:
        bridge.resume(first["request_id"])
    assert error.value.code == "codex_outcome_unknown"


def test_workspace_connection_tracks_new_root_and_retains_old_discussion(service, tmp_path):
    bridge, _, _ = service
    first = bridge.prepare("q")
    home = tmp_path / "profile"
    directory = home / "classcue"
    directory.mkdir(parents=True)
    connection = directory / "connection.json"
    old_root = str(tmp_path)
    connection.write_text(json.dumps({"format": "classcue-codex-connection", "version": 1,
                                      "roots": [old_root]}), encoding="utf-8-sig")
    new_root = tmp_path / "new-workspace"
    new_root.mkdir()
    bridge.configure(str(new_root), executable=__file__, home=str(home))
    value = json.loads(connection.read_text(encoding="utf-8"))
    assert value["roots"] == [str(new_root), old_root]
    # Simulate restoring the original profile identity without changing thread bindings.
    receipt = json.loads(bridge.request(first["request_id"])["receipt"])
    receipt["home"] = str(home)
    bridge._save(receipt)
    resumed = bridge.resume(first["request_id"], open_window=False)
    assert resumed["thread_id"] == first["thread_id"]
    assert resumed["source_path"] == first["source_path"]
    assert json.loads(connection.read_text(encoding="utf-8"))["roots"] == [old_root, str(new_root)]


@pytest.mark.parametrize("value", [{}, [], {"format": "other", "roots": []},
                                  {"format": "classcue-codex-connection", "version": 1, "roots": ["relative"]}])
def test_invalid_connection_never_replaced(tmp_path, value):
    directory = tmp_path / "classcue"
    directory.mkdir()
    connection = directory / "connection.json"
    original = json.dumps(value)
    connection.write_text(original, encoding="utf-8")
    with pytest.raises(ValueError):
        connect_workspace(tmp_path, tmp_path)
    assert connection.read_text(encoding="utf-8") == original


def test_primary_button_transfers_native_images_once_without_opening_plugin_panel(service):
    bridge, client, _ = service
    def native(**kwargs):
        client.native_calls.append(kwargs)
        return {"status": "pasted", "image_count": len(kwargs["images"])}
    bridge.native_inserter = native
    original = bridge.snapshot_provider
    bridge.snapshot_provider = lambda *args: {**original(*args), "content": [
        {"type": "text", "text": "合成原题"}, image_part("white"), image_part("black")]}
    first = bridge.prepare("q")
    assert first["native_transfer"] == {"status": "pasted", "image_count": 2}
    assert first["draft_transfer"]["status"] == "pasted"
    assert len(client.native_calls) == 1
    assert client.native_calls[0]["uri"] == first["uri"]
    assert client.native_calls[0]["text"] == "合成原题"
    assert len(client.native_calls[0]["images"]) == 2
    bridge.prepare("q")
    bridge.resume(first["request_id"])
    assert len(client.native_calls) == 1
    assert not any(method in {"turn/start", "thread/inject_items", "mcpServer/tool/call"}
                   for method, _ in client.calls)


def test_uncertain_native_transfer_requires_explicit_repair_and_keeps_original_thread(service):
    bridge, client, _ = service
    calls = []

    def native(**kwargs):
        calls.append(kwargs)
        return {"status": "unknown", "error": "合成回执丢失"}

    bridge.native_inserter = native
    first = bridge.prepare("q")
    assert first["native_transfer"]["status"] == "unknown"
    bridge.prepare("q")
    assert len(calls) == 1
    assert len(client.threads) == 1
    repaired = bridge.resume(first["request_id"], attach_again=True)
    assert len(calls) == 2
    assert repaired["thread_id"] == first["thread_id"]


def test_blocked_native_transfer_can_retry_from_primary_button_and_uses_user_title(service):
    bridge, client, _ = service
    bridge.native_inserter = lambda **_: {"status": "blocked", "error": "输入框未就绪"}
    first = bridge.prepare("q")
    client.threads[first["thread_id"]]["name"] = "用户自己的标题"
    seen = []
    bridge.native_inserter = lambda **kwargs: seen.append(kwargs) or {"status": "attached", "image_count": 0}
    result = bridge.prepare("q")
    assert result["native_transfer"]["status"] == "attached"
    assert seen[0]["title"] == "用户自己的标题"
    assert len(client.threads) == 1


def test_busy_native_transfer_preserves_receipt_and_never_navigates_other_chat(service):
    from filelock import FileLock
    bridge, client, _ = service
    receipt = bridge.prepare("q", open_window=False)
    lock = Path(receipt["home"]) / "classcue/native-draft.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(lock)):
        with pytest.raises(BridgeFailure) as error:
            bridge.resume(receipt["request_id"])
    assert error.value.code == "native_draft_busy"
    assert not client.native_calls
    assert "native_transfer" not in bridge.discussion(receipt["request_id"])
