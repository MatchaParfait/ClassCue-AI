import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from ai_provider import ProviderProfile
from api_failure import ApiUnavailable, api_failure
from core import TranscriptSegment
from processing_control import ProcessingCancelled, ResourcePaused
import video_pipeline
from video_library import register_managed_video
from video_store import VideoStore
from web_batch import make_batch, preview_directories


@pytest.fixture
def batch(tmp_path, monkeypatch):
    store = VideoStore(tmp_path / "workspace" / "project.db")
    profile = ProviderProfile(base_url="https://unused.example/v1", model="fake", max_retries=0)
    settings = SimpleNamespace(active_profile=profile)
    monkeypatch.setattr(api_main, "DIRECTORY_PROJECT_ROOT", tmp_path / "application")
    monkeypatch.setattr(api_main, "selected_model_id", lambda: "whisper-turbo")
    monkeypatch.setattr(api_main, "require_selectable_model", lambda _: None)
    monkeypatch.setattr(api_main, "load_provider_settings", lambda: settings)
    monkeypatch.setattr(api_main, "resolve_interface_key", lambda *_: "fixture-key")
    monkeypatch.setattr(api_main, "interface_provider", lambda _: "openai_compatible")
    monkeypatch.setattr(video_pipeline, "video_duration_ms", lambda _: 1000)
    asr_calls, ai_calls, ai_modes = [], [], []

    def transcribe(path, *_args, **_kwargs):
        asr_calls.append(Path(path).name)
        return [TranscriptSegment(1, 0, 1000, "会考，必须掌握")], 1000

    monkeypatch.setattr(video_pipeline, "_transcribe_video_with_fallback", transcribe)
    outcomes = []
    keys = []

    class Judge:
        def __init__(self, mode):
            self.mode = mode

        def analyze_video(self, current_store, video_id, **_kwargs):
            ai_calls.append(video_id)
            ai_modes.append(self.mode)
            outcome = outcomes.pop(0) if outcomes else None
            if isinstance(outcome, BaseException):
                raise outcome
            current_store.set_analysis_run(video_id, self.mode, "completed", 0)
            return "completed", 0

        def close(self):
            pass

    def create(_profile, key, *_args, **_kwargs):
        keys.append(key)
        return Judge(_kwargs.get("mode", "medium_recall"))

    monkeypatch.setattr(api_main, "create_judge", create)
    application = api_main.create_app(store)
    with TestClient(application) as client:
        yield SimpleNamespace(
            store=store, client=client, root=tmp_path, outcomes=outcomes,
            asr=asr_calls, ai=ai_calls, modes=ai_modes, keys=keys, application=application,
        )


def folder(batch, name="course", count=2):
    root = batch.root / name
    root.mkdir(parents=True)
    for index in range(count):
        (root / f"lesson-{index}.mp4").write_bytes(f"{name}-{index}".encode())
    return root


def wait(batch, job_id):
    batch.application.state.jobs.executor.submit(lambda: None).result(timeout=10)
    return batch.client.get(f"/api/jobs/{job_id}").json()


def start(batch, root):
    response = batch.client.post("/api/batch/process", json={"folders": [str(root)]})
    assert response.status_code == 202, response.text
    return wait(batch, response.json()["job_id"])


@pytest.mark.parametrize("extra", ["notes.txt", ".hidden", "nested/notes.txt", "nested/deeper/image.png"])
def test_strict_preview_and_process_have_zero_side_effects(batch, extra):
    root = folder(batch)
    other = folder(batch, "valid")
    (root / extra).parent.mkdir(parents=True, exist_ok=True)
    (root / extra).write_bytes(b"non video")
    response = batch.client.post("/api/batch/preview", json={"folders": [str(root), str(other)]})
    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert str(Path(extra)) in response.json()["folders"][0]["examples"]
    assert len(list(root.glob("*.mp4"))) == 2
    assert batch.client.post("/api/batch/process", json={"folders": [str(other), str(root)]}).status_code == 400
    assert batch.store.list_videos() == []
    assert batch.store.list_folders() == []
    assert batch.store.latest_batch_job() is None
    assert len(list(other.glob("*.mp4"))) == 2


def test_pure_video_preview_deduplicates_directories_and_handles_empty(batch):
    root = folder(batch)
    result = preview_directories([str(root), str(root)])
    assert result["valid"] and len(result["folders"]) == 1
    assert result["folders"][0]["video_count"] == 2
    empty = batch.root / "empty"
    empty.mkdir()
    assert not preview_directories([str(empty)])["valid"]


