"""Local desktop API. This router never exposes a network file-transfer server."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DevicePayload(Payload):
    device_id: str = Field(min_length=1, max_length=128)


class SendPayload(DevicePayload):
    video_ids: list[int] = Field(default_factory=list, max_length=10000)
    folder_id: int | None = Field(default=None, gt=0)
    library_id: str


class ItemAction(Payload):
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    action: str = Field(pattern=r"^(receive|overwrite|skip)$")


class AcceptPayload(Payload):
    items: list[ItemAction] = Field(max_length=10000)
    target_folder_id: int | None = Field(default=None, gt=0)
    library_id: str


class LibraryPayload(Payload):
    library_id: str


def device_transfer_router(manager, library_id):
    router = APIRouter(prefix="/api/device-transfers", tags=["device-transfers"])

    def call(operation):
        try:
            return operation()
        except (ValueError, OSError, TimeoutError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    def require_library(identity):
        if identity != library_id():
            raise HTTPException(status_code=409, detail="当前视频库已改变，请重新打开传输页面")

    @router.get("")
    def status():
        return call(lambda: manager().status())

    @router.post("/discover")
    def discover():
        call(lambda: manager().scan())
        return manager().status()

    @router.post("/pairings")
    def pairing(payload: DevicePayload):
        return call(lambda: manager().request_pairing(payload.device_id))

    @router.post("/pairings/{identity}/confirm")
    def confirm_pairing(identity: str):
        return call(lambda: manager().confirm_pairing(identity))

    @router.post("/send")
    def send(payload: SendPayload):
        require_library(payload.library_id)
        if bool(payload.video_ids) == (payload.folder_id is not None):
            raise HTTPException(status_code=400, detail="请选择视频或一个课程文件夹")
        return call(lambda: manager().create_send(payload.device_id, video_ids=payload.video_ids, folder_id=payload.folder_id))

    @router.post("/tasks/{identity}/accept")
    def accept(identity: str, payload: AcceptPayload):
        require_library(payload.library_id)
        return call(lambda: manager().accept(identity, [item.model_dump() for item in payload.items], payload.target_folder_id))

    @router.post("/tasks/{identity}/retry")
    def retry(identity: str, payload: LibraryPayload):
        require_library(payload.library_id)
        return call(lambda: manager().retry_failed(identity))

    @router.post("/tasks/{identity}/{action}")
    def control(identity: str, action: str, payload: LibraryPayload):
        require_library(payload.library_id)
        return call(lambda: manager().control(identity, action))

    @router.post("/snapshots/{identity}/restore")
    def restore(identity: str, payload: LibraryPayload):
        require_library(payload.library_id)
        return {"video_id": call(lambda: manager().restore(identity))}

    return router
