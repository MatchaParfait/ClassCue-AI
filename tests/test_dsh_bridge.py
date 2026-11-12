import base64
import json
import sqlite3
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.dsh_bridge import router
from classcue.dsh_bridge import BridgeStore, DshBridge, study_snapshot
from classcue.dsh_pipe import BridgeFailure, PipeClient
from classcue.sqlite_connection import ClosingConnection


class Store:
    def __init__(self, path):
        self.path = path

    def connect(self):
        db = sqlite3.connect(self.path, factory=ClosingConnection)
        db.row_factory = sqlite3.Row
        return db


class FakeClient:
    requests = {}
    instance = "test-instance"
    fail_after_write = False
    roots = {}

    def __init__(self, path):
        self.path = path

    def call(self, method, params=None):
        if method == "status":
            return {"instance_id": self.instance, "protocol": 1, "desktop_version": "0.2.1-rc.5",
                    "capabilities": ["course_workspace", "workspace_tree", "discussion_sync"]}
        if method == "discussions":
            return [receipt for receipt in self.requests.values()
                    if receipt.get("library_id") == params["library_id"]
                    and receipt.get("question_id") == params["question_id"]
                    and receipt.get("instance_id") == self.instance]
        if method == "workspaces":
            return []
        if method == "configure_root":
            result = {**params, "instance_id": self.instance, "workspace_id": f"workspace-{params['config_id']}"}
            self.roots[params["config_id"]] = result
            if self.fail_after_write:
                raise BridgeFailure("timeout", "synthetic timeout")
            return result
        if method == "prepare":
            request_id = params["request_id"]
            self.requests.setdefault(request_id, {
                **{key: params[key] for key in ("library_id", "document_id", "question_id", "course_id")
                   if key in params},
                "request_id": request_id, "session_id": f"session-{request_id}", "instance_id": self.instance,
                "status": "bound", "content_version": params["content_version"], "workspace_id": "native-workspace",
                "discussion_state": "draft",
            })
            if self.fail_after_write:
                raise BridgeFailure("timeout", "synthetic timeout")
        else:
            request_id = params["request_id"]
        return self.requests[request_id]


@pytest.fixture
def bridge(tmp_path):
    FakeClient.requests = {}
    FakeClient.instance = "test-instance"
    FakeClient.fail_after_write = False
    store = Store(tmp_path / "project.db")
    version = {"value": 1}

    def snapshot(_store, library, question):
        return {"library_id": library, "document_id": "document", "question_id": question,
                "content_version": version["value"], "content": [{"type": "text", "text": "合成题目（1）（2）"}]}

    bridge = DshBridge(store, "library", snapshot, FakeClient)
    bridge.records.connect(tmp_path / "connection.json", FakeClient)
    bridge.configure_root(str(tmp_path), FakeClient.instance)
    return bridge, version


def test_timeout_reuses_committed_intent_and_persists_binding_across_restart(bridge):
    service, _ = bridge
    FakeClient.fail_after_write = True
    with pytest.raises(BridgeFailure, match="timeout"):
        service.prepare("question")
    assert len(service.records.history("question")) == 1
    FakeClient.fail_after_write = False
    receipt = service.prepare("question")
    assert len(FakeClient.requests) == 1
    restarted = DshBridge(service.store, "library", service.snapshot_provider, FakeClient)
    assert restarted.resume(receipt["request_id"], open_session=True)["session_id"] == receipt["session_id"]
    assert restarted.records.history("question")[0]["receipt"] == receipt


def test_first_use_discovers_the_default_desktop_connection(tmp_path, monkeypatch):
    descriptor = tmp_path / "classcue-bridge" / "connection.json"
    descriptor.parent.mkdir()
    descriptor.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(BridgeStore, "default_descriptor", staticmethod(lambda: descriptor))
    store = Store(tmp_path / "project.db")
    service = DshBridge(store, "library", client_factory=FakeClient)

    client, status = service.active_client()

    assert client.path == descriptor
    assert status["instance_id"] == FakeClient.instance
    with store.connect() as db:
        assert db.execute("SELECT descriptor_path FROM dsh_bridge_connection").fetchone()[0] == str(descriptor)


def test_prepare_and_explicit_reopen_wake_desktop_but_polling_does_not(bridge, monkeypatch):
    service, _ = bridge
    wakes = []
    monkeypatch.setattr(service.records, "wake", lambda: wakes.append(True))
    receipt = service.prepare("question")
    assert len(wakes) == 1
    service.resume(receipt["request_id"])
    assert len(wakes) == 1
    reopened = service.resume(receipt["request_id"], open_session=True)
    assert len(wakes) == 2
    assert reopened["session_id"] == receipt["session_id"]
    assert len(FakeClient.requests) == 1


