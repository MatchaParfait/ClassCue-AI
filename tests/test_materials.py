"""Synthetic source-backed material regression tests; never calls a model service."""

from io import BytesIO
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from classcue.material_agent import organize_document, preprocess
from classcue.material_parser import parse_document
from classcue.material_store import MaterialStore
from classcue.processing_control import ProcessingCancelled
from classcue.video_store import VideoStore


@pytest.fixture
def repo(tmp_path):
    return MaterialStore(VideoStore(tmp_path / "library" / "project.db"))


def collection(repo):
    return repo.save_collection("数据库原理")["id"]


def document(repo, collection_id, name="模拟卷.md", text="# 模拟卷\n\n1. 绘制实体联系图。\n\n答案：\n"):
    result = repo.register(collection_id, name, text.encode("utf-8"))
    parse_document(repo, result["id"])
    return result


def category(repo, collection_id, topic="数据库设计", name="绘制实体联系图（E-R 图）"):
    parent = repo.save_category(collection_id, name=topic)
    return repo.save_category(collection_id, name=name, parent_id=parent, aliases=["ER 图", "实体联系图"])


def values(repo, doc, categories, **overrides):
    block = repo.blocks(doc["id"])[-2]
    source = {"block_id": block["id"]}
    if len(block.get("text", "").splitlines()) > 1:
        source.update(start=0, end=len(block.get("text", "")))
    return {"title": "绘制实体联系图", "kind": "question", "quality": "ready",
            "sources": [source],
            "category_ids": [categories], "tags": ["绘图"], **overrides}


def full_source(block):
    source = {"block_id": block["id"]}
    if block.get("text"):
        source.update(start=0, end=len(block["text"]))
    return source


def pdf_bytes():
    from reportlab.pdfgen import canvas
    stream = BytesIO()
    pdf = canvas.Canvas(stream, pagesize=(400, 500))
    pdf.drawString(30, 440, "1. Draw the entity relationship diagram.")
    pdf.rect(30, 300, 120, 50)
    pdf.drawString(35, 320, "Shared schema")
    pdf.showPage()
    pdf.drawString(30, 440, "(2) Use the schema from page 1.")
    pdf.save()
    return stream.getvalue()


def image_bytes():
    stream = BytesIO()
    Image.new("RGB", (320, 240), "white").save(stream, "PNG")
    return stream.getvalue()


def docx_bytes():
    from docx import Document
    doc = Document()
    doc.add_heading("Synthetic paper", 1)
    doc.add_paragraph("1. Use the following table.")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Attribute"
    table.cell(0, 1).text = "Value"
    table.cell(1, 0).text = "A"
    table.cell(1, 1).text = "42"
    doc.add_picture(BytesIO(image_bytes()))
    stream = BytesIO()
    doc.save(stream)
    return stream.getvalue()


@pytest.mark.parametrize("name,data,format_name", [
    ("paper.pdf", pdf_bytes, "pdf"),
    ("photo.png", image_bytes, "image"),
    ("notes.md", lambda: b"# Paper\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n```sql\nSELECT * FROM T;\n```\n\n$x^2$\n", "markdown"),
    ("paper.docx", docx_bytes, "docx"),
])
def test_four_formats_and_idempotent_parsing(repo, name, data, format_name):
    doc = repo.register(collection(repo), name, data())
    original = repo.resource(doc["resource"]).read_bytes()
    blocks = parse_document(repo, doc["id"])
    assert blocks and doc["format"] == format_name
    assert parse_document(repo, doc["id"]) == blocks
    assert repo.resource(doc["resource"]).read_bytes() == original
    assert all(b["coverage"] == "unprocessed" for b in blocks)
    if format_name == "pdf":
        assert len(blocks) == 2
        assert "Draw" in blocks[0]["text"]
        assert blocks[0]["words"][0]["bbox"][1] < 0.2
        assert repo.resource(blocks[0]["image"]).is_file()
    if format_name == "markdown":
        assert any("```sql" in b["text"] for b in blocks)
        assert all(b["line_start"] <= b["line_end"] for b in blocks)
    if format_name == "docx":
        assert any("| Attribute" in b["text"] for b in blocks)
        assert any(b.get("images") for b in blocks)


