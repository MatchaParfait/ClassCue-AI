import json
import time
from threading import Event

import pytest

av = pytest.importorskip("av")
np = pytest.importorskip("numpy")
import cv2

from fastapi.testclient import TestClient

from api.main import create_app
import api.main as api_main
import classcue.slide_extractor as extractor
from classcue.slide_extractor import SlideRoi, extract_and_store_slides, extract_slides
from classcue.processing_control import ProcessingCancelled
from classcue.video_store import VideoStore


def make_slide_video(path, scenes, *, width=320, height=180, rate=10):
    with av.open(str(path), "w") as container:
        stream = container.add_stream("ffv1", rate=rate)
        stream.width, stream.height, stream.pix_fmt = width, height, "bgr0"
        for color, seconds in scenes:
            pixels = color if isinstance(color, np.ndarray) else np.full((height, width, 3), color, np.uint8)
            for _ in range(round(seconds * rate)):
                for packet in stream.encode(av.VideoFrame.from_ndarray(pixels, format="rgb24")):
                    container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)


def test_extract_slides_tracks_time_ranges_and_repeated_visuals(tmp_path):
    source = tmp_path / "slides.mkv"
    make_slide_video(source, [
        ((30, 40, 220), 12),
        ((40, 210, 50), 5),
        ((30, 40, 220), 4),
    ])

    result = extract_slides(source, tmp_path / "staging", "a" * 64)

    assert [(slide.start_ms, slide.end_ms) for slide in result.slides] == [
        (0, 12_000), (12_000, 17_000), (17_000, 21_000),
    ]
    assert [slide.retention_class for slide in result.slides] == [
        "primary", "auxiliary", "auxiliary",
    ]
    assert result.slides[0].image_hash == result.slides[2].image_hash
    assert all(slide.image_path.is_file() for slide in result.slides)
    assert all(slide.representative_ms < slide.end_ms for slide in result.slides)


def test_extract_slides_discards_unstable_short_transition(tmp_path):
    source = tmp_path / "transition.mkv"
    make_slide_video(source, [
        ((30, 40, 220), 12),
        ((220, 50, 40), 1),
        ((40, 210, 50), 5),
    ])

    result = extract_slides(source, tmp_path / "staging", "b" * 64)

    assert len(result.slides) == 2
    assert result.slides[0].retention_class == "primary"
    assert result.slides[1].retention_class == "auxiliary"


def test_slide_api_extracts_queries_and_serves_visual_evidence(tmp_path, monkeypatch):
    source = tmp_path / "slides.mkv"
    make_slide_video(source, [((30, 40, 220), 12), ((40, 210, 50), 5)])
    store = VideoStore(tmp_path / "data" / "project.db")
    video_id = store.create_video("c" * 64, source.name, str(source), "audio.wav", 17_000)
    monkeypatch.setattr(api_main, "DIRECTORY_PROJECT_ROOT", tmp_path / "application")

    with TestClient(create_app(store)) as client:
        started = client.post(f"/api/videos/{video_id}/slides/extract")
        assert started.status_code == 202
        job_id = started.json()["job_id"]
        for _ in range(100):
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] in {"completed", "failed", "interrupted"}:
                break
            time.sleep(0.02)
        assert job["status"] == "completed"

        primary = client.get(f"/api/videos/{video_id}/slides").json()
        all_slides = client.get(
            f"/api/videos/{video_id}/slides?include_auxiliary=true"
        ).json()
        assert len(primary) == 1
        assert len(all_slides) == 2
        assert client.get(
            f"/api/videos/{video_id}/slides/{all_slides[0]['id']}/image"
        ).status_code == 200

        evidence = client.get(
            f"/api/videos/{video_id}/visual-evidence?start_ms=12000&end_ms=13000&context_ms=0"
        )
        assert evidence.status_code == 200
        assert len(evidence.json()["slides"]) == 1
        assert evidence.json()["slides"][0]["retention_class"] == "auxiliary"
        assert len(evidence.json()["images"]) == 1
        assert "visible_from_ms" in all_slides[0]


def slide_image(*lines, color=(20, 20, 20)):
    image = np.full((360, 640, 3), 245, np.uint8)
    cv2.putText(image, "LESSON", (35, 45), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (30, 60, 90), 2)
    for index, line in enumerate(lines):
        cv2.putText(image, line, (50, 100 + index * 65), cv2.FONT_HERSHEY_SIMPLEX, 0.85, color, 2)
    return image