def test_open_recovers_timeout_before_host_admission_using_the_saved_request(bridge):
    service, _ = bridge
    FakeClient.fail_after_write = True
    with pytest.raises(BridgeFailure):
        service.prepare("question")
    request_id = service.records.history("question")[0]["request_id"]
    FakeClient.requests.clear()
    original = FakeClient.call

    def call(self, method, params=None):
        if method == "open" and params["request_id"] not in self.requests:
            raise BridgeFailure("request_missing", "request missing")
        return original(self, method, params)

    FakeClient.fail_after_write = False
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(FakeClient, "call", call)
        assert service.resume(request_id, open_session=True)["request_id"] == request_id
    assert len(FakeClient.requests) == 1


def test_new_version_requires_explicit_new_discussion_and_old_binding_remains(bridge):
    service, version = bridge
    old = service.prepare("question")
    version["value"] = 2
    with pytest.raises(BridgeFailure) as error:
        service.prepare("question")
    assert error.value.code == "question_changed"
    new = service.prepare("question", new_discussion=True)
    assert old["session_id"] != new["session_id"]
    assert service.resume(old["request_id"])["content_version"] == 1
    assert len(service.records.history("question")) == 2


def test_switching_desktop_instances_never_resumes_old_binding_in_new_instance(bridge, tmp_path):
    service, _ = bridge
    receipt = service.prepare("question")
    FakeClient.instance = "another-instance"
    with pytest.raises(BridgeFailure) as error:
        service.resume(receipt["request_id"])
    assert error.value.code == "instance_mismatch"
    service.records.connect(tmp_path / "another.json", FakeClient)
    with pytest.raises(BridgeFailure):
        service.prepare("question")
    assert len(FakeClient.requests) == 1


def test_pipe_framing_and_pairing_token_never_enters_receipts(tmp_path):
    path = tmp_path / "connection.json"
    path.write_text(json.dumps({
        "protocol": 1, "desktop_version": "0.2.1-rc.3",
        "instance_id": "00000000-0000-0000-0000-000000000000",
        "pipe": r"\\.\pipe\classcue-dsh-00000000-0000-0000-0000-000000000000", "token": "a" * 64,
    }))

    def transport(pipe, frame, timeout):
        size = struct.unpack(">I", frame[:4])[0]
        assert size == len(frame) - 4
        request = json.loads(frame[4:])
        assert request["params"]["text"] == "中文题目"
        assert request["token"] == "a" * 64
        return b'{"ok":true,"value":{"status":"ready"}}'

    assert PipeClient(path, transport).call("prepare", {"text": "中文题目"}) == {"status": "ready"}
    path.write_text('{"pipe":"not-a-local-pipe"}')
    with pytest.raises(BridgeFailure) as error:
        PipeClient(path, transport).call("status")
    assert error.value.code == "plugin_unavailable"


def test_desktop_routes_are_unavailable_in_web_and_preserve_recovery(bridge):
    service, _ = bridge
    provider = type("Provider", (), {"get": lambda _: service.store, "library_id": lambda _: "library"})()
    app = FastAPI()
    app.include_router(router(provider, bridge_factory=lambda *_: service, desktop=lambda: False))
    assert TestClient(app).get("/api/dsh-bridge/status").status_code == 404
    app = FastAPI()
    app.include_router(router(provider, bridge_factory=lambda *_: service, desktop=lambda: True))
    client = TestClient(app)
    response = client.post("/api/dsh-bridge/discussions", json={"question_id": "question"})
    assert response.status_code == 202
    request_id = response.json()["request_id"]
    assert client.post(f"/api/dsh-bridge/requests/{request_id}/open").json()["request_id"] == request_id
    assert client.get("/api/dsh-bridge/questions/question/discussions").json() == []
    assert client.post("/api/dsh-bridge/discussions", json={"question_id": "q", "content": []}).status_code == 422


def test_saved_question_adapter_keeps_whole_question_and_ordered_image_regions(monkeypatch):
    import sys
    from types import SimpleNamespace
    from PIL import Image
    from contextlib import contextmanager

    class Materials:
        def __init__(self, store):
            pass

        def question(self, question_id):
            return {"id": question_id, "document_id": "doc", "version": 3, "number": "第三题",
                    "title": "情景题", "regions": [
                        {"block": {"id": "md", "kind": "markdown", "text": "共同题干\n（1）小问\n（2）小问"},
                         "start": 0, "end": 100},
                        {"block": {"id": "page1", "kind": "pdf_page"}, "bbox": [0, 0, 1, 1]},
                        {"block": {"id": "page2", "kind": "image"}, "bbox": [0, 0, 1, 1]},
                    ]}

        def document(self, document_id):
            return {"name": "合成试卷"}

        @contextmanager
        def region_image(self, document_id, block_id, bbox):
            with Image.new("RGB", (4, 4), "red" if block_id == "page1" else "blue") as image:
                yield image

    monkeypatch.setitem(sys.modules, "classcue.study_materials", SimpleNamespace(StudyMaterialStore=Materials))
    monkeypatch.setattr("classcue.study_courses.StudyCourseStore.question",
                        lambda self, question_id: {**Materials(None).question(question_id),
                            "source_kind": "document", "course_id": "course", "course_name": "合成课程"})
    result = study_snapshot(None, "library", "question")
    assert result["content_version"] == 3
    assert result["content"][0]["text"] == "共同题干\n（1）小问\n（2）小问"
    assert len(result["content"]) == 3
    for part in result["content"][1:]:
        assert base64.b64decode(part["data"]).startswith(b"\x89PNG")


