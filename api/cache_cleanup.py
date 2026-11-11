"""Local cache maintenance shares the application's single background worker."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

from classcue.cache_cleanup import AUTOMATIC, CacheCleanup


class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    interval_days: int = Field(default=7, ge=1, le=30, strict=True)
    retention_days: int = Field(default=30, ge=1, le=365, strict=True)
    preview_limit_mb: int = Field(default=512, ge=64, le=10240, strict=True)
    categories: list[Literal["transcription", "previews", "logs", "updates"]] = Field(
        default_factory=lambda: list(AUTOMATIC), max_length=4)


class CleanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["expired", "all"] = "expired"
    categories: list[Literal["transcription", "previews", "logs", "updates", "downloads", "ai", "backups"]] = Field(
        default_factory=lambda: list(AUTOMATIC), min_length=1, max_length=7)


def router(project, provider, jobs, update_center=None, *, on_slide_cleanup=None):
    routes = APIRouter(prefix="/api/cache")

    def manager():
        return CacheCleanup(project, provider.get(), update_center=update_center)

    def start(categories, mode="expired", automatic=False):
        with jobs._lock:
            if jobs.is_running() or provider.get().list_paused_jobs():
                raise RuntimeError("请先完成或停止运行及暂停中的任务，再清理缓存")

            def operation(_update, should_cancel):
                return manager().clean(categories, mode=mode, automatic=automatic, should_cancel=should_cancel)

            return jobs.submit("cache_cleanup", None, operation, request={"categories": categories, "mode": mode})

    def schedule(*, installers_only=False):
        from classcue import slide_runtime

        with jobs._lock:
            if jobs.is_running() or provider.get().list_paused_jobs():
                return
        if not installers_only:
            current = manager()
            if current.due():
                return start(current.state()["preferences"]["categories"], automatic=True)
        if slide_runtime.installer_cleanup_pending() and slide_runtime.acceleration_status()["ready"]:
            with jobs._lock:
                if jobs.is_running() or provider.get().list_paused_jobs():
                    return

                def operation(_update, _stop):
                    try:
                        released = slide_runtime.clear_installer_downloads()
                        return {"released_bytes": released,
                                "message": "PPT 安装包已自动清理" if released else "PPT 安装包检查完成，无需清理"}
                    finally:
                        if on_slide_cleanup:
                            on_slide_cleanup()

                return jobs.submit("slide_cache_cleanup", None, operation,
                                   request={"kind": "slide_cache_cleanup", "scope": "installers", "automatic": True})

    @routes.get("")
    def status():
        return {**manager().state(), "busy": jobs.is_running() or bool(provider.get().list_paused_jobs())}

    @routes.put("/preferences")
    def preferences(value: Preferences):
        try:
            return manager().save(value.model_dump())
        except (OSError, ValueError) as error:
            raise HTTPException(400, str(error)) from error

    @routes.post("/preview")
    def preview(value: CleanRequest):
        try:
            return manager().preview(value.mode)
        except (OSError, ValueError, TypeError) as error:
            raise HTTPException(400, "无法检查缓存目录，请检查存储设置后重试") from error

    @routes.post("/clean", status_code=202)
    def clean(value: CleanRequest):
        try:
            return {"job_id": start(value.categories, value.mode)}
        except RuntimeError as error:
            raise HTTPException(409, str(error)) from error

    return routes, schedule