def extract_images(tmp_path, scenes, **options):
    source = tmp_path / "formula.mkv"
    make_slide_video(source, scenes, width=640, height=360)
    return extract_slides(source, tmp_path / "frames", "f" * 64, **options)


def decode_image(path):
    with av.open(str(path)) as container:
        return next(container.decode(video=0)).to_ndarray(format="rgb24")


def test_incremental_formulas_save_only_complete_image_and_actual_visibility(tmp_path):
    first = slide_image("x = a + b")
    second = slide_image("x = a + b", "y = x * 2")
    final = slide_image("x = a + b", "y = x * 2", "z = x + y")
    result = extract_images(tmp_path, [(first, 6), (second, 6), (final, 3)])
    assert len(result.slides) == 3
    preferred = [slide for slide in result.slides if slide.is_llm_preferred]
    assert len(preferred) == 1
    slide = preferred[0]
    assert (slide.start_ms, slide.end_ms) == (12_000, 15_000)
    assert slide.visible_from_ms == 12_000
    assert slide.visible_until_ms == 15_000
    assert 12_000 <= slide.representative_ms < 15_000
    assert slide.retention_class == "primary"
    assert (slide.page_start_ms, slide.page_end_ms) == (0, 15_000)
    assert np.abs(decode_image(slide.image_path).astype(float) - final).mean() < 2
    assert sum(slide.retention_class == "animation" for slide in result.slides) == 2
    assert len(list((tmp_path / "frames").glob("*.jpg"))) == 3


def test_small_persistent_formula_below_old_area_gate_is_retained(tmp_path):
    first = slide_image("Main question")
    final = first.copy()
    cv2.putText(final, "E=mc2", (390, 290), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (10, 10, 10), 1)
    result = extract_images(tmp_path, [(first, 12), (final, 3)])
    assert len(result.slides) == 2
    slide = max(result.slides, key=lambda item: item.information_score)
    assert slide.is_llm_preferred
    assert slide.visible_from_ms == 12_000
    assert np.abs(decode_image(slide.image_path)[278:300, 385:455].astype(float)
                  - final[278:300, 385:455]).mean() < 3


def test_visual_evidence_sends_only_preferred_animation_state(tmp_path, monkeypatch):
    first = slide_image("x = a + b")
    second = slide_image("x = a + b", "y = x * 2")
    final = slide_image("x = a + b", "y = x * 2", "z = x + y")
    source = tmp_path / "formula.mkv"
    make_slide_video(source, [(first, 6), (second, 6), (final, 3)], width=640, height=360)
    store = VideoStore(tmp_path / "data" / "project.db")
    video_id = store.create_video("e" * 64, source.name, str(source), "audio.wav", 15_000)
    root = tmp_path / "slides"
    extract_and_store_slides(store, video_id, root)
    monkeypatch.setattr(api_main.asr_storage, "slide_directory", lambda *args: root)
    with TestClient(create_app(store)) as client:
        payload = client.get(
            f"/api/videos/{video_id}/visual-evidence?start_ms=0&end_ms=15000&context_ms=0"
        ).json()
        early_payload = client.get(
            f"/api/videos/{video_id}/visual-evidence?start_ms=0&end_ms=1000&context_ms=0"
        ).json()
    assert len(payload["slides"]) == 3
    assert sum(slide["is_llm_preferred"] for slide in payload["slides"]) == 1
    assert len(payload["images"]) == 1
    assert payload["images"][0]["slide_ids"] == [payload["slides"][-1]["id"]]
    assert len(early_payload["slides"]) == 1
    assert len(early_payload["images"]) == 1
    assert early_payload["images"][0]["time_instances"][0]["visible_from_ms"] == 12_000


def test_question_replaced_by_answer_keeps_complementary_images(tmp_path):
    result = extract_images(tmp_path, [
        (slide_image("Question: x + y = ?"), 8),
        (slide_image("Answer: 42"), 4),
    ])
    assert len(result.slides) == 2
    assert [(s.start_ms, s.end_ms) for s in result.slides] == [(0, 8000), (8000, 12000)]
    assert all(s.retention_class == "primary" for s in result.slides)
    assert all(s.page_start_ms == 0 and s.page_end_ms == 12000 for s in result.slides)
    assert result.slides[0].image_hash != result.slides[1].image_hash