@pytest.fixture
def real_materials(tmp_path):
    from classcue.study_materials import StudyMaterialStore
    from classcue.video_store import VideoStore
    FakeClient.requests = {}
    FakeClient.instance = "test-instance"
    FakeClient.fail_after_write = False
    materials = StudyMaterialStore(VideoStore(tmp_path / "library" / "project.db"))
    service = DshBridge(materials.store, "synthetic-library", client_factory=FakeClient)
    service.records.connect(tmp_path / "connection.json", FakeClient)
    service.configure_root(str(tmp_path), FakeClient.instance)
    return materials, service


def test_reader_markdown_and_supplement_transfer_then_reopen_without_rerender(real_materials, monkeypatch):
    from io import BytesIO
    from PIL import Image
    materials, service = real_materials
    raw = "# Q3\n\nShared scenario R(A,B).\n\n(1) Draw the diagram.\n(2) Explain it.\n\n![diagram](missing.png)\n"
    document = materials.register("paper.md", raw.encode())
    from classcue.study_courses import StudyCourseStore
    courses = StudyCourseStore(materials.store)
    courses.move_document(document["id"], courses.create(name="合成课程")["id"])
    blocks = materials.blocks(document["id"])
    regions = [{"block_id": block["id"]} for block in blocks]
    question = materials.save_question(document["id"], {"title": "Whole question", "regions": regions})
    with pytest.raises(BridgeFailure) as failure:
        service.prepare(question["id"])
    assert failure.value.code == "missing_image"
    assert not FakeClient.requests
    stream = BytesIO()
    with Image.new("RGB", (80, 40), "green") as image:
        image.save(stream, "PNG")
    supplement = materials.supplement_image(document["id"], "diagram.png", stream.getvalue())
    regions.append({"block_id": supplement["id"], "bbox": [0, 0, 1, 1]})
    question = materials.save_question(document["id"], {"regions": regions}, question["id"])
    receipt = service.prepare(question["id"])
    record = json.loads(service.records.request(receipt["request_id"])["snapshot"])
    assert record["content_version"] == 2
    assert record["content"][-1]["type"] == "image"
    assert "(1)" in str(record["content"]) and "(2)" in str(record["content"])

    def no_render(*args, **kwargs):
        raise AssertionError("Existing discussion must reopen its saved snapshot")
    monkeypatch.setattr(type(materials), "region_image", no_render)
    assert service.prepare(question["id"])["session_id"] == receipt["session_id"]
    assert len(FakeClient.requests) == 1


def test_reader_single_image_transfers_only_the_original_crop(real_materials):
    from io import BytesIO
    from PIL import Image
    from classcue.study_courses import StudyCourseStore
    materials, service = real_materials
    stream = BytesIO()
    Image.new("RGB", (80, 40), "green").save(stream, "PNG")
    document = materials.register("question.png", stream.getvalue())
    courses = StudyCourseStore(materials.store)
    courses.move_document(document["id"], courses.create(name="合成课程")["id"])
    block = materials.blocks(document["id"])[0]
    question = materials.save_question(document["id"], {
        "title": "Synthetic image question", "number": "3",
        "regions": [{"block_id": block["id"], "bbox": [0, 0, 1, 1]}],
    })
    receipt = service.prepare(question["id"])
    record = json.loads(service.records.request(receipt["request_id"])["snapshot"])
    assert [part["type"] for part in record["content"]] == ["image"]
    with Image.open(BytesIO(base64.b64decode(record["content"][0]["data"]))) as image:
        assert image.size == (80, 40)
        assert image.getpixel((10, 10))[:3] == (0, 128, 0)
    assert record["course_name"] == "合成课程"
    assert record["source"] == "question.png"


