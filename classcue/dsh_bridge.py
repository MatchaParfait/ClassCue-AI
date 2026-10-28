"""Question snapshots and conversation bindings, independent of study schemas."""

from __future__ import annotations

import base64
import json
import os
import time
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from classcue.dsh_pipe import BridgeFailure, PipeClient

MAX_CONTENT_BYTES = 20 * 1024 * 1024


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def study_snapshot(store, library_id: str, question_id: str) -> dict:
    try:
        from classcue.study_materials import StudyMaterialStore
    except ImportError:
        raise BridgeFailure("study_unavailable", "请先合入资料阅读模块，再使用已保存的题目") from None
    materials = StudyMaterialStore(store)
    question = materials.question(question_id)
    document = materials.document(question["document_id"])
    content = [{"type": "text", "text": (
        f"出处：{document['name']}\n题号：{question.get('number') or '未填写'}\n"
        f"题目：{question.get('title') or '所选题目'}"
    )}]
    for region in question["regions"]:
        block = region["block"]
        if block["kind"] in {"pdf_page", "image"}:
            with materials.region_image(question["document_id"], block["id"], region["bbox"]) as image:
                output = BytesIO()
                image.save(output, format="PNG")
            content.append({"type": "image", "media_type": "image/png",
                            "data": base64.b64encode(output.getvalue()).decode("ascii")})
        elif block["kind"] == "markdown":
            text = block.get("text", "")[region["start"]:region["end"]]
            if not text.strip():
                raise BridgeFailure("empty_question", "所选题目没有可发送的内容")
            supplements = [r for r in question["regions"] if r["block"].get("supplement")]
            if block.get("has_missing_images") and not supplements:
                raise BridgeFailure("missing_image", "题目含缺失图片，请先在资料中补齐题图")
            content.append({"type": "text", "text": text})
        else:
            raise BridgeFailure("unsupported_block", "此题目来源暂不能发送")
    if len(content) == 1:
        raise BridgeFailure("empty_question", "请先在资料中选择一道完整题目")
    if len(content) > 24 or sum(len(p.get("text", "").encode("utf-8")) +
                              len(p.get("data", "")) * 3 // 4 for p in content) > MAX_CONTENT_BYTES:
        raise BridgeFailure("content_too_large", "单题内容不能超过 20 MB 或 24 个片段，请缩小题图范围")
    return {"library_id": library_id, "question_id": question_id,
            "document_id": question["document_id"], "content_version": question["version"],
            "title": question.get("title", ""), "source": document["name"], "content": content}


class BridgeStore:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS dsh_bridge_connection (
                id INTEGER PRIMARY KEY CHECK(id=1), descriptor_path TEXT NOT NULL,
                instance_id TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS dsh_bridge_requests (
                request_id TEXT PRIMARY KEY, question_id TEXT NOT NULL, content_version INTEGER NOT NULL,
                instance_id TEXT NOT NULL, snapshot TEXT NOT NULL, receipt TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
            db.execute("CREATE INDEX IF NOT EXISTS dsh_bridge_question ON dsh_bridge_requests(question_id,content_version)")

    def connect(self, path: Path, factory=PipeClient):
        client = factory(path)
        status = client.call("status")
        if status.get("protocol") != 1 or status.get("desktop_version") != "0.2.1-rc.3":
            raise BridgeFailure("version_incompatible", "桌面桥接版本不兼容")
        with self.store.connect() as db:
            db.execute("""INSERT INTO dsh_bridge_connection VALUES(1,?,?)
                ON CONFLICT(id) DO UPDATE SET descriptor_path=excluded.descriptor_path,
                instance_id=excluded.instance_id""", (str(path.resolve()), status["instance_id"]))
        return status

    def client(self, factory=PipeClient):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM dsh_bridge_connection WHERE id=1").fetchone()
        if not row:
            raise BridgeFailure("not_paired", "请先选择 DSH 桌面插件生成的连接文件")
        client = factory(Path(row["descriptor_path"]))
        status = client.call("status")
        if status["instance_id"] != row["instance_id"]:
            raise BridgeFailure("instance_mismatch", "DSH 实例已变化，请重新连接；原会话绑定保留")
        return client, status

    def wake(self):
        with self.store.connect() as db:
            row = db.execute("SELECT descriptor_path FROM dsh_bridge_connection WHERE id=1").fetchone()
        # The OS protocol targets the ordinary desktop instance only. Custom test profiles
        # must be started explicitly, so a test can never wake the user's normal instance.
        if (os.name != "nt" or not row or Path(row["descriptor_path"]).resolve()
                != (Path.home() / ".dsh" / "classcue-bridge" / "connection.json").resolve()):
            return False
        try:
            os.startfile("dsh://open")
        except OSError:
            raise BridgeFailure("launch_failed", "无法唤起 DSH，请手动开启桌面版后重试") from None
        return True

    def history(self, question_id):
        with self.store.connect() as db:
            rows = db.execute("""SELECT request_id,content_version,instance_id,receipt,created_at
                FROM dsh_bridge_requests WHERE question_id=? ORDER BY created_at,rowid""", (question_id,)).fetchall()
        return [{**dict(row), "receipt": json.loads(row["receipt"]) if row["receipt"] else None} for row in rows]

    def request(self, request_id):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM dsh_bridge_requests WHERE request_id=?", (request_id,)).fetchone()
        if not row:
            raise KeyError("传输记录不存在")
        return dict(row)

    def save_receipt(self, request_id, receipt):
        with self.store.connect() as db:
            db.execute("UPDATE dsh_bridge_requests SET receipt=? WHERE request_id=?", (encode(receipt), request_id))


class DshBridge:
    def __init__(self, store, library_id, snapshot_provider=study_snapshot, client_factory=PipeClient):
        self.store = store
        self.library_id = library_id
        self.records = BridgeStore(store)
        self.snapshot_provider = snapshot_provider
        self.client_factory = client_factory

    def active_client(self):
        waking = self.records.wake()
        deadline = time.monotonic() + 8
        while True:
            try:
                return self.records.client(self.client_factory)
            except BridgeFailure as error:
                if not waking or error.code not in {"plugin_unavailable", "disconnected"} or time.monotonic() >= deadline:
                    raise
                time.sleep(0.15)

    def prepare(self, question_id, *, workspace_id=None, new_discussion=False):
        client, status = self.active_client()
        with self.store.connect() as db:
            prior = db.execute("""SELECT * FROM dsh_bridge_requests WHERE question_id=?
                ORDER BY created_at DESC,rowid DESC""", (question_id,)).fetchall()
        # Existing discussion opens from its immutable snapshot. First read the current version
        # without re-rendering PDF crops; changing the content still requires a new discussion.
        if prior and not new_discussion and self.snapshot_provider is study_snapshot:
            from classcue.study_materials import StudyMaterialStore
            version = StudyMaterialStore(self.store).question(question_id)["version"]
            snapshot = {"content_version": version}
        else:
            snapshot = self.snapshot_provider(self.store, self.library_id, question_id)
        with self.store.connect() as db:
            if prior and not new_discussion:
                row = next((row for row in prior if row["content_version"] == snapshot["content_version"]), None)
                if row is None:
                    raise BridgeFailure("question_changed", "题目内容已修改，请明确新建讨论")
                if row["instance_id"] != status["instance_id"]:
                    raise BridgeFailure("instance_mismatch", "该题绑定到另一个 DSH 实例，请连接原实例或明确新建讨论")
                request = json.loads(row["snapshot"])
            else:
                request = {**snapshot, "request_id": uuid4().hex,
                           "new_discussion": new_discussion}
                if workspace_id:
                    request["workspace_id"] = workspace_id
                # This is committed before crossing the pipe. Timeouts reuse this exact intent.
                db.execute("INSERT INTO dsh_bridge_requests VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                           (request["request_id"], question_id, snapshot["content_version"],
                            status["instance_id"], encode(request), None))
        receipt = client.call("prepare", request)
        self.records.save_receipt(request["request_id"], receipt)
        return receipt

    def resume(self, request_id, *, open_session=False):
        row = self.records.request(request_id)
        client, status = self.active_client() if open_session else self.records.client(self.client_factory)
        if row["instance_id"] != status["instance_id"]:
            raise BridgeFailure("instance_mismatch", "请连接此讨论原先使用的 DSH 桌面实例")
        try:
            receipt = client.call("open" if open_session else "request", {"request_id": request_id})
        except BridgeFailure as error:
            if not open_session or error.code != "request_missing" or row["receipt"] is not None:
                raise
            # The previous timeout may have happened before Host admission.
            receipt = client.call("prepare", json.loads(row["snapshot"]))
        self.records.save_receipt(request_id, receipt)
        return receipt