def test_nested_import_preserves_tree_empty_folders_and_video_ownership(batch):
    root = folder(batch, "nested-course", 1)
    child = folder(batch, "nested-course/chapter", 1)
    deep = folder(batch, "nested-course/chapter/lesson", 1)
    (root / "empty" / "also-empty").mkdir(parents=True)
    preview = batch.client.post("/api/batch/preview", json={"folders": [str(root)]}).json()
    assert preview["valid"]
    assert preview["folders"][0]["video_count"] == 3
    assert preview["folders"][0]["subfolder_count"] == 4
    assert "directories" not in preview["folders"][0]
    job = start(batch, root)
    assert job["status"] == "completed"
    assert job["batch_summary"]["completed"] == 3
    top, = batch.store.list_folders()
    children = {row["name"]: row for row in batch.store.list_folders(top["id"])}
    assert set(children) == {"chapter", "empty"}
    leaf, = batch.store.list_folders(children["chapter"]["id"])
    empty_leaf, = batch.store.list_folders(children["empty"]["id"])
    assert empty_leaf["name"] == "also-empty"
    for source, logical in [(root, top), (child, children["chapter"]), (deep, leaf)]:
        video, = batch.store.list_folder_videos(logical["id"])
        assert Path(video["source_folder"]) == source
    response = batch.client.get(f'/api/library?folder_id={leaf["id"]}').json()
    assert [row["name"] for row in response["ancestors"]] == ["nested-course", "chapter", "lesson"]
    assert response["folder"]["parent_id"] == children["chapter"]["id"]


def test_import_only_registers_videos_without_running_asr_or_ai(batch):
    root = folder(batch, count=2)
    response = batch.client.post("/api/batch/process", json={
        "folders": [str(root)],
        "import_only": True,
    })
    assert response.status_code == 202, response.text
    job = wait(batch, response.json()["job_id"])
    assert job["status"] == "completed"
    assert job["import_only"] is True
    assert job["message"] == "已导入 2 个视频，尚未开始处理"
    assert job["batch_summary"]["completed"] == 0
    assert job["batch_summary"]["pending_asr"] == 2
    assert batch.asr == []
    assert batch.ai == []
    assert len(batch.store.list_videos()) == 2
    assert batch.client.post(f'/api/jobs/{job["id"]}/retry').status_code == 409


def test_overlapping_source_selections_import_tree_only_once(batch):
    root = folder(batch, "overlapping", 1)
    child = folder(batch, "overlapping/child", 1)
    for paths in [[str(child), str(root), str(root)], [str(root), str(child)]]:
        preview = preview_directories(paths)
        assert preview["valid"] and len(preview["folders"]) == 1
        assert len(make_batch(preview, {})["entries"]) == 2
    response = batch.client.post("/api/batch/process", json={"folders": [str(child), str(root)]})
    assert response.status_code == 202, response.text
    assert wait(batch, response.json()["job_id"])["batch_summary"]["completed"] == 2
    top, = batch.store.list_folders()
    nested, = batch.store.list_folders(top["id"])
    assert nested["name"] == "child"


def test_reimport_reuses_nested_folders_and_keeps_user_names(batch):
    root = folder(batch, "reimport-tree", 0)
    child = folder(batch, "reimport-tree/chapter", 1)
    assert start(batch, root)["status"] == "completed"
    top, = batch.store.list_folders()
    nested, = batch.store.list_folders(top["id"])
    batch.store.update_folder(nested["id"], "renamed-chapter")
    (child / "new.mp4").write_bytes(b"new nested lesson")
    assert start(batch, root)["status"] == "completed"
    again, = batch.store.list_folders(top["id"])
    assert again["id"] == nested["id"] and again["name"] == "renamed-chapter"
    assert len(batch.store.list_folder_videos(again["id"])) == 2


def test_importing_parent_after_child_attaches_existing_folder(batch):
    root = folder(batch, "parent-later", 0)
    child = folder(batch, "parent-later/child", 1)
    assert start(batch, child)["status"] == "completed"
    existing, = batch.store.list_folders()
    (root / "another.mp4").write_bytes(b"parent lesson")
    assert start(batch, root)["status"] == "completed"
    top, = batch.store.list_folders()
    attached, = batch.store.list_folders(top["id"])
    assert top["name"] == "parent-later" and attached["id"] == existing["id"]


