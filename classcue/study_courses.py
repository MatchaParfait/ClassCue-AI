"""Course ownership shared by document questions, video examples and DSH."""

from __future__ import annotations

from uuid import uuid4


COURSE_SELECT = """SELECT c.*,
    (SELECT COUNT(*) FROM study_documents d WHERE d.course_id=c.id AND d.deleted=0) document_count,
    (SELECT COUNT(*) FROM study_questions q JOIN study_documents d ON d.id=q.document_id
     WHERE d.course_id=c.id AND d.deleted=0 AND q.deleted=0) question_count FROM study_courses c"""


def initialize_courses(db):
    db.execute("""CREATE TABLE IF NOT EXISTS study_courses (
        id TEXT PRIMARY KEY, name TEXT NOT NULL,
        folder_id INTEGER UNIQUE REFERENCES video_folders(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS study_course_folder_name
        AFTER UPDATE OF name ON video_folders
        BEGIN UPDATE study_courses SET name=NEW.name WHERE folder_id=NEW.id; END""")
    db.execute("""CREATE TABLE IF NOT EXISTS course_questions (
        id TEXT PRIMARY KEY, source_kind TEXT NOT NULL CHECK(source_kind IN ('document','video')),
        source_id TEXT NOT NULL, fingerprint TEXT NOT NULL DEFAULT '',
        version INTEGER NOT NULL DEFAULT 1, UNIQUE(source_kind,source_id))""")


