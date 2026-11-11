"""Synthetic semantic retrieval contracts; never download or invoke real models."""

import numpy as np
import pytest
from threading import Event

from classcue import semantic_embeddings as embeddings
from classcue.core import TranscriptSegment
from classcue.library_search import LibrarySearch
from classcue.processing_control import ProcessingCancelled
from classcue.semantic_index import build_index, coverage, subtitle_chunks
from tests.test_course_tools import fixture, video, call, settle


@pytest.fixture
def encoder(monkeypatch):
    calls = []
    monkeypatch.setattr(embeddings, "model_status", lambda: {
        "ready": True, "model_version": "synthetic-v1", "device": "cpu",
        "model_id": embeddings.MODEL_ID, "dimensions": embeddings.DIMENSIONS, "bytes": 0})

    def encode(texts, *, query=False, should_stop=None):
        calls.append((texts, query))
        result = np.zeros((len(texts), embeddings.DIMENSIONS), dtype="<f4")
        for index, text in enumerate(texts):
            result[index, 0 if any(word in text for word in ("冗余", "重复保存", "修改不一致")) else 1] = 1
        return result
    monkeypatch.setattr(embeddings.encoder, "encode", encode)
    return calls


def test_cleanup_runs_as_visible_job_and_preserves_course_evidence(fixture, monkeypatch, tmp_path):
    f = fixture
    root = tmp_path / "retrieval-assets"
    monkeypatch.setattr(embeddings, "active_storage_root", lambda _: root)
    monkeypatch.setattr(embeddings, "_gpu_detection", lambda: False)
    identity = video(f, "清理合成课程", text="数据库事务的原始字幕")
    with f.store.connect() as db:
        db.execute("INSERT INTO semantic_videos(video_id,source_version,model_version,chunk_count) "
                   "VALUES (?,?,?,?)", (identity, "source", "model", 1))
    directory = embeddings.model_directory()
    directory.mkdir(parents=True)
    (directory / "model.safetensors").write_bytes(b"synthetic")
    (directory / "ready.json").write_text("{}", encoding="utf-8")
    response = f.client.post("/api/semantic/cleanup")
    assert response.status_code == 202, response.text
    result = settle(f, response.json()["job_id"])
    assert result["status"] == "completed" and result["job_type"] == "semantic_cleanup"
    assert not directory.exists() and f.store.has_transcript(identity)
    with f.store.connect() as db:
        assert db.execute("SELECT count(*) FROM semantic_videos").fetchone()[0] == 1
    detail = f.client.get(f"/api/jobs/{result['job_id']}").json()["cleanup"]
    assert detail["phase"] == "completed" and detail["bytes_deleted"] == 11
    assert call(f, "list_jobs")["items"][0]["job_type"] == "semantic_cleanup"


def test_cleanup_rejects_active_or_paused_processing_before_mutation(fixture, monkeypatch):
    f = fixture
    monkeypatch.setattr(embeddings, "cleanup_model", lambda *_: pytest.fail("must not clean"))
    identity = "synthetic-paused"
    f.store.create_job(identity, "batch_video_process", None)
    f.store.update_job(identity, status="resource_paused")
    response = f.client.post("/api/semantic/cleanup")
    assert response.status_code == 409
    f.store.update_job(identity, status="running")
    assert f.client.post("/api/semantic/cleanup").status_code == 409