def test_nested_directory_link_is_blocked_without_side_effects(batch):
    import os
    import subprocess

    root = folder(batch, "link-source")
    target = folder(batch, "link-target")
    link = root / "linked"
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
    else:
        link.symlink_to(target, target_is_directory=True)
    try:
        result = preview_directories([str(root)])
        assert not result["valid"]
        assert "linked" in result["folders"][0]["examples"]
        assert batch.client.post("/api/batch/process", json={"folders": [str(root)]}).status_code == 400
        assert batch.store.list_videos() == [] and batch.store.list_folders() == []
    finally:
        # Remove only the link itself, never its target.
        if os.name == "nt":
            link.rmdir()
        else:
            link.unlink()


def test_library_folder_batch_includes_descendants_once(batch):
    ids = register_library_videos(batch)
    top = batch.store.create_folder("parent")
    child = batch.store.create_folder("child", top)
    deep = batch.store.create_folder("deep", child)
    for video_id, folder_id in zip(ids, [top, child, deep]):
        batch.store.set_video_folder(video_id, folder_id)
    response = batch.client.post("/api/batch/process", json={"folder_ids": [top, child], "video_ids": [ids[-1]]})
    assert response.status_code == 202, response.text
    assert response.json()["total"] == 3
    assert wait(batch, response.json()["job_id"])["batch_summary"]["completed"] == 3
    response = batch.client.post(f"/api/folders/{top}/process", json={})
    assert response.status_code == 202, response.text
    assert response.json()["video_ids"] == ids
    assert wait(batch, response.json()["job_id"])["status"] == "completed"


def test_custom_batch_retry_keeps_frozen_rules_after_edit_and_archival(batch, monkeypatch):
    ids = register_library_videos(batch, count=2)
    scheme = batch.client.post("/api/settings/recall-schemes", json={
        "name": "教师偏好", "base_mode": "medium_recall",
        "rules": [{"phrase": "拿笔画下来", "meaning": "emphasis"}], "notes": "原始偏好",
    }).json()
    mode = scheme["id"]
    snapshots = []
    original = api_main.create_judge
    def create(*args, **kwargs):
        snapshots.append(kwargs["scheme_snapshot"])
        return original(*args, **kwargs)
    monkeypatch.setattr(api_main, "create_judge", create)
    batch.outcomes.append(ApiUnavailable("测试 API 离线"))
    response = batch.client.post("/api/batch/process", json={"video_ids": ids, "analysis_modes": [mode]})
    assert response.status_code == 202, response.text
    job = wait(batch, response.json()["job_id"])
    assert job["status"] == "awaiting_api"
    assert len(batch.asr) == 2 and len(batch.ai) == 1
    batch.client.put(f"/api/settings/recall-schemes/{mode}", json={
        "name": "改名", "base_mode": "low_recall", "rules": [], "notes": "新偏好",
    })
    batch.client.delete(f"/api/settings/recall-schemes/{mode}")
    resumed = batch.client.post(f"/api/jobs/{job['id']}/retry")
    assert resumed.status_code == 202, resumed.text
    completed = wait(batch, resumed.json()["id"])
    assert completed["status"] == "completed", completed
    assert len(batch.asr) == 2
    assert all(snapshot["revision"] == 1 and snapshot["notes"] == "原始偏好" for snapshot in snapshots)
    assert batch.modes == [mode, mode, mode]
    assert batch.store.get_analysis_run(ids[0], "medium_recall") is None


def register_library_videos(batch, count=3):
    root = folder(batch, count=count)
    return [
        batch.client.post("/api/videos/register", json={"path": str(path)}).json()["video_id"]
        for path in sorted(root.glob("*.mp4"))
    ]


def test_selected_video_ids_use_durable_batch_order_and_leave_unselected_videos_untouched(batch):
    ids = register_library_videos(batch)
    folder_id = batch.store.create_folder("selected")
    batch.store.set_video_folder(ids[0], folder_id)
    batch.store.set_video_folder(ids[2], folder_id)
    response = batch.client.post("/api/batch/process", json={"video_ids": [ids[2], ids[0], ids[2]]})
    assert response.status_code == 202, response.text
    assert response.json()["video_ids"] == [ids[2], ids[0]]
    assert response.json()["total"] == 2
    job = wait(batch, response.json()["job_id"])
    assert job["job_type"] == "batch_video_process"
    assert job["status"] == "completed"
    assert batch.ai == [ids[2], ids[0]]
    assert len(batch.asr) == 2
    assert batch.store.get_video(ids[1])["status"] == "uploaded"
    assert all(batch.store.get_video(id)["library_folder_id"] == folder_id for id in (ids[0], ids[2]))
    state = json.loads(batch.store.get_job(job["id"])["request_json"])
    assert state["version"] == 2
    assert [entry["video_id"] for entry in state["entries"]] == [ids[2], ids[0]]
    assert all(entry["source_path"] == "" and entry["status"] == "completed" for entry in state["entries"])


