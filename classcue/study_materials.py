"""Local study documents and user-selected question regions."""

from __future__ import annotations

import hashlib
import json
import math
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from classcue.asr_storage import library_scope
from classcue.paths import PROJECT_ROOT


FORMATS = {".pdf": "pdf", ".md": "markdown", ".markdown": "markdown",
           ".jpg": "image", ".jpeg": "image", ".png": "image", ".webp": "image"}
MAX_PAGES = 500
MAX_IMAGE_PIXELS = 60_000_000
MAX_MARKDOWN_BYTES = 5_000_000
MAX_DOCUMENT_BYTES = 100 * 1024 * 1024


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def initialize_study_materials(db):
    from classcue.study_courses import initialize_courses
    initialize_courses(db)
    db.execute("""CREATE TABLE IF NOT EXISTS study_documents (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, format TEXT NOT NULL, resource TEXT NOT NULL,
        sha256 TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'ready', reason TEXT NOT NULL DEFAULT '',
        page_count INTEGER NOT NULL DEFAULT 0, reading_json TEXT NOT NULL DEFAULT '{}',
        deleted INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    db.execute("""CREATE TABLE IF NOT EXISTS study_document_blocks (
        id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES study_documents(id) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL, payload TEXT NOT NULL, UNIQUE(document_id,ordinal))""")
    db.execute("""CREATE TABLE IF NOT EXISTS study_questions (
        id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES study_documents(id) ON DELETE CASCADE,
        title TEXT NOT NULL, number TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL DEFAULT 1,
        deleted INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    db.execute("""CREATE TABLE IF NOT EXISTS study_question_regions (
        question_id TEXT NOT NULL REFERENCES study_questions(id) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL, block_id TEXT NOT NULL REFERENCES study_document_blocks(id),
        payload TEXT NOT NULL, PRIMARY KEY(question_id,ordinal))""")
    db.execute("CREATE INDEX IF NOT EXISTS study_documents_order ON study_documents(deleted,created_at)")
    db.execute("CREATE INDEX IF NOT EXISTS study_questions_document ON study_questions(document_id,deleted,created_at)")
    if "course_id" not in {row[1] for row in db.execute("PRAGMA table_info(study_documents)")}:
        db.execute("ALTER TABLE study_documents ADD COLUMN course_id TEXT REFERENCES study_courses(id)")
    db.execute("CREATE INDEX IF NOT EXISTS study_documents_course ON study_documents(course_id,deleted)")


def _id():
    return uuid4().hex


class StudyMaterialStore:
    def __init__(self, store):
        self.store = store
        self.root = Path(library_scope(PROJECT_ROOT, store.path)) / "study-materials"

    def resource(self, relative):
        root = self.root.resolve()
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("学习资料资源不存在或位置无效")
        return path

    def require(self, table, record_id):
        if table not in {"study_documents", "study_document_blocks", "study_questions"}:
            raise ValueError("无效记录类型")
        with self.store.connect() as db:
            row = db.execute(f"SELECT * FROM {table} WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise KeyError("学习资料记录不存在")
            return dict(row)

    def documents(self, *, include_deleted=False, course_id=None, unassigned=False):
        with self.store.connect() as db:
            rows = db.execute("""SELECT d.*,
                (SELECT COUNT(*) FROM study_questions q WHERE q.document_id=d.id AND q.deleted=0) question_count
                FROM study_documents d WHERE (? OR d.deleted=0)
                AND (? IS NULL OR d.course_id=?) AND (?=0 OR d.course_id IS NULL)
                ORDER BY d.created_at,d.rowid""",
                              (int(include_deleted), course_id, course_id, int(unassigned))).fetchall()
            return [self._document_value(row) for row in rows]

    def document(self, document_id, *, include_deleted=False):
        with self.store.connect() as db:
            row = db.execute("""SELECT d.*,
                (SELECT COUNT(*) FROM study_questions q WHERE q.document_id=d.id AND q.deleted=0) question_count
                FROM study_documents d WHERE d.id=?""", (document_id,)).fetchone()
            if row is None or (row["deleted"] and not include_deleted):
                raise KeyError("学习资料不存在")
            return self._document_value(row)

    @staticmethod
    def _document_value(row):
        value = dict(row)
        value["reading"] = json.loads(value.pop("reading_json"))
        value["deleted"] = bool(value["deleted"])
        return value

    def register(self, name, data, course_id=None):
        if not data:
            raise ValueError("文件为空")
        if len(data) > MAX_DOCUMENT_BYTES:
            raise ValueError("单个资料文件不能超过 100 MB")
        return self.register_stream(name, BytesIO(data), course_id)

    def register_stream(self, name, stream, course_id=None):
        from classcue.study_material_parser import parse_document_file
        if course_id is not None:
            from classcue.study_courses import StudyCourseStore
            StudyCourseStore(self.store).course(course_id)

        filename = Path(name).name[:240]
        suffix = Path(filename).suffix.lower()
        if suffix not in FORMATS:
            raise ValueError("支持 PDF、Markdown、JPEG、PNG 和 WebP")
        document_id = _id()
        directory = self.root / document_id
        directory.mkdir(parents=True, exist_ok=False)
        target = directory / ("original" + suffix)
        try:
            size = 0
            digest = hashlib.sha256()
            with target.open("xb") as output:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_DOCUMENT_BYTES:
                        raise ValueError("单个资料文件不能超过 100 MB")
                    output.write(chunk)
                    digest.update(chunk)
            if not size:
                raise ValueError("文件为空")
            blocks, page_count = parse_document_file(
                FORMATS[suffix], target, directory, self.root, document_id,
            )
            with self.store.connect() as db:
                db.execute("""INSERT INTO study_documents
                    (id,name,format,resource,sha256,page_count,course_id) VALUES (?,?,?,?,?,?,?)""",
                           (document_id, filename, FORMATS[suffix],
                            target.relative_to(self.root).as_posix(),
                            digest.hexdigest(), page_count, course_id))
                for ordinal, block in enumerate(blocks):
                    db.execute("""INSERT INTO study_document_blocks
                        (id,document_id,ordinal,payload) VALUES (?,?,?,?)""",
                               (block["id"], document_id, ordinal, encode(block)))
        except Exception:
            for path in sorted(directory.rglob("*"), reverse=True):
                if path.is_file():
                    path.unlink(missing_ok=True)
                elif path.is_dir():
                    path.rmdir()
            directory.rmdir()
            raise
        return self.document(document_id)

    def blocks(self, document_id):
        self.document(document_id)
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT * FROM study_document_blocks WHERE document_id=? ORDER BY ordinal",
                (document_id,),
            ).fetchall()
        return [self._block_value(row) for row in rows]

    def rename_document(self, document_id, name):
        self.document(document_id, include_deleted=True)
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 240:
            raise ValueError("资料名称需要 1 至 240 个字符")
        with self.store.connect() as db:
            db.execute("UPDATE study_documents SET name=? WHERE id=?", (name.strip(), document_id))
        return self.document(document_id, include_deleted=True)

    @staticmethod
    def _block_value(row):
        value = dict(row)
        value.update(json.loads(value.pop("payload")))
        return value

    def block(self, document_id, block_id):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM study_document_blocks WHERE id=? AND document_id=?",
                (block_id, document_id),
            ).fetchone()
        if row is None:
            raise KeyError("原文区域不存在")
        return self._block_value(row)

    def page_image(self, document_id, block_id):
        document = self.document(document_id)
        block = self.block(document_id, block_id)
        if block["kind"] == "image":
            return self.resource(block["image"])
        if block["kind"] != "pdf_page":
            raise ValueError("此原文区域没有页面图像")
        from classcue.study_material_parser import render_pdf_page

        source = self.resource(document["resource"])
        target = self.root / document_id / "rendered" / f"page-{block['page']:04d}.png"
        if not target.is_file():
            render_pdf_page(source, block["page"] - 1, target)
        return target

    def text_bounds(self, document_id, block_id):
        document = self.document(document_id)
        block = self.block(document_id, block_id)
        if block["kind"] != "pdf_page":
            return {"status": "no_text", "bbox": None}
        cached = block.get("text_bounds")
        if cached and cached.get("version") == 1:
            return cached
        from classcue.study_material_parser import pdf_text_bounds
        try:
            result = pdf_text_bounds(self.resource(document["resource"]), block["page"] - 1)
        except Exception:
            # An optional guide must never prevent reading or manual selection.
            return {"status": "unavailable", "bbox": None}
        result["version"] = 1
        with self.store.connect() as db:
            row = db.execute("SELECT payload FROM study_document_blocks WHERE id=? AND document_id=?",
                             (block_id, document_id)).fetchone()
            if row:
                payload = json.loads(row["payload"])
                payload["text_bounds"] = result
                db.execute("UPDATE study_document_blocks SET payload=? WHERE id=?", (encode(payload), block_id))
        return result

    def region_image(self, document_id, block_id, bbox):
        block = self.block(document_id, block_id)
        if block["kind"] not in {"pdf_page", "image"} or not isinstance(bbox, list) or len(bbox) != 4:
            raise ValueError("题目图像区域无效")
        from PIL import Image

        left, top, right, bottom = bbox
        if (any(isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1 for v in bbox)
                or left >= right or top >= bottom):
            raise ValueError("裁切范围必须是有效的页面相对坐标")
        with Image.open(self.page_image(document_id, block_id)) as source:
            box = (math.floor(left * source.width), math.floor(top * source.height),
                   math.ceil(right * source.width), math.ceil(bottom * source.height))
            return source.crop(box)

    def save_reading(self, document_id, value):
        self.document(document_id)
        if not isinstance(value, dict):
            raise ValueError("阅读位置格式无效")
        allowed = {"page", "scroll_top", "zoom", "question_id", "left_open", "right_open"}
        if set(value) - allowed:
            raise ValueError("阅读位置包含不支持的字段")
        page = value.get("page", 1)
        scroll_top = value.get("scroll_top", 0)
        zoom = value.get("zoom", 1)
        if (type(page) is not int or page < 1 or page > 500
                or isinstance(scroll_top, bool) or not isinstance(scroll_top, (int, float))
                or not 0 <= scroll_top <= 10_000_000
                or isinstance(zoom, bool) or not isinstance(zoom, (int, float)) or not 0.5 <= zoom <= 3):
            raise ValueError("阅读位置超出允许范围")
        with self.store.connect() as db:
            db.execute("UPDATE study_documents SET reading_json=? WHERE id=?",
                       (encode(value), document_id))

    def questions(self, document_id, *, include_deleted=False):
        self.document(document_id, include_deleted=include_deleted)
        with self.store.connect() as db:
            rows = db.execute("""SELECT * FROM study_questions
                WHERE document_id=? AND (? OR deleted=0) ORDER BY created_at,rowid""",
                              (document_id, int(include_deleted))).fetchall()
            return [self._question_value(db, row) for row in rows]

    def question(self, question_id, *, include_deleted=False):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM study_questions WHERE id=?", (question_id,)).fetchone()
            if row is None or (row["deleted"] and not include_deleted):
                raise KeyError("题目不存在")
            document = db.execute("SELECT deleted FROM study_documents WHERE id=?", (row["document_id"],)).fetchone()
            if document["deleted"] and not include_deleted:
                raise KeyError("题目来源已移除")
            return self._question_value(db, row)

    def _question_value(self, db, row):
        value = dict(row)
        value["deleted"] = bool(value["deleted"])
        regions = db.execute("""SELECT r.ordinal,r.block_id,r.payload,b.ordinal block_ordinal,b.payload block_payload
            FROM study_question_regions r JOIN study_document_blocks b ON b.id=r.block_id
            WHERE r.question_id=? ORDER BY r.ordinal""", (row["id"],)).fetchall()
        value["regions"] = []
        for region in regions:
            source = self._block_value({"id": region["block_id"], "document_id": row["document_id"],
                                       "ordinal": region["block_ordinal"], "payload": region["block_payload"]})
            value["regions"].append({"ordinal": region["ordinal"], "block_id": region["block_id"],
                                     **json.loads(region["payload"]),
                                     "block": source})
        return value

    def save_question(self, document_id, values, question_id=None):
        document = self.document(document_id)
        if not isinstance(values, dict) or set(values) - {"title", "number", "regions", "id"}:
            raise ValueError("题目内容无效")
        title = values.get("title", "")
        number = values.get("number", "")
        if not isinstance(title, str) or not isinstance(number, str):
            raise ValueError("题目标题和题号必须是文字")
        title, number = title.strip(), number.strip()
        regions = values.get("regions")
        if len(title) > 180 or len(number) > 120 or not isinstance(regions, list) or not regions or len(regions) > 100:
            raise ValueError("题目需要有效标题并至少选择一个原文范围")
        clean_regions = []
        for region in regions:
            if not isinstance(region, dict) or not isinstance(region.get("block_id"), str):
                raise ValueError("题目来源区域无效")
            block = self.block(document_id, region["block_id"])
            kind = block["kind"]
            if kind in {"pdf_page", "image"}:
                bbox = region.get("bbox")
                if (not isinstance(bbox, list) or len(bbox) != 4
                        or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1
                               for v in bbox)
                        or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]):
                    raise ValueError("图像题目必须提供有效的页面相对选区")
                clean = {"bbox": [float(v) for v in bbox]}
            elif kind == "markdown":
                if region.get("start", 0) != 0 or region.get("end", len(block["text"])) != len(block["text"]):
                    raise ValueError("Markdown 首版以完整内容块作为选题范围")
                clean = {"start": 0, "end": len(block["text"])}
            else:
                raise ValueError("不支持的题目来源类型")
            group = region.get("selection_group")
            if group is not None:
                if not isinstance(group, str) or not group or len(group) > 64:
                    raise ValueError("选区分组无效")
                clean["selection_group"] = group
            clean_regions.append((block["id"], clean))

        updating = question_id is not None
        question_id = question_id or _id()
        with self.store.connect() as db:
            existing = db.execute("SELECT * FROM study_questions WHERE id=?", (question_id,)).fetchone()
            if updating and existing is None:
                raise KeyError("题目不存在")
            if existing and (existing["document_id"] != document_id or existing["deleted"]):
                raise ValueError("题目不存在或来源文件不同")
            if existing:
                old_regions = [(r["block_id"], r["payload"]) for r in db.execute(
                    "SELECT block_id,payload FROM study_question_regions WHERE question_id=? ORDER BY ordinal",
                    (question_id,))]
                new_regions = [(block_id, encode(region)) for block_id, region in clean_regions]
                substantive = old_regions != new_regions
                version = existing["version"] + int(substantive)
                db.execute("""UPDATE study_questions SET title=?,number=?,version=?,
                    updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                           (title, number, version, question_id))
                if substantive:
                    db.execute("DELETE FROM study_question_regions WHERE question_id=?", (question_id,))
            else:
                db.execute("""INSERT INTO study_questions(id,document_id,title,number)
                    VALUES (?,?,?,?)""", (question_id, document_id, title, number))
            if not existing or substantive:
                for ordinal, (block_id, region) in enumerate(clean_regions):
                    db.execute("""INSERT INTO study_question_regions(question_id,ordinal,block_id,payload)
                        VALUES (?,?,?,?)""", (question_id, ordinal, block_id, encode(region)))
        return self.question(question_id)

    def supplement_image(self, document_id, name, data):
        document = self.document(document_id)
        suffix = Path(name).suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".webp"} or not data or len(data) > 20 * 1024 * 1024:
            raise ValueError("补充题图仅支持 20 MB 以内 JPEG、PNG 或 WebP")
        directory = self.root / document_id
        from PIL import Image, ImageOps
        from io import BytesIO
        target = directory / "attachments" / f"{_id()}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with Image.open(BytesIO(data)) as source:
                if source.width * source.height > MAX_IMAGE_PIXELS:
                    raise ValueError("补充图片尺寸过大")
                image = ImageOps.exif_transpose(source).convert("RGB")
                width, height = image.size
                image.save(target, "PNG")
            with self.store.connect() as db:
                ordinal = db.execute("""SELECT COALESCE(MAX(ordinal),-1)+1
                    FROM study_document_blocks WHERE document_id=?""", (document_id,)).fetchone()[0]
                block_id = _id()
                payload = {"kind": "image", "image": target.relative_to(self.root).as_posix(),
                           "width": width, "height": height, "label": Path(name).name[:240],
                           "supplement": True}
                db.execute("""INSERT INTO study_document_blocks(id,document_id,ordinal,payload)
                    VALUES (?,?,?,?)""", (block_id, document_id, ordinal, encode(payload)))
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return self.block(document_id, block_id)

    def set_deleted(self, table, record_id, deleted):
        if table not in {"study_documents", "study_questions"}:
            raise ValueError("不支持的移除类型")
        with self.store.connect() as db:
            row = db.execute(f"SELECT * FROM {table} WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise KeyError("学习资料记录不存在")
            db.execute(f"UPDATE {table} SET deleted=? WHERE id=?", (int(deleted), record_id))
