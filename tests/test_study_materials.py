"""Synthetic manual study selection, with no AI connections."""

from io import BytesIO
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from classcue.study_materials import StudyMaterialStore
from classcue.video_store import VideoStore


@pytest.fixture
def repo(tmp_path):
    return StudyMaterialStore(VideoStore(tmp_path / "library" / "project.db"))


def pdf_bytes(pages=14):
    from reportlab.pdfgen import canvas
    stream = BytesIO()
    pdf = canvas.Canvas(stream, pagesize=(600, 800))
    for index in range(pages):
        pdf.drawString(40, 740, f"Question {index + 1}: Use the diagram.")
        pdf.rect(40, 450, 300, 200)
        pdf.showPage()
    pdf.save()
    return stream.getvalue()


def image_bytes():
    stream = BytesIO()
    Image.new("RGB", (640, 480), "white").save(stream, "PNG")
    return stream.getvalue()


def test_pdf_metadata_lazy_render_cross_page_and_reopen(repo):
    doc = repo.register("paper.pdf", pdf_bytes())
    blocks = repo.blocks(doc["id"])
    assert len(blocks) == 14 and not list(repo.root.rglob("page-*.png"))
    image = repo.page_image(doc["id"], blocks[0]["id"])
    assert image.is_file()
    assert len(list(repo.root.rglob("page-*.png"))) == 1
    regions = [{"block_id": block["id"], "bbox": [0.05, 0.05, 0.9, 0.5]} for block in blocks[:2]]
    question = repo.save_question(doc["id"], {"regions": regions})
    assert len(question["regions"]) == 2 and question["title"] == ""
    assert [region["block_id"] for region in question["regions"]] == [block["id"] for block in blocks[:2]]
    reopened = StudyMaterialStore(VideoStore(repo.store.path))
    assert reopened.question(question["id"])["regions"] == question["regions"]
    crop = repo.region_image(doc["id"], blocks[0]["id"], [0.05, 0.05, 0.9, 0.5])
    assert crop.width > 100 and crop.height < Image.open(image).height


def test_markdown_blocks_keep_source_and_list_items_formula_table_code(repo):
    raw = "# 试卷\n\n1. 给出关系 R。\n   （1）画图。\n   （2）解释。\n2. 第二道题。\n\n$$\nx^2+y^2\n$$\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n```sql\nSELECT * FROM R;\n```\n\n![图](missing.png)\n"
    doc = repo.register("paper.md", raw.encode())
    blocks = repo.blocks(doc["id"])
    assert any("（1）" in block["text"] and "（2）" in block["text"] for block in blocks)
    assert sum(block["text"].startswith("1.") for block in blocks) == 1
    assert any(block["text"].startswith("2.") for block in blocks)
    assert any("| A | B |" in block["text"] for block in blocks)
    assert any("```sql" in block["text"] for block in blocks)
    assert any("$$" in block["text"] for block in blocks)
    assert blocks[-1]["has_missing_images"]
    lines = raw.splitlines(keepends=True)
    for block in blocks:
        assert block["text"] == "".join(lines[block["line_start"] - 1:block["line_end"]])
    selected = [{"block_id": block["id"]} for block in blocks[1:3]]
    question = repo.save_question(doc["id"], {"regions": selected})
    assert len(question["regions"]) == 2


def test_versions_soft_delete_and_source_validation(repo):
    doc = repo.register("photo.png", image_bytes())
    duplicate = repo.register("photo.png", image_bytes())
    block = repo.blocks(doc["id"])[0]
    foreign = repo.blocks(duplicate["id"])[0]
    values = {"regions": [{"block_id": block["id"], "bbox": [0.1, 0.1, 0.8, 0.8]}]}
    question = repo.save_question(doc["id"], values)
    assert doc["id"] != duplicate["id"] and doc["sha256"] == duplicate["sha256"]
    renamed = repo.save_question(doc["id"], {**values, "title": "图题", "number": "三"}, question["id"])
    assert renamed["version"] == 1
    revised = repo.save_question(doc["id"], {"regions": [
        {"block_id": block["id"], "bbox": [0.1, 0.1, 0.9, 0.8]}]}, question["id"])
    assert revised["version"] == 2
    for bbox in ([0, 0, float("nan"), 1], [0.5, 0, 0.2, 1], [-1, 0, 1, 1]):
        with pytest.raises(ValueError):
            repo.save_question(doc["id"], {"regions": [{"block_id": block["id"], "bbox": bbox}]})
    with pytest.raises(KeyError):
        repo.save_question(doc["id"], values, "nonexistent-question")
    with pytest.raises(KeyError):
        repo.save_question(doc["id"], {"regions": [{"block_id": foreign["id"], "bbox": [0, 0, 1, 1]}]})
    repo.set_deleted("study_documents", doc["id"], True)
    with pytest.raises(KeyError):
        repo.questions(doc["id"])
    repo.set_deleted("study_documents", doc["id"], False)
    assert repo.questions(doc["id"])[0]["version"] == 2
    repo.set_deleted("study_questions", question["id"], True)
    assert repo.questions(doc["id"]) == []
    repo.set_deleted("study_questions", question["id"], False)
    assert len(repo.questions(doc["id"])) == 1