def test_multiple_recall_modes_share_one_transcription_per_video(batch):
    ids = register_library_videos(batch, count=2)
    response = batch.client.post("/api/batch/process", json={
        "video_ids": ids, "analysis_modes": ["high_recall", "medium_recall", "low_recall"],
    })
    assert wait(batch, response.json()["job_id"])["status"] == "completed"
    assert len(batch.asr) == 2
    assert batch.ai == [ids[0]] * 3 + [ids[1]] * 3
    assert batch.modes == ["high_recall", "medium_recall", "low_recall"] * 2
    for video_id in ids:
        assert all(batch.store.get_analysis_run(video_id, mode)["status"] == "completed"
                   for mode in ["high_recall", "medium_recall", "low_recall"])


def test_multi_recall_retry_skips_modes_already_completed_in_this_batch(batch):
    ids = register_library_videos(batch, count=2)
    batch.outcomes.extend([None, ApiUnavailable("fixture offline")])
    response = batch.client.post("/api/batch/process", json={
        "video_ids": ids, "analysis_modes": ["medium_recall", "low_recall"],
    })
    job = wait(batch, response.json()["job_id"])
    assert job["status"] == "awaiting_api"
    assert len(batch.asr) == 2
    state = json.loads(batch.store.get_job(job["id"])["request_json"])
    assert state["entries"][0]["completed_modes"] == ["medium_recall"]
    retry = batch.client.post(f'/api/jobs/{job["id"]}/retry')
    assert wait(batch, retry.json()["id"])["status"] == "completed"
    assert list(zip(batch.ai, batch.modes)).count((ids[0], "medium_recall")) == 1
    assert batch.modes == ["medium_recall", "low_recall", "low_recall", "medium_recall", "low_recall"]
    assert len(batch.asr) == 2


def test_selected_batch_reuses_subtitles_and_retries_only_unfinished_ai(batch):
    ids = register_library_videos(batch, count=2)
    batch.store.mark_transcribed(ids[0], [TranscriptSegment(1, 0, 1000, "cached subtitle")])
    batch.outcomes.append(ApiUnavailable("fixture offline"))
    response = batch.client.post("/api/batch/process", json={"video_ids": ids})
    job = wait(batch, response.json()["job_id"])
    assert job["status"] == "awaiting_api"
    assert len(batch.asr) == 1
    assert job["batch_summary"]["awaiting_ai"] == 2
    retry = batch.client.post(f'/api/jobs/{job["id"]}/retry')
    assert retry.status_code == 202
    assert wait(batch, retry.json()["id"])["status"] == "completed"
    assert len(batch.asr) == 1


def test_selected_and_folder_inputs_deduplicate_registered_videos(batch):
    ids = register_library_videos(batch, count=2)
    folder_id = batch.store.create_folder("course")
    for id in ids:
        batch.store.set_video_folder(id, folder_id)
    response = batch.client.post("/api/batch/process", json={"video_ids": ids, "folder_ids": [folder_id]})
    assert response.status_code == 202
    assert response.json()["total"] == 2
    assert wait(batch, response.json()["job_id"])["batch_summary"]["completed"] == 2
    assert len(batch.asr) == 2


@pytest.mark.parametrize("values", [[], [0], [-1], [True], ["1"], [1.5]])
def test_selected_batch_rejects_invalid_id_lists_before_creating_jobs(batch, values):
    response = batch.client.post("/api/batch/process", json={"video_ids": values})
    assert response.status_code == 422
    assert batch.store.latest_batch_job() is None
    assert batch.asr == batch.ai == []


def test_selected_batch_validates_all_records_and_files_before_submitting(batch):
    ids = register_library_videos(batch)
    assert batch.client.post("/api/batch/process", json={"video_ids": [ids[0], 999999]}).status_code == 404
    Path(batch.store.get_video(ids[1])["video_path"]).unlink()
    assert batch.client.post("/api/batch/process", json={"video_ids": [ids[0], ids[1]]}).status_code == 400
    batch.store.set_status(ids[2], "processing")
    assert batch.client.post("/api/batch/process", json={"video_ids": [ids[0], ids[2]]}).status_code == 409
    assert batch.store.latest_batch_job() is None
    assert batch.asr == batch.ai == []


@pytest.mark.parametrize("status", ["running", "resource_paused"])
def test_selected_batch_cannot_start_while_another_job_is_running_or_paused(batch, status):
    ids = register_library_videos(batch, count=2)
    batch.store.create_job("busy", "video_process", ids[0])
    batch.store.update_job("busy", status=status)
    assert batch.client.post("/api/batch/process", json={"video_ids": ids}).status_code == 409
    assert batch.asr == batch.ai == []