def test_markdown_numbered_subquestions_get_independent_source_blocks(repo):
    document_id = repo.register(
        collection(repo),
        "numbered.md",
        "# 试卷\n\n## 一、查询题\n\n共享关系模式。\n\n"
        "1. 查询员工姓名。\n\n2. 查询项目名称。\n\n3. 优化查询树。\n".encode("utf-8"),
    )["id"]
    blocks = parse_document(repo, document_id)
    texts = [block["text"] for block in blocks]
    assert any("共享关系模式" in text for text in texts)
    assert any(text.lstrip().startswith("1.") for text in texts)
    assert any(text.lstrip().startswith("2.") for text in texts)
    assert any(text.lstrip().startswith("3.") for text in texts)
    assert len(blocks) >= 5


def test_import_copies_and_never_deduplicates(repo, tmp_path):
    source = tmp_path / "original.md"
    source.write_text("1. Example", encoding="utf-8")
    collection_id = collection(repo)
    first = repo.register(collection_id, source.name, source.read_bytes())
    second = repo.register(collection_id, source.name, source.read_bytes())
    assert first["id"] != second["id"] and first["sha256"] == second["sha256"]
    repo.soft_delete("material_documents", first["id"], True)
    assert source.read_text(encoding="utf-8") == "1. Example"
    assert repo.resource(first["resource"]).is_file()
    assert len(repo.documents(collection_id)) == 1


def test_classification_aggregates_papers_and_preserves_aliases(repo):
    collection_id = collection(repo)
    er = category(repo, collection_id)
    query = category(repo, collection_id, "查询优化", "绘制优化后的查询树")
    graph = category(repo, collection_id, "事务并发控制", "绘制前驱图并判断可串行化")
    for index in range(3):
        doc = document(repo, collection_id, f"模拟卷{index + 1}.md")
        repo.save_item(doc["id"], values(repo, doc, er))
    assert len(repo.find_items(collection_id, category_id=er)) == 3
    counts = {c["id"]: c["count"] for c in repo.list_categories(collection_id)}
    assert counts[er] == 3 and counts[query] == 0 and counts[graph] == 0
    assert repo.collections()[0]["total"] == 3
    renamed = "绘制业务实体联系图"
    old = next(c for c in repo.list_categories(collection_id) if c["id"] == er)
    repo.save_category(collection_id, category_id=er, name=renamed, parent_id=old["parent_id"],
                       aliases=["ER 图", old["name"]], position=0)
    with repo.store.connect() as db:
        same = repo._category(db, collection_id, old["name"], old["parent_id"])
    assert same == er
    assert next(c for c in repo.list_categories(collection_id) if c["id"] == er)["name"] == renamed


def test_category_merge_multiple_memberships_and_progress(repo):
    collection_id = collection(repo)
    first = category(repo, collection_id)
    second = category(repo, collection_id, "关系模型", "将实体联系图转换为关系模式")
    doc = document(repo, collection_id)
    item = repo.save_item(doc["id"], values(repo, doc, first, category_ids=[first, second]))
    repo.study(item["id"], "mastered", item["version"])
    assert repo.collections()[0]["total"] == repo.collections()[0]["mastered"] == 1
    repo.merge_categories(first, second)
    assert repo.read_items([item["id"]])[0]["category_ids"] == [second]
    with pytest.raises(ValueError):
        repo.save_category(collection_id, name="第三层", parent_id=second)
    reopened = MaterialStore(VideoStore(repo.store.path))
    assert reopened.read_items([item["id"]])[0]["study_status"] == "mastered"


def test_content_version_and_label_only_edit(repo):
    collection_id = collection(repo)
    doc = document(repo, collection_id)
    kind = category(repo, collection_id)
    item = repo.save_item(doc["id"], values(repo, doc, kind))
    repo.study(item["id"], "mastered", 1)
    renamed = repo.save_item(doc["id"], {"title": "人工标题", "tags": ["实体与联系"]}, item["id"])
    assert renamed["version"] == 1 and renamed["study_status"] == "mastered"
    revised = repo.save_item(doc["id"], {"correction": "用户确认后的修正文句"}, item["id"])
    assert revised["version"] == 2 and revised["study_status"] == "unreviewed"
    old = repo.version(item["id"], 1)
    assert old["study_status"] == "mastered" and old["payload"]["title"] == "人工标题"
    with pytest.raises(ValueError, match="版本"):
        repo.study(item["id"], "mastered", 1)


