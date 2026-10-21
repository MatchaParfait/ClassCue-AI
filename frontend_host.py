"""Serve the built React app from the API in desktop mode, including SPA routes."""

import hashlib
import os
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse
from starlette.routing import Match


def mount_frontend(api, project: Path):
    root = (project / "frontend" / "dist").resolve()
    index = root / "index.html"
    if not index.is_file():
        return
    root_id = hashlib.sha256(os.path.normcase(str(project.resolve())).encode()).hexdigest()[:12]

    @api.get("/__classroom/frontend-identity", include_in_schema=False)
    def identity():
        return {
            "service": "classroom-video-study-frontend", "mode": "static",
            "root_id": root_id, "build": hashlib.sha256(index.read_bytes()).hexdigest()[:16],
        }

    @api.get("/{frontend_path:path}", include_in_schema=False)
    def frontend(frontend_path: str, request: Request):
        if frontend_path.split("/", 1)[0] in {"api", "__classroom", "docs", "redoc", "openapi.json"}:
            # A catch-all must not turn the existing API's method-not-allowed
            # responses into 404s (e.g. GET on the POST-only key reveal route).
            for route in api.routes:
                if getattr(route, "path", "").startswith("/api/"):
                    match, _scope = route.matches(request.scope)
                    if match == Match.PARTIAL:
                        raise HTTPException(405, headers={"Allow": ", ".join(sorted(route.methods))})
            raise HTTPException(404)
        target = (root / frontend_path).resolve()
        if not target.is_relative_to(root):
            raise HTTPException(404)
        if target.is_file():
            return FileResponse(target, headers={
                "Cache-Control": "no-store" if target == index else "public, max-age=3600",
            })
        if Path(frontend_path).suffix:
            raise HTTPException(404)
        return FileResponse(index, headers={"Cache-Control": "no-store"})