def test_import_is_persisted_before_source_deletion(batch, monkeypatch):
    root = folder(batch)
    original = Path.unlink
    receipts = []

    def unlink(path, *args, **kwargs):
        if path.parent == root:
            state = json.loads(batch.store.latest_batch_job()["request_json"])
            entry = next(entry for entry in state["entries"] if entry["source_path"] == str(path))
            assert entry["video_id"] and entry["status"] == "pending_asr"
            assert batch.store.get_video(entry["video_id"])["library_folder_id"]
            receipts.append(entry["video_id"])
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    job = start(batch, root)
    assert job["status"] == "completed"
    assert job["batch_summary"]["completed"] == 2
    assert len(receipts) == 2 and not list(root.iterdir())
    assert "2 个完成" in job["message"]


def test_copy_batch_retains_sources_and_retry_reuses_library_and_subtitles(batch):
    root = folder(batch)
    data = batch.client.get("/api/settings/directories").json()
    payload = {key: data[key] for key in ("asr_storage_root", "audio_cache_dir", "log_dir", "video_library_dir")}
    payload["delete_source_after_import"] = False
    assert batch.client.put("/api/settings/directories", json=payload).status_code == 200
    batch.outcomes.append(ApiUnavailable("fixture API unavailable"))
    job = start(batch, root)
    assert job["status"] == "awaiting_api"
    assert len(batch.asr) == 2
    assert len(list(root.glob("*.mp4"))) == 2
    for source in root.iterdir():
        source.unlink()
    response = batch.client.post(f'/api/jobs/{job["id"]}/retry')
    assert response.status_code == 202, response.text
    retried = wait(batch, response.json()["id"])
    assert retried["status"] == "completed"
    assert len(batch.asr) == 2
    for video in batch.store.list_videos():
        assert Path(video["video_path"]).is_file()


def test_cross_batch_same_name_and_renamed_source_folder(batch):
    first = folder(batch, "first/course", 1)
    second = folder(batch, "second/course", 1)
    start(batch, first)
    start(batch, second)
    folders = batch.store.list_folders()
    assert {row["name"] for row in folders} == {"course", "course (1)"}
    original_id = next(row["id"] for row in folders if row["name"] == "course")
    batch.store.update_folder(original_id, "user-renamed")
    (first / "new.mp4").write_bytes(b"new content")
    start(batch, first)
    assert len(batch.store.list_folders()) == 2
    assert len(batch.store.list_folder_videos(original_id)) == 2


def test_same_request_same_name_does_not_merge(batch):
    one = folder(batch, "one/course", 1)
    two = folder(batch, "two/course", 1)
    response = batch.client.post("/api/batch/process", json={"folders": [str(one), str(two)]})
    wait(batch, response.json()["job_id"])
    assert {row["name"] for row in batch.store.list_folders()} == {"course", "course (1)"}


def test_duplicate_keeps_original_path_folder_and_review_edits(batch):
    one = folder(batch, "one", 1)
    start(batch, one)
    video = batch.store.list_videos()[0]
    marker = batch.store.add_marker(video["id"], 0, 500, "exam", "", "", "", .9, "medium_recall")
    batch.store.set_marker_jump_time(marker, 100)
    two = folder(batch, "two", 0)
    source = two / "other-name.mp4"
    source.write_bytes(Path(video["video_path"]).read_bytes())
    job = start(batch, two)
    updated = batch.store.get_video(video["id"])
    assert updated["video_path"] == video["video_path"]
    assert updated["library_folder_id"] == video["library_folder_id"]
    assert updated["filename"] == video["filename"]
    assert source.exists()
    assert len(batch.asr) == 1
    assert batch.store.list_markers(video["id"])[0]["user_start_ms"] == 100
    assert "重复视频" in job["batch_entries"][0]["warning"]


