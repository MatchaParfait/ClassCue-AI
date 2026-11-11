import cv2
import numpy as np
import pytest

from classcue import slide_extractor as extractor
from tests.test_slide_extractor import make_slide_video


@pytest.mark.parametrize("prefetch", [False, True])
def test_sample_decode_reuses_converter_and_matches_original_rgb(tmp_path, monkeypatch, prefetch):
    import av
    import av.video.reformatter as video_reformatter

    source = tmp_path / "colors.mkv"
    make_slide_video(source, [((20, 140, 230), 2), ((220, 70, 15), 2)])
    expected = []
    next_ms = 0
    with av.open(str(source)) as container:
        stream = container.streams.video[0]
        for frame in container.decode(stream):
            timestamp = extractor._frame_time_ms(frame, stream, 0)
            if timestamp >= next_ms:
                next_ms = timestamp + extractor.SAMPLE_INTERVAL_MS
                expected.append((timestamp, frame.to_ndarray(format="rgb24")))
    created = []
    original = video_reformatter.VideoReformatter

    def reformatter():
        instance = original()
        created.append(instance)
        return instance

    monkeypatch.setattr(video_reformatter, "VideoReformatter", reformatter)
    actual = list(extractor._sample_frames(
        source, extractor.SlideRoi(0, 0, 320, 180), (), {"last_ms": 0}, None, prefetch,
    ))
    assert len(created) == 1
    assert len(actual) == len(expected)
    for (timestamp, rgb, _feature), (expected_ms, expected_rgb) in zip(actual, expected):
        assert timestamp == expected_ms
        np.testing.assert_array_equal(rgb, expected_rgb)


@pytest.mark.parametrize("threshold", [0, 18, 28, 40, 48, 255])
def test_rgb_threshold_keeps_strict_channel_maximum_semantics(threshold):
    random = np.random.default_rng(72)
    delta = random.integers(0, 256, (31, 47, 3), dtype=np.uint8)
    delta[0] = threshold
    np.testing.assert_array_equal(
        extractor._changed_pixels(delta, threshold), delta.max(axis=2) > threshold,
    )


def test_detection_features_skip_unused_edges_and_preserve_pixels(monkeypatch):
    image = np.random.default_rng(12).integers(0, 256, (90, 160, 3), dtype=np.uint8)
    roi = extractor.SlideRoi(0, 0, 160, 90)
    ignored = (extractor.SlideRoi(100, 50, 40, 30),)
    complete = extractor._feature(image, roi, ignored, 80)

    def unexpected(*_args):
        pytest.fail("Detection should not compute Canny edges")

    monkeypatch.setattr(cv2, "Canny", unexpected)
    detection = extractor._feature(image, roi, ignored, 80, include_edges=False)
    assert detection["edges"] is None
    for key in ("rgb", "gray", "valid"):
        np.testing.assert_array_equal(detection[key], complete[key])


@pytest.mark.parametrize("pixels", [0, 5, 6, 15, 16, 20])
def test_pixel_precheck_skips_only_impossible_changes_and_preserves_refine_count(pixels):
    roi = extractor.SlideRoi(0, 0, 80, 40)
    first = np.full((40, 80, 3), 245, np.uint8)
    second = first.copy()
    second[20, 10:10 + pixels] = 20
    previous = extractor._feature(first, roi, (), 80, include_edges=False)
    current = extractor._feature(second, roi, (), 80, include_edges=False)
    calls = []

    def compare(before, after):
        calls.append(True)
        return extractor._difference(before, after)

    actual = extractor._detect_difference(previous, current, compare)
    expected = extractor._difference(previous, current)
    assert actual["raw_pixels"] == pixels
    assert actual["changed"] == expected["changed"]
    assert bool(calls) == (pixels >= extractor.MIN_CHANGE_PIXELS)