def test_erased_derivation_keeps_both_complementary_states(tmp_path):
    result = extract_images(tmp_path, [
        (slide_image("x = a + b", "y = x * 2"), 6),
        (slide_image("x = a + b", "z = a / b"), 6),
    ])
    assert len(result.slides) == 2
    assert all(slide.is_llm_preferred for slide in result.slides)


def test_removed_line_does_not_produce_redundant_image(tmp_path):
    result = extract_images(tmp_path, [
        (slide_image("x = a + b", "y = x * 2"), 8),
        (slide_image("x = a + b"), 4),
    ])
    assert len(result.slides) == 2
    assert result.slides[0].visible_until_ms == 8000
    assert result.slides[0].end_ms == 8000
    assert result.slides[1].end_ms == 12000
    assert result.slides[0].is_llm_preferred
    assert not result.slides[1].is_llm_preferred


def test_formula_symbol_change_is_not_treated_as_extra_information(tmp_path):
    first = slide_image("x = 2")
    cv2.line(first, (250, 100), (272, 100), (20, 20, 20), 2)
    final = first.copy()
    cv2.line(final, (261, 89), (261, 111), (20, 20, 20), 2)
    result = extract_images(tmp_path, [(first, 6), (final, 6)])
    assert len(result.slides) == 2
    assert all(slide.is_llm_preferred for slide in result.slides)


def test_change_of_existing_formula_colour_is_not_discarded(tmp_path):
    result = extract_images(tmp_path, [
        (slide_image("x = a + b"), 6),
        (slide_image("x = a + b", color=(230, 20, 20)), 6),
    ])
    assert len(result.slides) == 2
    assert all(slide.is_llm_preferred for slide in result.slides)


def test_one_second_overlay_is_not_published(tmp_path):
    first = slide_image("Question")
    overlay = first.copy()
    cv2.rectangle(overlay, (70, 160), (550, 300), (30, 30, 30), -1)
    result = extract_images(tmp_path, [(first, 6), (overlay, 1), (first, 6)])
    assert len(result.slides) == 1
    assert result.slides[0].end_ms == 13000


def test_gradual_formula_appearance_is_accumulated_against_stable_baseline(tmp_path):
    first = slide_image("Question")
    final = slide_image("Question", "y = x + a + b + c")
    scenes = [(first, 6)]
    for index in range(1, 9):
        gradual = first.copy()
        gradual[140:190, 40:40 + index * 40] = final[140:190, 40:40 + index * 40]
        scenes.append((gradual, 0.5))
    scenes.append((final, 3))
    result = extract_images(tmp_path, scenes)
    assert len(result.slides) == 2
    preferred = [slide for slide in result.slides if slide.is_llm_preferred]
    assert len(preferred) == 1
    assert np.abs(decode_image(preferred[0].image_path).astype(float) - final).mean() < 2
    assert preferred[0].visible_from_ms >= 9000


def test_ignored_camera_changes_do_not_remove_camera_from_output(tmp_path):
    scenes = []
    for color in (20, 160, 50, 220):
        frame = slide_image("Question")
        frame[270:350, 540:630] = color
        scenes.append((frame, 3))
    result = extract_images(tmp_path, scenes, ignore_regions=(SlideRoi(535, 265, 100, 90),))
    assert len(result.slides) == 1
    assert decode_image(result.slides[0].image_path)[300:330, 560:610].mean() > 210
    assert result.ignore_regions == (SlideRoi(535, 265, 100, 90),)


def test_moving_small_pointer_does_not_create_slide_variants(tmp_path):
    first = slide_image("Question")
    scenes = []
    for x in range(30, 150, 10):
        frame = first.copy()
        frame[300:304, x:x + 3] = 0
        scenes.append((frame, 1))
    result = extract_images(tmp_path, scenes)
    assert len(result.slides) == 1


def registered_video(tmp_path):
    source = tmp_path / "stored.mkv"
    make_slide_video(source, [((30, 40, 220), 5), ((40, 210, 50), 5), ((30, 40, 220), 5)])
    store = VideoStore(tmp_path / "library.db")
    video_id = store.create_video("b" * 64, source.name, str(source), "audio.wav", 15000)
    return source, store, video_id, tmp_path / "slides"


