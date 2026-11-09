"""Single-click alignment using the existing local task queue."""

import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from classcue.processing_control import ProcessingCancelled
from classcue.subtitle_search import MAX_SQLITE_INTEGER
from classcue.secondary_alignment_service import (
    JOB_TYPE, alignment_snapshot, change_result, latest_alignment,
    run_worker, save_result, snapshot_current,
)
from classcue.transcriber import require_prepared_asr_model


class AlignmentStartPayload(BaseModel):
    expected_secondary_video_id: int = Field(gt=0, le=MAX_SQLITE_INTEGER, strict=True)
    expected_offset_ms: int = Field(ge=-MAX_SQLITE_INTEGER, le=MAX_SQLITE_INTEGER, strict=True)


def router(provider, jobs, migrating):
    routes = APIRouter()

    def view_payload(store, video_id):
        view = store.get_secondary_view(video_id)
        if view is None:
            raise HTTPException(404, "副视角已移除")
        result = dict(view)
        latest = latest_alignment(store, video_id, view["video_id"])
        result["alignment_job_id"] = latest["id"] if latest else None
        return result

    @routes.post("/api/videos/{video_id}/secondary-view/alignment", status_code=202)
    def start(video_id: int, payload: AlignmentStartPayload | None = None):
        with jobs._lock:
            if migrating():
                raise HTTPException(409, "存储迁移期间暂不能自动对齐")
            store = provider.get()
            if store.get_video(video_id) is None:
                raise HTTPException(404, "视频不存在")
            if payload is not None:
                view = store.get_secondary_view(video_id)
                if (view is None or view["video_id"] != payload.expected_secondary_video_id
                        or view["offset_ms"] != payload.expected_offset_ms):
                    raise HTTPException(409, "副视角关联或时间偏移已变化，未启动自动对齐")
            try:
                snapshot = alignment_snapshot(store, video_id, provider.library_id())
                existing = latest_alignment(store, video_id, snapshot["secondary_id"])
                if existing and existing["status"] in {"queued", "running", "resource_paused"}:
                    return {"job_id": existing["id"], "status": existing["status"]}
                require_prepared_asr_model(snapshot["model"]["id"])
            except (ValueError, OSError, RuntimeError) as error:
                raise HTTPException(400, str(error)) from None

            def operation(update, cancelled):
                stale = False
                checked_at = 0.

                def stop():
                    nonlocal stale, checked_at
                    if cancelled():
                        return True
                    if time.monotonic() - checked_at >= 1:
                        checked_at = time.monotonic()
                        view = store.get_secondary_view(video_id)
                        stale = (provider.library_id() != snapshot["library_id"] or not view
                                 or view["video_id"] != snapshot["secondary_id"]
                                 or view["offset_ms"] != snapshot["original_offset_ms"])
                    return stale

                if not snapshot_current(store, snapshot, provider.library_id()):
                    result = {"outcome": "stale", "offset_ms": None, "evidence": [],
                              "message": "视频、字幕或 ASR 设置已变化，请重新对齐"}
                else:
                    update(.05, "正在准备自动对齐", "finding_speech")
                    try:
                        result = run_worker(store, snapshot, update, stop)
                    except ProcessingCancelled:
                        if cancelled() or not stale:
                            raise
                        result = {"outcome": "stale", "offset_ms": None, "evidence": [],
                                  "message": "副视角设置已变化，对齐已停止"}
                with jobs._lock:
                    if cancelled():
                        raise ProcessingCancelled("已停止自动对齐")
                    if not snapshot_current(store, snapshot, provider.library_id()):
                        result = {**result, "outcome": "stale",
                                  "message": "视频、字幕或偏移已变化，结果未应用"}
                    result = save_result(store, job_id, snapshot, result,
                                         apply=result.get("outcome") == "ready")
                return {"message": result["message"], "committed": result["outcome"] == "applied"}

            try:
                job_id = jobs.submit(
                    JOB_TYPE, video_id, operation, allow_queue=True,
                    request={"kind": JOB_TYPE, "model_id": snapshot["model"]["id"],
                             "alignment": {"snapshot": snapshot}},
                )
            except RuntimeError as error:
                raise HTTPException(409, str(error)) from None
            return {"job_id": job_id, "status": "queued"}

    @routes.post("/api/videos/{video_id}/secondary-view/alignment/{job_id}/apply")
    def apply(video_id: int, job_id: str):
        return change(video_id, job_id, "apply")

    @routes.post("/api/videos/{video_id}/secondary-view/alignment/{job_id}/undo")
    def undo(video_id: int, job_id: str):
        return change(video_id, job_id, "undo")

    def change(video_id, job_id, action):
        with jobs._lock:
            if migrating():
                raise HTTPException(409, "存储迁移期间暂不能修改偏移")
            store = provider.get()
            try:
                change_result(store, job_id, video_id, provider.library_id(), action)
            except KeyError as error:
                raise HTTPException(404, str(error.args[0])) from None
            except ValueError as error:
                raise HTTPException(409, str(error)) from None
            return view_payload(store, video_id)

    return routes
