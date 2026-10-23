"""One bounded visual analysis request, followed by deterministic target validation."""

from __future__ import annotations

import json

from classcue.bilibili_store import encode
from classcue.bilibili_study import StudyModel
from classcue.processing_control import ProcessingCancelled
from classcue.question_analysis import (
    MAX_IMAGE_BYTES, QuestionAnalysis, QuestionImage, SYSTEM, allowed_sources, to_target,
)


def load_slide_images(store, snapshot: dict, slide_root) -> list[QuestionImage]:
    root = slide_root.resolve()
    result = []
    for item in snapshot["question"]["images"]:
        row = store.get_slide(item["slide_id"])
        if item.get("missing") or row is None or row["video_id"] != snapshot["classroom"]["marker"]["video_id"]:
            raise ValueError("所选 PPT 已不存在，请重新选择")
        path = (root / row["image_path"]).resolve()
        if not path.is_relative_to(root):
            raise ValueError("PPT 图片路径无效")
        try:
            with path.open("rb") as source:
                data = source.read(MAX_IMAGE_BYTES + 1)
        except OSError:
            raise ValueError("PPT 图片文件不可用，请重新提取或选择") from None
        image = QuestionImage.from_bytes(item["source"], data)
        if image.sha256 != item["image_hash"]:
            raise ValueError("PPT 图片内容已变化，请重新提取后分析")
        result.append(image)
    return result


class QuestionModel(StudyModel):
    def analyze(self, snapshot, images) -> QuestionAnalysis:
        expected = {item["source"]: item["image_hash"] for item in snapshot["question"]["images"]}
        if len(images) != len(expected) or {item.source: item.sha256 for item in images} != expected:
            raise ValueError("实际题图与分析快照不一致")
        prompt = ("请分析题目并生成核验要求。返回符合以下 JSON Schema 的对象：\n"
                  + encode(QuestionAnalysis.model_json_schema()) + "\n可引用的来源编号：\n"
                  + encode(sorted(allowed_sources(snapshot))) + "\n待分析材料：\n" + encode(snapshot))
        content = [{"type": "text", "text": prompt}]
        for image in images:
            content.extend(image.content())
        try:
            raw = self.adapter._request_content(content if images else prompt, SYSTEM, self.adapter.profile.max_tokens)
            return QuestionAnalysis.model_validate(json.loads(raw))
        except (ValueError, TypeError):
            raise ValueError("AI 未返回有效的题目分析，请检查材料后重试") from None
        except Exception:
            raise RuntimeError("题目分析请求失败，请检查所选服务和模型是否支持图像输入") from None


def analyze_question(repo, run, model, images, *, update=lambda *_: None, should_cancel=lambda: False):
    def checkpoint():
        if should_cancel():
            raise ProcessingCancelled("题目分析已停止")

    try:
        checkpoint()
        run.update(status="analyzing")
        repo.save_run(run)
        update(.15, "正在理解题目并建立核验要求", "question_analysis")
        result = QuestionAnalysis.model_validate(model.analyze(run["input"], images))
        target = to_target(result, run["input"], run["id"])
        # Keep the completed response even when cancellation arrived during the request.
        run.update(status="completed", analysis=result.model_dump(),
                   target=target.model_dump() if target else None)
        repo.save_run(run)
        checkpoint()
        update(1, "题目分析已保存" if target else "题目信息不足，请补充材料", "completed")
        return {"analysis_id": run["id"], "ready": target is not None}
    except ProcessingCancelled:
        if run["status"] != "completed":
            run.update(status="interrupted", error="题目分析已停止")
            repo.save_run(run)
        raise
    except Exception:
        run.update(status="failed", error="题目分析未完成，请检查材料与模型连接后重试")
        repo.save_run(run)
        raise RuntimeError(run["error"]) from None
