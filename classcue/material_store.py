"""Independent, source-backed study materials in the active library."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from uuid import uuid4

from classcue.asr_storage import library_scope
from classcue.paths import PROJECT_ROOT


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def identity():
    return uuid4().hex


def initialize_materials(db):
    # executescript commits implicitly, so keep this inside the caller's schema transaction.
    statements = [
        """CREATE TABLE IF NOT EXISTS material_collections (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS material_collection_folders (
            collection_id TEXT NOT NULL REFERENCES material_collections(id) ON DELETE CASCADE,
            folder_id INTEGER NOT NULL REFERENCES video_folders(id) ON DELETE CASCADE,
            PRIMARY KEY(collection_id,folder_id))""",
        """CREATE TABLE IF NOT EXISTS material_documents (
            id TEXT PRIMARY KEY, collection_id TEXT NOT NULL REFERENCES material_collections(id),
            name TEXT NOT NULL, format TEXT NOT NULL, resource TEXT NOT NULL, sha256 TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'imported', reason TEXT NOT NULL DEFAULT '',
            deleted INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS material_blocks (
            id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES material_documents(id),
            ordinal INTEGER NOT NULL, payload TEXT NOT NULL,
            coverage TEXT NOT NULL DEFAULT 'unprocessed', reason TEXT NOT NULL DEFAULT '',
            UNIQUE(document_id,ordinal))""",
        """CREATE TABLE IF NOT EXISTS material_categories (
            id TEXT PRIMARY KEY, collection_id TEXT NOT NULL REFERENCES material_collections(id),
            parent_id TEXT REFERENCES material_categories(id), name TEXT NOT NULL,
            aliases TEXT NOT NULL DEFAULT '[]', position INTEGER NOT NULL DEFAULT 0,
            manual INTEGER NOT NULL DEFAULT 0)""",
        """CREATE TABLE IF NOT EXISTS material_items (
            id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES material_documents(id),
            ordinal INTEGER NOT NULL, version INTEGER NOT NULL DEFAULT 1, payload TEXT NOT NULL,
            study_status TEXT NOT NULL DEFAULT 'unreviewed', deleted INTEGER NOT NULL DEFAULT 0,
            origin TEXT NOT NULL DEFAULT 'manual', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS material_item_categories (
            item_id TEXT NOT NULL REFERENCES material_items(id),
            category_id TEXT NOT NULL REFERENCES material_categories(id) ON DELETE CASCADE,
            PRIMARY KEY(item_id,category_id))""",
        """CREATE TABLE IF NOT EXISTS material_versions (
            item_id TEXT NOT NULL REFERENCES material_items(id), version INTEGER NOT NULL,
            payload TEXT NOT NULL, study_status TEXT NOT NULL, origin TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(item_id,version))""",
        """CREATE TABLE IF NOT EXISTS material_answer_links (
            question_id TEXT NOT NULL REFERENCES material_items(id), answer_id TEXT NOT NULL REFERENCES material_items(id),
            PRIMARY KEY(question_id,answer_id))""",
        """CREATE TABLE IF NOT EXISTS material_runs (
            id TEXT PRIMARY KEY, collection_id TEXT NOT NULL REFERENCES material_collections(id),
            connection_id TEXT NOT NULL, state TEXT NOT NULL, stage TEXT NOT NULL DEFAULT 'starting',
            reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
        "CREATE INDEX IF NOT EXISTS material_items_document ON material_items(document_id,ordinal)",
        "CREATE INDEX IF NOT EXISTS material_blocks_document ON material_blocks(document_id,ordinal)",
    ]
    for statement in statements:
        db.execute(statement)


class MaterialStore:
    def __init__(self, store):
        self.store = store
        self.root = Path(library_scope(PROJECT_ROOT, store.path)) / "materials"

    def resource(self, relative):
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root.resolve()) or not path.is_file():
            raise ValueError("资料资源不存在或位置无效")
        return path

    @staticmethod
    def require(db, table, record_id):
        if table not in {"material_collections", "material_documents", "material_items", "material_categories",
                         "material_blocks", "material_runs"}:
            raise ValueError("无效记录类型")
        row = db.execute(f"SELECT * FROM {table} WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise KeyError("记录不存在")
        return dict(row)

    def collections(self):
        with self.store.connect() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM material_collections ORDER BY created_at,rowid")]
            for row in rows:
                row["folder_ids"] = [r[0] for r in db.execute(
                    "SELECT folder_id FROM material_collection_folders WHERE collection_id=?", (row["id"],))]
                stats = db.execute("""SELECT COUNT(*), COALESCE(SUM(i.study_status='mastered'),0)
                    FROM material_items i JOIN material_documents d ON d.id=i.document_id
                    WHERE d.collection_id=? AND d.deleted=0 AND i.deleted=0
                    AND json_extract(i.payload,'$.kind')='question'
                    AND json_extract(i.payload,'$.quality')='ready'""", (row["id"],)).fetchone()
                row.update(total=stats[0], mastered=stats[1])
            return rows

    def save_collection(self, name, folder_ids=(), collection_id=None):
        name = str(name).strip()
        if not name or len(name) > 120:
            raise ValueError("资料集名称需要 1 至 120 个字符")
        record_id = collection_id or identity()
        with self.store.connect() as db:
            if collection_id:
                self.require(db, "material_collections", record_id)
                db.execute("UPDATE material_collections SET name=? WHERE id=?", (name, record_id))
            else:
                db.execute("INSERT INTO material_collections(id,name) VALUES (?,?)", (record_id, name))
            ids = set(folder_ids)
            for folder_id in ids:
                if not db.execute("SELECT 1 FROM video_folders WHERE id=?", (folder_id,)).fetchone():
                    raise ValueError("关联的视频文件夹不存在")
            db.execute("DELETE FROM material_collection_folders WHERE collection_id=?", (record_id,))
            db.executemany("INSERT INTO material_collection_folders VALUES (?,?)",
                           [(record_id, folder_id) for folder_id in ids])
        return next(row for row in self.collections() if row["id"] == record_id)

    def documents(self, collection_id, include_deleted=False):
        with self.store.connect() as db:
            self.require(db, "material_collections", collection_id)
            return [dict(row) for row in db.execute("""SELECT d.*,
                (SELECT COUNT(*) FROM material_blocks b WHERE b.document_id=d.id) AS block_count,
                (SELECT COUNT(*) FROM material_blocks b WHERE b.document_id=d.id
                    AND b.coverage='unprocessed') AS remaining,
                (SELECT COUNT(*) FROM material_blocks b WHERE b.document_id=d.id
                    AND b.coverage='pending') AS pending_count
                FROM material_documents d WHERE collection_id=? AND (? OR deleted=0)
                ORDER BY created_at,rowid""", (collection_id, include_deleted))]

    def register(self, collection_id, name, data):
        from classcue.material_parser import FORMATS
        suffix = Path(name).suffix.lower()
        if suffix not in FORMATS:
            raise ValueError("支持 PDF、JPEG、PNG、WebP、Markdown 和 DOCX")
        if not data:
            raise ValueError("文件为空")
        document_id = identity()
        directory = self.root / document_id
        with self.store.connect() as db:
            self.require(db, "material_collections", collection_id)
            directory.mkdir(parents=True, exist_ok=False)
            target = directory / ("original" + suffix)
            try:
                target.write_bytes(data)
                db.execute("""INSERT INTO material_documents
                    (id,collection_id,name,format,resource,sha256) VALUES (?,?,?,?,?,?)""",
                           (document_id, collection_id, Path(name).name[:240], FORMATS[suffix],
                            target.relative_to(self.root).as_posix(), hashlib.sha256(data).hexdigest()))
            except Exception:
                target.unlink(missing_ok=True)
                directory.rmdir()
                raise
        return self.document(document_id)

    def document(self, document_id):
        with self.store.connect() as db:
            return self.require(db, "material_documents", document_id)

    def blocks(self, document_id):
        with self.store.connect() as db:
            self.require(db, "material_documents", document_id)
            return [self.block_value(row) for row in db.execute(
                "SELECT * FROM material_blocks WHERE document_id=? ORDER BY ordinal", (document_id,))]

    @staticmethod
    def block_value(row):
        row = dict(row)
        payload = json.loads(row.pop("payload"))
        return {**row, **payload}

    def save_blocks(self, document_id, blocks):
        with self.store.connect() as db:
            self.require(db, "material_documents", document_id)
            if db.execute("SELECT 1 FROM material_blocks WHERE document_id=?", (document_id,)).fetchone():
                return
            for ordinal, block in enumerate(blocks):
                issue = block.get("issue", "")
                db.execute("""INSERT INTO material_blocks(id,document_id,ordinal,payload,coverage,reason)
                    VALUES (?,?,?,?,?,?)""", (identity(), document_id, ordinal, encode(block),
                                            "pending" if issue else "unprocessed", issue))
            db.execute("UPDATE material_documents SET state='parsed',reason='' WHERE id=?", (document_id,))

    def document_state(self, document_id, state, reason=""):
        with self.store.connect() as db:
            self.require(db, "material_documents", document_id)
            db.execute("UPDATE material_documents SET state=?,reason=? WHERE id=?", (state, reason, document_id))

    def list_categories(self, collection_id):
        with self.store.connect() as db:
            self.require(db, "material_collections", collection_id)
            rows = [dict(row) for row in db.execute("""SELECT c.*,
                (SELECT COUNT(DISTINCT i.id) FROM material_item_categories ic
                 JOIN material_items i ON i.id=ic.item_id JOIN material_documents d ON d.id=i.document_id
                 WHERE ic.category_id=c.id AND i.deleted=0 AND d.deleted=0
                 AND json_extract(i.payload,'$.kind')='question'
                 AND json_extract(i.payload,'$.quality')='ready') AS count
                FROM material_categories c WHERE collection_id=? ORDER BY position,rowid""", (collection_id,))]
            for row in rows:
                row["aliases"] = json.loads(row["aliases"])
                if row["parent_id"] is None:
                    row["count"] = db.execute("""SELECT COUNT(DISTINCT i.id)
                        FROM material_item_categories ic JOIN material_items i ON i.id=ic.item_id
                        JOIN material_documents d ON d.id=i.document_id
                        JOIN material_categories c ON c.id=ic.category_id
                        WHERE (c.id=? OR c.parent_id=?) AND i.deleted=0 AND d.deleted=0
                        AND json_extract(i.payload,'$.kind')='question'
                        AND json_extract(i.payload,'$.quality')='ready'""", (row["id"], row["id"])).fetchone()[0]
            return rows

    def _category(self, db, collection_id, name, parent_id=None, aliases=(), manual=False, category_id=None,
                  position=None):
        name = str(name).strip()
        if not name or len(name) > 100 or name in {"其他知识点", "其他"}:
            raise ValueError("请使用完整、具体的中文分类名称（最多 100 字）")
        if len(aliases) > 30 or any(not isinstance(a, str) or len(a) > 100 for a in aliases):
            raise ValueError("分类别名无效")
        if parent_id:
            parent = self.require(db, "material_categories", parent_id)
            if parent["collection_id"] != collection_id or parent["parent_id"] or parent_id == category_id:
                raise ValueError("目录仅支持知识主题与具体题型两层")
            if category_id and db.execute("SELECT 1 FROM material_categories WHERE parent_id=?", (category_id,)).fetchone():
                raise ValueError("包含题型的主题不能移动为题型")
        if not category_id:
            existing = db.execute("""SELECT * FROM material_categories
                WHERE collection_id=? AND parent_id IS ?""", (collection_id, parent_id)).fetchall()
            for row in existing:
                if name in [row["name"], *json.loads(row["aliases"])]:
                    return row["id"]
            category_id = identity()
            if position is None:
                position = db.execute("SELECT COALESCE(MAX(position),0)+1 FROM material_categories WHERE collection_id=?",
                                      (collection_id,)).fetchone()[0]
            db.execute("""INSERT INTO material_categories
                (id,collection_id,parent_id,name,aliases,position,manual) VALUES (?,?,?,?,?,?,?)""",
                       (category_id, collection_id, parent_id, name, encode(list(aliases)), position, int(manual)))
        else:
            old = self.require(db, "material_categories", category_id)
            if old["collection_id"] != collection_id:
                raise ValueError("分类不属于当前资料集")
            if position is not None and parent_id == old["parent_id"] and position != old["position"]:
                if position < old["position"]:
                    db.execute("""UPDATE material_categories SET position=position+1
                        WHERE collection_id=? AND parent_id IS ? AND position>=? AND position<? AND id<>?""",
                               (collection_id, parent_id, position, old["position"], category_id))
                else:
                    db.execute("""UPDATE material_categories SET position=position-1
                        WHERE collection_id=? AND parent_id IS ? AND position>? AND position<=? AND id<>?""",
                               (collection_id, parent_id, old["position"], position, category_id))
            db.execute("""UPDATE material_categories SET name=?,parent_id=?,aliases=?,position=?,manual=1 WHERE id=?""",
                       (name, parent_id, encode(list(aliases)), old["position"] if position is None else position,
                        category_id))
        return category_id

    def save_category(self, collection_id, **values):
        with self.store.connect() as db:
            self.require(db, "material_collections", collection_id)
            return self._category(db, collection_id, manual=True, **values)

    def merge_categories(self, source_id, target_id):
        with self.store.connect() as db:
            source = self.require(db, "material_categories", source_id)
            target = self.require(db, "material_categories", target_id)
            if source_id == target_id or source["collection_id"] != target["collection_id"]:
                raise ValueError("合并目标无效")
            if bool(source["parent_id"]) != bool(target["parent_id"]):
                raise ValueError("只能合并相同层级的分类")
            db.execute("""INSERT OR IGNORE INTO material_item_categories SELECT item_id,?
                FROM material_item_categories WHERE category_id=?""", (target_id, source_id))
            db.execute("DELETE FROM material_item_categories WHERE category_id=?", (source_id,))
            db.execute("UPDATE material_categories SET parent_id=? WHERE parent_id=?", (target_id, source_id))
            aliases = list(dict.fromkeys([*json.loads(target["aliases"]), source["name"],
                                         *json.loads(source["aliases"])]))
            db.execute("UPDATE material_categories SET aliases=?,manual=1 WHERE id=?", (encode(aliases), target_id))
            db.execute("DELETE FROM material_categories WHERE id=?", (source_id,))

    def _references(self, db, document_id, refs):
        if not isinstance(refs, list) or not refs or len(refs) > 200:
            raise ValueError("题目必须有可定位的来源")
        result = []
        for ref in refs:
            if not isinstance(ref, dict):
                raise ValueError("来源区域格式无效")
            block = self.require(db, "material_blocks", ref.get("block_id"))
            if block["document_id"] != document_id:
                raise ValueError("题目来源不属于当前文件")
            payload = json.loads(block["payload"])
            bbox = ref.get("bbox")
            if bbox is not None:
                if (not payload.get("image") or not isinstance(bbox, list) or len(bbox) != 4
                        or any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n)
                               or not 0 <= n <= 1 for n in bbox)
                        or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]):
                    raise ValueError("来源区域需要有效的 0 到 1 归一化矩形坐标")
            start, end = ref.get("start"), ref.get("end")
            if start is not None or end is not None:
                if (type(start) is not int or type(end) is not int
                        or not 0 <= start < end <= len(payload.get("text", ""))):
                    raise ValueError("来源文字范围越界")
            elif bbox is None and len(payload.get("text", "").splitlines()) > 1:
                raise ValueError("多行文字来源必须提供精确的 start/end 范围")
            elif bbox is None and not payload.get("text") and (payload.get("image") or payload.get("images")):
                raise ValueError("图片来源必须提供 bbox")
            result.append({key: ref[key] for key in ("block_id", "bbox", "start", "end") if key in ref})
        return result

    def _save_item(self, db, document_id, values, *, item_id=None, origin="manual"):
        document = self.require(db, "material_documents", document_id)
        if document["deleted"]:
            raise ValueError("请先恢复来源文件")
        old_row = self.require(db, "material_items", item_id) if item_id else None
        if old_row and old_row["document_id"] != document_id:
            raise ValueError("不能更换题目的来源文件")
        old = json.loads(old_row["payload"]) if old_row else {}
        allowed = {"title", "number", "kind", "quality", "reason", "sources", "parent_id",
                   "references", "tags", "correction", "correction_image", "answer_sources"}
        if set(values) - allowed - {"category_ids", "id"}:
            raise ValueError("存在不支持的题目字段")
        payload = {**old, **{key: value for key, value in values.items() if key in allowed}}
        payload.setdefault("kind", "question")
        payload.setdefault("quality", "ready")
        payload.setdefault("tags", [])
        payload.setdefault("number", "")
        payload.setdefault("reason", "")
        payload.setdefault("references", [])
        payload.setdefault("parent_id", None)
        payload.setdefault("correction", "")
        payload.setdefault("correction_image", None)
        payload.setdefault("answer_sources", [])
        if not isinstance(payload.get("title"), str) or not payload["title"].strip() or len(payload["title"]) > 180:
            raise ValueError("题目标题需要 1 至 180 个字符")
        if any(not isinstance(payload[key], str) or len(payload[key]) > maximum
               for key, maximum in (("number", 120), ("reason", 2000))):
            raise ValueError("题号或异常说明无效")
        if payload["kind"] not in {"question", "context", "answer"} or payload["quality"] not in {"ready", "pending"}:
            raise ValueError("题目类型或审核状态无效")
        if payload["quality"] == "pending" and not str(payload["reason"]).strip():
            raise ValueError("待处理内容需要具体原因")
        if (not isinstance(payload["tags"], list) or len(payload["tags"]) > 30
                or any(not isinstance(t, str) or not t.strip() or len(t) > 100 for t in payload["tags"])):
            raise ValueError("辅助标签无效")
        if not isinstance(payload["correction"], str) or len(payload["correction"]) > 200000:
            raise ValueError("修正文字过长")
        if payload["correction_image"]:
            image = self.resource(payload["correction_image"])
            if not image.is_relative_to((self.root / document_id / "corrections").resolve()):
                raise ValueError("修正题图不属于当前文件")
        payload["sources"] = self._references(db, document_id, payload.get("sources"))
        if payload["answer_sources"]:
            payload["answer_sources"] = self._references(db, document_id, payload["answer_sources"])
        if payload["parent_id"]:
            parent = self.require(db, "material_items", payload["parent_id"])
            parent_payload = json.loads(parent["payload"])
            if (parent["document_id"] != document_id or parent["deleted"]
                    or parent_payload["kind"] != "context" or payload["kind"] == "context"):
                raise ValueError("共享题干必须引用同文件的有效大题")
            payload["parent_version"] = parent["version"]
        else:
            payload.pop("parent_version", None)
        if not isinstance(payload["references"], list) or len(payload["references"]) > 30:
            raise ValueError("前题引用无效")
        for ref in payload["references"]:
            if not isinstance(ref, dict) or not isinstance(ref.get("label"), str) or not ref["label"].strip():
                raise ValueError("前题引用需要保留原文描述")
            if ref.get("item_id"):
                linked = self.require(db, "material_items", ref["item_id"])
                if linked["document_id"] != document_id or linked["deleted"] or linked["id"] == item_id:
                    raise ValueError("前题引用必须属于当前文件，且不能引用自身")
                queue, seen = [linked["id"]], set()
                while queue:
                    reference_id = queue.pop()
                    if reference_id == item_id:
                        raise ValueError("前题引用不能形成循环")
                    if reference_id in seen:
                        continue
                    seen.add(reference_id)
                    reference = json.loads(self.require(db, "material_items", reference_id)["payload"])
                    queue.extend(r["item_id"] for r in reference.get("references", []) if r.get("item_id"))
                    if reference.get("parent_id"):
                        queue.append(reference["parent_id"])
            elif payload["quality"] == "ready":
                raise ValueError("引用前题尚未补充，请保留为待处理")
        categories = values.get("category_ids")
        if categories is None:
            categories = [r[0] for r in db.execute(
                "SELECT category_id FROM material_item_categories WHERE item_id=?", (item_id,))] if item_id else []
        if not isinstance(categories, list) or len(categories) > 50 or any(not isinstance(c, str) for c in categories):
            raise ValueError("题目分类列表无效")
        categories = list(dict.fromkeys(categories))
        for category_id in categories:
            category = self.require(db, "material_categories", category_id)
            if category["collection_id"] != document["collection_id"]:
                raise ValueError("分类不属于当前资料集")
        if payload["quality"] == "ready" and payload["kind"] != "context" and not categories:
            raise ValueError("可用条目至少需要一个分类")
        substantive = ("sources", "answer_sources", "parent_id", "references", "correction",
                       "correction_image", "kind", "parent_version")
        changed = old_row and any(payload.get(key) != old.get(key) for key in substantive)
        version = old_row["version"] + 1 if changed else old_row["version"] if old_row else 1
        status = "unreviewed" if changed or not old_row else old_row["study_status"]
        if changed:
            db.execute("INSERT INTO material_versions(item_id,version,payload,study_status,origin) VALUES (?,?,?,?,?)",
                       (item_id, old_row["version"], old_row["payload"], old_row["study_status"], old_row["origin"]))
        ordinal = old_row["ordinal"] if old_row else db.execute(
            "SELECT COALESCE(MAX(ordinal),-1)+1 FROM material_items WHERE document_id=?", (document_id,)).fetchone()[0]
        item_id = item_id or identity()
        db.execute("""INSERT INTO material_items(id,document_id,ordinal,version,payload,study_status,origin)
            VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            version=excluded.version,payload=excluded.payload,study_status=excluded.study_status,origin=excluded.origin""",
                   (item_id, document_id, ordinal, version, encode(payload), status, origin))
        db.execute("DELETE FROM material_item_categories WHERE item_id=?", (item_id,))
        db.executemany("INSERT INTO material_item_categories VALUES (?,?)", [(item_id, c) for c in categories])
        if old_row and old.get("kind") != payload["kind"]:
            if db.execute("""SELECT 1 FROM material_items
                WHERE deleted=0 AND json_extract(payload,'$.parent_id')=?""", (item_id,)).fetchone():
                raise ValueError("共享题干仍有小问引用，不能更改其内容类型")
            db.execute("DELETE FROM material_answer_links WHERE question_id=? OR answer_id=?", (item_id, item_id))
        if changed and payload["kind"] == "context":
            children = db.execute("""SELECT * FROM material_items
                WHERE json_extract(payload,'$.parent_id')=?""", (item_id,)).fetchall()
            for child in children:
                child_payload = json.loads(child["payload"])
                child_payload["parent_version"] = version
                db.execute("INSERT INTO material_versions(item_id,version,payload,study_status,origin) VALUES (?,?,?,?,?)",
                           (child["id"], child["version"], child["payload"], child["study_status"], child["origin"]))
                db.execute("UPDATE material_items SET version=version+1,payload=?,study_status='unreviewed' WHERE id=?",
                           (encode(child_payload), child["id"]))
        return item_id

    def save_item(self, document_id, values, item_id=None):
        with self.store.connect() as db:
            record_id = self._save_item(db, document_id, values, item_id=item_id)
        return self.read_items([record_id], include_deleted=True)[0]

    def find_items(self, collection_id, *, category_id=None, tags=(), query="", document_id=None,
                   study_status=None, view="all", include_deleted=False):
        with self.store.connect() as db:
            self.require(db, "material_collections", collection_id)
            rows = db.execute("""SELECT i.*,d.name AS document_name,d.collection_id,d.deleted AS document_deleted
                FROM material_items i JOIN material_documents d ON d.id=i.document_id
                WHERE d.collection_id=? AND (? OR (i.deleted=0 AND d.deleted=0))
                ORDER BY d.created_at,d.rowid,i.ordinal,i.rowid""", (collection_id, include_deleted)).fetchall()
            category_ids = None
            search_categories = set()
            if query:
                for category in db.execute("SELECT id,name,aliases FROM material_categories WHERE collection_id=?",
                                           (collection_id,)):
                    if query.casefold() in (category["name"] + " " + " ".join(json.loads(category["aliases"]))).casefold():
                        search_categories.add(category["id"])
                search_categories.update(category["id"] for category in db.execute(
                    "SELECT id,parent_id FROM material_categories WHERE collection_id=?", (collection_id,))
                    if category["parent_id"] in search_categories)
            if category_id:
                category_ids = {r[0] for r in db.execute(
                    "SELECT id FROM material_categories WHERE id=? OR parent_id=?", (category_id, category_id))}
            result = []
            for row in rows:
                value = self._item_value(db, row)
                if document_id and row["document_id"] != document_id:
                    continue
                if category_ids is not None and not category_ids.intersection(value["category_ids"]):
                    continue
                if tags and not set(tags).issubset(value["tags"]):
                    continue
                if study_status and row["study_status"] != study_status:
                    continue
                if view == "pending" and value["quality"] != "pending":
                    continue
                if view == "ready" and (value["quality"] != "ready" or value["kind"] == "context"):
                    continue
                searchable = " ".join([value["title"], value["number"], value.get("document_name", ""),
                                       value["correction"], *value["tags"],
                                       *[r["label"] for r in value["references"]]])
                if (query and query.casefold() not in searchable.casefold()
                        and not search_categories.intersection(value["category_ids"])):
                    # Source text, rather than a generated title alone, participates in search.
                    source_text = " ".join(self._source(db, ref).get("text", "") for ref in value["sources"])
                    if value["parent_id"]:
                        parent = self.require(db, "material_items", value["parent_id"])
                        source_text += " ".join(self._source(db, ref).get("text", "")
                                                for ref in json.loads(parent["payload"])["sources"])
                    if query.casefold() not in source_text.casefold():
                        continue
                result.append(value)
            return result

    @staticmethod
    def _item_value(db, row):
        value = dict(row)
        value.update(json.loads(value.pop("payload")))
        value["category_ids"] = [r[0] for r in db.execute(
            "SELECT category_id FROM material_item_categories WHERE item_id=?", (value["id"],))]
        return value

    def _source(self, db, ref):
        block = self.block_value(self.require(db, "material_blocks", ref["block_id"]))
        text = block.get("text", "")
        return {**block, **ref, "text": text[ref["start"]:ref["end"]] if "start" in ref else text}

    def read_items(self, item_ids, *, include_deleted=False):
        if len(item_ids) > 200:
            raise ValueError("一次最多读取 200 道题")
        with self.store.connect() as db:
            result = []
            for item_id in item_ids:
                row = self.require(db, "material_items", item_id)
                document = self.require(db, "material_documents", row["document_id"])
                if not include_deleted and (row["deleted"] or document["deleted"]):
                    raise KeyError("条目已移除")
                value = self._item_value(db, row)
                value["document_name"] = document["name"]
                value["document_deleted"] = document["deleted"]
                value["regions"] = [self._source(db, ref) for ref in value["sources"]]
                value["answer_regions"] = [self._source(db, ref) for ref in value["answer_sources"]]
                value["shared"] = None
                if value["parent_id"]:
                    parent = self.require(db, "material_items", value["parent_id"])
                    if not parent["deleted"]:
                        value["shared"] = self._item_value(db, parent)
                        value["shared"]["regions"] = [self._source(db, ref) for ref in value["shared"]["sources"]]
                value["answers"] = [self._item_value(db, answer) for answer in db.execute("""
                    SELECT a.* FROM material_answer_links l JOIN material_items a ON a.id=l.answer_id
                    JOIN material_documents d ON d.id=a.document_id
                    WHERE l.question_id=? AND a.deleted=0 AND d.deleted=0""", (item_id,))]
                value["versions"] = [dict(r) for r in db.execute(
                    "SELECT version,study_status,created_at FROM material_versions WHERE item_id=? ORDER BY version DESC",
                    (item_id,))]
                result.append(value)
            return result

    def version(self, item_id, version):
        with self.store.connect() as db:
            self.require(db, "material_items", item_id)
            row = db.execute("SELECT * FROM material_versions WHERE item_id=? AND version=?", (item_id, version)).fetchone()
            if row is None:
                raise KeyError("版本不存在")
            return {**dict(row), "payload": json.loads(row["payload"])}

    def study(self, item_id, status, version):
        if status not in {"unreviewed", "consolidate", "mastered"}:
            raise ValueError("学习状态无效")
        with self.store.connect() as db:
            item = self.require(db, "material_items", item_id)
            if item["version"] != version:
                raise ValueError("题目版本已变化，请刷新后重试")
            db.execute("UPDATE material_items SET study_status=? WHERE id=?", (status, item_id))

    def soft_delete(self, table, record_id, deleted):
        if table not in {"material_documents", "material_items"}:
            raise ValueError("不支持的移除类型")
        with self.store.connect() as db:
            self.require(db, table, record_id)
            if table == "material_items" and deleted:
                self._require_no_children(db, record_id)
            db.execute(f"UPDATE {table} SET deleted=? WHERE id=?", (int(deleted), record_id))
            if deleted:
                ids = [record_id] if table == "material_items" else [
                    r[0] for r in db.execute("SELECT id FROM material_items WHERE document_id=?", (record_id,))]
                for item_id in ids:
                    db.execute("DELETE FROM material_answer_links WHERE question_id=? OR answer_id=?", (item_id, item_id))

    def link_answers(self, question_id, answer_ids):
        with self.store.connect() as db:
            question = self.require(db, "material_items", question_id)
            document = self.require(db, "material_documents", question["document_id"])
            if question["deleted"] or document["deleted"] or json.loads(question["payload"])["kind"] != "question":
                raise ValueError("请选择有效练习题")
            for answer_id in answer_ids:
                answer = self.require(db, "material_items", answer_id)
                source = self.require(db, "material_documents", answer["document_id"])
                if (answer["deleted"] or source["deleted"] or source["collection_id"] != document["collection_id"]
                        or json.loads(answer["payload"])["kind"] != "answer"):
                    raise ValueError("请选择同资料集内的有效解析")
            db.execute("DELETE FROM material_answer_links WHERE question_id=?", (question_id,))
            db.executemany("INSERT INTO material_answer_links VALUES (?,?)",
                           [(question_id, answer_id) for answer_id in set(answer_ids)])

    def split(self, item_id, parts):
        with self.store.connect() as db:
            item = self.require(db, "material_items", item_id)
            if item["deleted"] or len(parts) < 2 or len(parts) > 30:
                raise ValueError("拆分需要 2 至 30 个片段")
            self._require_no_children(db, item_id)
            ids = [self._save_item(db, item["document_id"], part) for part in parts]
            db.execute("UPDATE material_items SET deleted=1 WHERE id=?", (item_id,))
            db.execute("DELETE FROM material_answer_links WHERE question_id=? OR answer_id=?", (item_id, item_id))
            return ids

    def merge(self, item_ids, values):
        with self.store.connect() as db:
            rows = [self.require(db, "material_items", i) for i in dict.fromkeys(item_ids)]
            if len(rows) < 2 or len({r["document_id"] for r in rows}) != 1 or any(r["deleted"] for r in rows):
                raise ValueError("仅能合并同文件内的有效条目")
            record_id = self._save_item(db, rows[0]["document_id"], values)
            for row in rows:
                self._require_no_children(db, row["id"])
                db.execute("UPDATE material_items SET deleted=1 WHERE id=?", (row["id"],))
                db.execute("DELETE FROM material_answer_links WHERE question_id=? OR answer_id=?", (row["id"], row["id"]))
            return record_id

    @staticmethod
    def _require_no_children(db, item_id):
        if db.execute("""SELECT 1 FROM material_items WHERE deleted=0
            AND json_extract(payload,'$.parent_id')=?""", (item_id,)).fetchone():
            raise ValueError("请先调整小问归属，再拆分或合并共享题干")
        if db.execute("""SELECT 1 FROM material_items i,json_each(i.payload,'$.references') r
            WHERE i.deleted=0 AND json_extract(r.value,'$.item_id')=?""", (item_id,)).fetchone():
            raise ValueError("此条目仍被其他题目引用，请先调整引用或移除依赖题目")

    def runs(self, collection_id):
        with self.store.connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM material_runs WHERE collection_id=? ORDER BY created_at DESC,rowid DESC",
                (collection_id,))]

    def start_run(self, collection_id, connection_id):
        record_id = identity()
        with self.store.connect() as db:
            self.require(db, "material_collections", collection_id)
            db.execute("INSERT INTO material_runs(id,collection_id,connection_id,state) VALUES (?,?,?,'running')",
                       (record_id, collection_id, connection_id))
        return record_id

    def run_state(self, run_id, state, stage, reason=""):
        with self.store.connect() as db:
            db.execute("""UPDATE material_runs SET state=?,stage=?,reason=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                       (state, stage, reason, run_id))

    def publish(self, document_id, items, coverage):
        """One Agent submission is atomic: bad references cannot publish half a result."""
        with self.store.connect() as db:
            document = self.require(db, "material_documents", document_id)
            content_items = [item for item in items if item.get("kind", "question") in {"question", "answer"}]
            if len(content_items) > 1 or sum(item.get("kind") == "context" for item in items) > 1:
                raise ValueError("一次只能发布一个小问或解析，最多附带一个共享情景题干")
            aliases = {}
            published = []
            for candidate in items:
                candidate = dict(candidate)
                local_id = candidate.pop("local_id", None)
                proposals = candidate.pop("categories", [])
                categories = list(candidate.pop("category_ids", []))
                for proposal in proposals:
                    topic = self._category(db, document["collection_id"], proposal["topic"])
                    categories.append(self._category(db, document["collection_id"], proposal["type"], topic,
                                                     proposal.get("aliases", [])))
                candidate["category_ids"] = categories
                if candidate.get("parent_id") in aliases:
                    candidate["parent_id"] = aliases[candidate["parent_id"]]
                for ref in candidate.get("references", []):
                    if ref.get("item_id") in aliases:
                        ref["item_id"] = aliases[ref["item_id"]]
                record_id = self._save_item(db, document_id, candidate, origin="automatic")
                if local_id:
                    if local_id in aliases:
                        raise ValueError("提交内临时 ID 重复")
                    aliases[local_id] = record_id
                published.append(record_id)
            for accounting in coverage:
                block = self.require(db, "material_blocks", accounting["block_id"])
                state = accounting["state"]
                reason = str(accounting.get("reason", "")).strip()
                if block["document_id"] != document_id or state not in {"identified", "non_question", "pending"}:
                    raise ValueError("来源覆盖记录无效")
                if state == "pending" and not reason:
                    raise ValueError("待处理来源需要说明原因")
                if state == "identified":
                    found = db.execute("""SELECT 1 FROM material_items i,json_each(i.payload,'$.sources') s
                        WHERE i.document_id=? AND i.deleted=0 AND json_extract(s.value,'$.block_id')=?""",
                                       (document_id, block["id"])).fetchone()
                    if not found:
                        raise ValueError("已识别来源必须有对应题目或共享题干")
                if state == "non_question" and db.execute("""SELECT 1 FROM material_items i,
                    json_each(i.payload,'$.sources') s WHERE i.document_id=? AND i.deleted=0
                    AND json_extract(s.value,'$.block_id')=?""", (document_id, block["id"])).fetchone():
                    raise ValueError("此来源仍有题目引用，请先移除对应条目")
                db.execute("UPDATE material_blocks SET coverage=?,reason=? WHERE id=?", (state, reason, block["id"]))
            return published
