import base64
import hashlib
import json
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from api.codex_bridge import router
from classcue.codex_bridge import CodexBridge
from classcue.codex_video_clip import clip_snapshot, preview_clip
from classcue.core import TranscriptSegment
from classcue.dsh_pipe import BridgeFailure
from classcue.video_store import VideoStore
from tests.test_codex_bridge import FakeCodex


@pytest.fixture
def clip(tmp_path):
    store = VideoStore(tmp_path / "project.db")
    video_id = store.create_video("synthetic", "lecture.mp4", str(tmp_path / "lecture.mp4"), "", 120_000)
    store.replace_transcript_for_reanalysis(video_id, [
        TranscriptSegment(1, 0, 10_000, "范围之前"),
        TranscriptSegment(2, 10_000, 22_000, "教师原话：速度等于路程除以时间。"),
        TranscriptSegment(3, 22_000, 40_000, "请结合图中的公式理解。"),
        TranscriptSegment(4, 50_000, 60_000, "范围之后"),
    ], "synthetic")
    root = tmp_path / "slides"
    root.mkdir()
    for index, color in enumerate(["white", "black"]):
        output = BytesIO()
        Image.new("RGB", (160, 96), color).save(output, format="PNG")
        (root / f"{index}.png").write_bytes(output.getvalue())
    rows = []
    # The final preferred image at 80s must not leak into a 15-30s clip.
    for index, (start, end, visible, file_index, preferred) in enumerate([
        (0, 15_000, 0, 0, False),
        (15_000, 25_000, 15_000, 0, True),
        (25_000, 40_000, 25_000, 0, True),
        (40_000, 100_000, 80_000, 1, True),
    ]):
        rows.append({
            "start_ms": start, "end_ms": end, "representative_ms": visible,
            "visible_from_ms": visible, "visible_until_ms": end,
            "page_start_ms": 0, "page_end_ms": 100_000,
            "image_path": f"{file_index}.png", "image_hash": hashlib.sha256((root / f"{file_index}.png").read_bytes()).hexdigest(),
            "duration_ms": end - start, "retention_class": "primary",
            "confidence": 1, "roi_json": "{}", "extraction_version": "synthetic",
            "is_llm_preferred": preferred,
        })
    store.replace_video_slides(video_id, rows)
    return store, video_id, root


def snapshot(clip, **kwargs):
    store, video_id, root = clip
    preview = preview_clip(store, video_id, 15_000, 30_000)
    return clip_snapshot(store, "synthetic", video_id, 15_000, 30_000, slide_root=root,
                         fingerprint=preview["fingerprint"],
                         slide_ids=kwargs.pop("slide_ids", [image["slide_id"] for image in preview["images"]]),
                         **kwargs)


def test_preview_preserves_boundary_sentences_and_deduplicates_only_visible_preferred_images(clip):
    store, video_id, _ = clip
    value = preview_clip(store, video_id, 15_000, 30_000)
    assert [item["id"] for item in value["subtitles"]] == [2, 3]
    assert "00:00:10,000 --> 00:00:22,000" in value["subtitle_srt"]
    assert len(value["images"]) == 1
    assert len(value["images"][0]["time_instances"]) == 2
    earlier = preview_clip(store, video_id, 45_000, 55_000)
    assert earlier["images"] == []
    assert [item["id"] for item in earlier["subtitles"]] == [4]


@pytest.mark.parametrize("start,end", [(-1, 1000), (1000, 1000), (2000, 1000), (0, 120_001)])
def test_range_rejects_invalid_or_out_of_duration_values(clip, start, end):
    with pytest.raises(BridgeFailure, match="起止时间"):
        preview_clip(clip[0], clip[1], start, end)


def test_empty_subtitles_cannot_start_a_discussion(clip):
    with pytest.raises(BridgeFailure, match="没有已保存的字幕"):
        preview_clip(clip[0], clip[1], 105_000, 110_000)


def test_snapshot_keeps_teacher_text_image_order_and_original_times(clip):
    value = snapshot(clip)
    assert value["source_kind"] == "video_clip"
    assert value["course_name"] == "视频片段"
    assert value["content"][0]["type"] == "text"
    assert "00:00:15.000 - 00:00:30.000" in value["content"][0]["text"]
    assert "图片 1" in value["content"][0]["text"]
    assert base64.b64decode(value["content"][1]["data"]) == (clip[2] / "0.png").read_bytes()
    assert snapshot(clip, slide_ids=[])["content"][0]["text"].endswith("不推测未提供的画面。")


def test_nested_video_reuses_existing_course_identity(clip):
    from classcue.study_courses import StudyCourseStore
    store, video_id, _ = clip
    folder = store.create_folder("合成课程")
    course = StudyCourseStore(store).create(folder_id=folder)
    nested = store.create_folder("第二章", folder)
    store.set_video_folder(video_id, nested)
    value = snapshot(clip)
    assert value["course_id"] == course["id"]
    assert value["document_id"] == f"video-{video_id}"


def test_reextracting_identical_slides_changes_preview_token_but_keeps_discussion_identity(clip):
    store, video_id, _ = clip
    before = snapshot(clip)
    old_preview = preview_clip(store, video_id, 15_000, 30_000)
    rows = [dict(row) for row in store.list_slides(video_id, include_auxiliary=True)]
    store.replace_video_slides(video_id, rows)
    after = snapshot(clip)
    new_preview = preview_clip(store, video_id, 15_000, 30_000)
    assert old_preview["fingerprint"] != new_preview["fingerprint"]
    assert before["content"] == after["content"]
    assert before["question_id"] == after["question_id"]


