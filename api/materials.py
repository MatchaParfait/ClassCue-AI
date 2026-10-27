"""Material endpoints share the source-backed repository used by the Agent."""

from functools import wraps
from io import BytesIO
import os
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from classcue.ai_provider import load_provider_settings, resolve_interface_key
from classcue.material_agent import MaterialModel, preprocess
from classcue.material_parser import image_asset, parse_document
from classcue.material_store import MaterialStore, identity


class StrictPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CollectionPayload(StrictPayload):
    name: str = Field(min_length=1, max_length=120)
    folder_ids: list[int] = Field(default_factory=list, max_length=200)


class CategoryPayload(StrictPayload):
    collection_id: str
    name: str
    parent_id: str | None = None
    aliases: list[str] = Field(default_factory=list)
    position: int | None = Field(default=None, ge=0)


class ProcessPayload(StrictPayload):
    connection_id: str | None = None


class ItemPayload(StrictPayload):
    document_id: str
    values: dict


class StudyPayload(StrictPayload):
    status: Literal["unreviewed", "consolidate", "mastered"]
    version: int = Field(ge=1)


class IdsPayload(StrictPayload):
    ids: list[str] = Field(max_length=200)


class MergePayload(IdsPayload):
    values: dict


class SplitPayload(StrictPayload):
    parts: list[dict] = Field(min_length=2, max_length=30)


class CoveragePayload(StrictPayload):
    state: Literal["identified", "non_question", "pending"]
    reason: str = ""