def test_shared_content_revision_resets_children_and_keeps_prior_version(repo):
    collection_id = collection(repo)
    doc = document(repo, collection_id)
    kind = category(repo, collection_id)
    parent = repo.save_item(doc["id"], values(repo, doc, kind, kind="context", title="共享关系模式"))
    child = repo.save_item(doc["id"], values(repo, doc, kind, parent_id=parent["id"]))
    repo.study(child["id"], "mastered", 1)
    repo.save_item(doc["id"], {"correction": "修正后的共享条件"}, parent["id"])
    latest = repo.read_items([child["id"]])[0]
    assert latest["version"] == 2 and latest["study_status"] == "unreviewed"
    assert repo.version(child["id"], 1)["payload"]["parent_version"] == 1
    assert repo.version(child["id"], 1)["study_status"] == "mastered"


def test_search_reads_original_content_shared_conditions_and_category_aliases(repo):
    collection_id = collection(repo)
    doc = document(repo, collection_id, text="借阅条件：每位读者最多借十本书。\n\n1. 绘制完整模型。\n")
    kind = category(repo, collection_id)
    blocks = repo.blocks(doc["id"])
    parent_source = {"block_id": blocks[0]["id"], "start": 0, "end": len(blocks[0]["text"])}
    item_source = {"block_id": blocks[1]["id"], "start": 0, "end": len(blocks[1]["text"])}
    parent = repo.save_item(doc["id"], {"title": "共享条件", "kind": "context", "sources": [parent_source]})
    item = repo.save_item(doc["id"], {"title": "绘制模型", "sources": [item_source],
                                     "parent_id": parent["id"], "category_ids": [kind]})
    assert any(i["id"] == item["id"] for i in repo.find_items(collection_id, query="十本书"))
    assert any(i["id"] == item["id"] for i in repo.find_items(collection_id, query="ER 图"))


def test_integrity_failure_is_visible_and_resource_scope_survives_relocation(repo, tmp_path, monkeypatch):
    import classcue.material_store as materials
    import classcue.asr_storage as storage
    project = tmp_path / "application"
    (project / "data").mkdir(parents=True)
    monkeypatch.setattr(materials, "PROJECT_ROOT", project)
    collection_id = collection(repo)
    doc = document(repo, collection_id)
    relocated = tmp_path / "moved" / "project.db"
    relocated.parent.mkdir()
    with repo.store.connect() as source:
        import sqlite3
        with sqlite3.connect(relocated) as target:
            source.backup(target)
    (project / "data" / "asr_storage.json").write_text(json.dumps({
        "library_identity": {"database": str(relocated.resolve()), "scope": str(repo.store.path.parent),
                             "id": storage.library_identity(project, repo.store.path)},
    }), encoding="utf-8")
    moved = MaterialStore(VideoStore(relocated))
    assert moved.root == repo.root and moved.resource(doc["resource"]).is_file()
    moved.resource(doc["resource"]).write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="完整性"):
        parse_document(moved, doc["id"])
    assert moved.document(doc["id"])["state"] == "failed"


def test_shared_cross_page_references_and_answers(repo):
    collection_id = collection(repo)
    doc = repo.register(collection_id, "exam.pdf", pdf_bytes())
    blocks = parse_document(repo, doc["id"])
    kind = category(repo, collection_id)
    context = repo.save_item(doc["id"], {"title": "共享模式", "kind": "context",
        "sources": [{"block_id": blocks[0]["id"], "bbox": [0.05, 0.3, 0.5, 0.5]}]})
    sources = [{"block_id": b["id"], "bbox": [0.02, 0.05, 0.98, 0.3]} for b in blocks]
    item = repo.save_item(doc["id"], {"title": "跨页小问", "sources": sources, "parent_id": context["id"],
                                     "references": [{"label": "使用第1题的模式", "item_id": context["id"]}],
                                     "category_ids": [kind]})
    detail = repo.read_items([item["id"]])[0]
    assert len(detail["regions"]) == 2 and detail["shared"]["id"] == context["id"]
    answer = repo.save_item(doc["id"], {"title": "已有解析", "kind": "answer",
                                       "sources": [sources[1]], "category_ids": [kind]})
    assert not repo.read_items([item["id"]])[0]["answers"]
    repo.link_answers(item["id"], [answer["id"]])
    assert repo.read_items([item["id"]])[0]["answers"][0]["id"] == answer["id"]
    assert repo.collections()[0]["total"] == 1
    repo.soft_delete("material_items", answer["id"], True)
    repo.soft_delete("material_items", answer["id"], False)
    assert not repo.read_items([item["id"]])[0]["answers"]


