"""Build immutable lecture excerpts from saved subtitles and preferred slides."""

from __future__ import annotations

import base64
import hashlib
from io import BytesIO
from pathlib import Path

from classcue.dsh_bridge import encode
from classcue.dsh_pipe import BridgeFailure
from classcue.study_courses import StudyCourseStore


def timestamp(milliseconds: int, *, srt: bool = False) -> str:
    seconds, fraction = divmod(milliseconds, 1000)
    value = f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}"
    return f"{value},{fraction:03}" if srt else f"{value}.{fraction:03}"


def preview_clip(store, video_id: int, start_ms: int, end_ms: int) -> dict:
    row = store.get_video(video_id)
    if row is None:
        raise KeyError(video_id)
    video = dict(row)
    if not 0 <= start_ms < end_ms <= video["duration_ms"]:
        raise BridgeFailure("invalid_clip_range", "请选择视频时长内的起止时间，结束时间须晚于开始时间")
    segments = [segment for segment in store.get_segments(video_id)
                if segment.start_ms < end_ms and segment.end_ms > start_ms and segment.text.strip()]
    if not segments:
        raise BridgeFailure("empty_clip", "所选范围没有已保存的字幕，请调整时间或先完成转写")
    # Keep complete boundary sentences and their original video timestamps.
    subtitles = [{"id": item.id, "start_ms": item.start_ms, "end_ms": item.end_ms, "text": item.text}
                 for item in segments]
    srt = "\n\n".join(f"{index}\n{timestamp(item.start_ms, srt=True)} --> "
                      f"{timestamp(item.end_ms, srt=True)}\n{item.text}"
                      for index, item in enumerate(segments, 1)) + "\n"
    if len(srt.encode("utf-8")) > 512 * 1024:
        raise BridgeFailure("clip_too_large", "字幕内容过多，请缩小片段范围")
    images = {}
    for item in store.list_slides(video_id, start_ms=start_ms, end_ms=end_ms, include_auxiliary=True):
        slide = dict(item)
        # A preferred final animation state must actually be visible in the excerpt.
        if not slide["is_llm_preferred"] or not (
            slide["visible_from_ms"] < end_ms and slide["visible_until_ms"] > start_ms
        ):
            continue
        image = images.setdefault(slide["image_hash"], {
            "slide_id": slide["id"], "image_hash": slide["image_hash"],
            "image_url": f"/api/videos/{video_id}/slides/{slide['id']}/image", "time_instances": [],
        })
        image["time_instances"].append({key: slide[key] for key in
            ("start_ms", "end_ms", "representative_ms", "visible_from_ms", "visible_until_ms")})
    title = video["display_name"] or video["filename"]
    value = {"video_id": video_id, "video_title": title, "start_ms": start_ms, "end_ms": end_ms,
             "subtitles": subtitles, "subtitle_srt": srt, "images": list(images.values())}
    value["fingerprint"] = hashlib.sha256(encode({
        **value, "video_hash": video["content_hash"],
    }).encode("utf-8")).hexdigest()
    return value