def boundary(method):
    @wraps(method)
    def wrapped(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except KeyError:
            raise HTTPException(404, "资料记录不存在") from None
        except (ValueError, TypeError) as error:
            raise HTTPException(400, str(error)) from None
    return wrapped


def router(provider, jobs):
    routes = APIRouter()

    def repository():
        return MaterialStore(provider.get())

    def idle():
        if jobs.is_running():
            raise HTTPException(409, "已有任务正在运行，请等待或停止后再操作")

    @routes.get("/api/material-collections")
    def collections():
        return repository().collections()

    @routes.get("/api/material-collections/folders")
    def folders():
        with provider.get().connect() as db:
            return [dict(r) for r in db.execute("SELECT id,name,parent_id FROM video_folders ORDER BY library_order,id")]

    @routes.post("/api/material-collections", status_code=201)
    @boundary
    def create_collection(payload: CollectionPayload):
        with jobs._lock:
            idle()
            return repository().save_collection(**payload.model_dump())

    @routes.put("/api/material-collections/{collection_id}")
    @boundary
    def update_collection(collection_id: str, payload: CollectionPayload):
        with jobs._lock:
            idle()
            return repository().save_collection(**payload.model_dump(), collection_id=collection_id)

    @routes.get("/api/material-collections/{collection_id}/runs")
    def runs(collection_id: str):
        repo = repository()
        rows = repo.runs(collection_id)
        # A stale 'running' checkpoint after process death is shown as interrupted; never auto-resume.
        if not jobs.is_running():
            for row in rows:
                if row["state"] == "running":
                    repo.run_state(row["id"], "interrupted", row["stage"], "服务已重启，请手动继续")
            rows = repo.runs(collection_id)
        return rows

    @routes.post("/api/material-collections/{collection_id}/process", status_code=202)
    @boundary
    def process(collection_id: str, payload: ProcessPayload):
        with jobs._lock:
            idle()
            repo = repository()
            repo.documents(collection_id)
            settings = load_provider_settings()
            selected = payload.connection_id or settings.active_connection_id
            connection = next((c for c in settings.connections if c.id == selected), None)
            if not connection:
                raise ValueError("请选择已保存的 AI 连接")
            profile = connection.profile.model_copy(deep=True)
            key = resolve_interface_key(profile, settings)
            profile.require_ready(key)
            run_id = repo.start_run(collection_id, selected)
            try:
                job_id = jobs.submit("material_preprocess", None,
                                    lambda update, cancel: preprocess(
                                        repo, collection_id, selected, lambda: MaterialModel(profile, key),
                                        update, cancel, run_id=run_id),
                                    request={"collection_id": collection_id, "run_id": run_id})
            except RuntimeError:
                repo.run_state(run_id, "failed", "starting", "未能启动任务")
                raise HTTPException(409, "任务未能启动，请稍后重试") from None
            return {"job_id": job_id, "run_id": run_id}

    @routes.get("/api/material-documents")
    @boundary
    def documents(collection_id: str, include_deleted: bool = False):
        return repository().documents(collection_id, include_deleted)

    async def body(request, maximum):
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > maximum:
                raise HTTPException(413, "文件过大")
            data.extend(chunk)
        return bytes(data)

    @routes.post("/api/material-documents", status_code=201)
    async def upload(request: Request, collection_id: str, name: str = Query(min_length=1, max_length=240)):
        data = await body(request, 100 * 1024 * 1024)
        @boundary
        def persist():
            with jobs._lock:
                idle()
                repo = repository()
                document = repo.register(collection_id, name, data)
                try:
                    parse_document(repo, document["id"])
                except ValueError:
                    pass
                return repo.document(document["id"])
        return await run_in_threadpool(persist)

    @routes.post("/api/material-documents/{document_id}/parse")
    @boundary
    def parse(document_id: str):
        with jobs._lock:
            idle()
            return parse_document(repository(), document_id)

    @routes.get("/api/material-documents/{document_id}/blocks")
    @boundary
    def blocks(document_id: str):
        return repository().blocks(document_id)

    @routes.put("/api/material-documents/{document_id}/blocks/{block_id}/coverage")
    @boundary
    def coverage(document_id: str, block_id: str, payload: CoveragePayload):
        with jobs._lock:
            idle()
            repository().publish(document_id, [], [{"block_id": block_id, **payload.model_dump()}])
        return {"ok": True}

    @routes.get("/api/material-documents/{document_id}/original")
    @boundary
    def original(document_id: str):
        repo = repository()
        document = repo.document(document_id)
        return FileResponse(repo.resource(document["resource"]), filename=document["name"],
                            content_disposition_type="attachment")

    @routes.post("/api/material-documents/{document_id}/open")
    @boundary
    def open_original(document_id: str):
        repo = repository()
        document = repo.document(document_id)
        path = repo.resource(document["resource"])
        try:
            os.startfile(str(path))
        except OSError:
            raise ValueError("系统未能打开此格式，请选择其他默认应用或导出原文件") from None
        return {"ok": True}

    @routes.get("/api/material-documents/{document_id}/asset")
    @boundary
    def asset(document_id: str, resource: str):
        repo = repository()
        repo.document(document_id)
        path = repo.resource(resource)
        if not path.is_relative_to((repo.root / document_id).resolve()) or path.suffix.lower() != ".png":
            raise ValueError("无效的资料图片")
        return FileResponse(path, media_type="image/png", headers={"X-Content-Type-Options": "nosniff"})

    @routes.get("/api/material-documents/{document_id}/region")
    @boundary
    def region(document_id: str, block_id: str, left: float = 0, top: float = 0,
               right: float = 1, bottom: float = 1):
        from PIL import Image
        repo = repository()
        refs = [{"block_id": block_id, "bbox": [left, top, right, bottom]}]
        with repo.store.connect() as db:
            repo._references(db, document_id, refs)
        block = next(b for b in repo.blocks(document_id) if b["id"] == block_id)
        with Image.open(repo.resource(block["image"])) as image:
            area = image.crop((int(left * image.width), int(top * image.height),
                               max(int(left * image.width) + 1, int(right * image.width)),
                               max(int(top * image.height) + 1, int(bottom * image.height))))
            output = BytesIO()
            area.save(output, "PNG")
        return Response(output.getvalue(), media_type="image/png")

    @routes.post("/api/material-documents/{document_id}/corrections")
    async def correction(document_id: str, request: Request):
        data = await body(request, 12 * 1024 * 1024)
        @boundary
        def persist():
            with jobs._lock:
                idle()
                repo = repository()
                repo.document(document_id)
                directory = repo.root / document_id / "corrections"
                directory.mkdir(parents=True, exist_ok=True)
                output = directory / (identity() + ".png")
                image_asset(data, output)
                return {"resource": output.relative_to(repo.root).as_posix()}
        return await run_in_threadpool(persist)

    @routes.delete("/api/material-documents/{document_id}")
    @boundary
    def delete_document(document_id: str):
        with jobs._lock:
            idle()
            repository().soft_delete("material_documents", document_id, True)
        return {"ok": True}

    @routes.post("/api/material-documents/{document_id}/restore")
    @boundary
    def restore_document(document_id: str):
        with jobs._lock:
            idle()
            repository().soft_delete("material_documents", document_id, False)
        return {"ok": True}

    @routes.get("/api/material-categories")
    @boundary
    def categories(collection_id: str):
        return repository().list_categories(collection_id)

    @routes.post("/api/material-categories")
    @boundary
    def create_category(payload: CategoryPayload):
        with jobs._lock:
            idle()
            return {"id": repository().save_category(**payload.model_dump())}

    @routes.put("/api/material-categories/{category_id}")
    @boundary
    def update_category(category_id: str, payload: CategoryPayload):
        with jobs._lock:
            idle()
            return {"id": repository().save_category(**payload.model_dump(), category_id=category_id)}

    @routes.post("/api/material-categories/{category_id}/merge")
    @boundary
    def merge_category(category_id: str, payload: IdsPayload):
        with jobs._lock:
            idle()
            if len(payload.ids) != 1:
                raise ValueError("需要一个合并目标分类")
            repository().merge_categories(category_id, payload.ids[0])
        return {"ok": True}

    @routes.get("/api/material-items")
    @boundary
    def items(collection_id: str, category_id: str | None = None, document_id: str | None = None,
              query: str = Query(default="", max_length=300), study_status: str | None = None,
              view: Literal["all", "ready", "pending"] = "ready", include_deleted: bool = False,
              tags: list[str] = Query(default=[])):
        return repository().find_items(collection_id, category_id=category_id, document_id=document_id,
                                       query=query, study_status=study_status, view=view,
                                       include_deleted=include_deleted, tags=tags)

    @routes.post("/api/material-items")
    @boundary
    def create_item(payload: ItemPayload):
        with jobs._lock:
            idle()
            return repository().save_item(payload.document_id, payload.values)

    @routes.post("/api/material-items/merge")
    @boundary
    def merge_items(payload: MergePayload):
        with jobs._lock:
            idle()
            return {"id": repository().merge(payload.ids, payload.values)}

    @routes.get("/api/material-items/{item_id}")
    @boundary
    def item(item_id: str):
        return repository().read_items([item_id], include_deleted=True)[0]

    @routes.put("/api/material-items/{item_id}")
    @boundary
    def save_item(item_id: str, payload: ItemPayload):
        with jobs._lock:
            idle()
            return repository().save_item(payload.document_id, payload.values, item_id)

    @routes.get("/api/material-items/{item_id}/versions/{version}")
    @boundary
    def version(item_id: str, version: int):
        return repository().version(item_id, version)

    @routes.put("/api/material-items/{item_id}/study")
    @boundary
    def study(item_id: str, payload: StudyPayload):
        with jobs._lock:
            repository().study(item_id, payload.status, payload.version)
        return {"ok": True}

    @routes.put("/api/material-items/{item_id}/answers")
    @boundary
    def answers(item_id: str, payload: IdsPayload):
        with jobs._lock:
            idle()
            repository().link_answers(item_id, payload.ids)
        return {"ok": True}

    @routes.post("/api/material-items/{item_id}/split")
    @boundary
    def split(item_id: str, payload: SplitPayload):
        with jobs._lock:
            idle()
            return {"ids": repository().split(item_id, payload.parts)}

    @routes.delete("/api/material-items/{item_id}")
    @boundary
    def delete_item(item_id: str):
        with jobs._lock:
            idle()
            repository().soft_delete("material_items", item_id, True)
        return {"ok": True}

    @routes.post("/api/material-items/{item_id}/restore")
    @boundary
    def restore_item(item_id: str):
        with jobs._lock:
            idle()
            repository().soft_delete("material_items", item_id, False)
        return {"ok": True}

    return routes