def test_reject_foreign_sources_invalid_ranges_and_cyclic_parent(repo):
    one, two = collection(repo), collection(repo)
    doc1, doc2 = document(repo, one), document(repo, two)
    kind1, kind2 = category(repo, one), category(repo, two)
    with pytest.raises(ValueError, match="来源"):
        repo.save_item(doc1["id"], values(repo, doc2, kind1))
    with pytest.raises(ValueError, match="分类"):
        repo.save_item(doc1["id"], values(repo, doc1, kind2))
    block = repo.blocks(doc1["id"])[0]
    for source in ({"block_id": block["id"], "start": 0, "end": 9000},
                   {"block_id": block["id"], "bbox": [0, 0, 1, 1]}):
        with pytest.raises(ValueError):
            repo.save_item(doc1["id"], {"title": "越界题目", "sources": [source], "category_ids": [kind1]})
    first = repo.save_item(doc1["id"], values(repo, doc1, kind1))
    with pytest.raises(ValueError):
        repo.save_item(doc1["id"], {"parent_id": first["id"]}, first["id"])
    with pytest.raises(ValueError):
        repo.resource("../project.db")


def test_failed_publish_is_atomic_and_every_block_accounted(repo):
    collection_id = collection(repo)
    doc = document(repo, collection_id)
    blocks = repo.blocks(doc["id"])
    good = {"title": "共享情景", "kind": "context", "sources": [full_source(blocks[1])],
            "categories": [{"topic": "数据库设计", "type": "绘制实体联系图（E-R 图）"}]}
    with pytest.raises(KeyError):
        repo.publish(doc["id"], [good, {**good, "kind": "question", "sources": [{"block_id": "invalid"}]}], [])
    assert repo.find_items(collection_id) == []
    assert repo.list_categories(collection_id) == []
    repo.publish(doc["id"], [good], [
        {"block_id": blocks[0]["id"], "state": "non_question"},
        {"block_id": blocks[1]["id"], "state": "identified"},
        {"block_id": blocks[2]["id"], "state": "pending", "reason": "只有答案占位符，不能作为解析"}])
    assert {b["coverage"] for b in repo.blocks(doc["id"])} == {"non_question", "identified", "pending"}


def test_publish_rejects_multiple_independent_questions_atomically(repo):
    collection_id = collection(repo)
    doc = document(repo, collection_id)
    blocks = repo.blocks(doc["id"])
    kind = category(repo, collection_id)
    question = {"title": "读取题目", "sources": [{"block_id": blocks[1]["id"]}],
                "category_ids": [kind]}
    with pytest.raises(ValueError, match="一次只能发布一个小问"):
        repo.publish(doc["id"], [question, {**question, "title": "读取第二题"}], [
            {"block_id": blocks[1]["id"], "state": "identified"}])
    assert repo.find_items(collection_id) == []
    assert repo.blocks(doc["id"])[1]["coverage"] == "unprocessed"


def test_publish_allows_one_context_and_one_child_question(repo):
    collection_id = collection(repo)
    doc = document(repo, collection_id)
    blocks = repo.blocks(doc["id"])
    kind = category(repo, collection_id)
    saved = repo.publish(doc["id"], [
        {"local_id": "scenario", "title": "共享情景", "kind": "context",
         "sources": [full_source(blocks[1])]},
        {"title": "情景小问", "parent_id": "scenario",
         "sources": [full_source(blocks[1])], "category_ids": [kind]},
    ], [{"block_id": blocks[1]["id"], "state": "identified"}])
    assert len(saved) == 2
    child = repo.read_items([saved[1]])[0]
    assert child["shared"]["id"] == saved[0]