def test_reader_cross_page_pdf_preserves_crop_order_version_and_removed_restore(real_materials):
    from io import BytesIO
    from PIL import Image
    from reportlab.pdfgen import canvas
    materials, service = real_materials
    stream = BytesIO()
    pdf = canvas.Canvas(stream, pagesize=(200, 200))
    for color in ("red", "blue"):
        pdf.setFillColor(color)
        pdf.rect(0, 0, 200, 200, fill=1, stroke=0)
        pdf.showPage()
    pdf.save()
    document = materials.register("cross-page.pdf", stream.getvalue())
    from classcue.study_courses import StudyCourseStore
    courses = StudyCourseStore(materials.store)
    courses.move_document(document["id"], courses.create(name="合成课程")["id"])
    blocks = materials.blocks(document["id"])
    regions = [{"block_id": block["id"], "bbox": [0, 0, 1, 1],
                "selection_group": "synthetic-continuous-question"} for block in reversed(blocks)]
    question = materials.save_question(document["id"], {"number": "3 (1)(2)", "regions": regions})
    assert all(region["selection_group"] == "synthetic-continuous-question" for region in question["regions"])
    receipt = service.prepare(question["id"])
    record = json.loads(service.records.request(receipt["request_id"])["snapshot"])
    assert [part["type"] for part in record["content"]] == ["image", "image"]
    colors = []
    for part in record["content"]:
        with Image.open(BytesIO(base64.b64decode(part["data"]))) as image:
            colors.append(image.getpixel((10, 10))[:3])
    assert colors == [(0, 0, 255), (255, 0, 0)]
    regions.reverse()
    revised = materials.save_question(document["id"], {"regions": regions}, question["id"])
    assert revised["version"] == 2
    with pytest.raises(BridgeFailure) as failure:
        service.prepare(question["id"])
    assert failure.value.code == "question_changed"
    new_receipt = service.prepare(question["id"], new_discussion=True)
    assert new_receipt["session_id"] != receipt["session_id"]
    materials.set_deleted("study_documents", document["id"], True)
    with pytest.raises(KeyError):
        service.prepare(question["id"])
    materials.set_deleted("study_documents", document["id"], False)
    assert service.prepare(question["id"])["session_id"] == new_receipt["session_id"]
    restarted = DshBridge(materials.store, "synthetic-library", client_factory=FakeClient)
    assert restarted.resume(receipt["request_id"], open_session=True)["content_version"] == 1


def test_application_import_save_prepare_and_restore_use_registered_routes(real_materials, monkeypatch, tmp_path):
    from api.main import create_app
    materials, _ = real_materials
    monkeypatch.setenv("CLASSROOM_DESKTOP_MODE", "1")
    monkeypatch.setattr(PipeClient, "call", lambda self, method, params=None:
                        FakeClient(tmp_path).call(method, params))
    with TestClient(create_app(materials.store)) as client:
        course = client.post("/api/study-courses", json={"name": "合成课程"}).json()
        document = client.post("/api/study-documents", params={"name": "whole-question.md", "course_id": course["id"]},
                               content=b"# Q3\n\nShared scenario.\n\n(1) Draw.\n(2) Explain.\n").json()
        blocks = client.get(f"/api/study-documents/{document['id']}/blocks").json()
        question = client.post("/api/study-questions", json={
            "document_id": document["id"], "number": "3", "regions": [
                {"block_id": block["id"]} for block in blocks],
        }).json()
        connected = client.post("/api/dsh-bridge/connection",
                                json={"descriptor_path": str(tmp_path / "fake-connection.json")})
        assert connected.status_code == 200
        configured = client.put("/api/dsh-bridge/workspace-root", json={
            "path": str(tmp_path), "instance_id": FakeClient.instance})
        assert configured.status_code == 200
        response = client.post("/api/dsh-bridge/discussions", json={"question_id": question["id"]})
        assert response.status_code == 202, response.text
        receipt = response.json()
        request_id = receipt["request_id"]
        record = json.loads(DshBridge(materials.store, "library").records.request(request_id)["snapshot"])
        assert "(1) Draw." in str(record["content"]) and "(2) Explain." in str(record["content"])
        assert client.get(f"/api/dsh-bridge/questions/{question['id']}/discussions").json() == []
        assert client.post(f"/api/dsh-bridge/requests/{request_id}/open").json()["session_id"] == receipt["session_id"]
        assert client.delete(f"/api/study-questions/{question['id']}").status_code == 200
        assert client.post("/api/dsh-bridge/discussions", json={"question_id": question["id"]}).status_code == 404
        assert client.post(f"/api/study-questions/{question['id']}/restore").status_code == 200
        assert client.post("/api/dsh-bridge/discussions", json={"question_id": question["id"]}).json()["request_id"] == request_id
        assert len(FakeClient.requests) == 1