def test_invalid_import_and_reading_and_relocation(repo, tmp_path, monkeypatch):
    for name, data in [("bad.pdf", b"not-pdf"), ("bad.md", b"\xff"), ("bad.docx", b"file"),
                       ("empty.md", b""), ("blank.md", b" \n\n")]:
        with pytest.raises(ValueError):
            repo.register(name, data)
    assert repo.documents() == []
    doc = repo.register("notes.md", b"# Question\n\nText.")
    repo.save_reading(doc["id"], {"page": 1, "scroll_top": 123, "zoom": 1.25})
    assert repo.document(doc["id"])["reading"]["zoom"] == 1.25
    import classcue.study_materials as module
    project = tmp_path / "app"
    (project / "data").mkdir(parents=True)
    monkeypatch.setattr(module, "PROJECT_ROOT", project)
    new_path = tmp_path / "new" / "project.db"
    new_path.parent.mkdir()
    with repo.store.connect() as source, sqlite3.connect(new_path) as target:
        source.backup(target)
    (project / "data" / "asr_storage.json").write_text(json.dumps({
        "library_identity": {"database": str(new_path.resolve()), "scope": str(repo.store.path.parent)},
    }))
    moved = StudyMaterialStore(VideoStore(new_path))
    assert moved.resource(moved.document(doc["id"])["resource"]).is_file()


def test_api_flow_and_attachment(repo):
    from api.main import create_app
    with TestClient(create_app(repo.store)) as client:
        course = client.post("/api/study-courses", json={"name": "合成课程"}).json()
        result = client.post("/api/study-documents", params={"name": "paper.pdf", "course_id": course["id"]}, content=pdf_bytes(2))
        assert result.status_code == 201, result.text
        doc = result.json()
        blocks = client.get(f"/api/study-documents/{doc['id']}/blocks").json()
        bounds = client.get(f"/api/study-documents/{doc['id']}/blocks/{blocks[0]['id']}/text-bounds")
        assert bounds.status_code == 200
        assert bounds.json()["status"] == "ready"
        assert client.get(f"/api/study-documents/{doc['id']}/blocks/missing/text-bounds").status_code == 404
        assert client.get(f"/api/study-documents/{doc['id']}/page/1").status_code == 200
        values = {"document_id": doc["id"], "regions": [{"block_id": blocks[0]["id"], "bbox": [0.1, 0.1, 0.8, 0.8]}]}
        question = client.post("/api/study-questions", json=values)
        assert question.status_code == 201, question.text
        question_id = question.json()["id"]
        assert question.json()["regions"][0]["block_id"] == blocks[0]["id"]
        assert client.put("/api/study-questions/missing", json=values).status_code == 404
        assert client.put(f"/api/study-questions/{question_id}", json={**values, "title": "改名"}).json()["version"] == 1
        assert client.put(f"/api/study-documents/{doc['id']}", json={"name": "新卷名"}).json()["name"] == "新卷名"
        assert client.get(f"/api/study-questions/{question_id}").json()["regions"][0]["block_id"] == blocks[0]["id"]
        assert client.get(f"/api/study-documents/{doc['id']}/blocks/{blocks[0]['id']}/region").status_code == 200
        assert client.put(f"/api/study-documents/{doc['id']}/reading",
                          json={"question_id": question_id, "zoom": 1.5}).status_code == 200
        attached = client.post(f"/api/study-documents/{doc['id']}/attachments",
                               params={"name": "supplement.png"}, content=image_bytes())
        assert attached.status_code == 200
        assert attached.json()["supplement"] is True
        assert client.delete(f"/api/study-documents/{doc['id']}").status_code == 200
        assert client.get("/api/study-documents").json() == []
        assert client.get(f"/api/study-questions/{question_id}").status_code == 404
        assert client.get(f"/api/study-questions/{question_id}?include_deleted=true").status_code == 200
        assert client.post(f"/api/study-documents/{doc['id']}/restore").status_code == 200


@pytest.mark.parametrize("format_name,suffix", [("JPEG", ".jpg"), ("PNG", ".png"), ("WEBP", ".webp")])
def test_photo_orientation_and_crop_use_original_pixels(repo, format_name, suffix):
    source = Image.new("RGB", (120, 80), "white")
    source.paste("red", (0, 0, 60, 80))
    data = BytesIO()
    exif = Image.Exif()
    exif[274] = 6
    source.save(data, format_name, exif=exif)
    doc = repo.register("photo" + suffix, data.getvalue())
    block = repo.blocks(doc["id"])[0]
    assert (block["width"], block["height"]) == (80, 120)
    crop = repo.region_image(doc["id"], block["id"], [0, 0, 1, .5])
    assert crop.size == (80, 60)
    assert crop.getpixel((30, 20))[0] > 200 and crop.getpixel((30, 20))[1] < 30
    tiny = repo.region_image(doc["id"], block["id"], [.1, .1, .10001, .10001])
    assert tiny.width >= 1 and tiny.height >= 1
    assert repo.resource(doc["resource"]).read_bytes() == data.getvalue()


