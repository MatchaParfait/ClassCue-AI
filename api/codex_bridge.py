"""Desktop entry points for the public Codex conversation bridge."""

import os
import sqlite3
from threading import RLock
from typing import Annotated

from fastapi import APIRouter, HTTPException
from filelock import Timeout
from pydantic import BaseModel, ConfigDict, Field

from classcue.codex_bridge import CodexBridge
from classcue.dsh_pipe import BridgeFailure
from classcue import asr_storage
from classcue.paths import PROJECT_ROOT


class SettingsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    root: str = Field(min_length=1, max_length=4096)
    executable: str | None = Field(default=None, max_length=4096)
    home: str | None = Field(default=None, max_length=4096)


class DiscussionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question_id: str = Field(min_length=1, max_length=100)
    new_discussion: bool = False
    copy_images: bool = False


class ClipRange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    video_id: int = Field(strict=True, gt=0)
    start_ms: int = Field(strict=True, ge=0)
    end_ms: int = Field(strict=True, gt=0)


class ClipPayload(ClipRange):
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    slide_ids: list[Annotated[int, Field(strict=True, gt=0)]] = Field(max_length=23)
    new_discussion: bool = False


def router(provider, *, bridge_factory=CodexBridge, desktop=None):
    routes = APIRouter(prefix="/api/codex-bridge", tags=["codex-bridge"])
    lock = RLock()

    def use(operation):
        if not (desktop() if desktop else os.getenv("CLASSROOM_DESKTOP_MODE") == "1"):
            raise HTTPException(404, "此功能仅在桌面应用中启用")
        try:
            with lock:
                return operation(bridge_factory(provider.get(), provider.library_id()))
        except BridgeFailure as error:
            raise HTTPException(409, str(error), headers={"X-ClassCue-Bridge-Error": error.code}) from None
        except KeyError:
            raise HTTPException(404, "视频、题目或讨论不存在") from None
        except Timeout:
            raise HTTPException(409, "另一讨论正在准备，请稍后重试") from None
        except sqlite3.Error:
            raise HTTPException(409, "插件回执库暂时不可用，请稍后重试；原讨论已保留") from None
        except (ValueError, OSError):
            raise HTTPException(400, "目录、学习快照或 Codex 路径不可用，请检查后重试") from None

    @routes.get("/settings")
    def settings():
        return use(lambda bridge: bridge.settings())

    @routes.put("/settings")
    def configure(payload: SettingsPayload):
        return use(lambda bridge: bridge.configure(**payload.model_dump()))

    @routes.post("/discussions", status_code=202)
    def prepare(payload: DiscussionPayload):
        return use(lambda bridge: bridge.prepare(**payload.model_dump()))

    @routes.post("/video-clips/preview")
    def preview_clip(payload: ClipRange):
        return use(lambda bridge: bridge.preview_clip(**payload.model_dump()))

    @routes.post("/video-clips", status_code=202)
    def prepare_clip(payload: ClipPayload):
        return use(lambda bridge: bridge.prepare_clip(
            **payload.model_dump(),
            slide_root=asr_storage.slide_directory(PROJECT_ROOT, provider.data_root())))

    @routes.get("/videos/{video_id}/discussions")
    def video_history(video_id: int):
        return use(lambda bridge: bridge.clip_history(video_id))

    @routes.get("/questions/{question_id}/discussions")
    def history(question_id: str):
        return use(lambda bridge: bridge.history(question_id))

    @routes.post("/requests/{request_id}/open")
    def open_discussion(request_id: str):
        return use(lambda bridge: bridge.resume(request_id))

    @routes.get("/requests/{request_id}")
    def discussion(request_id: str):
        return use(lambda bridge: bridge.discussion(request_id))

    @routes.post("/requests/{request_id}/copy-images")
    def copy_images(request_id: str):
        return use(lambda bridge: bridge.resume(request_id, copy_images=True))

    @routes.post("/requests/{request_id}/attach")
    def attach_discussion(request_id: str):
        return use(lambda bridge: bridge.resume(request_id, attach_again=True))

    return routes
