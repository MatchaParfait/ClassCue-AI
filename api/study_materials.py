"""Document reading and user-defined study questions; no model or Agent calls."""

from __future__ import annotations

from functools import wraps
from io import BytesIO

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from classcue.study_materials import StudyMaterialStore
from classcue.study_courses import StudyCourseStore


class StrictPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QuestionPayload(StrictPayload):
    document_id: str
    title: str = Field(default="", max_length=180)
    number: str = Field(default="", max_length=120)
    regions: list[dict] = Field(min_length=1, max_length=100)


class ReadingPayload(StrictPayload):
    page: int = Field(default=1, ge=1, le=500)
    scroll_top: float = Field(default=0, ge=0, le=10_000_000)
    zoom: float = Field(default=1, ge=0.5, le=3)
    question_id: str | None = None
    left_open: bool = True
    right_open: bool = True


class NamePayload(StrictPayload):
    name: str = Field(min_length=1, max_length=240)


class CoursePayload(StrictPayload):
    folder_id: int | None = Field(default=None, strict=True, gt=0)
    name: str = Field(default="", max_length=240)


class CourseBindingPayload(StrictPayload):
    folder_id: int = Field(strict=True, gt=0)


class DocumentCoursePayload(StrictPayload):
    course_id: str = Field(min_length=1, max_length=100)