@pytest.mark.parametrize("failure", [
    ConnectionError("offline"), TimeoutError("timeout"), ApiUnavailable("missing settings"),
    *[ApiUnavailable(f"HTTP {status}", status_code=status) for status in [400, 401, 403, 404, 429, 500, 503]],
])
def test_api_failure_finishes_all_asr_then_retry_uses_latest_key(batch, monkeypatch, failure):
    root = folder(batch, count=3)
    batch.outcomes.append(failure)
    job = start(batch, root)
    assert job["status"] == "awaiting_api"
    assert len(batch.asr) == 3 and len(batch.ai) == 1
    assert job["batch_summary"]["awaiting_ai"] == 3
    assert all(row["status"] == "transcribed" for row in batch.store.list_videos())
    assert all(batch.store.get_segments(row["id"]) for row in batch.store.list_videos())
    assert not list(root.iterdir())
    video = batch.store.list_videos()[0]
    renamed = Path(video["video_path"]).with_name("renamed.mp4")
    Path(video["video_path"]).rename(renamed)
    batch.store.update_video_file(video["id"], renamed)
    monkeypatch.setattr(api_main, "resolve_interface_key", lambda *_: "new-key")
    response = batch.client.post(f'/api/jobs/{job["id"]}/retry')
    assert response.status_code == 202
    result = wait(batch, response.json()["id"])
    assert result["status"] == "completed"
    assert result["batch_summary"]["completed"] == 3
    assert len(batch.asr) == 3 and len(batch.ai) == 4
    assert batch.keys == ["fixture-key", "new-key"]
    assert batch.client.get("/api/batch/latest").json()["id"] == result["id"]


def test_missing_configuration_does_not_block_asr(batch, monkeypatch):
    monkeypatch.setattr(api_main, "resolve_interface_key", lambda *_: "")
    job = start(batch, folder(batch))
    assert job["status"] == "awaiting_api"
    assert len(batch.asr) == 2 and batch.ai == []


def test_running_batch_uses_original_connection_after_global_switch(batch, monkeypatch):
    settings = SimpleNamespace(active_profile=ProviderProfile(base_url="https://original.test/v1", model="original"))
    monkeypatch.setattr(api_main, "load_provider_settings", lambda: settings)
    monkeypatch.setattr(api_main, "resolve_interface_key", lambda profile, _: f"{profile.model}-key")
    original_factory = api_main.create_judge
    used = []

    def create(profile, key, *args, **kwargs):
        used.append((profile.model, key))
        # Another settings request switches the global connection during this job.
        settings.active_profile = ProviderProfile(base_url="https://new.test/v1", model="new")
        return original_factory(profile, key, *args, **kwargs)

    monkeypatch.setattr(api_main, "create_judge", create)
    job = start(batch, folder(batch, count=3))
    assert job["batch_summary"]["completed"] == 3
    assert used and all(item == ("original", "original-key") for item in used)
    start(batch, folder(batch, name="next-job", count=1))
    assert used[-1] == ("new", "new-key")

def test_retry_saved_subtitles_does_not_require_asr_model(batch, monkeypatch):
    batch.outcomes.append(ConnectionError("offline"))
    job = start(batch, folder(batch))
    monkeypatch.setattr(video_pipeline, "get_model", lambda *_: pytest.fail("Saved subtitles must not look up ASR"))
    monkeypatch.setattr(api_main, "require_selectable_model", lambda *_: pytest.fail("Saved subtitles must not validate ASR"))
    response = batch.client.post(f'/api/jobs/{job["id"]}/retry')
    assert response.status_code == 202
    assert wait(batch, response.json()["id"])["batch_summary"]["completed"] == 2
    assert len(batch.asr) == 2

def test_asr_timeout_is_not_misclassified_as_api_outage(batch, monkeypatch):
    original = video_pipeline._transcribe_video_with_fallback
    first = True

    def transcribe(*args, **kwargs):
        nonlocal first
        if first:
            first = False
            raise TimeoutError("ASR decoder timeout")
        return original(*args, **kwargs)

    monkeypatch.setattr(video_pipeline, "_transcribe_video_with_fallback", transcribe)
    job = start(batch, folder(batch))
    assert job["status"] == "completed"
    assert job["batch_summary"]["failed"] == 1
    assert job["batch_summary"]["completed"] == 1
    assert len(batch.ai) == 1


def test_semantic_error_is_local_and_successful_items_not_retried(batch):
    batch.outcomes.append(ValueError("invalid evidence"))
    job = start(batch, folder(batch))
    assert job["status"] == "completed"
    assert job["batch_summary"]["failed"] == 1
    assert job["batch_summary"]["completed"] == 1
    assert len(batch.ai) == 2
    response = batch.client.post(f'/api/jobs/{job["id"]}/retry')
    retried = wait(batch, response.json()["id"])
    assert retried["batch_summary"]["completed"] == 2
    assert len(batch.ai) == 3 and len(batch.asr) == 2


