"""Browser-clicked playback links hand navigation to the existing desktop."""

import hashlib
import hmac
from html import escape
import json
from pathlib import Path
import re
import secrets

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from classcue import course_connection
from classcue.diagnostics import redact_text
from classcue.paths import PROJECT_ROOT
from classcue.playback_links import playback_url


ASSETS = PROJECT_ROOT / "assets"
MAX_INTEGER = (1 << 53) - 1


class OpenPlayback(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_id: int = Field(strict=True, gt=0, le=MAX_INTEGER)
    start_ms: int = Field(strict=True, ge=0, le=10**13)
    library_id: str = Field(min_length=1, max_length=100)
    proof: str = Field(pattern=r"^[a-f0-9]{64}$")


def _error(status, code, message):
    return HTTPException(status, {"code": code, "message": message})


def router(project, provider, jobs):
    routes = APIRouter()

    def proof(video_id, start_ms, library_id):
        secret = course_connection.connection_token(project).encode()
        payload = json.dumps([video_id, start_ms, library_id], separators=(",", ":")).encode()
        return hmac.new(secret, payload, hashlib.sha256).hexdigest()

    def target(video_id, start_ms, library_id):
        if library_id != provider.library_id():
            raise _error(409, "library_changed", "视频库已变化，请从对话中重新检索回放。")
        video = provider.get().get_video(video_id)
        if video is None or video["is_secondary"]:
            raise _error(404, "video_missing", "这段视频已不存在，请重新检索回放。")
        if not Path(video["video_path"]).is_file():
            raise _error(404, "video_unavailable", "视频文件不可用，请在 ClassCue 中检查。")
        if start_ms >= video["duration_ms"]:
            raise _error(422, "invalid_position", "回放位置超出视频时长，请重新选择片段。")
        return video["display_name"] or video["filename"]

    def origin(request):
        # Desktop IPC accepts the owned service's numeric loopback origin only.
        port = request.url.port or 80
        return f"http://127.0.0.1:{port}"

    def page_response(data, name="", start_ms=0, *, error=None):
        nonce = secrets.token_urlsafe(24)
        seconds = start_ms // 1000
        position = f"{seconds // 60:02d}:{seconds % 60:02d}"
        message = error.detail["message"] if error else "正在连接 ClassCue…"
        page = (ASSETS / "desktop-open.html").read_text(encoding="utf-8")
        replacements = {
            "__NONCE__": nonce,
            "__VIDEO_NAME__": escape(name),
            "__POSITION__": position,
            "__HEADING__": "回放暂时不可用" if error else "正在打开桌面版",
            "__ROLE__": "alert" if error else "status",
            "__MESSAGE__": escape(message),
            "__TARGET_HIDDEN__": "hidden" if error else "",
            "__TARGET__": json.dumps(data, ensure_ascii=True).replace("<", "\\u003c"),
        }
        page = re.sub(r"__[A-Z_]+__", lambda match: replacements.get(match[0], match[0]), page)
        return HTMLResponse(page, status_code=error.status_code if error else 200, headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": (
                f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                "img-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
            ),
        })

    @routes.get("/open/review/{video_id}", include_in_schema=False)
    def landing(request: Request, video_id: int, library_id: str = Query(min_length=1, max_length=100),
                start_ms: int = Query(default=0, ge=0, le=10**13)):
        try:
            if (not 0 < video_id <= MAX_INTEGER
                    or set(request.query_params) - {"start_ms", "library_id"}
                    or any(len(request.query_params.getlist(key)) != 1 for key in request.query_params)):
                raise _error(422, "invalid_link", "回放链接无效，请重新检索回放。")
            with jobs._lock:
                name = target(video_id, start_ms, library_id)
        except HTTPException as error:
            return page_response({"error": True}, error=error)
        data = {"video_id": video_id, "start_ms": start_ms, "library_id": library_id,
                "proof": proof(video_id, start_ms, library_id)}
        return page_response(data, name, start_ms)

    @routes.get("/api/desktop-links/client.js", include_in_schema=False)
    def script():
        return Response((ASSETS / "desktop-open.js").read_text(encoding="utf-8"),
                        media_type="application/javascript",
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @routes.get("/api/desktop-links/icon.png", include_in_schema=False)
    def icon():
        return FileResponse(ASSETS / "app-icon.png", media_type="image/png")

    @routes.post("/api/desktop-links/open", include_in_schema=False)
    def open_desktop(payload: OpenPlayback, request: Request):
        if not hmac.compare_digest(payload.proof, proof(payload.video_id, payload.start_ms, payload.library_id)):
            raise _error(403, "invalid_proof", "回放请求已失效，请重新打开链接。")
        with jobs._lock:
            target(payload.video_id, payload.start_ms, payload.library_id)
        url = playback_url(origin(request), payload.video_id, payload.start_ms, payload.library_id, desktop=False)
        try:
            result = course_connection.navigate_desktop(url, project=project)
        except (OSError, RuntimeError) as error:
            # A library switch or deletion must never become a link to another video.
            with jobs._lock:
                target(payload.video_id, payload.start_ms, payload.library_id)
            return JSONResponse(status_code=503, content={
                "code": "desktop_unavailable", "message": redact_text(error),
                "web_url": playback_url("", payload.video_id, payload.start_ms, payload.library_id, desktop=False),
            }, headers={"Cache-Control": "no-store"})
        return {"navigation": result["navigation"]}

    return routes