class StudyCourseStore:
    def __init__(self, store):
        self.store = store

    def folders(self):
        with self.store.connect() as db:
            rows = db.execute("""SELECT f.id,f.name,f.parent_id,c.id course_id
                FROM video_folders f LEFT JOIN study_courses c ON c.folder_id=f.id
                ORDER BY f.library_order,f.id""").fetchall()
        return [dict(row) for row in rows]

    def courses(self):
        with self.store.connect() as db:
            rows = db.execute(COURSE_SELECT + " ORDER BY c.created_at,c.rowid").fetchall()
        return [dict(row) for row in rows]

    def course(self, course_id):
        with self.store.connect() as db:
            row = db.execute(COURSE_SELECT + " WHERE c.id=?", (course_id,)).fetchone()
        if row is None:
            raise KeyError("课程资料夹不存在")
        return dict(row)

    def create(self, *, folder_id=None, name=""):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if folder_id is not None:
                existing = db.execute("SELECT id FROM study_courses WHERE folder_id=?", (folder_id,)).fetchone()
                if existing:
                    return self.course(existing["id"])
                folder = db.execute("SELECT name FROM video_folders WHERE id=?", (folder_id,)).fetchone()
                if folder is None:
                    raise KeyError("课程视频文件夹不存在")
                name = folder["name"]
            else:
                if not isinstance(name, str) or not 1 <= len(name.strip()) <= 240:
                    raise ValueError("课程名称需要 1 至 240 个字符")
                name = name.strip()
                order = db.execute("SELECT COALESCE(MAX(library_order),-1)+1 FROM video_folders WHERE parent_id IS NULL").fetchone()[0]
                folder_id = db.execute("INSERT INTO video_folders(name,library_order) VALUES(?,?)", (name, order)).lastrowid
            course_id = uuid4().hex
            db.execute("INSERT INTO study_courses(id,name,folder_id) VALUES(?,?,?)", (course_id, name, folder_id))
        return self.course(course_id)

    def bind(self, course_id, folder_id):
        self.course(course_id)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            folder = db.execute("SELECT name FROM video_folders WHERE id=?", (folder_id,)).fetchone()
            if folder is None:
                raise KeyError("视频文件夹不存在")
            owner = db.execute("SELECT id FROM study_courses WHERE folder_id=?", (folder_id,)).fetchone()
            if owner and owner["id"] != course_id:
                raise ValueError("此视频文件夹已绑定另一门课程")
            db.execute("UPDATE study_courses SET folder_id=?,name=? WHERE id=?", (folder_id, folder["name"], course_id))
        return self.course(course_id)

    def for_folder(self, folder_id):
        with self.store.connect() as db:
            row = db.execute("""WITH RECURSIVE ancestors(id,parent_id,depth) AS (
                SELECT id,parent_id,0 FROM video_folders WHERE id=?
                UNION ALL SELECT f.id,f.parent_id,a.depth+1 FROM video_folders f JOIN ancestors a ON f.id=a.parent_id)
                SELECT c.id FROM ancestors a JOIN study_courses c ON c.folder_id=a.id ORDER BY a.depth LIMIT 1""",
                             (folder_id,)).fetchone()
        return self.course(row["id"]) if row else None

    def move_document(self, document_id, course_id):
        from classcue.study_materials import StudyMaterialStore
        self.course(course_id)
        materials = StudyMaterialStore(self.store)
        materials.document(document_id, include_deleted=True)
        with self.store.connect() as db:
            db.execute("UPDATE study_documents SET course_id=? WHERE id=?", (course_id, document_id))
        return materials.document(document_id, include_deleted=True)

    def video_question(self, marker_id):
        from classcue.question_visuals import QuestionVisualStore, question_marker
        marker = question_marker(self.store, marker_id)
        if marker.get("review_status") == "excluded":
            raise KeyError("视频例题已移除")
        video = dict(self.store.get_video(marker["video_id"]))
        course = self.for_folder(video["library_folder_id"])
        if course is None:
            raise ValueError("请先为此视频的课程文件夹建立学习资料夹")
        visual = QuestionVisualStore(self.store).get(marker_id) or {}
        binding = visual.get("binding")
        if not binding:
            raise ValueError("请先为视频例题保存题图")
        from classcue.question_captures import QuestionCaptures
        captures = QuestionCaptures(self.store)
        capture = captures.get(marker_id, binding["capture_id"])
        if not captures.path(capture["id"]).is_file():
            raise ValueError("视频例题的题图文件不可用")
        fingerprint = capture["image_hash"]
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM course_questions WHERE source_kind='video' AND source_id=?",
                             (str(marker_id),)).fetchone()
            if row is None:
                question_id, version = uuid4().hex, 1
                db.execute("""INSERT INTO course_questions(id,source_kind,source_id,fingerprint)
                    VALUES(?,'video',?,?)""", (question_id, str(marker_id), fingerprint))
            else:
                question_id = row["id"]
                version = row["version"] + int(row["fingerprint"] != fingerprint)
                db.execute("UPDATE course_questions SET fingerprint=?,version=? WHERE id=?",
                           (fingerprint, version, question_id))
        return {"id": question_id, "source_kind": "video", "course_id": course["id"],
                "course_name": course["name"], "title": marker["title"], "version": version,
                "marker_id": marker_id, "video_id": marker["video_id"], "start_ms": marker["start_ms"],
                "capture_id": capture["id"], "source_name": video["display_name"],
                "mime_type": capture["mime_type"]}

    def video_questions(self, course_id):
        self.course(course_id)
        with self.store.connect() as db:
            rows = db.execute("""WITH RECURSIVE tree(id) AS (
                SELECT folder_id FROM study_courses WHERE id=?
                UNION ALL SELECT f.id FROM video_folders f JOIN tree t ON f.parent_id=t.id
                    WHERE NOT EXISTS(SELECT 1 FROM study_courses c WHERE c.folder_id=f.id))
                SELECT m.id,m.video_id,m.start_ms FROM question_visuals q
                JOIN video_markers m ON m.id=q.marker_id JOIN videos v ON v.id=m.video_id
                WHERE m.review_status!='excluded' AND json_extract(q.payload_json,'$.binding.capture_id') IS NOT NULL
                AND v.library_folder_id IN (SELECT id FROM tree)
                ORDER BY v.library_order,v.id,m.start_ms,m.id""", (course_id,)).fetchall()
        values = []
        for row in rows:
            try:
                values.append(self.video_question(row["id"]))
            except (KeyError, ValueError, OSError):
                # Keep broken sources visible for repair in the original video.
                values.append({"marker_id": row["id"], "video_id": row["video_id"],
                               "start_ms": row["start_ms"], "course_id": course_id,
                               "unavailable": True, "title": "题图不可用，请回到视频修正"})
        return values

    def question(self, question_id):
        from classcue.study_materials import StudyMaterialStore
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM course_questions WHERE id=? AND source_kind='video'", (question_id,)).fetchone()
        if row:
            return self.video_question(int(row["source_id"]))
        materials = StudyMaterialStore(self.store)
        question = materials.question(question_id)
        document = materials.document(question["document_id"])
        if not document["course_id"]:
            raise ValueError("请先把资料移入课程资料夹")
        course = self.course(document["course_id"])
        return {**question, "source_kind": "document", "course_id": course["id"],
                "course_name": course["name"], "source_name": document["name"]}