def clip_snapshot(store, library_id, video_id, start_ms, end_ms, *, slide_root: Path,
                  fingerprint: str, slide_ids: list[int]) -> dict:
    from PIL import Image

    preview = preview_clip(store, video_id, start_ms, end_ms)
    if preview["fingerprint"] != fingerprint:
        raise BridgeFailure("clip_changed", "字幕或 PPT 已更新，请重新预览后再准备草稿")
    if len(slide_ids) != len(set(slide_ids)) or len(slide_ids) > 23:
        raise BridgeFailure("clip_too_large", "一次最多附上 23 张精选 PPT，请减少所选图片")
    available = {image["slide_id"]: image for image in preview["images"]}
    if not set(slide_ids) <= available.keys():
        raise BridgeFailure("invalid_clip_slides", "所选 PPT 不属于当前片段，请重新预览")
    selected = [image for image in preview["images"] if image["slide_id"] in slide_ids]
    root = slide_root.resolve()
    content_images, metadata, total = [], [], len(preview["subtitle_srt"].encode("utf-8"))
    for index, image in enumerate(selected, 1):
        row = store.get_slide(image["slide_id"])
        if row is None or row["video_id"] != video_id or row["image_hash"] != image["image_hash"]:
            raise BridgeFailure("clip_changed", "PPT 已更新，请重新预览后再准备草稿")
        slide = dict(row)
        path = (root / slide["image_path"]).resolve()
        if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
            raise BridgeFailure("clip_image_unavailable", "精选 PPT 图片缺失或路径无效，请重新提取")
        data = path.read_bytes()
        total += len(data)
        if total > 20 * 1024 * 1024:
            raise BridgeFailure("clip_too_large", "片段资料超过 20 MB，请减少所选图片或缩小时间范围")
        if hashlib.sha256(data).hexdigest() != image["image_hash"]:
            raise BridgeFailure("clip_image_changed", "精选 PPT 图片内容已变化，请重新提取后再预览")
        with Image.open(BytesIO(data)) as picture:
            mime = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}.get(picture.format)
            picture.verify()
        if not mime:
            raise BridgeFailure("clip_image_unavailable", "精选 PPT 图片格式不可用，请重新提取")
        content_images.append({"type": "image", "media_type": mime,
                               "data": base64.b64encode(data).decode("ascii")})
        metadata.append({"attachment": index, **image})
    video = dict(store.get_video(video_id))
    course = StudyCourseStore(store).for_folder(video["library_folder_id"])
    # Root-level videos can be discussed without changing their library ownership.
    course_id = course["id"] if course else f"unfiled-{library_id}"
    course_name = course["name"] if course else "视频片段"
    interval = f"{timestamp(start_ms)} - {timestamp(end_ms)}"
    lines = [
        "请结合下面教师的原话和所附 PPT，讲解这段课堂内容。先说明在讲什么，再逐步解释"
        "概念、公式与推导，并指出教师语句与 PPT 的对应关系。引用原视频时间；"
        "字幕可能有转写错误，材料不清楚或不足时请明确说明，不要补造教师说过的话。",
        f"视频：{preview['video_title']}\n选定范围（原视频时间）：{interval}",
    ]
    lines.append("教师字幕（SRT，保留原视频时间；边界处保留完整字幕句）：\n"
                 + preview["subtitle_srt"])
    if metadata:
        descriptions = []
        for image in metadata:
            instances = "；".join(
                f"画面可见 {timestamp(item['visible_from_ms'])} - {timestamp(item['visible_until_ms'])}"
                f"，截图 {timestamp(item['representative_ms'])}" for item in image["time_instances"])
            descriptions.append(f"图片 {image['attachment']}：{instances}")
        lines.append("精选 PPT（与附件顺序对应，同图多次出现只附一次）：\n" + "\n".join(descriptions))
    else:
        lines.append("本次未附 PPT 图片，请仅依据字幕讲解，不推测未提供的画面。")
    text = "\n\n".join(lines)
    if total - len(preview["subtitle_srt"].encode("utf-8")) + len(text.encode("utf-8")) > 20 * 1024 * 1024:
        raise BridgeFailure("clip_too_large", "片段资料超过 20 MB，请减少所选图片或缩小时间范围")
    identity = hashlib.sha256(encode({
        "library": library_id, "video_id": video_id, "video_hash": video["content_hash"],
        "start_ms": start_ms, "end_ms": end_ms, "subtitle_srt": preview["subtitle_srt"],
        "images": [{"image_hash": image["image_hash"], "time_instances": image["time_instances"]}
                   for image in selected],
    }).encode("utf-8")).hexdigest()
    return {
        "library_id": library_id, "course_id": course_id, "course_name": course_name,
        "document_id": f"video-{video_id}", "source_kind": "video_clip",
        "source": preview["video_title"], "video_id": video_id,
        "question_id": f"clip-{identity}", "content_version": 1, "title": interval,
        "start_ms": start_ms, "end_ms": end_ms, "subtitle_srt": preview["subtitle_srt"],
        "slides": metadata, "content": [{"type": "text", "text": text}, *content_images],
    }
