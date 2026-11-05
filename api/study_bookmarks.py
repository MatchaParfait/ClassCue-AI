"""Local study navigation; no transcription or model calls."""

from functools import wraps

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from classcue.study_bookmarks import StudyBookmarkStore


class LibraryPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    library_id: str = Field(min_length=1, max_length=200)


class ProgressPayload(LibraryPayload):
    position_ms: int = Field(strict=True, ge=0, le=2**53 - 1)
    duration_ms: int = Field(default=0, strict=True, ge=0, le=2**53 - 1)
    observed_at: int = Field(strict=True, ge=0, le=2**53 - 1)


class BookmarkPayload(LibraryPayload):
    position_ms: int | None = Field(default=None, strict=True, ge=0, le=2**53 - 1)
    note: str = Field(default="", max_length=2000)


class NotePayload(LibraryPayload):
    note: str = Field(max_length=2000)


def boundary(method):
    @wraps(method)
    def wrapped(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except KeyError as error:
            raise HTTPException(404, str(error.args[0])) from None
        except ValueError as error:
            raise HTTPException(400, str(error)) from None
    return wrapped


def router(provider):
    routes = APIRouter()

    def repository(library_id=None):
        if library_id is not None and library_id != provider.library_id():
            raise HTTPException(409, "视频库已变化，请重新打开视频后重试")
        return StudyBookmarkStore(provider.get())

    @routes.get("/api/study-navigation")
    def navigation():
        return {"library_id": provider.library_id(), **repository().navigation()}

    @routes.get("/api/videos/{video_id}/study-progress")
    @boundary
    def progress(video_id: int, library_id: str = Query(min_length=1)):
        return repository(library_id).progress(video_id)

    @routes.put("/api/videos/{video_id}/study-progress")
    @boundary
    def save_progress(video_id: int, payload: ProgressPayload):
        return repository(payload.library_id).save_progress(
            video_id, payload.position_ms, payload.duration_ms, payload.observed_at)

    @routes.get("/api/study-bookmarks")
    @boundary
    def bookmarks(library_id: str = Query(min_length=1), folder_id: int | None = Query(default=None, gt=0),
                  video_id: int | None = Query(default=None, gt=0)):
        return repository(library_id).bookmarks(folder_id, video_id)

    @routes.post("/api/videos/{video_id}/bookmarks", status_code=201)
    @boundary
    def create(video_id: int, payload: BookmarkPayload):
        return repository(payload.library_id).create(video_id, payload.position_ms, payload.note)

    @routes.patch("/api/study-bookmarks/{bookmark_id}")
    @boundary
    def edit(bookmark_id: int, payload: NotePayload):
        return repository(payload.library_id).edit(bookmark_id, payload.note)

    @routes.delete("/api/study-bookmarks/{bookmark_id}")
    @boundary
    def delete(bookmark_id: int, payload: LibraryPayload):
        repository(payload.library_id).delete(bookmark_id)
        return {"deleted": True}

    return routes