def test_repeated_page_shares_image_and_evidence_upload_entry(tmp_path, monkeypatch):
    _, store, video_id, root = registered_video(tmp_path)
    result = extract_and_store_slides(store, video_id, root)
    rows = result["slides"]
    assert len(rows) == 3
    assert rows[0]["image_path"] == rows[2]["image_path"]
    assert len(list(root.rglob("*.jpg"))) == 2
    monkeypatch.setattr(api_main.asr_storage, "slide_directory", lambda *args: root)
    with TestClient(create_app(store)) as client:
        payload = client.get(
            f"/api/videos/{video_id}/visual-evidence?start_ms=0&end_ms=15000&context_ms=0"
        ).json()
        assert len(payload["slides"]) == 3
        assert len(payload["images"]) == 2
        assert len(payload["images"][0]["slide_ids"]) == 2
        other = store.create_video("d" * 64, "other.mkv", "other.mkv", "audio.wav", 1000)
        assert client.get(f"/api/videos/{other}/slides/{rows[0]['id']}/image").status_code == 404


def test_cache_identity_invalidates_roi_parameters_and_ignore_regions(tmp_path, monkeypatch):
    _, store, video_id, root = registered_video(tmp_path)
    extract_and_store_slides(store, video_id, root)
    assert extract_and_store_slides(store, video_id, root)["status"] == "cached"
    cropped = extract_and_store_slides(store, video_id, root, roi=SlideRoi(0, 0, 160, 180))
    assert cropped["status"] == "completed"
    ignored = extract_and_store_slides(store, video_id, root, ignore_regions=(SlideRoi(0, 0, 20, 20),))
    assert ignored["status"] == "completed"
    assert json.loads(ignored["slides"][0]["roi_json"])["width"] == 320
    extract_and_store_slides(store, video_id, root)
    monkeypatch.setattr(extractor, "PIXEL_THRESHOLD", extractor.PIXEL_THRESHOLD + 1)
    assert extract_and_store_slides(store, video_id, root)["status"] == "completed"
    monkeypatch.setattr(extractor, "SLIDE_EXTRACTION_VERSION", "synthetic-next-version")
    assert extract_and_store_slides(store, video_id, root)["status"] == "completed"


def test_cancel_and_publication_failure_preserve_old_files_and_rows(tmp_path, monkeypatch):
    _, store, video_id, root = registered_video(tmp_path)
    result = extract_and_store_slides(store, video_id, root)
    before = [dict(row) for row in result["slides"]]
    files = {path: path.read_bytes() for path in root.rglob("*.jpg")}
    with pytest.raises(ProcessingCancelled):
        extract_and_store_slides(store, video_id, root, force=True, should_stop=lambda: True)
    assert [dict(row) for row in store.list_slides(video_id, include_auxiliary=True)] == before

    def fail(*_args):
        raise RuntimeError("Synthetic publication failure")

    monkeypatch.setattr(store, "replace_video_slides", fail)
    with pytest.raises(RuntimeError):
        extract_and_store_slides(store, video_id, root, force=True)
    assert {path: path.read_bytes() for path in root.rglob("*.jpg")} == files
    assert [dict(row) for row in store.list_slides(video_id, include_auxiliary=True)] == before


def test_cache_invalidates_when_source_changes_or_image_is_missing(tmp_path):
    source, store, video_id, root = registered_video(tmp_path)
    initial = extract_and_store_slides(store, video_id, root)
    (root / initial["slides"][0]["image_path"]).unlink()
    assert extract_and_store_slides(store, video_id, root)["status"] == "completed"
    make_slide_video(source, [((120, 100, 20), 6)])
    rebuilt = extract_and_store_slides(store, video_id, root)
    assert rebuilt["status"] == "completed"
    assert len(rebuilt["slides"]) == 1


def test_legacy_slide_schema_migrates_without_changing_images_or_times(tmp_path):
    _, store, video_id, _ = registered_video(tmp_path)
    with store.connect() as connection:
        connection.execute("DROP TABLE video_slides")
        connection.execute("""
            CREATE TABLE video_slides (
                id INTEGER PRIMARY KEY, video_id INTEGER REFERENCES videos(id) ON DELETE CASCADE,
                start_ms INTEGER, end_ms INTEGER, representative_ms INTEGER,
                image_path TEXT, image_hash TEXT, duration_ms INTEGER, retention_class TEXT,
                confidence REAL, roi_json TEXT, extraction_version TEXT, created_at TEXT
            )
        """)
        connection.execute(
            "INSERT INTO video_slides VALUES(1, ?, 0, 5000, 4000, 'old.jpg', 'old', "
            "5000, 'auxiliary', 0.7, '{}', 'old', '')", (video_id,),
        )
    migrated = VideoStore(store.path)
    row = migrated.get_slide(1)
    assert row["image_path"] == "old.jpg"
    assert row["visible_from_ms"] == 4000
    assert row["page_start_ms"] == 0
    assert row["page_end_ms"] == 5000
    assert row["cache_identity"] == ""