def test_cleanup_api_records_explicit_target_and_rejects_pause(fixture, monkeypatch, tmp_path):
    from tests.test_semantic_runtime import runtime, weights
    f = fixture
    root = tmp_path / "retrieval-assets"
    monkeypatch.setattr(embeddings, "active_storage_root", lambda _: root)
    monkeypatch.setattr(embeddings, "_gpu_detection", lambda: True)
    weights(root)
    runtime(root, "cpu")
    runtime(root, "cuda")
    response = f.client.get("/api/semantic/cache")
    assert response.status_code == 200, response.text
    assert all(row["present"] and row["ready"] for row in response.json()["runtimes"].values())
    response = f.client.post("/api/semantic/cleanup", json={"target": "cpu"})
    assert response.status_code == 202
    task = response.json()["job_id"]
    result = settle(f, task)
    assert result["status"] == "completed"
    assert "CPU" in result["message"] and "GPU" not in result["message"]
    import json
    assert json.loads(f.store.get_job(task)["request_json"])["target"] == "cpu"
    assert f.client.get(f"/api/jobs/{task}").json()["cleanup_target"] == "cpu"
    cache = f.client.get("/api/semantic/cache").json()
    assert not cache["runtimes"]["cpu"]["present"]
    assert cache["runtimes"]["cuda"]["ready"] and cache["weights"]["ready"]
    assert f.client.post(f"/api/jobs/{task}/download/pause").status_code == 409
    assert f.client.post("/api/semantic/cleanup", json={"target": "other"}).status_code == 422


def test_cleanup_cancelled_api_explains_deleted_files_do_not_return(fixture, monkeypatch):
    def cancelled(*_args, **_kwargs):
        raise ProcessingCancelled("synthetic cancellation")
    monkeypatch.setattr(embeddings, "cleanup_model", cancelled)
    response = fixture.client.post("/api/semantic/cleanup", json={"target": "cuda"})
    result = settle(fixture, response.json()["job_id"])
    assert result["status"] == "interrupted"
    assert "GPU" in result["error_message"] and "已删除文件不会恢复" in result["error_message"]
    assert "已清空" not in result["error_message"]


@pytest.mark.parametrize("device,target", [("cpu", "cuda"), ("cuda", "cpu")])
def test_cleanup_api_keeps_requested_device_and_exposes_it_in_job(fixture, monkeypatch, device, target):
    calls = []
    monkeypatch.setattr(embeddings, "cleanup_model",
                        lambda stop, **options: calls.append(options) or 123)
    response = fixture.client.post("/api/semantic/cleanup",
                                   json={"target": target, "keep_device": device})
    assert response.status_code == 202
    result = settle(fixture, response.json()["job_id"])
    assert result["status"] == "completed"
    assert calls == [{"target": target, "keep_device": device}]
    detail = fixture.client.get(f"/api/jobs/{response.json()['job_id']}").json()
    assert detail["semantic_device"] == device and detail["cleanup_target"] == target
    assert f"仅使用 {'GPU' if device == 'cuda' else 'CPU'}" in detail["message"]
    invalid = fixture.client.post("/api/semantic/cleanup",
                                  json={"target": device, "keep_device": device})
    assert invalid.status_code == 422
    assert len(calls) == 1


def test_semantic_candidate_preserves_original_quote_and_literal_search_stays_independent(fixture, encoder):
    f = fixture
    relevant = video(f, "合成讲解", text="规范化用于减少冗余，避免更新异常。")
    video(f, "其他讲解", text="今天学习二叉树的遍历。")
    assert call(f, "search_library", query="修改不一致", phonetic_mode="off")["total"] == 0
    assert encoder == []
    build_index(f.store, [relevant])
    result = call(f, "search_library", semantic_query="重复保存信息导致修改不一致", sources=["subtitles"])
    first = result["items"][0]
    assert first["video_id"] == relevant
    assert first["evidence"][0]["text"] == "规范化用于减少冗余，避免更新异常。"
    assert first["match_type"] == "semantic" and first["matched_terms"] == []
    assert first["position_seconds"] == 10
    assert first["context_range"] == {"start_seconds": 0, "end_seconds": 35}
    assert not result["search_complete"] and not result["total_is_exact"]
    assert result["semantic_coverage"]["pending_videos"] == 1


