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


def router(project, provider, jobs, update_center=None):
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

    def schedule():
        current = manager()
        if current.due():
            start(current.state()["preferences"]["categories"], automatic=True)

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
