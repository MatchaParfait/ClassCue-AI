"""Marker-scoped question analysis endpoints; browsing history never calls AI."""

from fastapi import APIRouter, HTTPException

from classcue.ai_provider import load_provider_settings, resolve_interface_key
from classcue.question_analysis import QuestionMaterial
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
            return {"material": repo.material(marker_id), "runs": repo.runs(marker_id)}
        except KeyError:
            raise HTTPException(status_code=404, detail="考点不存在") from None

    @routes.put("/api/markers/{marker_id}/question")
    def save_material(marker_id: int, payload: QuestionMaterial):
        idle()
        try:
            return repository().save_material(marker_id, payload)
        except KeyError:
            raise HTTPException(status_code=404, detail="考点不存在") from None
        except ValueError:
            raise HTTPException(status_code=400, detail="所选 PPT 不存在或不属于当前课堂") from None

    @routes.post("/api/markers/{marker_id}/question/analyze", status_code=202)
    def analyze(marker_id: int):
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
