"""App controls for optional local semantic retrieval and index preparation."""

from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, ValidationError

from classcue.course_tools import CourseTools, RequestId, SemanticScope, SemanticTarget
from classcue.library_search import LibrarySearch
from classcue.processing_control import ProcessingCancelled


class Prepare(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device: Literal["cpu", "cuda"] | None = None
    request_id: RequestId | None = None


class Setup(SemanticTarget):
    device: Literal["cpu", "cuda"] | None = None
    request_id: RequestId | None = None


class Cleanup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: Literal["cpu", "cuda", "weights", "all"] = "all"
    keep_device: Literal["cpu", "cuda"] | None = None


def router(provider, jobs, ensure_changes):
    routes = APIRouter(prefix="/api/semantic")

    def service(request):
        return CourseTools(provider, jobs, ensure_changes=ensure_changes, origin=str(request.base_url))

    def checked(operation):
        try:
            return operation()
        except KeyError as error:
            raise HTTPException(404, str(error).strip("'")) from None
        except ValidationError:
            raise HTTPException(422, "课程检索参数无效") from None
        except (ValueError, RuntimeError) as error:
            raise HTTPException(409, str(error)) from None

    @routes.get("/courses")
    def courses(request: Request, offset: int = 0):
        return checked(lambda: service(request).call("list_courses", {"offset": offset, "limit": 50}))

    @routes.get("/status")
    def status(request: Request, course_id: str | None = None):
        with jobs._lock:
            return checked(lambda: service(request).call(
                "get_semantic_status", SemanticScope(course_id=course_id).model_dump()))

    @routes.get("/cache")
    def cache():
        from classcue.semantic_embeddings import cache_inventory
        return checked(cache_inventory)

    @routes.post("/prepare", status_code=202)
    def prepare(payload: Prepare, request: Request):
        arguments = payload.model_dump()
        arguments["request_id"] = payload.request_id or uuid4().hex
        return checked(lambda: service(request).call("start_semantic_prepare", arguments))

    @routes.post("/setup", status_code=202)
    def setup(payload: Setup, request: Request):
        arguments = payload.model_dump()
        arguments["request_id"] = payload.request_id or uuid4().hex
        return checked(lambda: service(request).call("start_semantic_setup", arguments))

    @routes.post("/index", status_code=202)
    def index(payload: SemanticTarget, request: Request):
        with jobs._lock:
            return checked(lambda: service(request).call("start_semantic_index", payload.model_dump()))

    @routes.post("/cleanup", status_code=202)
    def cleanup(payload: Cleanup | None = None):
        from classcue.semantic_embeddings import CLEANUP_SCOPES, cleanup_model
        target = payload.target if payload is not None else "all"
        scope = CLEANUP_SCOPES[target]
        keep_device = payload.keep_device if payload is not None else None
        if keep_device is not None and target != ("cuda" if keep_device == "cpu" else "cpu"):
            raise HTTPException(422, "清理范围必须是保留设备之外的运行环境")

        def start():
            with jobs._lock:
                if jobs.is_running() or provider.get().list_paused_jobs():
                    raise RuntimeError("请先完成或停止运行及暂停中的任务，再清理检索缓存")
                ensure_changes()

                def run(update, stop):
                    update(0, f"正在验证 {'GPU' if keep_device == 'cuda' else 'CPU'} 处理环境"
                           if keep_device else f"正在清理{scope}", "cleaning")
                    try:
                        released = (cleanup_model(stop, target=target, keep_device=keep_device)
                                    if keep_device else cleanup_model(stop, target=target))
                    except ProcessingCancelled:
                        raise ProcessingCancelled(
                            f"{scope}清理已停止，已删除文件不会恢复；请查看剩余缓存") from None
                    message = (f"已切换为仅使用 {'GPU' if keep_device == 'cuda' else 'CPU'} 处理 · "
                               if keep_device else "")
                    return {"message": f"{message}{scope}已清空 · 释放 {released} 字节"}

                return {"job_id": jobs.submit(
                    "semantic_cleanup", None, run,
                    request={"kind": "semantic_cleanup", "target": target, "device": keep_device})}

        return checked(start)

    @routes.post("/search")
    def search(payload: LibrarySearch, request: Request):
        return checked(lambda: service(request).call(
            "search_library", payload.model_dump(), expected_library_id=provider.library_id()))

    return routes