def test_root_selection_persists_offline_reuses_identity_and_changes_only_new_request_intents(bridge, tmp_path):
    service, _ = bridge
    original = service.records.workspace_root()
    assert original and original["instance_id"] == FakeClient.instance
    first = service.prepare("first")
    second_path = tmp_path / "another-root"
    second_path.mkdir()
    second = service.configure_root(str(second_path), FakeClient.instance)
    assert second["config_id"] != original["config_id"]
    restarted = DshBridge(service.store, "library", service.snapshot_provider, FakeClient)
    assert restarted.records.workspace_root() == second
    new = restarted.prepare("second")
    assert json.loads(restarted.records.request(new["request_id"])["snapshot"])["root_id"] == second["config_id"]
    assert json.loads(restarted.records.request(first["request_id"])["snapshot"])["root_id"] == original["config_id"]
    assert restarted.configure_root(str(tmp_path), FakeClient.instance)["config_id"] == original["config_id"]
    assert restarted.records.workspace_root() == original


def test_root_timeout_retries_one_saved_intent_and_keeps_previous_active_root(bridge, tmp_path):
    service, _ = bridge
    previous = service.records.workspace_root()
    selected = tmp_path / "retry-root"
    selected.mkdir()
    FakeClient.fail_after_write = True
    with pytest.raises(BridgeFailure):
        service.configure_root(str(selected), FakeClient.instance)
    assert service.records.workspace_root() == previous
    with service.store.connect() as db:
        pending = dict(db.execute("SELECT * FROM dsh_bridge_roots WHERE path=?", (str(selected),)).fetchone())
    FakeClient.fail_after_write = False
    confirmed = service.configure_root(str(selected), FakeClient.instance)
    assert confirmed["config_id"] == pending["config_id"]
    assert service.records.workspace_root() == confirmed
    with service.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM dsh_bridge_roots WHERE path=?", (str(selected),)).fetchone()[0] == 1


def test_new_tree_requires_root_and_updated_plugin_before_persisting_any_question(bridge, monkeypatch):
    service, _ = bridge
    with service.store.connect() as db:
        db.execute("DELETE FROM dsh_bridge_root_selection")
    with pytest.raises(BridgeFailure) as failure:
        service.prepare("missing-root")
    assert failure.value.code == "root_required"
    original = FakeClient.call

    def legacy(self, method, params=None):
        value = original(self, method, params)
        return {**value, "capabilities": ["course_workspace"]} if method == "status" else value

    monkeypatch.setattr(FakeClient, "call", legacy)
    with pytest.raises(BridgeFailure) as failure:
        service.prepare("old-plugin")
    assert failure.value.code == "plugin_outdated"
    assert not service.records.history("missing-root") and not service.records.history("old-plugin")


def test_instance_changes_keep_root_bindings_separate_and_reject_stale_selection(bridge, tmp_path):
    service, _ = bridge
    original = service.records.workspace_root()
    with pytest.raises(BridgeFailure) as failure:
        service.configure_root(str(tmp_path), "another-instance")
    assert failure.value.code == "instance_mismatch"
    FakeClient.instance = "another-instance"
    service.records.connect(tmp_path / "another.json", FakeClient)
    assert service.records.workspace_root() is None
    other = service.configure_root(str(tmp_path), FakeClient.instance)
    assert other["config_id"] != original["config_id"]
    assert service.records.workspace_root("test-instance") == original