def test_index_reuses_vectors_and_source_edits_invalidate_the_whole_video(fixture, encoder):
    identity = video(fixture, "索引复用", text="减少冗余。")
    first = build_index(fixture.store, [identity])
    count = len(encoder)
    assert first["completed"] == 1
    assert build_index(fixture.store, [identity])["skipped"] == 1 and len(encoder) == count
    with fixture.store.connect() as db:
        db.execute("UPDATE video_transcript_segments SET text='新的原文' WHERE video_id=?", (identity,))
    with fixture.store.connect() as db:
        assert coverage(db, [identity])["pending_videos"] == 1
        assert db.execute("SELECT COUNT(*) FROM semantic_chunks").fetchone()[0] == 0
    build_index(fixture.store, [identity])
    assert len(encoder) == count + 1


def test_semantic_evidence_keeps_substantive_sentences_among_short_asr_fillers(fixture, encoder):
    identity = video(fixture, "短句与内容")
    rows = [TranscriptSegment(i, i * 1000, i * 1000 + 800, "那") for i in range(1, 12)]
    rows.append(TranscriptSegment(12, 12000, 15000, "规范化可以减少冗余，避免修改时出现更新异常。"))
    fixture.store.mark_transcribed(identity, rows)
    build_index(fixture.store, [identity])
    result = call(fixture, "search_library", semantic_query="重复保存", sources=["subtitles"])
    assert any(e["record_id"] == 12 for e in result["items"][0]["evidence"])


def test_semantic_candidate_limit_never_claims_exhaustive_search(fixture, encoder):
    identity = video(fixture, "超过候选上限")
    fixture.store.mark_transcribed(identity, [
        TranscriptSegment(index + 1, index * 20000, index * 20000 + 1000, "规范化减少冗余")
        for index in range(101)
    ])
    with fixture.store.connect() as db:
        db.execute("UPDATE videos SET duration_ms=2100000 WHERE id=?", (identity,))
    build_index(fixture.store, [identity])
    result = call(fixture, "search_library", semantic_query="重复保存", video_id=identity,
                  sources=["subtitles"])
    assert result["semantic_coverage"]["pending_videos"] == 0
    assert result["semantic_candidates_truncated"]
    assert result["total"] == 100 and result["has_more"]
    assert not result["search_complete"] and not result["total_is_exact"]


def test_reassigning_subtitle_invalidates_both_video_indexes(fixture, encoder):
    one = video(fixture, "原视频", text="减少冗余")
    two = video(fixture, "新视频", text="二叉树")
    with fixture.store.connect() as db:
        db.execute("UPDATE video_transcript_segments SET segment_id=2 WHERE video_id=?", (two,))
    build_index(fixture.store, [one, two])
    with fixture.store.connect() as db:
        db.execute("UPDATE video_transcript_segments SET video_id=? WHERE video_id=?", (two, one))
        assert db.execute("SELECT COUNT(*) FROM semantic_videos").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM semantic_chunks").fetchone()[0] == 0


def test_semantic_scope_time_and_exclusions_are_hard_filters(fixture, encoder):
    f = fixture
    folder = f.store.create_folder("限定范围")
    child = f.store.create_folder("子目录", folder)
    inside = video(f, "范围内", child, "冗余导致更新异常。")
    outside = video(f, "范围外", text="冗余导致更新异常。")
    build_index(f.store, [inside, outside])
    args = {"semantic_query": "重复保存", "course_id": f"folder:{folder}", "sources": ["subtitles"]}
    assert {r["video_id"] for r in call(f, "search_library", **args)["items"]} == {inside}
    assert call(f, "search_library", **args, video_id=outside)["items"] == []
    assert call(f, "search_library", **args, start_seconds=30, end_seconds=40)["items"] == []
    assert call(f, "search_library", **args, exclude_terms=["更新异常"])["items"] == []
    with f.store.connect() as db:
        db.execute("UPDATE videos SET is_secondary=1 WHERE id=?", (inside,))
    assert call(f, "search_library", **args)["items"] == []


