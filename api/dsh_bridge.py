"""Desktop-only bridge routes; question selection remains owned by study materials."""

import os
from pathlib import Path
from threading import RLock

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from classcue.dsh_bridge import DshBridge
from classcue.dsh_pipe import BridgeFailure


class StrictPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConnectionPayload(StrictPayload):
    descriptor_path: str = Field(min_length=1, max_length=4096)


class PreparePayload(StrictPayload):
    question_id: str = Field(min_length=1, max_length=100)
    workspace_id: str | None = Field(default=None, max_length=100)
    new_discussion: bool = False


class WorkspaceRootPayload(StrictPayload):
    path: str = Field(min_length=1, max_length=4096)
    instance_id: str = Field(min_length=1, max_length=100)


def router(provider, *, bridge_factory=DshBridge, desktop=None):
    routes = APIRouter(prefix="/api/dsh-bridge", tags=["dsh-bridge"])
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
            raise HTTPException(404, "题目或传输记录不存在") from None
        except ValueError as error:
            raise HTTPException(400, str(error)) from None

    @routes.post("/connection")
    def connect(payload: ConnectionPayload):
        return use(lambda bridge: bridge.records.connect(Path(payload.descriptor_path), bridge.client_factory))

    @routes.post("/connection/auto")
    def auto_connect():
        return use(lambda bridge: bridge.records.auto_connect(bridge.client_factory))

    @routes.get("/status")
    def status():
        return use(lambda bridge: bridge.records.client(bridge.client_factory)[1])

    @routes.get("/workspaces")
    def workspaces():
        return use(lambda bridge: bridge.records.client(bridge.client_factory)[0].call("workspaces"))

    @routes.get("/workspace-root")
    def workspace_root():
        return use(lambda bridge: bridge.records.workspace_root())

    @routes.put("/workspace-root")
    def configure_root(payload: WorkspaceRootPayload):
        return use(lambda bridge: bridge.configure_root(**payload.model_dump()))

    @routes.post("/discussions", status_code=202)
    def prepare(payload: PreparePayload):
        return use(lambda bridge: bridge.prepare(**payload.model_dump()))

    @routes.get("/questions/{question_id}/discussions")
    def history(question_id: str):
        return use(lambda bridge: bridge.history(question_id))

    @routes.get("/requests/{request_id}")
    def request(request_id: str):
        return use(lambda bridge: bridge.resume(request_id))

    @routes.post("/requests/{request_id}/open", status_code=202)
    def open_session(request_id: str):
        return use(lambda bridge: bridge.resume(request_id, open_session=True))

    return routes
