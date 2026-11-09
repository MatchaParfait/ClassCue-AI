"""Authenticated, library-bound entry point for the installed local MCP bridge."""

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from classcue.course_connection import authenticated, connection_token
from classcue.course_tools import CourseTools
from classcue.diagnostics import redact_text


class Call(BaseModel):
    model_config = ConfigDict(extra="forbid")
    library_id: str = Field(min_length=1, max_length=100)
    arguments: dict = Field(default_factory=dict)


def router(provider, jobs, ensure_changes, *, batch=None):
    routes = APIRouter(prefix="/api/course-tools")
    token = connection_token()

    @routes.post("/{tool}")
    def call(tool: str, payload: Call, request: Request, authorization: str | None = Header(default=None)):
        if not authenticated(authorization, token):
            raise HTTPException(403, "ClassCue 插件连接未授权，请检查安装和连接设置")
        if payload.library_id != provider.library_id():
            raise HTTPException(409, "视频库身份已变化，请重新连接")
        try:
            service = CourseTools(provider, jobs, ensure_changes=ensure_changes,
                                  origin=str(request.base_url), batch=batch)
            if tool in {"read_slide", "read_video_frame"}:
                return service.call(tool, payload.arguments, expected_library_id=payload.library_id)
            with jobs._lock:
                return service.call(tool, payload.arguments)
        except KeyError as error:
            raise HTTPException(404, str(error).strip("'")) from None
        except ValidationError as error:
            details = []
            for item in error.errors(include_input=False, include_url=False)[:5]:
                field = ".".join(str(part) for part in item["loc"]) or "arguments"
                details.append(f"{field}: {item['msg']}")
            raise HTTPException(422, "工具参数无效；" + "；".join(details)) from None
        except (ValueError, RuntimeError) as error:
            raise HTTPException(409, redact_text(error)) from None
    return routes