def test_agent_rejects_multiline_source_without_character_range(repo):
    collection_id = collection(repo)
    doc = document(repo, collection_id, text="# 模拟卷\n\n1. 第一行条件。\n第二行要求。\n")
    blocks = repo.blocks(doc["id"])
    assert any("\n" in block.get("text", "") for block in blocks)

    class MissingRangeModel(FakeModel):
        def step(self, content, cancel):
            self.calls += 1
            catalog = json.loads(content[0]["text"])
            history = json.loads(content[1]["text"])
            block = next(b for b in catalog["blocks"] if b["coverage"] == "unprocessed")
            if not history:
                return {"tool": "read_blocks", "ids": [block["id"]]}
            return {"tool": "submit", "items": [{
                "title": "多行题目", "sources": [{"block_id": block["id"]}],
                "categories": [{"topic": "数据库设计", "type": "绘制实体联系图（E-R 图）"}],
            }], "coverage": [{"block_id": block["id"], "state": "identified"}]}

    with pytest.raises(ValueError, match="连续四次校验失败"):
        organize_document(repo, doc, MissingRangeModel(), lambda *args: None, lambda: False)
    assert repo.find_items(collection_id) == []
    assert all(block["coverage"] == "unprocessed" for block in blocks)


def test_missing_md_image_complex_word_and_bad_file_are_visible(repo):
    collection_id = collection(repo)
    missing = document(repo, collection_id, text="1. 读图作答\n\n![附图](./missing.png)\n")
    assert any(b["coverage"] == "pending" and "图片" in b["reason"] for b in repo.blocks(missing["id"]))
    from docx import Document
    from docx.oxml import OxmlElement
    doc = Document()
    doc.add_paragraph("Equation")._p.append(OxmlElement("m:oMath"))
    doc.add_paragraph("Automatically numbered question", style="List Number")
    stream = BytesIO()
    doc.save(stream)
    word = repo.register(collection_id, "formula.docx", stream.getvalue())
    parsed = parse_document(repo, word["id"])
    assert any(b["coverage"] == "pending" for b in parsed)
    assert any("自动编号" in b["reason"] for b in parsed)
    bad = repo.register(collection_id, "broken.pdf", b"not a pdf")
    with pytest.raises(ValueError):
        parse_document(repo, bad["id"])
    assert repo.document(bad["id"])["state"] == "failed"


def test_split_merge_soft_delete_recovery_and_no_lost_progress(repo):
    collection_id = collection(repo)
    doc = document(repo, collection_id)
    kind = category(repo, collection_id)
    item = repo.save_item(doc["id"], values(repo, doc, kind))
    repo.study(item["id"], "mastered", 1)
    parts = repo.split(item["id"], [values(repo, doc, kind, title="小问1"), values(repo, doc, kind, title="小问2")])
    assert len(parts) == 2
    merged = repo.merge(parts, values(repo, doc, kind, title="完整题目"))
    assert repo.collections()[0]["total"] == 1
    repo.soft_delete("material_documents", doc["id"], True)
    assert repo.collections()[0]["total"] == 0
    repo.soft_delete("material_documents", doc["id"], False)
    assert repo.collections()[0]["total"] == 1
    assert repo.read_items([merged])[0]["title"] == "完整题目"
    assert repo.read_items([item["id"]], include_deleted=True)[0]["study_status"] == "mastered"


class FakeModel:
    def __init__(self):
        self.closed = False
        self.calls = 0

    def step(self, content, cancel):
        self.calls += 1
        catalog = json.loads(content[0]["text"])
        history = json.loads(content[1]["text"])
        block = next(b for b in catalog["blocks"] if b["coverage"] == "unprocessed")
        if not history:
            return {"tool": "read_blocks", "ids": [block["id"]]}
        if block["has_images"] and not any(h["call"]["tool"] == "view_images" for h in history):
            return {"tool": "view_images", "ids": [block["id"]]}
        source = {"block_id": block["id"]}
        if block["has_images"]:
            source["bbox"] = [0.05, 0.05, 0.95, 0.95]
        elif block["characters"] > 0 and block["lines"][0] != block["lines"][1]:
            source.update(start=0, end=block["characters"])
        return {"tool": "submit", "items": [{"title": "绘制实体联系图", "sources": [source],
                 "categories": [{"topic": "数据库设计", "type": "绘制实体联系图（E-R 图）"}]}],
                "coverage": [{"block_id": block["id"], "state": "identified"}]}

    def close(self):
        self.closed = True