@pytest.mark.parametrize("width,height", [(8, 8), (320, 180), (640, 360), (80, 160)])
def test_integer_ssim_matches_reference_including_borders_and_exclusions(width, height):
    random = np.random.default_rng(53)
    roi = extractor.SlideRoi(0, 0, width, height)
    ignored = (extractor.SlideRoi(width // 2, height // 2, width // 4, height // 4),)
    compare = extractor._CpuDifference()
    for level in (0, 32, 128, 245, 255):
        first = np.full((height, width, 3), level, np.uint8)
        second = np.clip(
            first.astype(np.int16) + random.integers(-35, 36, first.shape, dtype=np.int16),
            0, 255,
        ).astype(np.uint8)
        first[0, :, :] = 0
        second[-1, :, :] = 255
        previous = extractor._feature(first, roi, ignored, width)
        current = extractor._feature(second, roi, ignored, width)
        reference = extractor._difference(previous, current)
        actual = compare(previous, current)
        np.testing.assert_array_equal(actual["mask"], reference["mask"])
        for key in ("changed", "area", "raw_pixels"):
            assert actual[key] == reference[key]
        assert actual["similarity"] == pytest.approx(reference["similarity"], abs=3e-10)
    assert len(compare.frames) == 8


@pytest.mark.parametrize("width", [320, 640])
def test_component_bounds_match_cleaned_pixel_bounds(width):
    random = np.random.default_rng(15)
    current = {"valid": np.ones((width // 2, width), bool),
               "gray": np.zeros((width // 2, width), np.uint8)}
    for index in range(12):
        mask = (random.random(current["valid"].shape) < index / 100).astype(np.uint8)
        if index % 2:
            mask[10:12, 15:40] = 1
            mask[30:33, 55:61] = 1
        actual = extractor._summarize_difference(current, mask, 100, 0.9)
        count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        keep = np.zeros(count, np.uint8)
        keep[1:] = stats[1:, cv2.CC_STAT_AREA] >= extractor.MIN_COMPONENT_PIXELS
        cleaned = keep[labels] > 0
        ys, xs = np.nonzero(cleaned)
        pixels = int(cleaned.sum())
        span = max(xs.max() - xs.min() + 1, ys.max() - ys.min() + 1) if pixels else 0
        area = pixels / current["valid"].size
        threshold = extractor.MIN_CHANGED_AREA * min(1, (extractor.FEATURE_WIDTH / width) ** 2)
        expected = pixels >= extractor.MIN_CHANGE_PIXELS and span >= extractor.MIN_CHANGE_SPAN and area >= threshold
        np.testing.assert_array_equal(actual["mask"], cleaned)
        assert actual["area"] == area
        assert actual["changed"] == expected


def test_parallel_decode_and_cached_cpu_keep_full_extraction_signature(tmp_path):
    source = tmp_path / "formula.mkv"
    random = np.random.default_rng(8)
    scenes = []
    for index in range(3):
        image = np.full((180, 320, 3), 245, np.uint8)
        cv2.putText(image, f"x = {index}", (20, 90), cv2.FONT_HERSHEY_SIMPLEX, 1, (20, 20, 20), 2)
        image = np.clip(image.astype(np.int16) + random.integers(-2, 3, image.shape), 0, 255).astype(np.uint8)
        scenes.append((image, 4))
    make_slide_video(source, scenes)
    original = extractor.extract_slides(
        source, tmp_path / "reference", "a" * 64, compare=extractor._difference,
    )
    actual = extractor.extract_slides(source, tmp_path / "cached", "a" * 64, prefetch=True)
    names = ("start_ms", "end_ms", "representative_ms", "visible_from_ms", "visible_until_ms",
             "page_start_ms", "page_end_ms", "image_hash", "retention_class",
             "is_llm_preferred", "information_score")
    assert [[getattr(slide, name) for name in names] for slide in actual.slides] == [
        [getattr(slide, name) for name in names] for slide in original.slides
    ]


def test_cuda_reuses_unchanged_graph_inputs_and_releases_cached_frames(monkeypatch):
    torch = pytest.importorskip("torch")
    if not torch.cuda.is_available():
        pytest.skip("Optional PPT CUDA environment is unavailable")
    from classcue.slide_cuda import CudaDifference

    roi = extractor.SlideRoi(0, 0, 32, 24)
    image = np.full((24, 32, 3), 245, np.uint8)
    previous = extractor._feature(image, roi, (), 32)
    image[8:12, 10:25] = 20
    current = extractor._feature(image, roi, (), 32)
    compare = CudaDifference()
    try:
        expected = extractor._difference(previous, current)
        first = compare(previous, current)
        np.testing.assert_array_equal(first["mask"], expected["mask"])
        assert first["similarity"] == pytest.approx(expected["similarity"], abs=3e-10)
        copies = []
        original_copy = torch.Tensor.copy_

        def record_copy(target, *args, **kwargs):
            copies.append(True)
            return original_copy(target, *args, **kwargs)

        monkeypatch.setattr(torch.Tensor, "copy_", record_copy)
        compare(previous, current)
        assert not copies
        compare(previous, dict(current))
        assert len(copies) == 5
        for _ in range(12):
            compare(previous, dict(current))
        assert len(compare.frames) == 8
        compare.clear_frames()
        assert not compare.frames
        assert not compare.indices
        assert all(all(value is None for value in entry[-1]) for entry in compare.graphs.values())
        copies.clear()
        repeated = compare(previous, current)
        assert len(copies) == 10
        np.testing.assert_array_equal(repeated["mask"], expected["mask"])
    finally:
        compare.clear_frames()