def test_changed_preview_and_foreign_slide_are_rejected(clip):
    store, video_id, root = clip
    preview = preview_clip(store, video_id, 15_000, 30_000)
    with pytest.raises(BridgeFailure, match="不属于"):
        snapshot(clip, slide_ids=[99999])
    with store.connect() as db:
        db.execute("UPDATE video_transcript_segments SET text='合成修改' WHERE video_id=?", (video_id,))
    with pytest.raises(BridgeFailure, match="重新预览"):
        clip_snapshot(store, "synthetic", video_id, 15_000, 30_000, slide_root=root,
                      fingerprint=preview["fingerprint"], slide_ids=[])


@pytest.mark.parametrize("failure", ["missing", "changed", "escape", "corrupt"])
def test_bad_images_are_rejected_before_any_native_transfer(clip, tmp_path, failure):
    store, video_id, root = clip
    if failure == "missing":
        (root / "0.png").unlink()
    elif failure == "changed":
        (root / "0.png").write_bytes(b"changed")
    elif failure == "escape":
        with store.connect() as db:
            db.execute("UPDATE video_slides SET image_path='../outside.png' WHERE video_id=?", (video_id,))
        (tmp_path / "outside.png").write_bytes((root / "0.png").read_bytes())
    else:
        (root / "0.png").write_bytes(b"not an image")
        digest = hashlib.sha256(b"not an image").hexdigest()
        with store.connect() as db:
            db.execute("UPDATE video_slides SET image_hash=? WHERE video_id=?", (digest, video_id))
    with pytest.raises((BridgeFailure, OSError)):
        snapshot(clip)


def test_bridge_reuses_clip_snapshot_and_native_draft_without_model_or_duplicate_paste(clip, tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "home"))
    store, video_id, root = clip
    client = FakeCodex()
    pasted = []
    bridge = CodexBridge(store, "synthetic", client_factory=lambda *_: client, opener=lambda _: None,
                         native_inserter=lambda **value: pasted.append(value) or {"status": "pasted", "image_count": len(value["images"])})
    bridge.configure(str(tmp_path), executable=__file__)
    preview = bridge.preview_clip(video_id, 15_000, 30_000)
    args = dict(slide_root=root, fingerprint=preview["fingerprint"],
                slide_ids=[image["slide_id"] for image in preview["images"]])
    first = bridge.prepare_clip(video_id, 15_000, 30_000, **args)
    second = bridge.prepare_clip(video_id, 15_000, 30_000, **args)
    assert first["thread_id"] == second["thread_id"]
    assert len(pasted) == 1
    assert "教师原话" in pasted[0]["text"]
    assert Path(first["subtitle_file"]).read_text(encoding="utf-8") == preview["subtitle_srt"]
    context = json.loads(Path(first["context_file"]).read_text(encoding="utf-8"))
    assert context["source_kind"] == "video_clip"
    store.replace_video_slides(video_id, [dict(row) for row in store.list_slides(video_id, include_auxiliary=True)])
    refreshed = bridge.preview_clip(video_id, 15_000, 30_000)
    args.update(fingerprint=refreshed["fingerprint"],
                slide_ids=[image["slide_id"] for image in refreshed["images"]])
    same_content = bridge.prepare_clip(video_id, 15_000, 30_000, **args)
    assert same_content["thread_id"] == first["thread_id"]
    assert len(pasted) == 1
    assert len(bridge.clip_history(video_id)) == 1
    independent = bridge.prepare_clip(video_id, 15_000, 30_000, new_discussion=True, **args)
    assert independent["thread_id"] != first["thread_id"]
    assert len(pasted) == 2
    assert not any(method in {"turn/start", "thread/inject_items"} for method, _ in client.calls)


def test_api_validates_clip_range_and_desktop_boundary(clip, tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "home"))
    store, video_id, root = clip
    native = FakeCodex()
    bridge = CodexBridge(store, "synthetic", client_factory=lambda *_: native, opener=lambda _: None,
                         native_inserter=lambda **_: {"status": "pasted"})
    bridge.configure(str(tmp_path), executable=__file__)
    class Provider:
        def get(self): return store
        def library_id(self): return "synthetic"
        def data_root(self): return tmp_path
    import api.codex_bridge as api_module
    monkeypatch.setattr(api_module.asr_storage, "slide_directory", lambda *_: root)
    app = FastAPI()
    app.include_router(router(Provider(), bridge_factory=lambda *_: bridge, desktop=lambda: True))
    api = TestClient(app)
    body = {"video_id": video_id, "start_ms": 15_000, "end_ms": 30_000}
    preview = api.post("/api/codex-bridge/video-clips/preview", json=body)
    assert preview.status_code == 200
    for invalid in (True, "15000", 1.5):
        assert api.post("/api/codex-bridge/video-clips/preview", json={**body, "start_ms": invalid}).status_code == 422
    sent = api.post("/api/codex-bridge/video-clips", json={
        **body, "fingerprint": preview.json()["fingerprint"], "slide_ids": [],
    })
    assert sent.status_code == 202, sent.text
    assert len(api.get(f"/api/codex-bridge/videos/{video_id}/discussions").json()) == 1
    web = FastAPI()
    web.include_router(router(Provider(), desktop=lambda: False))
    assert TestClient(web).post("/api/codex-bridge/video-clips/preview", json=body).status_code == 404
