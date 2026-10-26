"""Marker-scoped question analysis endpoints; browsing history never calls AI."""

from typing import Literal
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from classcue.ai_provider import load_provider_settings, resolve_interface_key
from classcue.question_analysis import MAX_IMAGE_BYTES, QuestionMaterial
from classcue.question_captures import QuestionCaptures
from classcue.question_service import QuestionModel, analyze_question, load_slide_images
from classcue.question_store import QuestionStore
from classcue import question_visuals
from classcue.question_visuals import (
    QuestionVisualStore, VisualQuestionModel, question_marker, source_snapshot, verify_one,
)


class VisualBindingPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slide_id: int | None = Field(strict=True, gt=0)


class CaptureBindingPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capture_id: str | None = Field(pattern=r"^[0-9a-f]{32}$")
    binding_mode: Literal["manual", "auto"] = "manual"


def router(provider, jobs, slide_root):
    routes = APIRouter()

    def repository():
        return QuestionStore(provider.get())

    def idle():
        if jobs.is_running():
            raise HTTPException(status_code=409, detail="已有任务正在运行，请等待或停止后再操作")

    @routes.get("/api/videos/{video_id}/question-visuals")
    def visual_states(video_id: int):
        store = provider.get()
        if store.get_video(video_id) is None:
            raise HTTPException(status_code=404, detail="视频不存在")
        repo = QuestionVisualStore(store)
        active = jobs.is_running()
        markers = store.list_markers(video_id, "question_extraction")
        slides = store.list_slides(video_id, include_auxiliary=True) if markers else []
        segments = store.get_segments(video_id) if markers else []
        items = []
        for marker in markers:
            value = repo.projection(dict(marker), active=active,
                                   snapshot=source_snapshot(store, marker, slides=slides, segments=segments))
            items.append({key: value.get(key) for key in (
                "marker_id", "question_id", "status", "verdict", "binding_mode", "binding", "stale", "reason",
                "algorithm",
            )})
        return {"items": items, "active": active}

    @routes.get("/api/markers/{marker_id}/question/visual")
    def visual_state(marker_id: int):
        repo = QuestionVisualStore(provider.get())
        try:
            marker = question_marker(repo.store, marker_id)
            snapshot = source_snapshot(repo.store, marker)
            return {**repo.projection(marker, active=jobs.is_running(), snapshot=snapshot),
                    "location": snapshot["location"], "search_ranges": snapshot["search_ranges"],
                    "candidates": [{**row, "video_id": marker["video_id"]}
                                   for row in snapshot["slides"]]}
        except KeyError:
            raise HTTPException(status_code=404, detail="题目不存在") from None
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from None

    @routes.put("/api/markers/{marker_id}/question/visual/binding")
    def visual_binding(marker_id: int, payload: VisualBindingPayload):
        with jobs._lock:
            idle()
            repo = QuestionVisualStore(provider.get())
            try:
                repo.manual_binding(marker_id, payload.slide_id, slide_root())
                return visual_state(marker_id)
            except KeyError:
                raise HTTPException(status_code=404, detail="题目不存在") from None
            except (OSError, ValueError):
                raise HTTPException(status_code=400, detail="题图不可用，请重新读取附近 PPT 后选择") from None

    @routes.post("/api/videos/{video_id}/question-images/assign")
    def assign_images(video_id: int):
        with jobs._lock:
            idle()
            store = provider.get()
            if store.get_video(video_id) is None:
                raise HTTPException(status_code=404, detail="视频不存在")
            summary = question_visuals.assign_after_extraction(store, video_id, slide_root=slide_root())
            return {**visual_states(video_id), "summary": summary}

    @routes.post("/api/markers/{marker_id}/question/visual/auto")
    def assign_image(marker_id: int):
        with jobs._lock:
            idle()
            try:
                return question_visuals.assign_question_image(provider.get(), marker_id, slide_root(), force=True)
            except KeyError:
                raise HTTPException(status_code=404, detail="题目不存在") from None
            except (OSError, ValueError):
                raise HTTPException(status_code=400, detail="自动配图失败，原题图保留，请检查 PPT 图片") from None

    @routes.put("/api/markers/{marker_id}/question/visual/capture-binding")
    def capture_binding(marker_id: int, payload: CaptureBindingPayload):
        with jobs._lock:
            idle()
            try:
                return QuestionVisualStore(provider.get()).capture_binding(
                    marker_id, payload.capture_id, binding_mode=payload.binding_mode)
            except KeyError:
                raise HTTPException(status_code=404, detail="题目或截图不存在") from None
            except ValueError as error:
                raise HTTPException(status_code=400, detail=str(error)) from None

    @routes.post("/api/markers/{marker_id}/question/visual/verify", status_code=202)
    def visual_verify(marker_id: int):
        if not question_visuals.LLM_VISUAL_REVIEW_ENABLED:
            raise HTTPException(status_code=503, detail=question_visuals.LLM_VISUAL_REVIEW_DISABLED_REASON)
        with jobs._lock:
            idle()
            repo = QuestionVisualStore(provider.get())
            try:
                marker = question_marker(repo.store, marker_id)
                snapshot = source_snapshot(repo.store, marker)
                if not snapshot["slides"]:
                    raise ValueError("请先提取当前视频的 PPT")
            except KeyError:
                raise HTTPException(status_code=404, detail="题目不存在") from None
            except ValueError as error:
                raise HTTPException(status_code=400, detail=str(error)) from None
            settings = load_provider_settings()
            profile = settings.active_profile.model_copy(deep=True)
            key = resolve_interface_key(profile, settings)
            try:
                profile.require_ready(key)
            except ValueError:
                raise HTTPException(status_code=400, detail="请先配置支持识图的 AI 服务") from None
            root = slide_root()

            def operation(update, should_cancel):
                update(0, "正在核验题图", "question_visual")
                result = verify_one(repo, marker_id, root, lambda: VisualQuestionModel(profile, key),
                                    profile.model_dump(exclude={"credential_id"}),
                                    force=True, should_cancel=should_cancel)
                if result["status"] == "failed":
                    raise ValueError(result["reason"])
                return {"marker_id": marker_id, "status": result["status"], "verdict": result["verdict"]}

            try:
                job_id = jobs.submit("question_visual", marker["video_id"], operation,
                                     request={"marker_id": marker_id})
            except RuntimeError:
                raise HTTPException(status_code=409, detail="已有任务正在运行，请稍后重试") from None
            return {"job_id": job_id}

    @routes.get("/api/markers/{marker_id}/question")
    def state(marker_id: int):
        repo = repository()
        try:
            material = repo.material(marker_id)
            captures = QuestionCaptures(repo.store)
            # The deferred cleanup must not race recycle-bin undo, which
            # restores capture rows and files under the same lock.
            with jobs._lock:
                captures.collect()
            return {"material": material, "runs": repo.runs(marker_id), "captures": captures.list(marker_id)}
        except KeyError:
            raise HTTPException(status_code=404, detail="考点不存在") from None

    @routes.put("/api/markers/{marker_id}/question")
    def save_material(marker_id: int, payload: QuestionMaterial):
        # Marker validation and persistence stay in one critical section with
        # video deletion; otherwise the insert can hit a deleted marker.
        with jobs._lock:
            idle()
            try:
                return repository().save_material(marker_id, payload)
            except KeyError:
                raise HTTPException(status_code=404, detail="考点不存在") from None
            except ValueError:
                raise HTTPException(status_code=400, detail="所选题图不存在或不属于当前考点与课堂") from None

    @routes.post("/api/markers/{marker_id}/question/captures", status_code=201)
    async def upload_capture(marker_id: int, request: Request, captured_at_ms: int = Query(ge=0, le=9007199254740991),
                             as_question_image: bool = False):
        idle()
        if request.headers.get("content-type", "").split(";")[0] not in {"image/jpeg", "image/png", "image/webp"}:
            raise HTTPException(status_code=415, detail="截图仅支持 JPEG、PNG 或 WebP")
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > MAX_IMAGE_BYTES:
                raise HTTPException(status_code=413, detail="截图超过 6 MB")
            data.extend(chunk)

        def persist():
            # The bounded body is received outside the lock; the final idle
            # check and the capture write share the delete/undo critical section.
            with jobs._lock:
                idle()
                store = provider.get()
                repo = QuestionVisualStore(store)
                if as_question_image:
                    question_marker(store, marker_id)
                previous = repo.get(marker_id) or {}
                capture = QuestionCaptures(store).add(marker_id, captured_at_ms, bytes(data))
                if not as_question_image:
                    return capture
                value = repo.capture_binding(marker_id, capture["id"])
                return {**capture, "visual": value, "previous_image": {
                    "capture_id": (previous.get("binding") or {}).get("capture_id"),
                    "binding_mode": previous.get("binding_mode", "auto"),
                }}

        try:
            return await run_in_threadpool(persist)
        except KeyError:
            raise HTTPException(status_code=404, detail="考点不存在") from None
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from None

    @routes.get("/api/markers/{marker_id}/question/captures/{capture_id}")
    def capture_image(marker_id: int, capture_id: str):
        captures = QuestionCaptures(provider.get())
        try:
            row = captures.get(marker_id, capture_id)
            path = captures.path(capture_id)
            if not path.is_file():
                raise KeyError(capture_id)
        except (KeyError, ValueError):
            raise HTTPException(status_code=404, detail="截图不可用") from None
        return FileResponse(path, media_type=row["mime_type"], headers={"Cache-Control": "private, no-store"})

    @routes.delete("/api/markers/{marker_id}/question/captures/{capture_id}")
    def delete_capture(marker_id: int, capture_id: str):
        with jobs._lock:
            idle()
            try:
                QuestionCaptures(provider.get()).delete(marker_id, capture_id)
            except KeyError:
                raise HTTPException(status_code=404, detail="截图不存在或不属于当前考点") from None
        return {"deleted": True}

    @routes.post("/api/markers/{marker_id}/question/analyze", status_code=202)
    def analyze(marker_id: int):
        # Snapshot, run creation and submit share the delete/undo lock so the
        # analysis row and its job cannot reference a deleted marker or video.
        with jobs._lock:
            idle()
            repo = repository()
            try:
                snapshot = repo.snapshot(marker_id)
                images = load_slide_images(repo.store, snapshot, slide_root())
            except KeyError:
                raise HTTPException(status_code=404, detail="考点不存在") from None
            except ValueError as error:
                raise HTTPException(status_code=400, detail=str(error)) from None
            settings = load_provider_settings()
            profile = settings.active_profile.model_copy(deep=True)
            key = resolve_interface_key(profile, settings)
            try:
                profile.require_ready(key)
            except ValueError:
                raise HTTPException(status_code=400, detail="请先配置可用的 AI 服务") from None
            run = repo.create_run(snapshot, profile.model_dump(exclude={"credential_id"}))

            def operation(update, should_cancel):
                with QuestionModel(profile, key) as model:
                    return analyze_question(repo, run, model, images, update=update, should_cancel=should_cancel)

            try:
                jobs.submit("question_analysis", snapshot["classroom"]["marker"]["video_id"], operation,
                            request={"question_analysis_id": run["id"]}, job_id=run["job_id"])
            except RuntimeError:
                run.update(status="failed", error="任务未能启动，请稍后重试")
                repo.save_run(run)
                raise HTTPException(status_code=409, detail=run["error"]) from None
            return {"analysis_id": run["id"], "job_id": run["job_id"]}

    return routes