def test_rotated_scanned_pdf_uses_display_dimensions_and_needs_no_ocr(repo):
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader
    stream = BytesIO()
    pdf = canvas.Canvas(stream, pagesize=(600, 800))
    pdf.setPageRotation(90)
    pdf.drawImage(ImageReader(BytesIO(image_bytes())), 20, 30, width=500, height=350)
    pdf.showPage()
    pdf.save()
    doc = repo.register("scan.pdf", stream.getvalue())
    block = repo.blocks(doc["id"])[0]
    assert repo.text_bounds(doc["id"], block["id"])["status"] == "no_text"
    with Image.open(repo.page_image(doc["id"], block["id"])) as rendered:
        assert rendered.width / rendered.height == pytest.approx(block["width"] / block["height"], abs=.005)
        crop = repo.region_image(doc["id"], block["id"], [0, 0, .5, .5])
        assert crop.size == (rendered.width // 2, rendered.height // 2)
    question = repo.save_question(doc["id"], {"regions": [{"block_id": block["id"], "bbox": [0, 0, .5, .5]}]})
    assert question["regions"][0]["block"]["kind"] == "pdf_page"


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_text_bounds_maximum_width_matches_rotated_cropped_page(repo, rotation):
    from reportlab.pdfgen import canvas
    from pypdf import PdfReader, PdfWriter
    from PIL import ImageChops
    source = BytesIO()
    pdf = canvas.Canvas(source, pagesize=(600, 800))
    pdf.drawString(55, 710, "left column")
    pdf.drawString(350, 350, "right column")
    pdf.drawString(30, 750, "header extends left")
    pdf.drawString(460, 55, "footer")
    pdf.showPage()
    pdf.save()
    writer = PdfWriter()
    page = PdfReader(BytesIO(source.getvalue())).pages[0]
    page.cropbox.lower_left = (20, 30)
    page.cropbox.upper_right = (560, 770)
    page.rotate(rotation)
    writer.add_page(page)
    output = BytesIO()
    writer.write(output)
    doc = repo.register("bounds.pdf", output.getvalue())
    block = repo.blocks(doc["id"])[0]
    bounds = repo.text_bounds(doc["id"], block["id"])
    assert bounds["status"] == "ready"
    with Image.open(repo.page_image(doc["id"], block["id"])) as image:
        ink = ImageChops.difference(image.convert("RGB"), Image.new("RGB", image.size, "white")).getbbox()
        expected = [ink[0] / image.width, ink[1] / image.height, ink[2] / image.width, ink[3] / image.height]
        assert bounds["bbox"] == pytest.approx(expected, abs=.003)
    # Cached metadata and cross-page group identity survive reopening without changing question identity.
    question = repo.save_question(doc["id"], {"regions": [{"block_id": block["id"], "bbox": [.1, .1, .9, .9],
                                                         "selection_group": "cross-page-gesture"}]})
    reopened = StudyMaterialStore(VideoStore(repo.store.path))
    assert reopened.block(doc["id"], block["id"])["text_bounds"] == bounds
    assert reopened.question(question["id"])["regions"][0]["selection_group"] == "cross-page-gesture"


def test_markdown_indented_code_stays_a_complete_block(repo):
    doc = repo.register("code.md", b"# Exercise\n\n    SELECT *\n    FROM R;\n\nExplain.\n")
    blocks = repo.blocks(doc["id"])
    assert len(blocks) == 3
    assert blocks[1]["text"] == "    SELECT *\n    FROM R;\n"


def test_real_application_data_migration_preserves_study_resources(repo, tmp_path, monkeypatch):
    import classcue.application_data_migration as migration
    import classcue.study_materials as module
    project = tmp_path / "application"
    (project / "data").mkdir(parents=True)
    monkeypatch.setattr(module, "PROJECT_ROOT", project)
    original = StudyMaterialStore(repo.store)
    doc = original.register("paper.pdf", pdf_bytes(2))
    block = original.blocks(doc["id"])[0]
    question = original.save_question(doc["id"], {"regions": [{"block_id": block["id"], "bbox": [.1, .1, .8, .8]}]})
    original.save_reading(doc["id"], {"page": 2, "zoom": 1.5, "question_id": question["id"]})
    destination = tmp_path / "relocated"
    plan = migration.plan(project, repo.store.path, str(destination))
    migration.schedule(project, repo.store.path, str(destination), plan["plan_id"])
    new_root = migration.apply_pending(project, repo.store.path)
    moved = StudyMaterialStore(VideoStore(new_root / "project.db"))
    assert moved.root == original.root
    assert moved.document(doc["id"])["reading"]["question_id"] == question["id"]
    assert moved.question(question["id"])["regions"][0]["block_id"] == block["id"]
    assert moved.resource(doc["resource"]).read_bytes() == original.resource(doc["resource"]).read_bytes()
    assert moved.region_image(doc["id"], block["id"], [.1, .1, .8, .8]).width > 100
