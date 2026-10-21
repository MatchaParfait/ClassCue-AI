"""Same-origin public controls and a native-only installation reservation."""

import hmac
import os
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app_updates import write_json


class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    auto_check: bool | None = None
    auto_download: bool | None = None
    show_announcements: bool | None = None


class ReadAnnouncements(BaseModel):
    ids: list[str] = Field(max_length=100)


def local_request(request: Request):
    if request.url.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise HTTPException(403, "更新控制只允许本机访问")
    origin = request.headers.get("origin")
    if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
        raise HTTPException(403, "不允许跨站更新请求")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "不允许跨站更新请求")


def router(center, jobs, provider, migrating, updating):
    routes = APIRouter(prefix="/api/updates", dependencies=[Depends(local_request)])

    def execute(operation):
        try:
            operation()
            return status()
        except (ValueError, OSError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @routes.get("")
    def status():
        result = center.status()
        result["busy"] = migrating.is_set() or updating.is_set() or jobs.is_running() or bool(provider.get().list_paused_jobs())
        # Journal paths and complete file inventory are not browser state.
        if result["install"]:
            result["install"] = {key: result["install"].get(key) for key in ("status", "version", "message")}
        return result

    @routes.post("/check")
    def check():
        return execute(center.start_check)

    @routes.post("/download")
    def download():
        return execute(center.start_download)

    @routes.post("/pause")
    def pause():
        return execute(center.pause_download)

    @routes.put("/preferences")
    def preferences(value: Preferences):
        return execute(lambda: center.preferences(value.model_dump(exclude_none=True)))

    @routes.post("/read")
    def mark_read(value: ReadAnnouncements):
        return execute(lambda: center.mark_read(value.ids))

    def native(request):
        expected = os.getenv("CLASSROOM_UPDATE_TOKEN", "")
        if not expected or not hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {expected}"):
            raise HTTPException(403, "安装必须由桌面程序确认")
        if request.headers.get("origin"):
            raise HTTPException(403, "网页不能直接安装更新")

    @routes.post("/prepare-install")
    def prepare(request: Request):
        native(request)
        with jobs._lock:
            if migrating.is_set() or updating.is_set() or jobs.is_running() or provider.get().list_paused_jobs():
                raise HTTPException(409, "请先完成或停止运行及暂停中的任务，再安装更新")
            try:
                plan = center.reserve_install()
            except (ValueError, OSError) as exc:
                raise HTTPException(409, str(exc)) from exc
            plan["database_path"] = str(provider.database_path())
            write_json(center.directory / "install-plan.json", plan)
            updating.set()
        return {"id": plan["id"]}

    @routes.post("/cancel-install")
    def cancel(request: Request):
        native(request)
        with jobs._lock:
            updating.clear()
        return {"ok": True}

    return routes