def test_long_sentences_are_fully_indexed_without_inventing_char_timestamps():
    text = "甲" * 1300
    chunks = subtitle_chunks([{"segment_id": 7, "start_ms": 1000, "end_ms": 9000, "text": text}])
    assert "".join(c["text"] for c in chunks) == text
    assert all(len(c["text"]) <= 500 and (c["start_ms"], c["end_ms"]) == (1000, 9000) for c in chunks)
    assert [(c["references"][0]["start_char"], c["references"][0]["end_char"]) for c in chunks] == [
        (0, 500), (500, 1000), (1000, 1300)]


def test_time_range_inside_one_chunk_returns_only_overlapping_original_sentences(fixture, encoder):
    identity = video(fixture, "块内限定时间")
    fixture.store.mark_transcribed(identity, [
        TranscriptSegment(index + 1, index * 10000, (index + 1) * 10000, f"规范化第{index + 1}句减少冗余。")
        for index in range(8)
    ])
    build_index(fixture.store, [identity])
    result = call(fixture, "search_library", semantic_query="重复保存", video_id=identity,
                  sources=["subtitles"], start_seconds=70, end_seconds=75)
    first = result["items"][0]
    assert first["position_seconds"] == first["start_seconds"] == 70
    assert first["end_seconds"] == 80
    assert [item["record_id"] for item in first["evidence"]] == [8]
    assert first["evidence"][0]["text"] == "规范化第8句减少冗余。"


def test_time_range_exclusions_ignore_sentences_outside_requested_window(fixture, encoder):
    identity = video(fixture, "块内排除词")
    fixture.store.mark_transcribed(identity, [
        TranscriptSegment(index + 1, index * 10000, (index + 1) * 10000,
                          "苹果是例子。" if index == 0 else "规范化减少冗余。")
        for index in range(8)
    ])
    build_index(fixture.store, [identity])
    args = {"semantic_query": "重复保存", "video_id": identity, "sources": ["subtitles"]}
    assert call(fixture, "search_library", **args, exclude_terms=["苹果"])["items"] == []
    result = call(fixture, "search_library", **args, exclude_terms=["苹果"],
                  start_seconds=70, end_seconds=75)
    assert [item["record_id"] for item in result["items"][0]["evidence"]] == [8]
    assert result["items"][0]["position_seconds"] == 70
    assert call(fixture, "search_library", **args, exclude_terms=["冗余"],
                start_seconds=70, end_seconds=75)["items"] == []


def test_cancellation_keeps_completed_videos_and_never_publishes_partial_vectors(fixture, encoder):
    one = video(fixture, "已完成", text="减少冗余")
    two = video(fixture, "待取消", text="二叉树")
    build_index(fixture.store, [one])
    with pytest.raises(ProcessingCancelled):
        build_index(fixture.store, [two], should_stop=lambda: True)
    with fixture.store.connect() as db:
        assert coverage(db, [one, two])["indexed_videos"] == 1


def test_concurrent_source_change_rejects_stale_publication(fixture, encoder, monkeypatch):
    identity = video(fixture, "并发修改", text="减少冗余")
    original = embeddings.encoder.encode
    def changing(*args, **kwargs):
        result = original(*args, **kwargs)
        with fixture.store.connect() as db:
            db.execute("UPDATE video_transcript_segments SET text='修改后字幕' WHERE video_id=?", (identity,))
        return result
    monkeypatch.setattr(embeddings.encoder, "encode", changing)
    with pytest.raises(ValueError, match="字幕或检索模型已变化"):
        build_index(fixture.store, [identity])
    with fixture.store.connect() as db:
        assert coverage(db, [identity])["indexed_videos"] == 0