def boundary(method):
    @wraps(method)
    def wrapped(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except KeyError:
            raise HTTPException(404, "学习资料记录不存在") from None
        except (ValueError, TypeError) as error:
            raise HTTPException(400, str(error)) from None
    return wrapped


def router(provider, jobs=None):
    routes = APIRouter()

    def repository():
        return StudyMaterialStore(provider.get())

    def courses():
        return StudyCourseStore(provider.get())

    @routes.get("/api/study-courses")
    @boundary
    def list_courses():
        return courses().courses()

    @routes.get("/api/study-courses/folders")
    def course_folders():
        return courses().folders()

    @routes.get("/api/study-courses/for-folder/{folder_id}")
    @boundary
    def folder_course(folder_id: int):
        return courses().for_folder(folder_id)

    @routes.post("/api/study-courses", status_code=201)
    @boundary
    def create_course(payload: CoursePayload):
        return courses().create(**payload.model_dump())

    @routes.put("/api/study-courses/{course_id}/folder")
    @boundary
    def bind_course(course_id: str, payload: CourseBindingPayload):
        return courses().bind(course_id, payload.folder_id)

    @routes.get("/api/study-courses/{course_id}/video-questions")
    @boundary
    def video_questions(course_id: str):
        return courses().video_questions(course_id)

    @routes.get("/api/course-questions/video/{marker_id}")
    @boundary
    def video_question(marker_id: int):
        return courses().video_question(marker_id)

    @routes.get("/api/course-questions/{question_id}")
    @boundary
    def course_question(question_id: str):
        return courses().question(question_id)

    @routes.put("/api/study-documents/{document_id}/course")
    @boundary
    def move_document(document_id: str, payload: DocumentCoursePayload):
        return courses().move_document(document_id, payload.course_id)

    async def body(request: Request, maximum: int):
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > maximum:
                raise HTTPException(413, "文件超过允许大小")
            data.extend(chunk)
        return bytes(data)

    @routes.get("/api/study-documents")
    def documents(include_deleted: bool = False, course_id: str | None = None, unassigned: bool = False):
        return repository().documents(include_deleted=include_deleted, course_id=course_id, unassigned=unassigned)

    @routes.post("/api/study-documents", status_code=201)
    async def upload(request: Request, name: str = Query(min_length=1, max_length=240),
                     course_id: str = Query(min_length=1, max_length=100)):
        data = await body(request, 100 * 1024 * 1024)
        repo = repository()
        try:
            return await run_in_threadpool(repo.register, name, data, course_id)
        except KeyError:
            raise HTTPException(404, "课程资料夹不存在") from None
        except (ValueError, TypeError) as error:
            raise HTTPException(400, str(error)) from None
        except OSError:
            raise HTTPException(400, "资料无法读取或保存，请检查文件内容和本地磁盘空间") from None

    @routes.get("/api/study-documents/{document_id}")
    @boundary
    def document(document_id: str):
        return repository().document(document_id)

    @routes.put("/api/study-documents/{document_id}")
    @boundary
    def rename(document_id: str, payload: NamePayload):
        return repository().rename_document(document_id, payload.name)

    @routes.get("/api/study-documents/{document_id}/blocks")
    @boundary
    def blocks(document_id: str):
        return repository().blocks(document_id)

    @routes.get("/api/study-documents/{document_id}/page/{page_number}")
    @boundary
    def page(document_id: str, page_number: int):
        repo = repository()
        blocks = repo.blocks(document_id)
        block = next((item for item in blocks if item.get("kind") in {"pdf_page", "image"}
                      and item.get("page") == page_number), None)
        if block is None:
            raise HTTPException(404, "页面不存在")
        return FileResponse(repo.page_image(document_id, block["id"]), media_type="image/png",
                            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=3600"})

    @routes.get("/api/study-documents/{document_id}/blocks/{block_id}/region")
    @boundary
    def region(document_id: str, block_id: str,
               left: float = 0, top: float = 0, right: float = 1, bottom: float = 1):
        repo = repository()
        output_image = repo.region_image(document_id, block_id, [left, top, right, bottom])
        output = BytesIO()
        output_image.save(output, "PNG")
        return Response(output.getvalue(), media_type="image/png",
                        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=3600"})

    @routes.get("/api/study-documents/{document_id}/blocks/{block_id}/text-bounds")
    @boundary
    def text_bounds(document_id: str, block_id: str):
        return repository().text_bounds(document_id, block_id)

    @routes.put("/api/study-documents/{document_id}/reading")
    @boundary
    def save_reading(document_id: str, payload: ReadingPayload):
        repo = repository()
        values = payload.model_dump()
        if values.get("question_id"):
            question = repo.question(values["question_id"])
            if question["document_id"] != document_id:
                raise HTTPException(400, "阅读位置题目不属于当前资料")
        repo.save_reading(document_id, values)
        return {"ok": True}

    @routes.post("/api/study-documents/{document_id}/attachments")
    async def supplement(document_id: str, request: Request,
                         name: str = Query(min_length=1, max_length=240)):
        data = await body(request, 20 * 1024 * 1024)
        try:
            return await run_in_threadpool(repository().supplement_image, document_id, name, data)
        except KeyError:
            raise HTTPException(404, "学习资料记录不存在") from None
        except (ValueError, TypeError) as error:
            raise HTTPException(400, str(error)) from None
        except OSError:
            raise HTTPException(400, "补充题图无法读取或保存，请检查文件") from None

    @routes.delete("/api/study-documents/{document_id}")
    @boundary
    def remove_document(document_id: str):
        repository().set_deleted("study_documents", document_id, True)
        return {"ok": True}

    @routes.post("/api/study-documents/{document_id}/restore")
    @boundary
    def restore_document(document_id: str):
        repository().set_deleted("study_documents", document_id, False)
        return {"ok": True}

    @routes.get("/api/study-questions")
    @boundary
    def questions(document_id: str = Query(min_length=1), include_deleted: bool = False):
        return repository().questions(document_id, include_deleted=include_deleted)

    @routes.post("/api/study-questions", status_code=201)
    @boundary
    def create_question(payload: QuestionPayload):
        return repository().save_question(payload.document_id, payload.model_dump(exclude={"document_id"}))

    @routes.get("/api/study-questions/{question_id}")
    @boundary
    def question(question_id: str, include_deleted: bool = False):
        return repository().question(question_id, include_deleted=include_deleted)

    @routes.put("/api/study-questions/{question_id}")
    @boundary
    def update_question(question_id: str, payload: QuestionPayload):
        return repository().save_question(
            payload.document_id, payload.model_dump(exclude={"document_id"}), question_id,
        )

    @routes.delete("/api/study-questions/{question_id}")
    @boundary
    def remove_question(question_id: str):
        repository().set_deleted("study_questions", question_id, True)
        return {"ok": True}

    @routes.post("/api/study-questions/{question_id}/restore")
    @boundary
    def restore_question(question_id: str):
        repository().set_deleted("study_questions", question_id, False)
        return {"ok": True}

    return routes