def test_rejects_invalid_ranges_without_deleting_existing_records(tmp_path):
    _, store, video_id, root = registered_video(tmp_path)
    result = extract_and_store_slides(store, video_id, root)
    rows = [dict(row) for row in result["slides"]]
    invalid = [dict(row) for row in rows]
    invalid[1]["start_ms"] = 0
    with pytest.raises(ValueError):
        store.replace_video_slides(video_id, invalid)
    assert [dict(row) for row in store.list_slides(video_id, include_auxiliary=True)] == rows


def test_rejects_ignore_mask_covering_whole_slide(tmp_path):
    with pytest.raises(ValueError, match="排除区域"):
        extract_images(tmp_path, [(slide_image("Question"), 3)],
                       ignore_regions=(SlideRoi(0, 0, 640, 360),))


def test_cancel_after_encoding_before_commit_keeps_previous_generation(tmp_path):
    _, store, video_id, root = registered_video(tmp_path)
    previous = extract_and_store_slides(store, video_id, root)
    before = [dict(row) for row in previous["slides"]]
    paths = set(root.rglob("*.jpg"))
    stop = False

    def progress(_value, _message, stage):
        nonlocal stop
        if stage == "saving":
            stop = True

    with pytest.raises(ProcessingCancelled):
        extract_and_store_slides(store, video_id, root, force=True,
                                 progress=progress, should_stop=lambda: stop)
    assert set(root.rglob("*.jpg")) == paths
    assert [dict(row) for row in store.list_slides(video_id, include_auxiliary=True)] == before


@pytest.mark.parametrize("report_progress", [False, True])
def test_cancel_after_commit_returns_published_generation(tmp_path, monkeypatch, report_progress):
    _, store, video_id, root = registered_video(tmp_path)
    previous = extract_and_store_slides(store, video_id, root)["slides"]
    old_paths = {row["image_path"] for row in previous}
    cancelled = Event()
    replace = store.replace_video_slides

    def commit_then_cancel(*args, **kwargs):
        rows = replace(*args, **kwargs)
        cancelled.set()
        return rows

    def progress(*_args):
        if cancelled.is_set():
            raise ProcessingCancelled("Synthetic cancellation after publication")

    monkeypatch.setattr(store, "replace_video_slides", commit_then_cancel)
    result = extract_and_store_slides(
        store, video_id, root, force=True, should_stop=cancelled.is_set,
        progress=progress if report_progress else None,
    )
    assert result["status"] == "completed" and result["committed"] is True
    rows = store.list_slides(video_id, include_auxiliary=True)
    assert list(result["slides"]) == list(rows)
    assert {row["image_path"] for row in rows}.isdisjoint(old_paths)
    assert all((root / row["image_path"]).is_file() for row in rows)


def test_api_cancel_after_slide_commit_completes_published_job(tmp_path, monkeypatch):
    _, store, video_id, root = registered_video(tmp_path)
    previous = extract_and_store_slides(store, video_id, root)["slides"]
    old_paths = {row["image_path"] for row in previous}
    ready = Event()
    replace = store.replace_video_slides
    monkeypatch.setattr(api_main.asr_storage, "slide_directory", lambda *_args: root)
    app = create_app(store)
    job_id = None

    def gated_extract(*args, **kwargs):
        assert ready.wait(10), "API did not return the job id"
        return extract_and_store_slides(*args, **kwargs)

    def commit_then_cancel(*args, **kwargs):
        rows = replace(*args, **kwargs)
        app.state.jobs.cancel(job_id)
        return rows

    monkeypatch.setattr(api_main, "extract_and_store_slides", gated_extract)
    monkeypatch.setattr(store, "replace_video_slides", commit_then_cancel)
    with TestClient(app) as client:
        try:
            started = client.post(f"/api/videos/{video_id}/slides/extract", json={"force": True})
            assert started.status_code == 202
            job_id = started.json()["job_id"]
            future = app.state.jobs._futures[job_id]
            ready.set()
            future.result(timeout=20)
            job = client.get(f"/api/jobs/{job_id}").json()
            assert job["status"] == job["stage"] == "completed"
            assert job["progress"] == 1.0
            rows = store.list_slides(video_id, include_auxiliary=True)
            assert {row["image_path"] for row in rows}.isdisjoint(old_paths)
            assert all((root / row["image_path"]).is_file() for row in rows)
        finally:
            ready.set()