def test_hybrid_fuses_votes_and_snapshot_rejects_changed_index(fixture, encoder):
    identity = video(fixture, "混合检索", text="规范化减少冗余")
    build_index(fixture.store, [identity])
    args = {"query": "规范化", "semantic_query": "重复保存", "phonetic_mode": "off"}
    result = call(fixture, "search_library", **args, limit=1)
    assert result["items"][0]["retrieval_methods"] == ["literal", "semantic"]
    assert result["items"][0]["match_type"] == "mixed"
    assert result["items"][0]["score"] == pytest.approx(2 / 61)
    with fixture.store.connect() as db:
        db.execute("DELETE FROM semantic_chunks")
        db.execute("DELETE FROM semantic_videos")
    with pytest.raises(ValueError, match="资料或参数已变化"):
        call(fixture, "search_library", **args, offset=1, snapshot_id=result["snapshot_id"])


@pytest.mark.parametrize("args", [
    {"semantic_query": " "}, {"semantic_query": "a\0b"},
    {"semantic_query": "a", "sources": ["bookmarks"]},
    {"semantic_query": "a", "query": "b", "queries": ["c"]},
])
def test_semantic_arguments_preserve_literal_validation(args):
    with pytest.raises(ValueError):
        LibrarySearch.model_validate(args)


def test_frontend_api_status_and_index_job_use_existing_queue(fixture, encoder):
    f = fixture
    folder = f.store.create_folder("测试课程")
    identity = video(f, "测试讲解", folder, "减少冗余")
    target = {"course_id": f"folder:{folder}"}
    status = f.client.get("/api/semantic/status", params=target).json()
    assert status["ready"] and status["coverage"]["pending_videos"] == 1
    response = f.client.post("/api/semantic/index", json=target)
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    f.app.state.jobs.executor.submit(lambda: None).result(timeout=30)
    result = f.client.get(f"/api/jobs/{job_id}").json()
    assert result["status"] == "completed"
    assert f.client.get("/api/semantic/status", params=target).json()["coverage"]["indexed_videos"] == 1
    assert call(f, "get_job", job_id=job_id)["job_type"] == "semantic_index"
    assert f.client.post("/api/semantic/index", json={}).status_code == 422
    with f.store.connect() as db:
        assert coverage(db, [identity])["chunk_count"] == 1


def test_setup_prepares_then_indexes_only_requested_scope_and_reuses_request(fixture, encoder, monkeypatch):
    f = fixture
    folder = f.store.create_folder("尚未准备的课程")
    identity = video(f, "课程内", folder, "规范化减少冗余")
    outside = video(f, "课程外", text="规范化减少冗余")
    original_status = embeddings.model_status
    state = {"ready": False, "device": "cuda"}
    monkeypatch.setattr(embeddings, "model_status", lambda: {**original_status(), **state})
    prepared = []

    def prepare(device, progress, stop):
        prepared.append(device)
        assert not encoder
        progress(.9, "合成准备", "preparing")
        state["ready"] = True
    monkeypatch.setattr(embeddings, "prepare_model", prepare)
    args = {"course_id": f"folder:{folder}", "request_id": "semantic-setup-one"}
    task = call(f, "start_semantic_setup", **args)
    result = settle(f, task["job_id"])
    assert result["status"] == "completed" and result["summary"]["completed"] == 1
    assert prepared == ["cuda"]
    assert f.client.get(f"/api/jobs/{task['job_id']}").json()["semantic_device"] == "cuda"
    repeated = call(f, "start_semantic_setup", **args)
    assert repeated["job_id"] == task["job_id"] and repeated["reused"]
    assert prepared == ["cuda"]
    with f.store.connect() as db:
        assert coverage(db, [identity])["indexed_videos"] == 1
        assert coverage(db, [outside])["indexed_videos"] == 0
    assert call(f, "list_jobs", video_id=identity)["items"][0]["job_type"] == "semantic_setup"
    assert call(f, "list_jobs", video_id=outside)["items"] == []
    found = call(f, "search_library", semantic_query="重复保存", course_id=f"folder:{folder}")
    assert found["items"][0]["video_id"] == identity
    with pytest.raises(ValueError, match="request_id 已用于"):
        call(f, "start_semantic_setup", **{**args, "device": "cpu"})
    with pytest.raises(ValueError, match="request_id 已用于"):
        call(f, "start_semantic_prepare", request_id=args["request_id"])