def test_agent_has_source_tools_checkpoints_and_no_automatic_resume(repo):
    collection_id = collection(repo)
    document(repo, collection_id, text="1. Draw an E-R diagram.")
    fake = FakeModel()
    result = preprocess(repo, collection_id, "fake", lambda: fake, lambda *args: None, lambda: False)
    assert result["collection_id"] == collection_id and fake.closed and fake.calls == 2
    assert repo.find_items(collection_id)[0]["origin"] == "automatic"
    def forbidden():
        raise AssertionError("已完成内容不能再次调用模型")
    preprocess(repo, collection_id, "fake", forbidden, lambda *args: None, lambda: False)
    interrupted = collection(repo)
    document(repo, interrupted, text="1. Draw another diagram.")
    with pytest.raises(ProcessingCancelled):
        preprocess(repo, interrupted, "fake", forbidden, lambda *args: None, lambda: True)
    assert repo.runs(interrupted)[0]["state"] == "interrupted"
    assert MaterialStore(VideoStore(repo.store.path)).find_items(interrupted) == []


def test_agent_must_view_image_and_can_leave_blurry_pending(repo):
    collection_id = collection(repo)
    doc = repo.register(collection_id, "blurry.png", image_bytes())
    parse_document(repo, doc["id"])
    fake = FakeModel()
    organize_document(repo, doc, fake, lambda *args: None, lambda: False)
    assert fake.calls == 3
    assert repo.blocks(doc["id"])[0]["coverage"] == "identified"
    second = repo.register(collection_id, "unreadable.png", image_bytes())
    parse_document(repo, second["id"])

    class PendingModel(FakeModel):
        def step(self, content, cancel):
            result = super().step(content, cancel)
            if result["tool"] == "submit":
                result["items"][0].update(quality="pending", reason="题图模糊，关键条件不可读")
                result["coverage"][0].update(state="pending", reason="题图模糊，需用户补图")
            return result

    organize_document(repo, second, PendingModel(), lambda *args: None, lambda: False)
    pending = repo.find_items(collection_id, document_id=second["id"])
    assert pending[0]["quality"] == "pending"
    assert repo.collections()[0]["total"] == 1


def test_cancel_after_checkpoint_keeps_results_and_manual_resume_only_calls_remaining(repo):
    collection_id = collection(repo)
    document(repo, collection_id, "first.md", "1. Draw an E-R diagram.")
    second = document(repo, collection_id, "second.md", "1. Draw another E-R diagram.")
    fake = FakeModel()
    with pytest.raises(ProcessingCancelled):
        preprocess(repo, collection_id, "fake", lambda: fake, lambda *args: None,
                   lambda: bool(repo.find_items(collection_id)))
    assert len(repo.find_items(collection_id)) == 1 and fake.closed
    assert repo.blocks(second["id"])[0]["coverage"] == "unprocessed"
    resume = FakeModel()
    preprocess(repo, collection_id, "fake", lambda: resume, lambda *args: None, lambda: False)
    assert len(repo.find_items(collection_id)) == 2 and resume.calls == 2


def test_category_order_move_and_rename_preserves_members(repo):
    collection_id = collection(repo)
    er = category(repo, collection_id)
    topic = next(c for c in repo.list_categories(collection_id) if c["id"] == er)["parent_id"]
    convert = repo.save_category(collection_id, name="将实体联系图转换为关系模式", parent_id=topic)
    repo.save_category(collection_id, category_id=convert, name="将实体联系图转换为关系模式", parent_id=topic, position=2)
    children = [c for c in repo.list_categories(collection_id) if c["parent_id"] == topic]
    assert [c["id"] for c in children] == [convert, er]
    other = repo.save_category(collection_id, name="关系模型")
    repo.save_category(collection_id, category_id=convert, name="转换为关系模式", parent_id=other, position=1)
    assert next(c for c in repo.list_categories(collection_id) if c["id"] == convert)["parent_id"] == other


