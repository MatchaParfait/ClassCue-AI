import base64
import json
import sqlite3
import struct
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

    def __init__(self, path):
        self.path = path

    def call(self, method, params=None):
        if method == "status":
            return {"instance_id": self.instance, "protocol": 1, "desktop_version": "0.2.1-rc.3",
                    "capabilities": ["course_workspace"]}
        if method == "workspaces":
            return []
        if method == "prepare":
            request_id = params["request_id"]
            self.requests.setdefault(request_id, {
                "request_id": request_id, "session_id": f"session-{request_id}", "instance_id": self.instance,
                "status": "bound", "content_version": params["content_version"],
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
    assert len(client.get("/api/dsh-bridge/questions/question/discussions").json()) == 1
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
    assert "（1）" in result["content"][1]["text"] and "（2）" in result["content"][1]["text"]
    assert len(result["content"]) == 4
    for part in result["content"][2:]:
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
    assert len(record["content"]) == 3
    colors = []
    for part in record["content"][1:]:
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
        response = client.post("/api/dsh-bridge/discussions", json={"question_id": question["id"]})
        assert response.status_code == 202, response.text
        receipt = response.json()
        request_id = receipt["request_id"]
        record = json.loads(DshBridge(materials.store, "library").records.request(request_id)["snapshot"])
        assert "(1) Draw." in str(record["content"]) and "(2) Explain." in str(record["content"])
        assert len(client.get(f"/api/dsh-bridge/questions/{question['id']}/discussions").json()) == 1
        assert client.post(f"/api/dsh-bridge/requests/{request_id}/open").json()["session_id"] == receipt["session_id"]
        assert client.delete(f"/api/study-questions/{question['id']}").status_code == 200
        assert client.post("/api/dsh-bridge/discussions", json={"question_id": question["id"]}).status_code == 404
        assert client.post(f"/api/study-questions/{question['id']}/restore").status_code == 200
        assert client.post("/api/dsh-bridge/discussions", json={"question_id": question["id"]}).json()["request_id"] == request_id
        assert len(FakeClient.requests) == 1