def test_final_progress_error_is_not_hidden_after_publication(tmp_path):
    _, store, video_id, root = registered_video(tmp_path)
    old_paths = {row["image_path"] for row in extract_and_store_slides(store, video_id, root)["slides"]}

    def progress(_value, _message, stage):
        if stage == "completed":
            raise RuntimeError("Synthetic progress failure")

    with pytest.raises(RuntimeError, match="Synthetic progress failure"):
        extract_and_store_slides(store, video_id, root, force=True, progress=progress)
    rows = store.list_slides(video_id, include_auxiliary=True)
    assert {row["image_path"] for row in rows}.isdisjoint(old_paths)
    assert all((root / row["image_path"]).is_file() for row in rows)


def test_overlap_boundaries_context_and_subtitles_remain_time_addressable(tmp_path, monkeypatch):
    from classcue.core import TranscriptSegment

    _, store, video_id, root = registered_video(tmp_path)
    extract_and_store_slides(store, video_id, root)
    store.mark_transcribed(video_id, [
        TranscriptSegment(1, 1000, 3000, "Synthetic question"),
        TranscriptSegment(2, 6000, 8000, "Synthetic answer"),
    ])
    monkeypatch.setattr(api_main.asr_storage, "slide_directory", lambda *args: root)
    with TestClient(create_app(store)) as client:
        result = client.get(
            f"/api/videos/{video_id}/slides?start_ms=5000&end_ms=10000&include_auxiliary=true"
        ).json()
        assert len(result) == 1
        assert result[0]["start_ms"] == 5000
        context = client.get(
            f"/api/videos/{video_id}/visual-evidence?start_ms=6000&end_ms=7000"
        ).json()
        assert context["context_range"] == {"start_ms": 0, "end_ms": 27000}
        assert len(context["slides"]) == 3
        assert len(context["images"]) == 2
        assert len(context["subtitles"]) == 2


def test_recycle_roundtrip_handles_shared_image_resource_and_temporal_metadata(tmp_path):
    _, store, video_id, root = registered_video(tmp_path)
    before = [dict(row) for row in extract_and_store_slides(store, video_id, root)["slides"]]
    files = {row["image_path"]: (root / row["image_path"]).read_bytes() for row in before}
    recycled = store.trash_video(video_id, tmp_path / "media" / ".recycle_bin", root)
    assert store.list_slides(video_id, include_auxiliary=True) == []
    assert all(not (root / path).exists() for path in files)
    assert recycled["operation_id"]
    store.undo_latest_recycle(tmp_path / "media" / ".recycle_bin")
    assert [dict(row) for row in store.list_slides(video_id, include_auxiliary=True)] == before
    assert {path: (root / path).read_bytes() for path in files} == files


def test_no_stable_results_does_not_erase_a_usable_previous_extraction(tmp_path):
    source, store, video_id, root = registered_video(tmp_path)
    previous = [dict(row) for row in extract_and_store_slides(store, video_id, root)["slides"]]
    paths = set(root.rglob("*.jpg"))
    make_slide_video(source, [((20, 30, 40), 1), ((80, 120, 200), 1)])
    with pytest.raises(ValueError, match="已有截图未替换"):
        extract_and_store_slides(store, video_id, root, force=True)
    assert [dict(row) for row in store.list_slides(video_id, include_auxiliary=True)] == previous
    assert set(root.rglob("*.jpg")) == paths


def test_long_unchanging_page_has_one_interval_from_80_to_200_seconds(tmp_path):
    source = tmp_path / "long.mkv"
    make_slide_video(source, [((30, 40, 220), 80), ((40, 210, 50), 120)], rate=2)
    result = extract_slides(source, tmp_path / "frames", "f" * 64)
    assert [(s.start_ms, s.end_ms) for s in result.slides] == [(0, 80000), (80000, 200000)]


def test_jpeg_preserves_444_and_limits_long_edge(tmp_path):
    path = tmp_path / "frame.jpg"
    path.write_bytes(extractor._encode_jpeg(np.full((1400, 2800, 3), 245, np.uint8)))
    with av.open(str(path)) as container:
        frame = next(container.decode(video=0))
        assert (frame.width, frame.height) == (2560, 1280)
        assert frame.format.name == "yuvj444p"
