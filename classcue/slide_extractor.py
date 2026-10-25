"""Extract a compact, time-addressable collection of complete slide images."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from typing import Callable

import cv2
import numpy as np
from skimage.metrics import structural_similarity

from classcue.processing_control import ProcessingCancelled


SLIDE_EXTRACTION_VERSION = "slide-v4-overlay-aware-preferred"
SAMPLE_INTERVAL_MS = 500
STABLE_WINDOW_MS = 1500
REPRESENTATIVE_TAIL_MS = 500
PRIMARY_MIN_DURATION_MS = 10_000
AUXILIARY_MIN_DURATION_MS = 2_000
MAX_IMAGE_EDGE = 2560
FEATURE_WIDTH = 320
REFINE_WIDTH = 640
PIXEL_THRESHOLD = 18
COLOR_CHANGE_THRESHOLD = 48
STRUCTURE_THRESHOLD = 0.94
MIN_CHANGE_PIXELS = 16
MIN_COMPONENT_PIXELS = 3
MIN_CHANGE_SPAN = 10
MIN_CHANGED_AREA = 0.0008
PAGE_CHANGE_AREA = 0.35
PAGE_MIN_SIMILARITY = 0.55
EDGE_LOW = 50
EDGE_HIGH = 120
COVERAGE_LOSS_RATIO = 0.002
COVERAGE_PIXEL_THRESHOLD = 28
SELECTION_LOSS_RATIO = 0.02
SELECTION_COLOR_THRESHOLD = 40
SELECTION_COMPONENT_PIXELS = 10
CAPTION_BAND_START = 0.8
CAPTION_CORE_CHANGE_RATIO = 0.004
CAPTION_MIN_REPLACEMENTS = 3

ProgressCallback = Callable[[float, str, str | None], None]


@dataclass(frozen=True)
class SlideRoi:
    x: int = 0
    y: int = 0
    width: int | None = None
    height: int | None = None

    def normalize(self, frame_width: int, frame_height: int) -> "SlideRoi":
        x = max(0, min(int(self.x), frame_width - 1))
        y = max(0, min(int(self.y), frame_height - 1))
        width = frame_width - x if self.width is None else int(self.width)
        height = frame_height - y if self.height is None else int(self.height)
        return SlideRoi(x, y, max(1, min(width, frame_width - x)),
                        max(1, min(height, frame_height - y)))

    def as_dict(self) -> dict[str, int]:
        return dict(x=self.x, y=self.y, width=int(self.width or 0), height=int(self.height or 0))


@dataclass
class SlideDraft:
    start_ms: int
    end_ms: int
    representative_ms: int
    image_path: Path
    image_hash: str
    duration_ms: int
    retention_class: str
    confidence: float
    roi: SlideRoi
    visible_from_ms: int
    visible_until_ms: int
    page_start_ms: int
    page_end_ms: int
    is_llm_preferred: bool
    information_score: float


@dataclass
class SlideExtractionResult:
    source_content_hash: str
    duration_ms: int
    roi: SlideRoi
    slides: list[SlideDraft]
    ignore_regions: tuple[SlideRoi, ...] = ()
    cache_identity: str = ""
    extraction_version: str = SLIDE_EXTRACTION_VERSION


def _slide_row_payload(result: SlideExtractionResult, slide: SlideDraft, image_path: str) -> dict:
    return {
        "start_ms": slide.start_ms, "end_ms": slide.end_ms,
        "representative_ms": slide.representative_ms,
        "visible_from_ms": slide.visible_from_ms, "visible_until_ms": slide.visible_until_ms,
        "page_start_ms": slide.page_start_ms, "page_end_ms": slide.page_end_ms,
        "image_path": image_path, "image_hash": slide.image_hash,
        "duration_ms": slide.duration_ms, "retention_class": slide.retention_class,
        "confidence": slide.confidence,
        "is_llm_preferred": slide.is_llm_preferred,
        "information_score": slide.information_score,
        "roi_json": json.dumps(result.roi.as_dict(), sort_keys=True),
        "ignore_regions_json": json.dumps([r.as_dict() for r in result.ignore_regions], sort_keys=True),
        "extraction_version": result.extraction_version, "cache_identity": result.cache_identity,
    }


def _parameters() -> dict:
    return {key: value for key, value in globals().items()
            if key.isupper() and isinstance(value, (int, float, str))}


def extraction_identity(
    source_content_hash: str, width: int, height: int, duration_ms: int, roi: SlideRoi,
    *, ignore_regions: tuple[SlideRoi, ...] = (), source_stat: tuple[int, int] = (0, 0),
) -> str:
    payload = dict(content_hash=source_content_hash, width=width, height=height,
                   duration_ms=duration_ms, roi=roi.as_dict(),
                   ignore_regions=[r.as_dict() for r in ignore_regions],
                   source_stat=source_stat, parameters=_parameters())
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _video_metadata(source: Path, roi: SlideRoi | None, ignore_regions: tuple[SlideRoi, ...]):
    import av

    with av.open(str(source)) as container:
        stream = next(iter(container.streams.video), None)
        if stream is None or not stream.width or not stream.height:
            raise ValueError("文件没有可读取的视频画面")
        width, height = int(stream.width), int(stream.height)
        duration = (round(container.duration / av.time_base * 1000)
                    if container.duration is not None else
                    round(stream.duration * stream.time_base * 1000)
                    if stream.duration is not None and stream.time_base is not None else 0)
    regions = tuple(sorted((r.normalize(width, height) for r in ignore_regions),
                           key=lambda r: (r.x, r.y, r.width, r.height)))
    return width, height, duration, (roi or SlideRoi()).normalize(width, height), regions


def _check_stop(should_stop: Callable[[], bool] | None) -> None:
    if should_stop and should_stop():
        raise ProcessingCancelled("用户已请求停止 PPT 提取")


def extract_and_store_slides(
    store, video_id: int, slide_root: Path, *, roi: SlideRoi | None = None,
    ignore_regions: tuple[SlideRoi, ...] = (), force: bool = False,
    progress: ProgressCallback | None = None, should_stop: Callable[[], bool] | None = None,
) -> dict:
    video = store.get_video(video_id)
    if video is None:
        raise ValueError("视频记录不存在")
    source = Path(str(video["video_path"])).expanduser().resolve()
    if not source.is_file():
        raise ValueError("原视频已被移动或删除")
    width, height, duration, region, ignored = _video_metadata(source, roi, ignore_regions)
    stat = source.stat()
    source_hash = str(video["content_hash"])
    identity = extraction_identity(source_hash, width, height, duration, region,
                                   ignore_regions=ignored, source_stat=(stat.st_size, stat.st_mtime_ns))
    old_rows = store.list_slides(video_id, include_auxiliary=True)
    if not force and old_rows and all(
        row["cache_identity"] == identity and (slide_root / row["image_path"]).is_file()
        for row in old_rows
    ):
        return dict(status="cached", slides=old_rows, count=len(old_rows))

    # Each generation owns its files, so a failed replacement cannot damage the published one.
    target = slide_root / source_hash
    target.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="generation-", dir=target))
    published = False
    try:
        result = extract_slides(source, staging, source_hash, roi=region, ignore_regions=ignored,
                                progress=progress, should_stop=should_stop)
        _check_stop(should_stop)
        if not result.slides:
            raise ValueError("未找到持续至少 2 秒的稳定 PPT 画面，已有截图未替换")
        after = source.stat()
        if (after.st_size, after.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
            raise ValueError("视频文件在提取过程中发生变化，请重试")
        result.cache_identity = identity
        rows = [_slide_row_payload(result, slide, slide.image_path.relative_to(slide_root).as_posix())
                for slide in result.slides]
        if progress:
            progress(0.99, "正在发布 PPT 图片和时间范围", "saving")
        _check_stop(should_stop)
        stored = store.replace_video_slides(video_id, rows)
        published = True
        new_paths = {row["image_path"] for row in rows}
        for raw in {row["image_path"] for row in old_rows} - new_paths:
            path = (slide_root / raw).resolve()
            if path.is_relative_to(slide_root.resolve()):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        if progress:
            try:
                progress(1.0, f"PPT 提取完成，保留 {len(rows)} 张代表截图", "completed")
            except ProcessingCancelled:
                # Publication has committed; cancellation can no longer undo it.
                pass
        return dict(status="completed", slides=stored, count=len(stored), committed=True)
    finally:
        if not published:
            shutil.rmtree(staging, ignore_errors=True)


def _frame_time_ms(frame, stream, fallback_ms: int) -> int:
    if frame.time is not None:
        return max(0, round(float(frame.time) * 1000))
    if frame.pts is not None and stream.time_base is not None:
        return max(0, round(float(frame.pts * stream.time_base) * 1000))
    return fallback_ms


def _feature(rgb: np.ndarray, roi: SlideRoi, ignored: tuple[SlideRoi, ...], width: int) -> dict:
    height, source_width = rgb.shape[:2]
    size = (max(8, min(width, source_width)), max(8, round(min(width, source_width) * height / source_width)))
    preview = cv2.resize(rgb, size, interpolation=cv2.INTER_AREA)
    valid = np.ones(preview.shape[:2], np.uint8)
    sx, sy = size[0] / source_width, size[1] / height
    for region in ignored:
        x0, y0 = max(roi.x, region.x), max(roi.y, region.y)
        x1 = min(roi.x + source_width, region.x + int(region.width))
        y1 = min(roi.y + height, region.y + int(region.height))
        if x1 > x0 and y1 > y0:
            valid[max(0, int((y0 - roi.y) * sy)):int(np.ceil((y1 - roi.y) * sy)),
                  max(0, int((x0 - roi.x) * sx)):int(np.ceil((x1 - roi.x) * sx))] = 0
    if not valid.any():
        raise ValueError("排除区域覆盖了整个 PPT 区域")
    preview[valid == 0] = 0
    gray = cv2.cvtColor(preview, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, EDGE_LOW, EDGE_HIGH)
    edges[cv2.erode(valid, np.ones((3, 3), np.uint8)) == 0] = 0
    return dict(rgb=preview, gray=gray, edges=edges > 0, valid=valid > 0)


def _difference(previous: dict, current: dict) -> dict:
    delta = np.max(cv2.absdiff(previous["rgb"], current["rgb"]), axis=2)
    _, similarity_map = structural_similarity(previous["gray"], current["gray"],
                                               data_range=255, full=True)
    mask = ((delta > PIXEL_THRESHOLD) & (
        (similarity_map < STRUCTURE_THRESHOLD) | (delta > COLOR_CHANGE_THRESHOLD)
    )).astype(np.uint8)
    mask &= current["valid"].astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    keep = np.zeros(count, np.uint8)
    if count > 1:
        keep[1:] = (stats[1:, cv2.CC_STAT_AREA] >= MIN_COMPONENT_PIXELS)
    cleaned = keep[labels] > 0
    pixels = int(cleaned.sum())
    ys, xs = np.nonzero(cleaned)
    span = int(max(xs.max() - xs.min() + 1, ys.max() - ys.min() + 1)) if pixels else 0
    area = pixels / max(int(current["valid"].sum()), 1)
    # Retain thin formula strokes; remove isolated speckles, not whole small regions.
    area_threshold = MIN_CHANGED_AREA * min(1.0, (FEATURE_WIDTH / current["gray"].shape[1]) ** 2)
    changed = pixels >= MIN_CHANGE_PIXELS and span >= MIN_CHANGE_SPAN and area >= area_threshold
    return dict(changed=changed, area=area, raw_pixels=int((delta > PIXEL_THRESHOLD).sum()),
                similarity=float(similarity_map[current["valid"]].mean()), mask=cleaned)


def _covers(complete: dict, partial: dict) -> bool:
    """Conservative visual containment, including colour and existing strokes."""
    edges = partial["edges"]
    count = int(edges.sum())
    delta = np.max(cv2.absdiff(complete["rgb"], partial["rgb"]), axis=2)
    if count < 8:
        complete_bg = np.median(complete["rgb"][complete["valid"]], axis=0)
        partial_bg = np.median(partial["rgb"][partial["valid"]], axis=0)
        return (np.max(np.abs(complete_bg - partial_bg)) < 8
                and float((delta[partial["valid"]] > PIXEL_THRESHOLD).mean()) < PAGE_CHANGE_AREA)
    nearby = cv2.dilate(complete["edges"].astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
    lost = edges & (~nearby | (delta > COVERAGE_PIXEL_THRESHOLD))
    if int(lost.sum()) > max(2, int(count * COVERAGE_LOSS_RATIO)):
        return False
    # A stroke touching an old formula can change a symbol rather than add a new line.
    old_nearby = cv2.dilate(edges.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
    added = complete["edges"] & ~edges
    touched = added & old_nearby & (delta > COVERAGE_PIXEL_THRESHOLD)
    return int(touched.sum()) <= max(2, int(count * COVERAGE_LOSS_RATIO))


def _same_page(previous: dict, current: dict) -> bool:
    difference = _difference(previous, current)
    if not difference["changed"]:
        return True
    if _covers(current, previous) or _covers(previous, current):
        return True
    return (difference["area"] < PAGE_CHANGE_AREA
            and difference["similarity"] >= PAGE_MIN_SIMILARITY)


def _encode_jpeg(rgb: np.ndarray) -> bytes:
    import av

    height, width = rgb.shape[:2]
    scale = min(1.0, MAX_IMAGE_EDGE / max(width, height))
    image = av.VideoFrame.from_ndarray(rgb, format="rgb24")
    if scale < 1:
        image = image.reformat(width=max(1, round(width * scale)), height=max(1, round(height * scale)))
    image = image.reformat(format="yuvj444p")
    image.pts, image.time_base = 0, Fraction(1, 25)
    encoder = av.CodecContext.create("mjpeg", "w")
    encoder.width, encoder.height, encoder.pix_fmt = image.width, image.height, "yuvj444p"
    encoder.qmin = encoder.qmax = 2
    encoder.time_base = Fraction(1, 25)
    return b"".join(bytes(packet) for packet in [*encoder.encode(image), *encoder.encode(None)])


@dataclass
class _Candidate:
    start_ms: int
    visible_from_ms: int
    visible_until_ms: int
    representative_ms: int
    feature: dict
    path: Path
    image_hash: str
    information_score: float


class _PageCollector:
    def __init__(self, directory: Path, roi: SlideRoi, ignored: tuple[SlideRoi, ...]):
        self.directory, self.roi, self.ignored = directory, roi, ignored
        self.candidates: list[_Candidate] = []
        self.slides: list[SlideDraft] = []
        self.page_start = 0
        self.previous: dict | None = None
        self.published_paths: set[Path] = set()

    def accept(self, start: int, end: int, representative: int, rgb: np.ndarray) -> None:
        feature = _feature(rgb, self.roi, self.ignored, REFINE_WIDTH)
        if self.previous is not None and not _same_page(self.previous, feature):
            self.flush(start)
        self.previous = feature
        if not self.candidates:
            self.page_start = start
        encoded = _encode_jpeg(rgb)
        image_hash = hashlib.sha256(encoded).hexdigest()
        path = self.directory / f"{image_hash}.jpg"
        if not path.exists():
            temporary = path.with_suffix(".tmp")
            temporary.write_bytes(encoded)
            temporary.replace(path)
        if self.candidates and self.candidates[-1].image_hash == image_hash:
            # A short overlay or cursor excursion can return to the exact same
            # stable image; keep one detailed state instead of duplicating it.
            self.candidates[-1].visible_until_ms = end
            self.previous = feature
            return
        self.candidates.append(_Candidate(
            start, start, end, representative, feature, path, image_hash,
            _information_score(feature),
        ))

    def flush(self, end: int) -> None:
        if not self.candidates:
            return
        primary = end - self.page_start >= PRIMARY_MIN_DURATION_MS
        preferred = _preferred_candidates(self.candidates)
        for index, candidate in enumerate(self.candidates):
            stop = self.candidates[index + 1].start_ms if index + 1 < len(self.candidates) else end
            is_preferred = index in preferred
            self.slides.append(SlideDraft(
                start_ms=candidate.start_ms, end_ms=stop,
                representative_ms=candidate.representative_ms, image_path=candidate.path,
                image_hash=candidate.image_hash, duration_ms=stop - candidate.start_ms,
                retention_class=("primary" if primary else "auxiliary") if is_preferred else "animation",
                confidence=(0.9 if primary else 0.7) if is_preferred else 0.65, roi=self.roi,
                visible_from_ms=candidate.visible_from_ms,
                visible_until_ms=min(candidate.visible_until_ms, stop),
                page_start_ms=self.page_start, page_end_ms=end,
                is_llm_preferred=is_preferred,
                information_score=candidate.information_score,
            ))
            self.published_paths.add(candidate.path)
        self.candidates.clear()
        self.previous = None


def _information_score(feature: dict) -> float:
    """Estimate stable visual information without OCR or semantic inference."""
    valid = feature["valid"]
    if not valid.any():
        return 0.0
    edge_density = float(feature["edges"][valid].mean())
    contrast = min(1.0, float(np.std(feature["gray"][valid])) / 96.0)
    return round(min(1.0, 0.7 * min(1.0, edge_density * 8.0) + 0.3 * contrast), 6)


def _preferred_candidates(candidates: list[_Candidate]) -> set[int]:
    """Select content representatives without deleting any time-addressable state."""
    if not candidates:
        return set()
    features = _selection_features([candidate.feature for candidate in candidates])
    # Select representatives directly, so approximate equality cannot discard
    # both members of a pair or collapse a chain with no surviving endpoint.
    order = sorted(range(len(candidates)), key=lambda i: (
        int(features[i]["edges"].sum()), candidates[i].information_score,
        candidates[i].visible_until_ms - candidates[i].visible_from_ms,
        candidates[i].start_ms,
    ), reverse=True)
    preferred: set[int] = set()
    for index in order:
        if any(_selection_covers(features[other], features[index]) for other in preferred):
            continue
        replaced = {other for other in preferred
                    if _selection_covers(features[index], features[other])}
        preferred.difference_update(replaced)
        preferred.add(index)
    return preferred


def _has_selection_detail(mask: np.ndarray) -> bool:
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), 8)
    if count <= 1:
        return False
    components = stats[1:]
    return bool(np.any(
        (components[:, cv2.CC_STAT_AREA] >= SELECTION_COMPONENT_PIXELS)
        & (np.maximum(components[:, cv2.CC_STAT_WIDTH],
                      components[:, cv2.CC_STAT_HEIGHT]) >= 5)
    ))


def _selection_covers(complete: dict, partial: dict) -> bool:
    """Tolerate recording noise, but retain coherent missing or modified strokes."""
    valid = complete["valid"] & partial["valid"]
    edges = partial["edges"] & valid
    count = int(edges.sum())
    if count < 8:
        return _covers(complete, partial)
    kernel = np.ones((3, 3), np.uint8)
    nearby = cv2.dilate(complete["edges"].astype(np.uint8), kernel) > 0
    # Blur suppresses JPEG/antialiasing noise without weakening geometric checks.
    complete_rgb = cv2.GaussianBlur(complete["rgb"], (3, 3), 0)
    partial_rgb = cv2.GaussianBlur(partial["rgb"], (3, 3), 0)
    delta = np.max(cv2.absdiff(complete_rgb, partial_rgb), axis=2)
    # Canny may toggle a faint compression/watermark edge even when the actual
    # pixels barely changed. Such an edge is not missing page information.
    lost = edges & ~nearby & (delta > PIXEL_THRESHOLD)
    recolored = edges & (delta > SELECTION_COLOR_THRESHOLD)
    missing = lost | recolored
    if int(missing.sum()) > max(3, int(count * SELECTION_LOSS_RATIO)):
        return False
    if _has_selection_detail(missing):
        return False
    # New strokes intersecting an old symbol may turn "-" into "+", not merely
    # complete an animation. Ignore subpixel edge jitter, not such modifications.
    old_nearby = cv2.dilate(edges.astype(np.uint8), kernel) > 0
    added = complete["edges"] & valid & ~old_nearby & (delta > PIXEL_THRESHOLD)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(added.astype(np.uint8), 8)
    near_old = cv2.dilate(edges.astype(np.uint8), np.ones((7, 7), np.uint8)) > 0
    for component in range(1, count):
        x, y, width, height, area = stats[component]
        if area < SELECTION_COMPONENT_PIXELS or max(width, height) < 5:
            continue
        region = labels[y:y + height, x:x + width] == component
        if np.any(region & near_old[y:y + height, x:x + width]):
            return False
    return True


def _selection_features(features: list[dict]) -> list[dict]:
    """Ignore recurrent footer replacements only within repeated-body clusters."""
    if len(features) <= CAPTION_MIN_REPLACEMENTS:
        return features
    height, width = features[0]["gray"].shape
    band_start = round(height * CAPTION_BAND_START)
    groups: list[list[int]] = []
    blurred = [cv2.GaussianBlur(feature["rgb"], (3, 3), 0) for feature in features]
    for index, feature in enumerate(features):
        for group in groups:
            anchor = group[0]
            valid = feature["valid"][:band_start] & features[anchor]["valid"][:band_start]
            delta = np.max(cv2.absdiff(blurred[index][:band_start],
                                      blurred[anchor][:band_start]), axis=2)
            if valid.any() and float((delta[valid] > PIXEL_THRESHOLD).mean()) <= CAPTION_CORE_CHANGE_RATIO:
                group.append(index)
                break
        else:
            groups.append([index])
    selected = list(features)
    kernel = np.ones((3, 3), np.uint8)
    replacements: list[tuple[list[int], list[np.ndarray]]] = []
    for group in groups:
        row_changes = []
        for previous, current in zip(group, group[1:]):
            before = features[previous]["edges"][band_start:]
            after = features[current]["edges"][band_start:]
            delta = np.max(cv2.absdiff(blurred[previous][band_start:],
                                      blurred[current][band_start:]), axis=2)
            changed = delta > PIXEL_THRESHOLD
            lost = before & ~(cv2.dilate(after.astype(np.uint8), kernel) > 0) & changed
            added = after & ~(cv2.dilate(before.astype(np.uint8), kernel) > 0) & changed
            # Require repeated replacement, not just lines accumulating at the
            # bottom of a slide. A single formula change must remain visible.
            if int(lost.sum()) < 12 or int(added.sum()) < 12:
                continue
            changes = lost | added
            xs = np.nonzero(changes)[1]
            if xs.size and int(xs.max() - xs.min()) >= width * 0.15:
                row_changes.append(changes.any(axis=1))
        if row_changes:
            replacements.append((group, row_changes))
    all_changes = [change for _, changes in replacements for change in changes]
    if len(all_changes) < CAPTION_MIN_REPLACEMENTS:
        return features
    recurring = np.sum(all_changes, axis=0) >= 2
    for group, changes in replacements:
        # A subtitle band learned from several body layouts also applies to a
        # page shown for only two spoken sentences, not only long-held pages.
        rows = np.any(changes, axis=0) & recurring
        if not rows.any():
            continue
        rows = cv2.dilate(rows.astype(np.uint8)[:, None], np.ones((5, 1), np.uint8))[:, 0] > 0
        for index in group:
            original = features[index]
            feature = {key: value.copy() for key, value in original.items()}
            for value in feature.values():
                value[band_start:][rows] = 0
            selected[index] = feature
    return selected


def extract_slides(
    source: Path, staging_directory: Path, source_content_hash: str, *,
    roi: SlideRoi | None = None, ignore_regions: tuple[SlideRoi, ...] = (),
    progress: ProgressCallback | None = None, should_stop: Callable[[], bool] | None = None,
) -> SlideExtractionResult:
    import av

    source = source.expanduser().resolve()
    staging_directory.mkdir(parents=True, exist_ok=True)
    _, _, duration, region, ignored = _video_metadata(source, roi, ignore_regions)
    collector = _PageCollector(staging_directory, region, ignored)
    anchor = None
    anchor_rgb = None
    refined_anchor = None
    run_start = last_ms = next_ms = 0
    tail_rgb = latest_rgb = None
    tail_ms = latest_ms = 0

    def finish_run(end: int) -> None:
        if latest_rgb is not None and end - run_start >= max(STABLE_WINDOW_MS, AUXILIARY_MIN_DURATION_MS):
            collector.accept(run_start, end, tail_ms, tail_rgb)

    with av.open(str(source)) as container:
        stream = container.streams.video[0]
        for frame in container.decode(stream):
            _check_stop(should_stop)
            timestamp = _frame_time_ms(frame, stream, last_ms + 1)
            last_ms = timestamp
            if timestamp < next_ms:
                continue
            next_ms = timestamp + SAMPLE_INTERVAL_MS
            rgb = frame.to_ndarray(format="rgb24")
            rgb = np.ascontiguousarray(rgb[region.y:region.y + int(region.height),
                                           region.x:region.x + int(region.width)])
            feature = _feature(rgb, region, ignored, FEATURE_WIDTH)
            change = _difference(anchor, feature) if anchor is not None else None
            if change and not change["changed"] and change["raw_pixels"] >= MIN_COMPONENT_PIXELS * 2:
                if refined_anchor is None:
                    refined_anchor = _feature(anchor_rgb, region, ignored, REFINE_WIDTH)
                change = _difference(refined_anchor, _feature(rgb, region, ignored, REFINE_WIDTH))
            if anchor is None or change["changed"]:
                finish_run(timestamp)
                anchor, run_start = feature, timestamp
                anchor_rgb, refined_anchor = rgb, None
                tail_rgb, tail_ms = rgb, timestamp
            elif latest_rgb is not None and timestamp - latest_ms >= REPRESENTATIVE_TAIL_MS:
                tail_rgb, tail_ms = latest_rgb, latest_ms
            latest_rgb, latest_ms = rgb, timestamp
            if progress and duration:
                progress(min(0.98, timestamp / duration),
                         f"正在筛选完整 PPT 画面（已读取 {timestamp // 1000} 秒）", "detecting")
    end = duration or max(last_ms + 1, latest_ms + SAMPLE_INTERVAL_MS)
    finish_run(end)
    collector.flush(end)
    used = {slide.image_path for slide in collector.slides}
    for path in staging_directory.glob("*.jpg"):
        if path not in used:
            path.unlink()
    return SlideExtractionResult(source_content_hash, end, region, collector.slides, ignored)