def test_processing_api_uses_explicit_connection_and_task_center_without_real_requests(repo, monkeypatch):
    import time
    import api.materials as endpoint
    from api.main import create_app
    from classcue.ai_provider import ProviderProfile, ProviderSettings, SavedConnection
    settings = ProviderSettings(connections=[
        SavedConnection(id="first", name="当前连接", profile=ProviderProfile(base_url="https://example.invalid/v1", model="one")),
        SavedConnection(id="selected", name="所选连接", profile=ProviderProfile(base_url="https://example.invalid/v1", model="two")),
    ], active_connection_id="first")
    used = []
    monkeypatch.setattr(endpoint, "load_provider_settings", lambda: settings)
    monkeypatch.setattr(endpoint, "resolve_interface_key", lambda *args: "synthetic-placeholder")
    def factory(profile, key):
        used.append(profile.model)
        return FakeModel()
    monkeypatch.setattr(endpoint, "MaterialModel", factory)
    collection_id = collection(repo)
    document(repo, collection_id, text="1. Draw an E-R diagram.")
    with TestClient(create_app(repo.store)) as client:
        saved = client.post(f"/api/material-collections/{collection_id}/process", json={"connection_id": "selected"})
        assert saved.status_code == 202
        job_id = saved.json()["job_id"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] in {"completed", "failed", "interrupted"}:
                break
            time.sleep(0.02)
        assert job["status"] == "completed", job.get("error_message")
        assert job["job_type"] == "material_preprocess" and job["video_id"] is None
        assert used == ["two"]
        assert repo.runs(collection_id)[0]["state"] == "completed"


def test_folders_do_not_own_collections(repo):
    folder_id = repo.store.create_folder("合成课程")
    saved = repo.save_collection("独立资料", [folder_id])
    with repo.store.connect() as db:
        db.execute("DELETE FROM video_folders WHERE id=?", (folder_id,))
    assert repo.collections()[0]["id"] == saved["id"]
    assert repo.collections()[0]["folder_ids"] == []


def test_api_read_correct_region_restore_and_asset_isolation(repo, monkeypatch):
    from api.main import create_app
    import api.materials as endpoint
    opened = []
    monkeypatch.setattr(endpoint.os, "startfile", lambda filename: opened.append(Path(filename)))
    with TestClient(create_app(repo.store)) as client:
        saved = client.post("/api/material-collections", json={"name": "合成资料集"})
        assert saved.status_code == 201
        collection_id = saved.json()["id"]
        uploaded = client.post("/api/material-documents", params={"collection_id": collection_id, "name": "paper.pdf"},
                               content=pdf_bytes())
        assert uploaded.status_code == 201
        document_id = uploaded.json()["id"]
        blocks = client.get(f"/api/material-documents/{document_id}/blocks").json()
        assert client.post(f"/api/material-documents/{document_id}/open").status_code == 200
        assert opened == [repo.resource(repo.document(document_id)["resource"])]
        image = client.get(f"/api/material-documents/{document_id}/region", params={
            "block_id": blocks[0]["id"], "left": 0.1, "top": 0.1, "right": 0.9, "bottom": 0.3})
        assert image.status_code == 200
        assert Image.open(BytesIO(image.content)).height < Image.open(repo.resource(blocks[0]["image"])).height
        assert client.get(f"/api/material-documents/{document_id}/asset", params={"resource": "../project.db"}).status_code == 400
        assert client.get(f"/api/material-documents/{document_id}/region", params={
            "block_id": blocks[0]["id"], "left": float("nan")}).status_code == 400
        kind = category(repo, collection_id)
        created = client.post("/api/material-items", json={"document_id": document_id, "values": {
            "title": "合成题目", "sources": [full_source(blocks[0])], "category_ids": [kind]}})
        assert created.status_code == 200
        item_id = created.json()["id"]
        assert client.put(f"/api/material-items/{item_id}/study", json={"status": "mastered", "version": 1}).status_code == 200
        assert client.delete(f"/api/material-documents/{document_id}").status_code == 200
        assert client.get("/api/material-items", params={"collection_id": collection_id}).json() == []
        assert client.post(f"/api/material-documents/{document_id}/restore").status_code == 200
        assert client.get(f"/api/material-items/{item_id}").json()["study_status"] == "mastered"