def test_legacy_pending_request_reopens_without_root_and_tree_downgrade_is_rejected(bridge, monkeypatch):
    service, _ = bridge
    tree = service.prepare("tree")
    legacy = {"request_id": "legacy", "question_id": "legacy", "library_id": "library", "document_id": "old",
              "content_version": 1, "content": [{"type": "text", "text": "合成旧题目"}]}
    with service.store.connect() as db:
        db.execute("INSERT INTO dsh_bridge_requests VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                   ("legacy", "legacy", 1, FakeClient.instance, json.dumps(legacy), None))
        db.execute("DELETE FROM dsh_bridge_root_selection")
    original = FakeClient.call

    def old_plugin(self, method, params=None):
        value = original(self, method, params)
        if method == "status":
            return {**value, "capabilities": ["course_workspace"]}
        return value

    monkeypatch.setattr(FakeClient, "call", old_plugin)
    # Simulate a timeout before the Host admitted the old request.
    def legacy_open(self, method, params=None):
        if method == "open" and params["request_id"] == "legacy":
            raise BridgeFailure("request_missing", "not admitted")
        return old_plugin(self, method, params)
    monkeypatch.setattr(FakeClient, "call", legacy_open)
    assert service.resume("legacy", open_session=True)["request_id"] == "legacy"
    with pytest.raises(BridgeFailure) as failure:
        service.resume(tree["request_id"], open_session=True)
    assert failure.value.code == "plugin_outdated"


def test_root_api_preserves_selection_on_bad_path_and_excludes_web_mode(bridge, tmp_path):
    service, _ = bridge
    provider = type("Provider", (), {"get": lambda _: service.store, "library_id": lambda _: "library"})()
    app = FastAPI()
    app.include_router(router(provider, bridge_factory=lambda *_: service, desktop=lambda: True))
    client = TestClient(app)
    original = client.get("/api/dsh-bridge/workspace-root").json()
    assert client.put("/api/dsh-bridge/workspace-root", json={
        "path": str(tmp_path / "missing"), "instance_id": FakeClient.instance}).status_code == 400
    assert client.get("/api/dsh-bridge/workspace-root").json() == original
    assert client.put("/api/dsh-bridge/workspace-root", json={
        "path": str(tmp_path), "instance_id": FakeClient.instance, "token": "not-allowed"}).status_code == 422
    web = FastAPI()
    web.include_router(router(provider, bridge_factory=lambda *_: service, desktop=lambda: False))
    assert TestClient(web).get("/api/dsh-bridge/workspace-root").status_code == 404


def fork_receipt(service, parent_id, request_id="native-child", session_id="native-session", **changes):
    parent = service.records.request(parent_id)
    source = json.loads(parent["snapshot"])
    receipt = json.loads(parent["receipt"])
    return {key: value for key, value in {
        **source, **receipt, "content": None, "request_id": request_id, "session_id": session_id,
        "library_id": service.library_id, "question_id": parent["question_id"],
        "document_id": source["document_id"], "course_id": source.get("course_id"),
        "origin": "fork", "parent_request_id": parent_id, "parent_session_id": receipt["session_id"],
        "workspace_id": "native-workspace", "status": "linked", "created_at": "2026-10-30T12:00:00.000Z",
        "discussion_state": "active",
        **changes,
    }.items() if key != "content"}


def serve_forks(monkeypatch, receipts):
    original = FakeClient.call

    def call(self, method, params=None):
        if method == "discussions":
            return [*original(self, method, params), *receipts]
        result = original(self, method, params)
        if method == "status":
            return {**result, "capabilities": [*result["capabilities"], "native_forks"]}
        return result

    monkeypatch.setattr(FakeClient, "call", call)


def test_native_forks_sync_reopen_and_restart_without_transmitting_a_question(bridge, monkeypatch):
    service, _ = bridge
    original = service.prepare("question")
    FakeClient.requests[original["request_id"]]["discussion_state"] = "active"
    child = fork_receipt(service, original["request_id"])
    child["created_at"] = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
    serve_forks(monkeypatch, [child])
    FakeClient.requests[child["request_id"]] = child
    assert len(service.history("question")) == 2
    row = service.records.request(child["request_id"])
    assert "content" not in json.loads(row["snapshot"])
    assert service.history("question") == service.history("question")
    restarted = DshBridge(service.store, "library", service.snapshot_provider, FakeClient)
    assert restarted.resume(child["request_id"], open_session=True)["session_id"] == child["session_id"]
    assert restarted.prepare("question")["session_id"] == child["session_id"]
    assert len(restarted.history("question")) == 2
    assert len(FakeClient.requests) == 2
    original_call = FakeClient.call

    def missing(self, method, params=None):
        if method == "open" and params["request_id"] == child["request_id"]:
            raise BridgeFailure("request_missing", "分支记录已移除")
        if method == "prepare":
            pytest.fail("原生分支不得通过 prepare 重建或发送题目")
        return original_call(self, method, params)

    monkeypatch.setattr(FakeClient, "call", missing)
    with pytest.raises(BridgeFailure, match="分支记录已移除"):
        restarted.resume(child["request_id"], open_session=True)
    assert len(restarted.records.history("question")) == 2


def test_a_plugin_downgrade_keeps_native_fork_links_but_does_not_open_them_as_independent_questions(bridge, monkeypatch):
    service, _ = bridge
    original = service.prepare("question")
    child = fork_receipt(service, original["request_id"])
    service.records.sync_discussions([child], library_id="library", question_id="question", instance_id=FakeClient.instance)
    original_call = FakeClient.call

    def legacy(self, method, params=None):
        value = original_call(self, method, params)
        return {**value, "capabilities": ["workspace_tree"]} if method == "status" else value

    monkeypatch.setattr(FakeClient, "call", legacy)
    assert len(service.history("question")) == 1
    with pytest.raises(BridgeFailure) as failure:
        service.resume(child["request_id"], open_session=True)
    assert failure.value.code == "plugin_outdated"
    assert service.resume(original["request_id"], open_session=True)["session_id"] == original["session_id"]


def test_native_forks_follow_parent_version_and_nested_ancestry_even_when_listed_in_reverse(bridge, monkeypatch):
    service, version = bridge
    original = service.prepare("question")
    FakeClient.requests[original["request_id"]]["discussion_state"] = "active"
    child = fork_receipt(service, original["request_id"])
    service.records.sync_discussions([child], library_id="library", question_id="question", instance_id=FakeClient.instance)
    grandchild = fork_receipt(service, child["request_id"], "grandchild", "grandchild-session")
    with service.store.connect() as db:
        db.execute("DELETE FROM dsh_bridge_requests WHERE request_id=?", (child["request_id"],))
    version["value"] = 2
    newer = service.prepare("question", new_discussion=True)
    FakeClient.requests[newer["request_id"]]["discussion_state"] = "active"
    new_child = fork_receipt(service, newer["request_id"], "new-child", "new-child-session")
    serve_forks(monkeypatch, [grandchild, new_child, child])
    rows = service.history("question")
    assert len(rows) == 5
    versions = {row["request_id"]: row["content_version"] for row in rows}
    assert versions[child["request_id"]] == versions[grandchild["request_id"]] == 1
    assert versions[new_child["request_id"]] == 2
    assert next(row for row in rows if row["request_id"] == grandchild["request_id"])["parent_request_id"] == child["request_id"]


@pytest.mark.parametrize("changes", [
    {"question_id": "another-question"}, {"library_id": "another-library"},
    {"instance_id": "another-instance"}, {"content_version": 99},
    {"document_id": "another-document"}, {"course_id": "another-course"},
    {"parent_session_id": "another-parent"}, {"session_id": "../unsafe"},
])
def test_native_fork_rejects_inconsistent_binding_without_partial_writes(bridge, changes):
    service, _ = bridge
    original = service.prepare("question")
    good = fork_receipt(service, original["request_id"], "good", "good-session")
    bad = fork_receipt(service, original["request_id"], **changes)
    with pytest.raises(BridgeFailure) as failure:
        service.records.sync_discussions([good, bad], library_id="library", question_id="question",
                                  instance_id=FakeClient.instance)
    assert failure.value.code == "invalid_response"
    assert len(service.records.history("question")) == 1


@pytest.mark.parametrize("changes", [
    {"request_id": []}, {"request_id": {}}, {"session_id": []},
    {"parent_request_id": []}, {"parent_session_id": {}},
    {"discussion_state": []}, {"workspace_id": []},
    {"created_at": "2026-10-30T12:00:00"},
])
def test_malformed_fork_fields_are_rejected_without_replacing_cached_history(bridge, changes):
    service, _ = bridge
    original = service.prepare("question")
    FakeClient.requests[original["request_id"]]["discussion_state"] = "active"
    before = service.history("question")
    malformed = fork_receipt(service, original["request_id"], **changes)
    with pytest.raises(BridgeFailure) as failure:
        service.records.sync_discussions([malformed], library_id="library", question_id="question",
                                        instance_id=FakeClient.instance)
    assert failure.value.code == "invalid_response"
    assert service.records.history("question", visible_only=True) == before
    assert len(service.records.history("question")) == 1


def test_unresolved_fork_ancestry_rolls_back_updates_and_preserves_cached_history(bridge):
    service, _ = bridge
    original = service.prepare("question")
    FakeClient.requests[original["request_id"]]["discussion_state"] = "active"
    before = service.history("question")
    good = fork_receipt(service, original["request_id"], "good", "good-session")
    orphan = fork_receipt(service, original["request_id"], parent_request_id="unknown-parent")
    with pytest.raises(BridgeFailure) as failure:
        service.records.sync_discussions([good, orphan], library_id="library", question_id="question",
                                        instance_id=FakeClient.instance)
    assert failure.value.code == "invalid_response"
    assert service.records.history("question", visible_only=True) == before
    assert len(service.records.history("question")) == 1


@pytest.mark.parametrize("native_state", ["archived", "missing"])
def test_hidden_other_instance_bindings_still_require_explicit_new_discussion(bridge, tmp_path, native_state):
    service, _ = bridge
    original = service.prepare("question")
    FakeClient.requests[original["request_id"]]["discussion_state"] = native_state
    assert service.history("question") == []
    FakeClient.instance = "another-instance"
    service.records.connect(tmp_path / "another.json", FakeClient)
    service.configure_root(str(tmp_path), FakeClient.instance)
    with pytest.raises(BridgeFailure) as failure:
        service.prepare("question")
    assert failure.value.code == "instance_mismatch"
    assert len(service.records.history("question")) == 1
    assert len(FakeClient.requests) == 1
    fresh = service.prepare("question", new_discussion=True)
    assert fresh["instance_id"] == FakeClient.instance
    assert fresh["session_id"] != original["session_id"]


def test_cached_forks_remain_readable_offline_without_starting_dsh_and_receipt_updates_are_idempotent(bridge, monkeypatch):
    service, _ = bridge
    original = service.prepare("question")
    FakeClient.requests[original["request_id"]]["discussion_state"] = "active"
    child = fork_receipt(service, original["request_id"])
    serve_forks(monkeypatch, [child])
    service.history("question")
    service.records.save_receipt(child["request_id"], {**child, "outcome": "opened"})
    assert len(service.history("question")) == 2
    monkeypatch.setattr(service.records, "wake", lambda: pytest.fail("读取历史不得启动 DSH"))
    monkeypatch.setattr(service.records, "auto_connect", lambda *_: pytest.fail("读取历史不得自动配对"))

    def offline(*_):
        raise BridgeFailure("plugin_unavailable", "离线")

    monkeypatch.setattr(service.records, "client", offline)
    assert len(service.history("question")) == 2
    provider = type("Provider", (), {"get": lambda _: service.store, "library_id": lambda _: "library"})()
    app = FastAPI()
    app.include_router(router(provider, bridge_factory=lambda *_: service, desktop=lambda: True))
    response = TestClient(app).get("/api/dsh-bridge/questions/question/discussions")
    assert response.status_code == 200
    assert next(row for row in response.json() if row.get("origin") == "fork")["parent_request_id"] == original["request_id"]


def test_unsent_drafts_are_recoverable_but_never_count_as_discussion_history(bridge):
    service, _ = bridge
    first = service.prepare("question")
    assert service.history("question") == []
    assert service.prepare("question")["request_id"] == first["request_id"]
    second = service.prepare("question", new_discussion=True)
    assert service.history("question") == []
    assert service.prepare("question")["request_id"] == second["request_id"]
    assert len(service.records.history("question")) == 2
    # A transport "sent" receipt alone is insufficient; DSH must have started a real turn.
    FakeClient.requests[second["request_id"]]["status"] = "sent"
    assert service.history("question") == []
    FakeClient.requests[second["request_id"]]["discussion_state"] = "active"
    assert [row["request_id"] for row in service.history("question")] == [second["request_id"]]


def test_archiving_and_restoring_in_dsh_controls_history_without_deleting_bindings(bridge):
    service, _ = bridge
    first = service.prepare("question")
    native = FakeClient.requests[first["request_id"]]
    native["discussion_state"] = "active"
    assert len(service.history("question")) == 1
    native["discussion_state"] = "archived"
    assert service.history("question") == []
    assert len(service.records.history("question")) == 1
    native["discussion_state"] = "active"
    assert service.history("question")[0]["request_id"] == first["request_id"]


@pytest.mark.parametrize("native_state", ["archived", "missing"])
def test_default_button_prepares_a_new_draft_when_all_old_discussions_are_hidden(bridge, native_state):
    service, _ = bridge
    first = service.prepare("question")
    FakeClient.requests[first["request_id"]]["discussion_state"] = native_state
    second = service.prepare("question")
    assert second["request_id"] != first["request_id"]
    assert service.history("question") == []
    assert len(service.records.history("question")) == 2


def test_default_button_skips_archived_latest_discussion_and_uses_older_active_one(bridge):
    service, _ = bridge
    first = service.prepare("question")
    FakeClient.requests[first["request_id"]]["discussion_state"] = "active"
    second = service.prepare("question", new_discussion=True)
    FakeClient.requests[second["request_id"]]["discussion_state"] = "archived"
    assert service.prepare("question")["request_id"] == first["request_id"]
    assert len(service.records.history("question")) == 2


def test_successful_empty_native_inventory_hides_stale_links_but_read_failures_do_not(bridge, monkeypatch):
    service, _ = bridge
    first = service.prepare("question")
    FakeClient.requests[first["request_id"]]["discussion_state"] = "active"
    assert len(service.history("question")) == 1
    original = FakeClient.call

    def failed(self, method, params=None):
        if method == "discussions":
            raise BridgeFailure("disconnected", "暂时断线")
        return original(self, method, params)

    monkeypatch.setattr(FakeClient, "call", failed)
    assert len(service.history("question")) == 1
    monkeypatch.setattr(FakeClient, "call", original)
    FakeClient.requests.clear()
    assert service.history("question") == []
    assert len(service.records.history("question")) == 1


def test_old_unverified_sent_receipts_are_not_presented_as_confirmed_discussions(bridge, monkeypatch):
    service, _ = bridge
    receipt = service.prepare("question")
    service.records.save_receipt(receipt["request_id"], {key: value for key, value in
                                {**receipt, "status": "sent"}.items() if key != "discussion_state"})
    monkeypatch.setattr(service.records, "client",
                        lambda *_: (_ for _ in ()).throw(BridgeFailure("plugin_unavailable", "离线")))
    assert service.history("question") == []
