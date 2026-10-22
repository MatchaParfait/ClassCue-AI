from copy import deepcopy
import json
import sqlite3

from fastapi.testclient import TestClient
import pytest

import api.main as api_main
from classcue.core import TranscriptSegment
import classcue.study_share as sharing
from classcue.video_store import VideoStore


def video(store, root, content=b"same video content", name="lesson.mp4"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return store.create_video(store.file_hash(path), name, str(path), "local-audio.wav", 10_000,
                              source_size=len(content), source_mtime_ns=path.stat().st_mtime_ns)


@pytest.fixture
def shared(tmp_path, monkeypatch):
    monkeypatch.setattr(api_main, "DIRECTORY_PROJECT_ROOT", tmp_path / "app")
    monkeypatch.setattr(api_main, "process_video_once", lambda *a, **kw: pytest.fail("No ASR or AI allowed"))
    source = VideoStore(tmp_path / "sender" / "db.sqlite")
    sid = video(source, tmp_path / "sender")
    source.mark_transcribed(sid, [
        TranscriptSegment(1, 0, 2000, "这里是考试重点", [
            {"word": "这里", "start": 0.0, "end": 0.8, "probability": 0.95},
        ]),
        TranscriptSegment(2, 2500, 5000, "这是第二句字幕"),
    ], "firered-aed")
    source.set_analysis_run(sid, "medium_recall", "completed", 2)
    source.set_analysis_run(sid, "low_recall", "completed", 0)
    mid = source.add_marker(sid, 0, 2000, "考试重点", "这是说明", "这里是考试重点", "重点", .9, "medium_recall")
    source.set_marker_importance(mid, "critical")
    source.set_marker_jump_time(mid, 1200)
    source.set_marker_review_status(mid, "mastered")
    excluded = source.add_marker(sid, 2500, 5000, "不分享的已移除考点", "", "第二句", "会考", .5, "medium_recall")
    source.set_marker_review_status(excluded, "excluded")
    scheme = source.save_recall_scheme({
        "name": "老师方案", "base_mode": "high_recall",
        "rules": [{"phrase": "秘密教师表达", "meaning": "emphasis"}],
        "notes": "私人提示词", "advanced_prompt": "do not share secret prompt",
    })
    source.set_analysis_run(sid, scheme["id"], "completed", 1)
    source.add_marker(sid, 2500, 5000, "自定义考点", "", "这是第二句字幕", "重点", 1., scheme["id"])
    source.set_status(sid, "review_ready")
    target = VideoStore(tmp_path / "receiver" / "db.sqlite")
    tid = video(target, tmp_path / "receiver", name="renamed.mp4")
    application = api_main.create_app(target)
    client = TestClient(application)
    yield source, sid, target, tid, client, application
    application.state.jobs.shutdown(wait=True)


def exported(shared):
    source, sid, *_ = shared
    return sharing.export_package(source, sid)


def preview(client, content):
    return client.post("/api/study-shares/preview", content=content)


def commit(client, content, plan):
    return client.post("/api/study-shares/import", content=content, params={
        key: plan[key] for key in ("package_hash", "video_id", "library_id")
    })


def test_cross_library_roundtrip_preserves_results_and_skips_processing(shared):
    source, sid, target, tid, client, _ = shared
    content = exported(shared)
    plan = preview(client, content).json()
    assert plan["can_import"] and not plan["already_imported"]
    assert plan["video_name"] == "renamed"
    assert plan["subtitle_count"] == 2 and plan["marker_count"] == 2
    before = dict(target.get_video(tid))
    result = commit(client, content, plan)
    assert result.status_code == 200, result.text
    assert result.json()["video"]["status"] == "review_ready"
    current = dict(target.get_video(tid))
    for key in ("id", "video_path", "audio_path", "display_name", "library_folder_id", "source_folder"):
        assert current[key] == before[key]
    assert target.get_segments(tid) == source.get_segments(sid)
    markers = target.list_markers(tid)
    assert len(markers) == 2
    assert markers[0]["importance"] == "critical" and markers[0]["user_start_ms"] == 1200
    assert all(m["review_status"] == "active" for m in markers)
    assert target.get_analysis_run(tid, "low_recall")["status"] == "completed"
    assert target.list_active_jobs() == []
    assert len(client.get(f"/api/videos/{tid}/subtitles").json()) == 2
    search = client.get("/api/library/search", params={"q": "考试"}).json()
    assert search["total"] == 1 and search["items"][0]["video_id"] == tid
    with target.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM processing_jobs").fetchone()[0] == 0
    assert commit(client, content, plan).status_code == 200
    assert len(target.list_markers(tid)) == 2
    assert len(target.list_recall_schemes()) == 1
    assert preview(client, content).json()["already_imported"]


def test_export_is_allowlisted_and_does_not_include_private_state(shared):
    content = exported(shared).decode("utf-8")
    for private in ("video_path", "source_folder", "audio_path", "api_key", "review_status",
                    "不分享的已移除考点", "秘密教师表达", "私人提示词", "secret prompt", "experiment_id"):
        assert private not in content
    assert json.loads(content)["markers"][0]["user_start_ms"] == 1200


def test_custom_modes_are_archived_independent_labels_not_overwritten_configuration(shared):
    _, _, target, tid, client, _ = shared
    own = target.save_recall_scheme({"name": "老师方案", "base_mode": "low_recall", "rules": [], "notes": "own"})
    original = deepcopy(target.get_recall_scheme(own["id"]))
    content = exported(shared)
    plan = preview(client, content).json()
    assert commit(client, content, plan).status_code == 200
    assert target.get_recall_scheme(own["id"]) == original
    run = next(r for r in client.get(f"/api/videos/{tid}/analysis-runs").json()
               if r["analysis_mode"].startswith("custom_"))
    assert run["scheme_name"] == "老师方案" and run["archived"]
    assert run["analysis_mode"] != own["id"]
    assert run["scheme_revision"] == run["latest_scheme_revision"]
    assert sharing.decode_package(sharing.export_package(target, tid)).markers


@pytest.mark.parametrize("kind", ["transcript", "marker", "run", "status"])
def test_existing_results_are_not_overwritten(shared, kind):
    _, _, target, tid, client, _ = shared
    if kind == "transcript":
        target.mark_transcribed(tid, [TranscriptSegment(5, 0, 1000, "我的字幕")])
    elif kind == "marker":
        target.add_marker(tid, 0, 1000, "我的考点", "", "", "重点", 1.)
    elif kind == "run":
        target.set_analysis_run(tid, "medium_recall", "completed", 0)
    else:
        target.set_status(tid, "review_ready")
    content = exported(shared)
    plan = preview(client, content).json()
    assert not plan["can_import"] and plan["block_reason"]
    assert commit(client, content, plan).status_code == 409
    assert len(target.list_recall_schemes()) == 0


def test_rechecks_existing_results_file_bytes_library_and_package_after_preview(shared):
    _, _, target, tid, client, _ = shared
    content = exported(shared)
    plan = preview(client, content).json()
    assert commit(client, content, {**plan, "library_id": "another-library"}).status_code == 409
    assert commit(client, content, {**plan, "package_hash": "0" * 64}).status_code == 409
    assert commit(client, content, {**plan, "video_id": tid + 1}).status_code == 409
    path = target.get_video(tid)["video_path"]
    from pathlib import Path
    Path(path).write_bytes(b"changed video bytes")
    assert commit(client, content, plan).status_code == 409
    assert preview(client, content).status_code == 409
    assert target.get_segments(tid) == []


def test_no_match_and_same_name_different_bytes_are_rejected(shared, tmp_path):
    _, _, target, tid, client, _ = shared
    with target.connect() as connection:
        connection.execute("UPDATE videos SET content_hash = ? WHERE id = ?", ("f" * 64, tid))
    assert preview(client, exported(shared)).status_code == 404
    assert target.get_segments(tid) == []


@pytest.mark.parametrize("status", ["queued", "running", "resource_paused"])
@pytest.mark.parametrize("kind", ["video", "batch-id", "batch-path", "batch-hash"])
def test_occupied_video_is_protected_including_entire_batch_queue(shared, status, kind):
    _, _, target, tid, client, _ = shared
    content = exported(shared)
    plan = preview(client, content).json()
    target.create_job("occupied", "video_process" if kind == "video" else "batch_video_process",
                      tid if kind == "video" else None)
    entry = {"video_id": tid} if kind == "batch-id" else (
        {"source_path": target.get_video(tid)["video_path"]} if kind == "batch-path" else
        {"content_hash": target.get_video(tid)["content_hash"]})
    target.update_job("occupied", status=status, request_json=json.dumps({"entries": [entry]}))
    assert not preview(client, content).json()["can_import"]
    assert commit(client, content, plan).status_code == 409
    assert target.get_segments(tid) == []
    target.update_job("occupied", status="interrupted")
    assert commit(client, content, plan).status_code == 200


def test_other_video_job_does_not_block_import_and_migration_does(shared):
    _, _, target, tid, client, application = shared
    content = exported(shared)
    plan = preview(client, content).json()
    application.state.storage_migrating.set()
    assert commit(client, content, plan).status_code == 409
    application.state.storage_migrating.clear()
    target.create_job("other", "video_process", None)
    assert commit(client, content, plan).status_code == 200
    target.update_job("other", status="interrupted")


def test_commit_rolls_back_all_tables_on_database_failure(shared):
    _, _, target, tid, client, _ = shared
    package = sharing.decode_package(exported(shared))
    with target.connect() as connection:
        connection.execute("""CREATE TRIGGER fail_share BEFORE INSERT ON video_markers
            BEGIN SELECT RAISE(ABORT, 'fixture failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        sharing.import_package(target, package, expected_hash=sharing.package_digest(package), expected_video_id=tid)
    assert target.get_segments(tid) == []
    assert target.list_recall_schemes() == []
    assert target.get_analysis_run(tid, "low_recall") is None
    assert target.get_video(tid)["status"] == "uploaded"
    with target.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM video_share_imports").fetchone()[0] == 0


@pytest.mark.parametrize("change", [
    lambda p: p.update(version=2),
    lambda p: p.update(version=True),
    lambda p: p.update(video_path="C:/private"),
    lambda p: p["video"].update(sha256="invalid"),
    lambda p: p["subtitles"][0].update(start_ms=-1),
    lambda p: p["subtitles"][0].update(end_ms=20_000),
    lambda p: p["subtitles"][0].update(start_ms=1, end_ms=0),
    lambda p: p["subtitles"].append(p["subtitles"][0]),
    lambda p: p["analysis_runs"].append(p["analysis_runs"][0]),
    lambda p: p["markers"].append(p["markers"][0]),
    lambda p: p["markers"][0].update(analysis_mode="custom_missing"),
    lambda p: p["markers"][0].update(user_start_ms=10_001),
    lambda p: p["markers"][0].update(confidence=float("nan")),
    lambda p: p["markers"][0].update(importance="execute"),
    lambda p: p["subtitles"][0]["words"][0].update(end=11),
])
def test_invalid_packages_never_write_data(shared, change):
    _, _, target, tid, client, _ = shared
    payload = json.loads(exported(shared))
    change(payload)
    response = preview(client, json.dumps(payload, ensure_ascii=False).encode())
    assert response.status_code == 400, response.text
    assert target.get_segments(tid) == [] and target.list_recall_schemes() == []


def test_truncated_empty_and_oversized_files_are_rejected(shared, monkeypatch):
    *_, client, _ = shared
    for data in (b"", b"{", b"not a share file"):
        assert preview(client, data).status_code == 400
    monkeypatch.setattr(sharing, "MAX_SHARE_BYTES", 10)
    assert preview(client, b"01234567890").status_code == 413


def test_export_api_blocks_incomplete_missing_and_occupied_video(shared):
    source, sid, *_ = shared
    application = api_main.create_app(source)
    try:
        client = TestClient(application)
        response = client.get(f"/api/videos/{sid}/study-share")
        assert response.status_code == 200
        assert ".studyshare" in response.headers["content-disposition"]
        assert response.headers["cache-control"] == "no-store"
        source.create_job("busy", "video_process", sid)
        assert client.get(f"/api/videos/{sid}/study-share").status_code == 409
        source.update_job("busy", status="interrupted")
        source.set_status(sid, "transcribed")
        assert client.get(f"/api/videos/{sid}/study-share").status_code == 409
        assert client.get("/api/videos/999/study-share").status_code == 404
    finally:
        application.state.jobs.shutdown(wait=True)


def test_existing_asr_rounding_and_end_of_video_jump_are_shareable(shared):
    source, sid, target, tid, client, _ = shared
    source.mark_transcribed(sid, [
        TranscriptSegment(1, 9000, 10_001, "保留舍入时间", [{"word": "时间", "start": 9., "end": 10.001}]),
        TranscriptSegment(2, 10_000, 10_000, "零时长字幕"),
    ])
    marker = source.list_markers(sid)[0]
    source.set_marker_jump_time(marker["id"], 10_000)
    source.set_status(sid, "review_ready")
    content = exported(shared)
    plan = preview(client, content).json()
    assert commit(client, content, plan).status_code == 200
    assert target.get_segments(tid) == source.get_segments(sid)
    assert target.list_markers(tid)[0]["user_start_ms"] == 10_000


def test_import_receipt_survives_recycle_restore_and_is_cleared_by_retranscription(shared, tmp_path):
    _, _, target, tid, client, _ = shared
    content = exported(shared)
    plan = preview(client, content).json()
    assert commit(client, content, plan).status_code == 200
    trash = tmp_path / "trash"
    target.trash_video(tid, trash)
    assert preview(client, content).status_code == 404
    target.undo_latest_recycle(trash)
    assert preview(client, content).json()["already_imported"]
    target.replace_transcript_for_reanalysis(tid, [TranscriptSegment(1, 0, 1000, "我的新字幕")], "new-engine")
    rechecked = preview(client, content).json()
    assert not rechecked["already_imported"] and not rechecked["can_import"]
    assert commit(client, content, plan).status_code == 409
    assert target.get_segments(tid)[0].text == "我的新字幕"
