"""Synthetic visuals only; never launch a browser or contact a model."""

import io
from copy import deepcopy

from PIL import Image, ImageDraw

from classcue.bilibili_store import digest
from classcue.question_analysis import QuestionImage


def synthetic_frame(time_ms=0):
    image = Image.new("RGB", (960, 540), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((30, 30, 930, 100), fill=(20, 90, 100))
    draw.text((60, 150), "SYNTHETIC EXERCISE - " + str(time_ms), fill="black")
    draw.rectangle((60, 220, 400 + time_ms % 400, 350), outline="red", width=4)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def localization(material):
    rows = material["segments"]
    return {"hits": [{"start_segment_id": rows[0]["id"], "end_segment_id": rows[-1]["id"],
                     "relevance": "likely", "kind": "example", "reason": "合成字幕中有运算迹象",
                     "focus_segment_ids": [rows[0]["id"]]}]}


def assessment(material, *, status="yes", followup=()):
    frame = material["frames"][0]["source"]
    subtitle = material["segments"][0]["id"]
    dimension = {"status": status, "reason": "合成图文依据", "frame_ids": [frame], "segment_ids": [subtitle]}
    return {key: deepcopy(dimension) for key in ("knowledge", "problem_type", "explanation")} | {
        "reason": "合成题型比较，不要求实例数值相同", "followup_segment_ids": list(followup),
    }


def light_assessment(material, *, status="yes", followup=()):
    return {"match": {"yes": "similar", "partial": "related", "no": "different", "unknown": "unclear"}[status],
            "explaining": "yes" if status == "yes" else "unclear", "reason": "合成题图比较",
            "frame_ids": [material["frames"][0]["source"]],
            "segment_ids": [material["segments"][0]["id"]], "followup_segment_ids": list(followup)}


class FakeFrameReader:
    def __init__(self, *_args, error=None, after_capture=lambda: None, **_kwargs):
        self.calls, self.error, self.after_capture = [], error, after_capture
        self.consume, self.checkpoint_callback = lambda *_: None, lambda: None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def capture(self, unit, requested_ms):
        self.checkpoint_callback()
        self.consume("browser_requests")
        self.consume("browser_bytes", 1024)
        self.calls.append((unit["id"], requested_ms))
        if self.error:
            raise self.error
        result = {"captured_ms": requested_ms, "data": synthetic_frame(requested_ms), "time_source": "decoded_frame"}
        self.after_capture()
        return result


class MemoryFrames:
    def __init__(self):
        self.saved = []
        self.images = {}

    def find(self, marker, run, unit, requested_ms):
        return next((row for row in self.saved if row["marker_id"] == marker and row["run_id"] == run
                     and row["unit_id"] == unit and row["requested_ms"] == requested_ms), None)

    def add(self, run, unit, requested_ms, captured_ms, data, time_source):
        marker = run["input"]["marker"]
        frame_id = digest([run["id"], unit["id"], requested_ms])[:32]
        image = QuestionImage.from_bytes("frame:" + frame_id, data)
        record = {"id": frame_id, "marker_id": marker["id"], "run_id": run["id"], "unit_id": unit["id"],
                  **{key: unit[key] for key in ("bvid", "cid", "page")},
                  "requested_ms": requested_ms, "captured_ms": captured_ms, "time_source": time_source,
                  "image_hash": image.sha256, "width": image.width, "height": image.height}
        self.saved.append(record)
        self.images[frame_id] = image
        return record

    def image(self, record):
        return self.images[record["id"]]