def test_resource_pause_preserves_complete_queue_and_ai_waiting(batch, monkeypatch):
    original = video_pipeline._transcribe_video_with_fallback
    calls = 0

    def pause(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ResourcePaused("fixture out of memory")
        return original(*args, **kwargs)

    monkeypatch.setattr(video_pipeline, "_transcribe_video_with_fallback", pause)
    batch.outcomes.append(ConnectionError("offline"))
    job = start(batch, folder(batch, count=3))
    assert job["status"] == "resource_paused"
    request = json.loads(batch.store.get_job(job["id"])["request_json"])
    assert len(request["entries"]) == 3
    assert request["entries"][0]["status"] == "pending_ai"
    response = batch.client.post(f'/api/jobs/{job["id"]}/resume', json={"skip_current": True})
    resumed = wait(batch, response.json()["job_id"])
    assert resumed["batch_summary"]["completed"] == 2
    assert resumed["batch_summary"]["skipped"] == 1
    assert len(batch.asr) == 2


@pytest.mark.parametrize("nested", [False, True])
def test_restart_recovers_import_manifest_without_original_source(batch, nested):
    root = folder(batch, count=0 if nested else 1)
    source_directory = folder(batch, "course/chapter", 1) if nested else root
    options = api_main.VideoProcessPayload(asr_model_id="whisper-turbo")
    state = make_batch(preview_directories([str(root)]), options.model_dump())
    entry = state["entries"][0]
    entry["content_hash"] = batch.store.file_hash(Path(entry["source_path"]))
    folder_id = batch.store.import_folder(root)
    if nested:
        folder_id = batch.store.import_folder(source_directory, folder_id)
    video_id, _path, _, _ = register_managed_video(
        batch.store, entry["source_path"], batch.root / "audio", batch.root / "library",
    )
    batch.store.set_video_folder(video_id, folder_id)
    batch.store.create_job("interrupted-import", "batch_video_process")
    batch.store.update_job("interrupted-import", status="running", request_json=json.dumps(state))
    assert not Path(entry["source_path"]).exists()
    batch.store.mark_jobs_interrupted()
    response = batch.client.post("/api/jobs/interrupted-import/retry")
    assert response.status_code == 202
    result = wait(batch, response.json()["id"])
    assert result["status"] == "completed" and result["batch_summary"]["completed"] == 1
    assert len(batch.store.list_videos()) == 1
    assert batch.store.get_video(video_id)["library_folder_id"] == folder_id


def test_database_reopen_recovers_ids_of_inflight_batch(batch):
    root = folder(batch)
    batch.outcomes.append(ConnectionError("offline"))
    job = start(batch, root)
    batch.store.update_job(job["id"], status="running")
    state = json.loads(batch.store.get_job(job["id"])["request_json"])
    batch.store.set_status(state["entries"][0]["video_id"], "processing")
    reopened = VideoStore(batch.store.path)
    assert reopened.mark_jobs_interrupted() == 1
    assert reopened.get_video(state["entries"][0]["video_id"])["status"] == "failed"
    assert len(json.loads(reopened.get_job(job["id"])["request_json"])["entries"]) == 2
    response = batch.client.post(f'/api/jobs/{job["id"]}/retry')
    result = wait(batch, response.json()["id"])
    assert result["batch_summary"]["completed"] == 2
    assert len(batch.asr) == 2


def test_missing_video_cannot_be_reimported_from_empty_source(batch):
    job = start(batch, folder(batch, count=1))
    assert batch.client.post(f'/api/jobs/{job["id"]}/retry').status_code == 409


@pytest.mark.parametrize("nested", [False, True])
def test_all_directories_rechecked_by_worker_before_moves(batch, monkeypatch, nested):
    root = folder(batch)
    destination = root / "deep" / "nested" if nested else root
    destination.mkdir(parents=True, exist_ok=True)
    application = batch.application
    original = application.state.jobs.submit

    def submit(*args, **kwargs):
        (destination / "added-after-preview.txt").write_bytes(b"non-video")
        return original(*args, **kwargs)

    monkeypatch.setattr(application.state.jobs, "submit", submit)
    job = start(batch, root)
    assert job["status"] == "failed"
    assert not batch.store.list_videos()
    assert not batch.store.list_folders()
    assert len(list(root.glob("*.mp4"))) == 2


def test_import_source_migration_only_backfills_unambiguous_folders(tmp_path):
    store = VideoStore(tmp_path / "legacy.db")
    single = store.create_folder("single")
    mixed = store.create_folder("mixed")
    shared = store.create_folder("shared")
    unknown = store.create_folder("unknown")
    specifications = [
        (single, tmp_path / "one"), (mixed, tmp_path / "two"), (mixed, tmp_path / "three"),
        (shared, tmp_path / "two"), (unknown, tmp_path / "four"), (unknown, None),
    ]
    for index, (folder_id, source) in enumerate(specifications):
        path = tmp_path / f"{index}.mp4"
        video_id = store.create_video(str(index), path.name, str(path), "audio.wav", 1000)
        store.set_video_folder(video_id, folder_id)
        with store.connect() as connection:
            connection.execute("UPDATE videos SET source_folder = ? WHERE id = ?", (str(source) if source else "", video_id))
    with store.connect() as connection:
        connection.execute("DROP INDEX folders_by_import_source")
        connection.execute("ALTER TABLE video_folders DROP COLUMN import_source")
    migrated = VideoStore(store.path)
    assert migrated.import_folder(tmp_path / "one") == single
    assert migrated.get_folder(mixed)["import_source"] is None
    assert migrated.get_folder(shared)["import_source"] is None
    assert migrated.get_folder(unknown)["import_source"] is None
    assert len(migrated.list_videos()) == len(specifications)


def test_deleted_record_and_missing_file_do_not_block_remaining(batch):
    batch.outcomes.append(ConnectionError("offline"))
    job = start(batch, folder(batch, count=3))
    videos = batch.store.list_videos()
    with batch.store.connect() as connection:
        connection.execute("DELETE FROM videos WHERE id = ?", (videos[0]["id"],))
    Path(videos[1]["video_path"]).unlink()
    response = batch.client.post(f'/api/jobs/{job["id"]}/retry')
    result = wait(batch, response.json()["id"])
    assert result["batch_summary"]["failed"] == 2
    assert result["batch_summary"]["completed"] == 1
    assert len(batch.asr) == 3


def test_cancellation_preserves_queue_and_can_retry(batch):
    batch.outcomes.append(ProcessingCancelled("stop"))
    job = start(batch, folder(batch))
    assert job["status"] == "interrupted"
    assert len(job["batch_entries"]) == 2
    response = batch.client.post(f'/api/jobs/{job["id"]}/retry')
    result = wait(batch, response.json()["id"])
    assert result["batch_summary"]["completed"] == 2
    assert len(batch.asr) == 2


def test_legacy_batch_retries_current_paths(batch):
    root = folder(batch, count=1)
    path = next(root.iterdir())
    video_id = batch.store.create_video("legacy", path.name, str(path), "audio.wav", 1000)
    batch.store.mark_transcribed(video_id, [TranscriptSegment(1, 0, 1000, "saved")])
    batch.store.create_job("legacy", "batch_video_process")
    batch.store.update_job("legacy", status="failed", request_json=json.dumps({
        "kind": "batch", "items": [[video_id, "old-missing-path.mp4"]], "options": {},
    }))
    response = batch.client.post("/api/jobs/legacy/retry")
    assert response.status_code == 202
    assert wait(batch, response.json()["id"])["batch_summary"]["completed"] == 1
    assert batch.asr == []


def test_legacy_completed_batch_exposes_failed_items_and_preserves_success(batch):
    root = folder(batch)
    ids = []
    for index, path in enumerate(sorted(root.iterdir())):
        video_id = batch.store.create_video(f"legacy-{index}", path.name, str(path), "audio.wav", 1000)
        batch.store.mark_transcribed(video_id, [TranscriptSegment(1, 0, 1000, "saved")])
        ids.append(video_id)
    batch.store.set_analysis_run(ids[0], "medium_recall", "completed", 0)
    batch.store.set_status(ids[0], "review_ready")
    batch.store.set_status(ids[1], "failed")
    batch.store.create_job("legacy-ended", "batch_video_process")
    batch.store.update_job("legacy-ended", status="completed", request_json=json.dumps({
        "kind": "batch", "items": [[video_id, "missing.mp4"] for video_id in ids],
        "options": {"analysis_mode": "medium_recall"},
    }))
    latest = batch.client.get("/api/batch/latest").json()
    assert latest["batch_summary"]["completed"] == 1
    assert latest["batch_summary"]["awaiting_ai"] == 1
    response = batch.client.post("/api/jobs/legacy-ended/retry")
    assert wait(batch, response.json()["id"])["batch_summary"]["completed"] == 2
    assert batch.ai == [ids[1]]
    assert batch.asr == []


def test_api_failure_classification_is_structural():
    rejected = RuntimeError("not classified by text")
    rejected.status_code = 404
    assert api_failure(rejected) is rejected
    assert api_failure(ValueError("API timeout 401")) is None
    wrapped = RuntimeError("sanitized")
    wrapped.__cause__ = rejected
    assert api_failure(wrapped) is rejected
