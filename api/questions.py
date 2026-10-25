"""Marker-scoped question analysis endpoints; browsing history never calls AI."""

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from classcue.ai_provider import load_provider_settings, resolve_interface_key
from classcue.question_analysis import MAX_IMAGE_BYTES, QuestionMaterial
from classcue.question_captures import QuestionCaptures
from classcue.question_service import QuestionModel, analyze_question, load_slide_images
from classcue.question_store import QuestionStore


def router(provider, jobs, slide_root):
    routes = APIRouter()

    def repository():
        return QuestionStore(provider.get())

    def idle():
        if jobs.is_running():
            raise HTTPException(status_code=409, detail="已有任务正在运行，请等待或停止后再操作")

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
    async def upload_capture(marker_id: int, request: Request, captured_at_ms: int = Query(ge=0, le=9007199254740991)):
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
                return QuestionCaptures(provider.get()).add(marker_id, captured_at_ms, bytes(data))

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
