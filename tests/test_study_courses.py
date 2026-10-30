"""Synthetic course ownership and document/video discussion contracts."""

from io import BytesIO
import json
import sqlite3

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from api.main import create_app
from classcue.dsh_bridge import DshBridge, study_snapshot
from classcue.dsh_pipe import BridgeFailure
from classcue.question_captures import QuestionCaptures
from classcue.question_visuals import QuestionVisualStore
from classcue.study_courses import StudyCourseStore
from classcue.study_materials import StudyMaterialStore, initialize_study_materials
from classcue.video_store import VideoStore
from tests.test_dsh_bridge import FakeClient


@pytest.fixture
def courses(tmp_path, monkeypatch):
    import classcue.study_materials as materials
    import classcue.question_captures as captures
    monkeypatch.setattr(materials, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(captures, "PROJECT_ROOT", tmp_path)
    store = VideoStore(tmp_path / "library" / "project.db")
    return StudyCourseStore(store)


def document_question(courses, course=None):
    materials = StudyMaterialStore(courses.store)
    doc = materials.register("synthetic.md", b"# Q3\n\n(1) Draw.\n(2) Explain.\n", course)
    question = materials.save_question(doc["id"], {"regions": [{"block_id": b["id"]} for b in materials.blocks(doc["id"])]})
    return doc, question


def video_question(courses, tmp_path, folder_id):
    path = tmp_path / "synthetic.mp4"
    path.write_bytes(b"synthetic video")
    video_id = courses.store.create_video("synthetic", path.name, str(path), "missing.wav", 10000)
    courses.store.set_video_folder(video_id, folder_id)
    marker = courses.store.add_marker(video_id, 1000, 2000, "合成例题", "", "", "AI-question", 1, "question_extraction")
    capture = replace_image(courses, marker, "white")
    return video_id, marker, capture


def replace_image(courses, marker, color):
    output = BytesIO()
    Image.new("RGB", (100, 60), color).save(output, "PNG")
    capture = QuestionCaptures(courses.store).add(marker, 1000, output.getvalue())
    QuestionVisualStore(courses.store).capture_binding(marker, capture["id"])
    return capture


def test_course_identity_binds_folder_and_survives_rename_delete_restart(courses):
    course = courses.create(name="数据库原理")
    assert courses.create(folder_id=course["folder_id"])["id"] == course["id"]
    child = courses.store.create_folder("第一周", course["folder_id"])
    assert courses.for_folder(child)["id"] == course["id"]
    doc, question = document_question(courses, course["id"])
    courses.store.update_folder(course["folder_id"], "数据库系统")
    assert courses.course(course["id"])["name"] == "数据库系统"
    with courses.store.connect() as db:
        db.execute("DELETE FROM video_folders WHERE id=?", (course["folder_id"],))
    restarted = StudyCourseStore(VideoStore(courses.store.path))
    assert restarted.course(course["id"])["folder_id"] is None
    assert restarted.course(course["id"])["name"] == "数据库系统"
    assert restarted.question(question["id"])["course_id"] == course["id"]
    assert StudyMaterialStore(courses.store).document(doc["id"])["course_id"] == course["id"]
    target = courses.store.create_folder("重建课程")
    assert restarted.bind(course["id"], target)["id"] == course["id"]


def test_nested_courses_have_one_nearest_owner_and_different_ids_even_same_name(courses, tmp_path):
    outer = courses.create(name="课程")
    sub = courses.store.create_folder("课程", outer["folder_id"])
    inner = courses.create(folder_id=sub)
    assert outer["id"] != inner["id"]
    _, marker, _ = video_question(courses, tmp_path, sub)
    question = courses.video_question(marker)
    assert question["course_id"] == inner["id"]
    assert courses.video_questions(outer["id"]) == []
    assert [q["id"] for q in courses.video_questions(inner["id"])] == [question["id"]]
    with pytest.raises(ValueError, match="已绑定"):
        courses.bind(outer["id"], sub)


def test_unassigned_move_and_material_course_scope_do_not_change_question_version(courses):
    a = courses.create(name="数据库")
    b = courses.create(name="算法")
    doc, question = document_question(courses)
    materials = StudyMaterialStore(courses.store)
    assert len(materials.documents(unassigned=True)) == 1
    with pytest.raises(ValueError, match="移入"):
        courses.question(question["id"])
    courses.move_document(doc["id"], a["id"])
    assert not materials.documents(unassigned=True)
    assert len(materials.documents(course_id=a["id"])) == 1
    assert not materials.documents(course_id=b["id"])
    courses.move_document(doc["id"], b["id"])
    assert courses.question(question["id"])["version"] == 1
    assert courses.course(a["id"])["question_count"] == 0
    assert courses.course(b["id"])["question_count"] == 1


def test_video_and_document_snapshots_share_course_and_only_send_question_image(courses, tmp_path):
    course = courses.create(name="数据库")
    child = courses.store.create_folder("第一讲", course["folder_id"])
    _, marker, _ = video_question(courses, tmp_path, child)
    video = courses.video_question(marker)
    _, material = document_question(courses, course["id"])
    snapshots = [study_snapshot(courses.store, "library", q["id"]) for q in [video, material]]
    assert {q["course_id"] for q in snapshots} == {course["id"]}
    assert snapshots[0]["source_kind"] == "video"
    assert [p["type"] for p in snapshots[0]["content"]] == ["image"]
    assert "字幕" not in str(snapshots[0]["content"])
    assert "(1)" in str(snapshots[1]["content"]) and "(2)" in str(snapshots[1]["content"])
    revised = replace_image(courses, marker, "red")
    same = courses.video_question(marker)
    assert same["id"] == video["id"] and same["version"] == 2
    assert courses.video_question(marker)["version"] == 2
    QuestionCaptures(courses.store).path(revised["id"]).unlink()
    assert courses.video_questions(course["id"])[0]["unavailable"]
    with pytest.raises(ValueError, match="不可用"):
        courses.question(video["id"])


def test_video_changes_require_new_discussion_and_original_receipt_stays(courses, tmp_path):
    course = courses.create(name="数据库")
    _, marker, _ = video_question(courses, tmp_path, course["folder_id"])
    question = courses.video_question(marker)
    FakeClient.requests = {}
    FakeClient.instance = "test-instance"
    FakeClient.fail_after_write = False
    bridge = DshBridge(courses.store, "library", client_factory=FakeClient)
    bridge.records.connect(tmp_path / "connection.json", FakeClient)
    bridge.configure_root(str(tmp_path), FakeClient.instance)
    first = bridge.prepare(question["id"])
    replace_image(courses, marker, "red")
    with pytest.raises(BridgeFailure) as failure:
        bridge.prepare(question["id"])
    assert failure.value.code == "question_changed"
    second = bridge.prepare(question["id"], new_discussion=True)
    assert first["session_id"] != second["session_id"]
    assert bridge.resume(first["request_id"], open_session=True)["session_id"] == first["session_id"]
    snapshot = json.loads(bridge.records.request(second["request_id"])["snapshot"])
    assert snapshot["course_id"] == course["id"] and snapshot["content_version"] == 2


def test_courses_api_requires_import_ownership_and_exposes_shared_questions(courses, tmp_path):
    with TestClient(create_app(courses.store)) as client:
        assert client.post("/api/study-documents", params={"name": "a.md"}, content=b"# Q").status_code == 422
        assert client.post("/api/study-courses", json={}).status_code == 400
        course = client.post("/api/study-courses", json={"name": "数据库"}).json()
        imported = client.post("/api/study-documents", params={"name": "a.md", "course_id": course["id"]}, content=b"# Q")
        assert imported.status_code == 201
        assert len(client.get("/api/study-documents", params={"course_id": course["id"]}).json()) == 1
        assert client.get("/api/study-documents?unassigned=true").json() == []
        _, marker, _ = video_question(courses, tmp_path, course["folder_id"])
        question = client.get(f"/api/course-questions/video/{marker}").json()
        assert client.get(f"/api/course-questions/{question['id']}").json()["id"] == question["id"]
        assert client.get(f"/api/study-courses/{course['id']}/video-questions").json()[0]["id"] == question["id"]
        assert client.get(f"/api/study-courses/for-folder/{course['folder_id']}").json()["id"] == course["id"]
        assert len(client.get("/api/study-courses/folders").json()) == 1


def test_legacy_schema_upgrade_preserves_documents_as_unassigned(tmp_path):
    with sqlite3.connect(tmp_path / "legacy.db") as db:
        db.execute("CREATE TABLE video_folders(id INTEGER PRIMARY KEY,name TEXT)")
        db.execute("""CREATE TABLE study_documents (
            id TEXT PRIMARY KEY,name TEXT,format TEXT,resource TEXT,sha256 TEXT,state TEXT,reason TEXT,
            page_count INTEGER,reading_json TEXT,deleted INTEGER,created_at TEXT)""")
        db.execute("INSERT INTO study_documents VALUES('old','old.pdf','pdf','old/original.pdf','hash','ready','',2,'{}',0,'2026-01-01')")
        initialize_study_materials(db)
        initialize_study_materials(db)
        row = db.execute("SELECT id,resource,course_id FROM study_documents").fetchone()
        assert row == ("old", "old/original.pdf", None)


def test_moving_video_changes_course_without_replacing_question_identity(courses, tmp_path):
    first = courses.create(name="数据库")
    second = courses.create(name="算法")
    video, marker, _ = video_question(courses, tmp_path, first["folder_id"])
    question = courses.video_question(marker)
    courses.store.set_video_folder(video, second["folder_id"])
    moved = courses.question(question["id"])
    assert moved["id"] == question["id"] and moved["version"] == question["version"]
    assert moved["course_id"] == second["id"]
    assert courses.video_questions(first["id"]) == []
    assert courses.video_questions(second["id"])[0]["id"] == question["id"]
    with courses.store.connect() as db:
        db.execute("UPDATE video_markers SET review_status='excluded' WHERE id=?", (marker,))
    assert courses.video_questions(second["id"]) == []
    with pytest.raises(KeyError):
        courses.question(question["id"])