@pytest.mark.parametrize("cancelled", [False, True])
def test_failed_or_cancelled_preparation_never_indexes(fixture, encoder, monkeypatch, cancelled):
    identity = video(fixture, "未索引", text="减少冗余")

    def fail(*args):
        if cancelled:
            raise ProcessingCancelled("合成停止")
        raise RuntimeError("合成准备失败")
    monkeypatch.setattr(embeddings, "prepare_model", fail)
    task = call(fixture, "start_semantic_setup", video_id=identity, device="cpu",
                request_id=f"failed-setup-{cancelled}")
    result = settle(fixture, task["job_id"])
    assert result["status"] == ("interrupted" if cancelled else "failed")
    assert encoder == []
    with fixture.store.connect() as db:
        assert coverage(db, [identity])["indexed_videos"] == 0


def test_api_prepare_and_setup_preserve_device_and_accept_idempotent_retries(fixture, encoder, monkeypatch):
    f = fixture
    prepared = []
    monkeypatch.setattr(embeddings, "prepare_model", lambda device, *_: prepared.append(device))
    response = f.client.post("/api/semantic/prepare", json={"device": "cuda"})
    assert response.status_code == 202
    assert settle(f, response.json()["job_id"])["status"] == "completed"
    assert prepared == ["cuda"]
    response = f.client.post("/api/semantic/prepare", json={})
    assert response.status_code == 202
    settle(f, response.json()["job_id"])
    assert prepared == ["cuda", "cpu"]
    identity = video(f, "新课程", text="减少冗余")
    payload = {"video_id": identity, "request_id": "api-semantic-setup"}
    response = f.client.post("/api/semantic/setup", json=payload)
    assert response.status_code == 202
    assert settle(f, response.json()["job_id"])["status"] == "completed"
    repeated = f.client.post("/api/semantic/setup", json=payload).json()
    assert repeated["job_id"] == response.json()["job_id"] and repeated["reused"]
    assert f.client.post("/api/semantic/setup", json={}).status_code == 422
    assert f.client.post("/api/semantic/setup", json={**payload, "device": "gpu"}).status_code == 422


def test_setup_rejects_empty_scope_before_installation(fixture, encoder, monkeypatch):
    prepared = []
    monkeypatch.setattr(embeddings, "prepare_model", lambda *_: prepared.append(True))
    with pytest.raises(ValueError, match="没有视频"):
        call(fixture, "start_semantic_setup", course_id="root", request_id="empty-semantic-scope")
    with pytest.raises(ValueError, match="明确指定"):
        call(fixture, "start_semantic_setup", request_id="missing-semantic-scope")
    assert not prepared


def test_setup_is_discoverable_and_cancel_between_prepare_and_index_preserves_prior_work(
        fixture, encoder, monkeypatch):
    f = fixture
    complete = video(f, "已有索引", text="减少冗余")
    pending = video(f, "尚未索引", text="减少冗余")
    build_index(f.store, [complete])
    encoded_before = len(encoder)
    entered, release = Event(), Event()

    def prepare(*_):
        entered.set()
        assert release.wait(15)
    monkeypatch.setattr(embeddings, "prepare_model", prepare)
    task = call(f, "start_semantic_setup", course_id="root", request_id="cancellable-semantic-setup")
    try:
        assert entered.wait(15)
        status = call(f, "get_semantic_status", video_id=pending)
        assert status["busy"] and status["active_job_id"] == task["job_id"]
        active = call(f, "list_jobs", status="active", video_id=pending)
        assert active["items"][0]["job_id"] == task["job_id"]
        call(f, "cancel_job", job_id=task["job_id"])
    finally:
        release.set()
    assert settle(f, task["job_id"])["status"] == "interrupted"
    assert len(encoder) == encoded_before
    with f.store.connect() as db:
        assert coverage(db, [complete])["indexed_videos"] == 1
        assert coverage(db, [pending])["indexed_videos"] == 0
