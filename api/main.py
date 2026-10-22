"""Local HTTP API for the classroom video review page.

The module exposes an application factory for tests and an ``app`` instance
for ASGI servers. Importing this module never starts a server.
"""

from __future__ import annotations

import asyncio
import mimetypes
import hashlib
import json
import os
import sqlite3
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, RLock
from tempfile import TemporaryDirectory
from typing import Annotated, Any, Callable, Literal
from urllib.parse import urlsplit

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

import classcue.asr_storage as asr_storage
import classcue.storage_migration as storage_migration
import classcue.application_data_migration as application_data_migration
from classcue.asr_status_cache import AsrStatusCache
from classcue.asr_gpu_status import inspect_cuda_device
from classcue.diagnostics import LOG_DIR as ACTIVE_LOG_DIR
from classcue.processing_control import ProcessingCancelled, ResourcePaused
from classcue.model_downloads import PreparationPaused, cleanup_incomplete, download_snapshot
from classcue.qwen_resilience import qwen_queue
from classcue.firered_resilience import firered_queue, resource_events, resource_profile
from classcue.ai_provider import (
    DEFAULT_OUTPUT_TOKENS, DEFAULT_TIMEOUT_SECONDS, DEFAULT_MAX_RETRIES, DEFAULT_REASONING_EFFORT,
    DEFAULT_REQUEST_CONCURRENCY, MAX_REQUEST_CONCURRENCY,
    ProviderProfile, ProviderSettings, create_judge, interface_key_path,
    interface_provider, load_provider_settings, resolve_interface_key,
    save_provider_settings,
    SavedConnection, provider_settings_lock,
)
from classcue.asr_models import (
    MODELS, MODEL_ROOT, OPERATION_LOCK, ModelBusyError, cache_size, format_size, get_model, model_is_downloaded,
    FIRERED_CPU_NOTICE, NANO_RETIRED_NOTICE, RETIRED_MODELS, require_selectable_model,
    select_model, selected_model_id, selected_firered_device,
    selected_paraformer_device, selected_nano_device, selected_dolphin_device,
)
from classcue.local_credentials import delete_api_key, save_api_key
from classcue.path_picker import discard_path_picker, read_path_picker_result, start_path_picker
from classcue.picker_manager import PickerManager
from classcue.diagnostics import configure_logging, get_logger, log_exception, redact_text
from classcue.database_lease import DatabaseLease
from classcue.desktop_startup import save_library_snapshot, save_startup_cover
from classcue.qwen_runtime import clear_runtime, runtime_is_ready, runtime_size, runtime_python as qwen_runtime_python
from classcue.firered_runtime import (
    runtime_is_ready as firered_runtime_is_ready, runtime_size as firered_runtime_size,
    runtime_python as firered_runtime_python,
)
from classcue.paraformer_runtime import (
    runtime_is_ready as paraformer_runtime_is_ready, runtime_size as paraformer_runtime_size,
    cuda_status as paraformer_cuda_status,
)
from classcue.nano_runtime import (
    runtime_is_ready as nano_runtime_is_ready, runtime_size as nano_runtime_size,
    cuda_status as nano_cuda_status,
)
from classcue.dolphin_runtime import (
    runtime_is_ready as dolphin_runtime_is_ready, runtime_size as dolphin_runtime_size,
    cuda_status as dolphin_cuda_status,
)
from classcue.transcriber import asr_session, clear_asr_model, clear_asr_runtime, prepare_asr_model
from classcue.cleanup_progress import cleanup_events, CleanupProgress
from classcue.video_pipeline import (
    MAX_VIDEO_BYTES, MAX_VIDEO_DURATION_MS, SUPPORTED_VIDEO_SUFFIXES,
    prepare_video_for_analysis, process_video_once, process_saved_analysis_once,
)
from classcue.app_storage import load_data_root
from classcue.video_store import VideoStore
from classcue.recall_schemes import ModeId, RecallPromptInput, RecallSchemeInput, freeze_schemes, preview_recall_prompt
from classcue.subtitle_search import MAX_PAGE_SIZE, MAX_QUERY_LENGTH, MAX_SQLITE_INTEGER, validate_query
from api.denoise import router as denoise_router
from classcue.video_library import register_managed_video, video_filename
from classcue.video_thumbnail import ThumbnailUnavailable, video_thumbnail, video_preview_frame
from classcue.api_failure import ApiUnavailable
from classcue.web_batch import batch_summary, make_batch, normalize_batch, preview_directories, run_web_batch
from classcue.llm_experiments import (
    LabConfig, execute_run, prepare_snapshot, register_run, require_approval,
)


MarkerStatus = Literal["active", "mastered", "excluded"]
MarkerStatusFilter = Literal["active", "mastered", "excluded", "all"]
LOGGER = get_logger(__name__)
DIRECTORY_PROJECT_ROOT = Path(__file__).resolve().parents[1]
ACTIVE_ASR_STORAGE_ROOT = MODEL_ROOT.parent.parent


class MarkerPatch(BaseModel):
    """Accept either frontend marker field spelling, and no other fields."""

    model_config = ConfigDict(extra="forbid")

    review_status: MarkerStatus | None = None
    status: MarkerStatus | None = None

    @model_validator(mode="after")
    def require_one_status_field(self) -> "MarkerPatch":
        if (self.review_status is None) == (self.status is None):
            raise ValueError("只能提供 review_status 或 status 其中一个字段")
        return self

    @property
    def resolved_status(self) -> MarkerStatus:
        return self.review_status or self.status  # type: ignore[return-value]


class StorageDirectoriesPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asr_storage_root: str = Field(min_length=1)
    audio_cache_dir: str | None = None
    log_dir: str = Field(min_length=1)
    library_id: str | None = None
    video_library_dir: str | None = None
    media_storage_root: str | None = Field(default=None, min_length=1)
    delete_source_after_import: bool | None = Field(default=None, strict=True)


class StorageMigrationPayload(StorageDirectoriesPayload):
    plan_id: str


class ApplicationDataPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    destination: str = Field(min_length=1)
    library_id: str
    plan_id: str | None = None


class MarkerJumpTimePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_start_ms: int | None = Field(..., ge=0, strict=True)


class MarkerImportancePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    importance: Literal["standard", "important", "critical"]


class ASRSelectionPayload(BaseModel):
    model_id: str
    firered_device: Literal["gpu", "cpu"] | None = None
    paraformer_device: Literal["gpu", "cpu"] | None = None
    nano_device: Literal["gpu", "cpu"] | None = None
    dolphin_device: Literal["gpu", "cpu"] | None = None


class VideoRegisterPayload(BaseModel):
    path: str | None = None
    video_path: str | None = None
    display_name: str | None = None
    filename: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def resolve_path(self) -> "VideoRegisterPayload":
        value = (self.path or self.video_path or "").strip()
        if not value:
            raise ValueError("请提供本机视频路径")
        self.path = value
        return self


class FolderCreatePayload(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    parent_id: int | None = None


class VideoFilenamePayload(BaseModel):
    filename: str = Field(min_length=1, max_length=200)


class FolderRenamePayload(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class FolderParentPayload(BaseModel):
    parent_id: int | None = None


class VideoFolderPayload(BaseModel):
    folder_id: int | None = None


class LibraryReorderPayload(BaseModel):
    parent_id: int | None = None
    # Items are intentionally tagged so folders and videos can be interleaved.
    ordered_items: list[dict[str, object]] = Field(default_factory=list)


class QwenSettingsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["auto", "low-resource", "manual"] = "auto"
    batch_size: int = Field(default=2, ge=1, le=8)
    chunk_seconds: int = Field(default=90, ge=30, le=1200)
    align_batch_size: int = Field(default=2, ge=1, le=8)
    load_profile: Literal["standard", "low-memory"] = "standard"
    auto_recovery: bool = True
    bs2_free_mib: int = Field(default=6656, ge=0)
    audio_profile: Literal["general", "mostly-silent"] = "general"


class ResumePayload(BaseModel):
    skip_current: bool = False


class VideoProcessPayload(BaseModel):
    analysis_mode: ModeId = "medium_recall"
    analysis_modes: list[ModeId] | None = Field(default=None, min_length=1, max_length=8)
    analysis_schemes: dict[str, dict] = Field(default_factory=dict)
    use_keyword_prefilter: bool = False
    filter_formula_noise: bool = False
    asr_model_id: str | None = None
    exam_focus: bool | None = None
    extension_focus: bool | None = None
    keywords: list[str] = Field(default_factory=list)
    filter_formula: str | None = Field(default=None, max_length=2000)
    filter_low_information: bool | None = None
    force_retranscribe: bool = False
    qwen_options: QwenSettingsPayload = Field(default_factory=QwenSettingsPayload)

    @model_validator(mode="after")
    def resolve_ui_options(self) -> "VideoProcessPayload":
        if self.exam_focus is not None or self.extension_focus is not None:
            exam = self.exam_focus is not False
            extension = self.extension_focus is not False
            if not exam and not extension:
                raise ValueError("请至少选择期末考点或扩展重点")
            self.analysis_mode = "strict_exam_intent" if exam and not extension else "high_recall"
        self.analysis_modes = list(dict.fromkeys(self.analysis_modes or [self.analysis_mode]))
        self.analysis_mode = self.analysis_modes[0]
        self.keywords = list(dict.fromkeys(word.strip() for word in self.keywords if word.strip()))
        if self.keywords:
            self.use_keyword_prefilter = True
        if self.filter_low_information is not None:
            self.filter_formula_noise = self.filter_low_information
        return self


class ExperimentPayload(LabConfig):
    name: str = Field(min_length=1, max_length=200)
    approved_snapshot_hash: str | None = None


class TranscribePayload(BaseModel):
    asr_model_id: str | None = None
    force_retranscribe: bool = False
    qwen_options: QwenSettingsPayload = Field(default_factory=QwenSettingsPayload)


class RecallAnalysisPayload(BaseModel):
    analysis_mode: ModeId


class BatchProcessPayload(VideoProcessPayload):
    folders: list[str] = Field(default_factory=list)
    paths: list[str] = Field(default_factory=list)
    folder_paths: list[str] = Field(default_factory=list)
    folder_ids: list[int] = Field(default_factory=list)
    video_ids: list[Annotated[int, Field(gt=0, strict=True)]] = Field(default_factory=list)
    import_only: bool = False

    @model_validator(mode="after")
    def resolve_folders(self) -> "BatchProcessPayload":
        values = [*self.folders, *self.paths, *self.folder_paths]
        self.folders = list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
        self.folder_ids = list(dict.fromkeys(int(value) for value in self.folder_ids))
        self.video_ids = list(dict.fromkeys(self.video_ids))
        if not self.folders and not self.folder_ids and not self.video_ids:
            raise ValueError("请至少选择一个视频或视频文件夹")
        return self


class BatchPreviewPayload(BaseModel):
    folders: list[str] = Field(min_length=1)


class AISettingsPayload(BaseModel):
    connection_id: str | None = None
    connection_name: str | None = Field(default=None, min_length=1, max_length=80)
    api_key: str | None = Field(default=None, repr=False)
    remove_key: bool = False
    provider: Literal["deepseek", "openai_compatible"] | None = None
    service_type: Literal["deepseek", "openai_compatible"] | None = None
    base_url: str
    model: str
    max_tokens: int = Field(default=DEFAULT_OUTPUT_TOKENS, ge=256, le=131072)
    output_tokens: int | None = Field(default=None, ge=1, le=131072)
    token_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    json_mode: bool = True
    timeout_seconds: float = Field(default=DEFAULT_TIMEOUT_SECONDS, ge=5, le=600)
    max_retries: int = Field(default=DEFAULT_MAX_RETRIES, ge=0, le=5)
    request_concurrency: int = Field(
        default=DEFAULT_REQUEST_CONCURRENCY, ge=1, le=MAX_REQUEST_CONCURRENCY, strict=True,
    )
    reasoning_effort: Literal["low", "medium", "high"] = DEFAULT_REASONING_EFFORT

    @field_validator("reasoning_effort", mode="before")
    @classmethod
    def default_missing_reasoning_effort(cls, value: object) -> object:
        return DEFAULT_REASONING_EFFORT if value is None or value == "" else value

    @model_validator(mode="after")
    def resolve_output_tokens(self) -> "AISettingsPayload":
        if self.output_tokens is not None:
            self.max_tokens = max(256, self.output_tokens)
        return self


class AIKeyPayload(BaseModel):
    api_key: str
    base_url: str | None = None
    connection_id: str | None = None


class AIEndpointPayload(BaseModel):
    base_url: str | None = None
    connection_id: str | None = None
    model: str | None = None
    api_key: str | None = Field(default=None, repr=False)
    settings: AISettingsPayload | None = None


class AIConnectionPayload(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    source_id: str | None = None
    settings: AISettingsPayload | None = None
    api_key: str | None = Field(default=None, repr=False)
    remove_key: bool = False


class AIConnectionRenamePayload(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class PickerPayload(BaseModel):
    kind: Literal["file", "directory"]


class JobManager:
    """Single local worker with durable status rows and restart recovery."""

    def __init__(self, provider: "StoreProvider") -> None:
        from classcue.live_transcript import LiveTranscriptBuffer

        self.provider = provider
        self.live_transcripts = LiveTranscriptBuffer()
        from classcue.analysis_events import LiveAnalysisBuffer
        self.live_analysis = LiveAnalysisBuffer()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="classroom-job")
        self._lock = RLock()
        self._futures: dict[str, Future[Any]] = {}
        self._cancel_events: dict[str, Event] = {}
        self._pause_events: dict[str, Event] = {}
        self._closing = False
        self.before_submit = lambda _kind: None

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _row_payload(row: sqlite3.Row, store=None) -> dict[str, object]:
        options = json.loads(row["request_json"] or "{}").get("options", {})
        return {
            "id": str(row["id"]),
            "video_id": int(row["video_id"]) if row["video_id"] is not None else None,
            "job_type": row["job_type"],
            "status": row["status"],
            "stage": row["stage"],
            "progress": float(row["progress"] or 0),
            "message": row["message"] or "",
            "error_message": row["error_message"] or "",
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "created_at": row["created_at"],
            "resource": json.loads(row["resource_json"]) if row["resource_json"] else None,
            "preparation": json.loads(row["preparation_json"]) if row["preparation_json"] else None,
            "cleanup": json.loads(row["cleanup_json"]) if row["cleanup_json"] else None,
            "migration": json.loads(row["request_json"] or "{}").get("migration"),
            "model_id": json.loads(row["request_json"] or "{}").get("model_id"),
            "analysis_modes": options.get("analysis_modes") or ([options["analysis_mode"]] if options.get("analysis_mode") else []),
            "analysis_schemes": {key: {field: value.get(field) for field in ("id", "name", "base_mode", "revision")}
                                 for key, value in options.get("analysis_schemes", {}).items()},
            **JobManager._batch_payload(row, store),
        }

    @staticmethod
    def _batch_payload(row: sqlite3.Row, store=None) -> dict:
        if row["job_type"] != "batch_video_process":
            return {}
        state = json.loads(row["request_json"] or "{}")
        if state.get("version") != 2:
            return {}
        return {
            "batch_summary": batch_summary(state, store),
            "import_only": bool(state.get("import_only", False)),
            "batch_entries": [
                {key: entry.get(key) for key in ("video_id", "source_path", "label", "status", "error", "warning")}
                for entry in state.get("entries", [])
            ],
            "folders": state.get("folders", []),
        }

    def is_running(self) -> bool:
        with self._lock:
            return bool(self._cancel_events or self.provider.get().list_active_jobs())

    def _record_failure(self, store, job_id: str, exc: BaseException, **context) -> None:
        log_exception(LOGGER, "Background job failed", exc=exc, job_id=job_id, **context)
        if store is None:
            return
        try:
            store.update_job(
                job_id, status="failed", stage="failed", message="任务失败",
                error_message=redact_text(str(exc) or type(exc).__name__),
                finished_at=self._timestamp(),
            )
        except Exception as write_error:
            log_exception(
                LOGGER, "Could not persist job failure", exc=write_error, job_id=job_id,
            )

    def submit(
        self,
        job_type: str,
        video_id: int | None,
        operation: Callable[
            [Callable[[float, str, str | None], None], Callable[[], bool]], object
        ],
        request: dict | None = None,
        *,
        job_id: str | None = None,
    ) -> str:
        with self._lock:
            if self._closing:
                raise RuntimeError("后端正在关闭，不能启动新任务。")
            self.before_submit(job_type)
            store = self.provider.get()
            if self._cancel_events or store.list_active_jobs():
                raise RuntimeError("已有任务正在运行，请等待当前任务完成。")
            job_id = job_id or uuid.uuid4().hex
            try:
                store.create_job(job_id, job_type, video_id)
                if request:
                    store.update_job(job_id, request_json=json.dumps(request, ensure_ascii=False))
                self._cancel_events[job_id] = Event()
                self._pause_events[job_id] = Event()
                model_id = (request or {}).get("model_id") or (request or {}).get("options", {}).get("asr_model_id")
                future = self.executor.submit(
                    self._run, job_id, operation, store, job_type, video_id, model_id,
                )
                self._futures[job_id] = future
                return job_id
            except Exception as exc:
                self._futures.pop(job_id, None)
                self._cancel_events.pop(job_id, None)
                self._pause_events.pop(job_id, None)
                self._record_failure(store, job_id, exc, stage="submitting", job_type=job_type)
                raise

    def _run(
        self,
        job_id: str,
        operation: Callable[
            [Callable[[float, str, str | None], None], Callable[[], bool]], object
        ],
        store: VideoStore | None = None,
        job_type: str | None = None,
        video_id: int | None = None,
        model_id: str | None = None,
    ) -> None:
        current_stage = "starting"

        def should_cancel() -> bool:
            return cancel_event.is_set()

        def checkpoint() -> None:
            if should_cancel():
                raise ProcessingCancelled("用户已请求停止处理")
            event = self._pause_events.get(job_id)
            if event and event.is_set():
                raise PreparationPaused("下载已暂停，已保留文件和续传记录")

        def update(progress: float, message: str, stage: str | None = None) -> None:
            nonlocal current_stage
            checkpoint()
            current_stage = stage or _infer_job_stage(message)
            store.update_job(
                job_id,
                status="running",
                progress=max(0.0, min(1.0, float(progress))),
                message=str(message),
                stage=current_stage,
            )

        try:
            store = store if store is not None else self.provider.get()
            cancel_event = self._cancel_events[job_id]
            store.update_job(job_id, status="running", stage=current_stage, started_at=self._timestamp())

            def report_resource(resource):
                nonlocal current_stage
                if should_cancel():
                    raise ProcessingCancelled("用户已请求停止处理")
                if resource.get("status") == "recovering":
                    current_stage = "recovering"
                store.update_job(job_id, resource_json=json.dumps(resource, ensure_ascii=False),
                                 stage="recovering" if resource.get("status") == "recovering" else None)

            from classcue.preparation_progress import preparation_events

            def report_preparation(detail):
                checkpoint()
                store.update_job(job_id, preparation_json=json.dumps(detail, ensure_ascii=False))

            def report_cleanup(detail):
                nonlocal current_stage
                checkpoint()
                current_stage = "cleaning"
                fraction = detail.get("phase_progress")
                store.update_job(job_id, cleanup_json=json.dumps(detail, ensure_ascii=False),
                                 progress=fraction if fraction is not None else 0,
                                 stage="cleaning", message=detail["message"])

            from classcue.transcript_events import transcript_observer
            from classcue.analysis_events import analysis_observer

            preview = (
                (lambda segments, watermark: self.live_transcripts.publish(job_id, video_id, segments, watermark))
                if video_id is not None and job_type in {"video_process", "video_transcribe"} else None
            )
            def analysis_preview(current_video, mode, **state):
                if current_video == video_id and job_type in {"video_process", "video_analysis"}:
                    self.live_analysis.publish(job_id, video_id, mode, **state)

            with analysis_observer(analysis_preview), transcript_observer(preview), resource_events(report_resource), preparation_events(report_preparation, checkpoint=checkpoint), cleanup_events(report_cleanup), asr_session():
                result = operation(update, should_cancel)
            if should_cancel() and not (isinstance(result, dict) and result.get("committed")):
                raise ProcessingCancelled("用户已请求停止处理")
            store.update_job(
                job_id,
                status=result.get("job_status", "completed") if isinstance(result, dict) else "completed",
                stage=result.get("job_status", "completed") if isinstance(result, dict) else "completed",
                progress=1.0,
                message=result.get("message", "任务完成") if isinstance(result, dict) else "任务完成",
                finished_at=self._timestamp(),
            )
        except (ResourcePaused, ProcessingCancelled) as exc:
            try:
                if isinstance(exc, ResourcePaused):
                    store.update_job(
                        job_id, status="resource_paused", stage="resource_paused",
                        message="资源不足，进度已保存，等待继续", error_message=redact_text(exc),
                        request_json=json.dumps(exc.resume_request, ensure_ascii=False) if exc.resume_request else None,
                        finished_at=self._timestamp(),
                    )
                else:
                    row = store.get_job(job_id)
                    paused = isinstance(exc, PreparationPaused)
                    store.update_job(
                        job_id, status="interrupted", stage="download_paused" if paused else "interrupted",
                        message="下载已暂停，已保留文件和续传记录" if paused else "已停止处理",
                        error_message="" if paused else redact_text(exc),
                        finished_at=self._timestamp(),
                    )
                    if row and row["video_id"] is not None:
                        store.mark_interrupted_video_failed(int(row["video_id"]))
            except Exception as write_error:
                log_exception(
                    LOGGER, "Could not persist job interruption", exc=write_error,
                    job_id=job_id, reason=type(exc).__name__,
                )
        except Exception as exc:
            self._record_failure(
                store, job_id, exc, job_type=job_type, video_id=video_id,
                model_id=model_id, stage=current_stage,
            )
        finally:
            with self._lock:
                self._futures.pop(job_id, None)
                self._cancel_events.pop(job_id, None)
                self._pause_events.pop(job_id, None)

    def pause_preparation(self, job_id: str) -> dict[str, object]:
        with self._lock:
            row = self.provider.get().get_job(job_id)
            if row is None:
                raise KeyError(job_id)
            if row["job_type"] != "asr_prepare":
                raise ValueError("只有独立模型准备任务支持暂停下载")
            if row["status"] not in {"queued", "running"}:
                return self._row_payload(row, self.provider.get())
            detail = json.loads(row["preparation_json"] or "{}")
            if detail.get("phase") in {"installing", "finalizing"}:
                raise ValueError("当前正在安装环境或完成准备，请在权重下载阶段暂停")
            event = self._pause_events.get(job_id)
            if event is None:
                raise ValueError("任务进程已退出，请刷新后继续下载")
            event.set()
            self.provider.get().update_job(job_id, stage="pausing", message="正在暂停，正在保存续传记录…")
            return self._row_payload(self.provider.get().get_job(job_id), self.provider.get())

    def cancel(self, job_id: str) -> dict[str, object]:
        with self._lock:
            existing = self.provider.get().get_job(job_id)
            if existing and existing["job_type"] == "storage_migration":
                raise ValueError("目录迁移会自动完成或恢复原设置，请等待迁移结束")
            event = self._cancel_events.get(job_id)
            if event is not None:
                event.set()
            row = self.provider.get().get_job(job_id)
            if row is None:
                raise KeyError(job_id)
            if row["status"] in {"completed", "failed", "interrupted"}:
                return self._row_payload(row, self.provider.get())
            if row["status"] in {"resource_paused", "awaiting_api"}:
                self.provider.get().update_job(job_id, status="interrupted", stage="interrupted",
                                               message="已停止暂停的队列，缓存仍保留",
                                               finished_at=self._timestamp())
                return self._row_payload(self.provider.get().get_job(job_id), self.provider.get())

            future = self._futures.get(job_id)
            if future is not None and future.cancel():
                try:
                    video_id = row["video_id"]
                    self.provider.get().update_job(
                        job_id,
                        status="interrupted",
                        stage="interrupted",
                        message="已停止排队任务",
                        finished_at=self._timestamp(),
                    )
                    if video_id is not None:
                        self.provider.get().mark_interrupted_video_failed(int(video_id))
                finally:
                    self._futures.pop(job_id, None)
                    self._cancel_events.pop(job_id, None)
                    self._pause_events.pop(job_id, None)
            else:
                self.provider.get().update_job(
                    job_id,
                    message="正在停止，等待当前步骤安全退出",
                )
            current = self.provider.get().get_job(job_id)
            return self._row_payload(current or row, self.provider.get())

    def get(self, job_id: str) -> dict[str, object] | None:
        row = self.provider.get().get_job(job_id)
        if row and row["job_type"] == "batch_video_process":
            state = json.loads(row["request_json"] or "{}")
            if state.get("kind") == "batch" and state.get("version") != 2:
                upgraded = dict(row)
                upgraded["request_json"] = json.dumps(normalize_batch(state, self.provider.get()), ensure_ascii=False)
                return self._row_payload(upgraded, self.provider.get())
        return self._row_payload(row, self.provider.get()) if row else None

    def shutdown(self, *, wait: bool = False) -> None:
        with self._lock:
            self._closing = True
            job_ids = list(self._cancel_events)
            for event in self._cancel_events.values():
                event.set()
        for job_id in job_ids:
            try:
                self.cancel(job_id)
            except Exception as exc:
                log_exception(LOGGER, "Job cancellation during shutdown failed", exc=exc, job_id=job_id)
        self.executor.shutdown(wait=wait, cancel_futures=True)
        with self._lock:
            for job_id, future in list(self._futures.items()):
                if future.done():
                    self._futures.pop(job_id, None)
                    self._cancel_events.pop(job_id, None)
                    self._pause_events.pop(job_id, None)


def _infer_job_stage(message: str) -> str:
    text = message.lower()
    if "ai" in text or "筛选" in message:
        return "analyzing"
    if (any(token in message for token in ("转写", "音轨", "字幕", "读取音频"))
            or "transcrib" in text or "vad" in text):
        return "transcribing"
    if "保存重点" in message:
        return "saving_markers"
    if "完成" in message:
        return "completed"
    return "processing"


class StoreProvider:
    """Resolve and pin exactly one database for this server's lifetime."""

    def __init__(self, injected_store: VideoStore | None = None) -> None:
        self.injected_store = injected_store
        self._store = injected_store
        self._database: Path | None = (
            Path(injected_store.path).expanduser().resolve() if injected_store is not None else None
        )

    def database_path(self) -> Path:
        if self._database is None:
            self._database = load_data_root() / "project.db"
        return self._database

    def data_root(self) -> Path:
        return self.database_path().parent

    def get(self) -> VideoStore:
        if self._store is None:
            self._store = VideoStore(self.database_path())
        return self._store

    def library_id(self) -> str:
        return asr_storage.library_identity(DIRECTORY_PROJECT_ROOT, self.database_path())


def _row_to_dict(row: sqlite3.Row) -> dict[str, object]:
    return {key: row[key] for key in row.keys()}


def _video_or_404(store: VideoStore, video_id: int) -> sqlite3.Row:
    video = store.get_video(video_id)
    if video is None:
        raise HTTPException(status_code=404, detail=f"视频 {video_id} 不存在")
    return video


def _marker_or_404(store: VideoStore, marker_id: int) -> sqlite3.Row:
    with store.connect() as connection:
        marker = connection.execute(
            "SELECT * FROM video_markers WHERE id = ?", (marker_id,)
        ).fetchone()
    if marker is None:
        raise HTTPException(status_code=404, detail=f"重点 {marker_id} 不存在")
    return marker


def _marker_rows(
    store: VideoStore,
    video_id: int,
    mode: str | None,
    status: MarkerStatusFilter | None,
) -> list[sqlite3.Row]:
    if status == "excluded":
        rows = store.list_excluded_markers(video_id)
        return [row for row in rows if mode is None or row["analysis_mode"] == mode]

    if status in {"active", "mastered", None}:
        rows = store.list_markers(video_id, mode)
        if status is None:
            return rows
        return [row for row in rows if row["review_status"] == status]

    # ``all`` is intentionally implemented here rather than by changing
    # VideoStore.list_markers(), whose existing contract hides excluded rows.
    with store.connect() as connection:
        if mode is None:
            return connection.execute(
                "SELECT * FROM video_markers WHERE video_id = ? ORDER BY start_ms, id",
                (video_id,),
            ).fetchall()
        return connection.execute(
            """
            SELECT * FROM video_markers
            WHERE video_id = ? AND analysis_mode = ?
            ORDER BY start_ms, id
            """,
            (video_id, mode),
        ).fetchall()


def _iter_file(path: Path, start: int, end: int, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
    remaining = end - start + 1
    with path.open("rb") as source:
        source.seek(start)
        while remaining:
            chunk = source.read(min(chunk_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def _parse_range(value: str, size: int) -> tuple[int, int] | None:
    if not value.startswith("bytes=") or "," in value:
        return None
    range_spec = value[6:].strip()
    if "-" not in range_spec:
        return None
    start_text, end_text = (part.strip() for part in range_spec.split("-", 1))
    try:
        if not start_text:
            suffix_length = int(end_text)
            if suffix_length <= 0:
                return None
            return max(0, size - suffix_length), size - 1
        start = int(start_text)
        if start < 0 or start >= size:
            return None
        end = size - 1 if not end_text else min(int(end_text), size - 1)
        if end < start:
            return None
        return start, end
    except ValueError:
        return None


def create_app(store: VideoStore | None = None) -> FastAPI:
    """Create the local review API around an optional VideoStore instance."""

    provider = StoreProvider(store)
    startup_snapshot_enabled = store is None and os.getenv("CLASSROOM_DESKTOP_MODE") == "1"
    jobs = JobManager(provider)
    lease = DatabaseLease()
    instance_id = uuid.uuid4().hex
    pickers = PickerManager(
        start=lambda kind: start_path_picker(kind),
        read=lambda state: read_path_picker_result(state),
        discard=lambda state: discard_path_picker(state),
    )
    status_cache = AsrStatusCache(
        DIRECTORY_PROJECT_ROOT / ".runtime" / "asr_status_cache.json" if store is None else None,
    )
    storage_migrating = Event()
    updating = Event()
    from classcue.app_updates import UpdateCenter, read_json as read_update_json, write_json as write_update_json
    from classcue.app_version import VERSION
    from api.updates import router as update_router
    update_center = UpdateCenter(DIRECTORY_PROJECT_ROOT)
    recovery_needs_restart = Event()

    def check_storage_before_job(kind):
        if updating.is_set():
            raise RuntimeError("正在准备更新，请等待软件重启")
        if storage_migrating.is_set() and kind != "storage_migration":
            raise RuntimeError("目录正在迁移，请等待迁移完成")
        if (kind != "storage_migration" and (recovery_needs_restart.is_set() or (
                asr_storage.read_configuration(DIRECTORY_PROJECT_ROOT).get("migration_requires_restart")
                and asr_storage.storage_root(DIRECTORY_PROJECT_ROOT) != ACTIVE_ASR_STORAGE_ROOT))):
            raise RuntimeError("模型资源已迁移，请重启软件后再处理或准备模型")
    jobs.before_submit = check_storage_before_job

    async def reap_pickers():
        while True:
            await asyncio.sleep(30)
            try:
                await asyncio.to_thread(pickers.reap_expired)
            except Exception as exc:
                log_exception(LOGGER, "Path picker reaper failed", exc=exc)

    @asynccontextmanager
    async def lifespan(_api: FastAPI):
        reaper = None
        try:
            # Acquire ownership before recovery can alter another server's jobs.
            lease.acquire(provider.database_path())
            configure_logging()
            provider.get().mark_jobs_interrupted()
            before_recovery = asr_storage.storage_root(DIRECTORY_PROJECT_ROOT)
            storage_migration.recover_migration(DIRECTORY_PROJECT_ROOT, provider.get())
            if before_recovery != asr_storage.storage_root(DIRECTORY_PROJECT_ROOT):
                # Modules may already have captured the uncommitted model path.
                recovery_needs_restart.set()
            reaper = asyncio.create_task(reap_pickers())
            if store is None:
                transaction_path = update_center.directory / "transaction.json"
                transaction = read_update_json(transaction_path, {})
                if (transaction.get("status") == "installed" and transaction.get("version") == VERSION
                        and update_center.portable()):
                    transaction.update(status="completed", message="新版本已启动；程序和数据库备份已保留")
                    write_update_json(transaction_path, transaction)
                update_center.start_background()
            yield
        finally:
            update_center.close()
            if reaper is not None:
                reaper.cancel()
                try:
                    await reaper
                except asyncio.CancelledError:
                    pass
            try:
                await asyncio.to_thread(pickers.shutdown)
            finally:
                try:
                    await asyncio.to_thread(jobs.shutdown, wait=True)
                finally:
                    lease.release()

    api = FastAPI(title="课堂视频重点复习 API", version=VERSION, lifespan=lifespan)
    api.include_router(denoise_router)
    api.include_router(update_router(update_center, jobs, provider, storage_migrating, updating))
    api.state.update_center = update_center
    api.state.updating = updating
    api.state.jobs = jobs
    api.state.pickers = pickers
    api.state.database_lease = lease
    api.state.asr_status_cache = status_cache
    api.state.storage_migrating = storage_migrating

    @api.middleware("http")
    async def guard_storage_migration(request: Request, call_next):
        if updating.is_set() and request.method not in {"GET", "HEAD", "OPTIONS"} and request.url.path != "/api/updates/cancel-install":
            return JSONResponse(status_code=409, content={"detail": "正在准备更新，请等待软件重启"})
        if storage_migrating.is_set() and request.method not in {"GET", "HEAD", "OPTIONS"}:
            return JSONResponse(status_code=409, content={"detail": "目录正在迁移，请等待迁移完成后操作"})
        return await call_next(request)

    @api.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        log_exception(
            LOGGER, "Unhandled API error", exc=exc, instance_id=instance_id,
            method=request.method, endpoint=request.url.path,
        )
        return JSONResponse(status_code=500, content={"detail": "后端发生异常，请查看本地诊断日志"})

    def library_payload() -> dict[str, object]:
        root = provider.data_root()
        database_path = provider.database_path()
        audio_dir = asr_storage.audio_directory(DIRECTORY_PROJECT_ROOT, root)
        active_jobs = provider.get().list_active_jobs()
        return {
            "data_root": str(root),
            "database_path": str(database_path),
            "temporary_audio_dir": str(audio_dir),
            "video_count": len(provider.get().list_videos()),
            "processing": bool(active_jobs),
            "active_job_id": str(active_jobs[0]["id"]) if active_jobs else None,
            "library_id": provider.library_id(),
            "library_scope": asr_storage.library_scope(DIRECTORY_PROJECT_ROOT, database_path),
        }

    @api.get("/api/library/status")
    def get_library_status() -> dict[str, object]:
        return library_payload()

    def directories_payload() -> dict[str, object]:
        root = provider.data_root()
        assets = asr_storage.storage_root(DIRECTORY_PROJECT_ROOT)
        logs = asr_storage.log_directory(DIRECTORY_PROJECT_ROOT)
        paused = bool(provider.get().list_paused_jobs())
        videos = asr_storage.video_directory(DIRECTORY_PROJECT_ROOT, root)
        audio = asr_storage.audio_directory(DIRECTORY_PROJECT_ROOT, root)
        configured_media = asr_storage.configured_media_root(DIRECTORY_PROJECT_ROOT)
        media = configured_media
        if media is None and videos.parent == audio.parent:
            media = videos.parent
        with provider.get().connect() as connection:
            migration_row = connection.execute(
                "SELECT * FROM processing_jobs WHERE job_type = 'storage_migration' "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            ).fetchone()
        return {
            "media_storage_root": str(media) if media is not None else None,
            "media_storage_bound": configured_media is not None,
            "media_storage_default_root": str(root),
            "asr_storage_root": str(assets),
            "audio_cache_dir": str(asr_storage.audio_directory(DIRECTORY_PROJECT_ROOT, root)),
            "audio_cache_default_dir": str(root / "temporary_audio"),
            "video_library_dir": str(asr_storage.video_directory(DIRECTORY_PROJECT_ROOT, root)),
            "video_library_default_dir": str(root / "videos"),
            "delete_source_after_import": asr_storage.delete_source_after_import(DIRECTORY_PROJECT_ROOT, root),
            "log_dir": str(logs),
            "model_dir": str(assets / "models" / "asr"),
            "runtime_dir": str(assets / ".runtime"),
            "package_cache_dir": str(assets / ".runtime" / "cache"),
            "active_asr_storage_root": str(ACTIVE_ASR_STORAGE_ROOT),
            "active_log_dir": str(ACTIVE_LOG_DIR),
            "restart_required": assets != ACTIVE_ASR_STORAGE_ROOT or logs != ACTIVE_LOG_DIR,
            "busy": storage_migrating.is_set() or jobs.is_running() or paused,
            "has_paused_jobs": paused,
            "config_dir": str(asr_storage.application_config_directory(DIRECTORY_PROJECT_ROOT)),
            "application_data": application_data_migration.status(DIRECTORY_PROJECT_ROOT, provider.database_path()),
            "data_root": str(root),
            "library_id": provider.library_id(),
            "migration_job": JobManager._row_payload(migration_row, provider.get()) if migration_row else None,
        }

    @api.get("/api/settings/directories")
    def get_directories() -> dict[str, object]:
        try:
            return directories_payload()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def check_application_data_change(payload):
        if updating.is_set():
            raise HTTPException(status_code=409, detail="正在准备更新")
        if payload.library_id != provider.library_id():
            raise HTTPException(status_code=409, detail="视频库身份已变化，请刷新设置")
        if storage_migrating.is_set() or jobs.is_running() or provider.get().list_paused_jobs():
            raise HTTPException(status_code=409, detail="请先完成或停止任务，再迁移应用数据")

    @api.post("/api/settings/application-data/preview")
    def preview_application_data(payload: ApplicationDataPayload):
        with jobs._lock:
            check_application_data_change(payload)
            try:
                return application_data_migration.public_plan(application_data_migration.plan(
                    DIRECTORY_PROJECT_ROOT, provider.database_path(), payload.destination,
                ))
            except (ValueError, OSError) as exc:
                raise HTTPException(status_code=400, detail=f"无法迁移应用数据：{exc}") from exc

    @api.post("/api/settings/application-data/migrate", status_code=202)
    def migrate_application_data(payload: ApplicationDataPayload, background: BackgroundTasks):
        with jobs._lock:
            check_application_data_change(payload)
            try:
                application_data_migration.require_owned_launcher(DIRECTORY_PROJECT_ROOT, instance_id)
                record = application_data_migration.schedule(
                    DIRECTORY_PROJECT_ROOT, provider.database_path(), payload.destination, payload.plan_id,
                )
                storage_migrating.set()
                def restart():
                    try:
                        process = application_data_migration.restart_owned_application(DIRECTORY_PROJECT_ROOT, instance_id, record["id"])
                        # Usually this API process exits during the helper's restart.
                        # If ownership checks fail before shutdown, restore interactivity.
                        if process.wait() != 0:
                            raise RuntimeError("自动重启未完成，请查看本地启动日志")
                    except Exception as exc:
                        record.update(status="failed", message=f"无法启动迁移重启：{exc}")
                        application_data_migration._write_json(application_data_migration.journal_path(DIRECTORY_PROJECT_ROOT), record)
                        storage_migrating.clear()
                background.add_task(restart)
                return {"id": record["id"], "status": "restarting"}
            except (ValueError, OSError) as exc:
                raise HTTPException(status_code=400, detail=f"无法迁移并重启：{exc}") from exc

    def check_directory_change(payload):
        if updating.is_set():
            raise HTTPException(status_code=409, detail="正在准备更新")
        if payload.library_id is not None and payload.library_id != provider.library_id():
            raise HTTPException(status_code=409, detail="连接的视频库已变化，请刷新目录设置后重试")
        if storage_migrating.is_set() or jobs.is_running() or provider.get().list_paused_jobs():
            raise HTTPException(status_code=409, detail="请先完成或停止正在运行、暂停中的任务，再修改存储目录")
        # Protect sources outside the managed library too.
        _media, _videos, audio = asr_storage.resolve_media_directories(
            DIRECTORY_PROJECT_ROOT, provider.data_root(), media_storage_root=payload.media_storage_root,
            video_library_dir=payload.video_library_dir, audio_cache_dir=payload.audio_cache_dir,
        )
        temporary_paths = (
            audio,
            asr_storage.validate_directory(payload.log_dir),
            asr_storage.validate_directory(payload.asr_storage_root) / "models" / "asr",
            asr_storage.validate_directory(payload.asr_storage_root) / ".runtime",
        )
        for video in provider.get().list_videos():
            asr_storage.validate_video_directory(Path(video["video_path"]).resolve(), *temporary_paths)

    @api.post("/api/settings/directories/preview")
    def preview_directory_migration(payload: StorageDirectoriesPayload) -> dict:
        with jobs._lock:
            try:
                check_directory_change(payload)
                return storage_migration.public_plan(storage_migration.plan_migration(
                    DIRECTORY_PROJECT_ROOT, provider.get(), payload.model_dump(),
                ))
            except (OSError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=f"无法迁移目录：{exc}") from exc

    @api.post("/api/settings/directories/migrate", status_code=202)
    def start_directory_migration(payload: StorageMigrationPayload) -> dict:
        with jobs._lock:
            try:
                check_directory_change(payload)
                job_id = uuid.uuid4().hex
                values = payload.model_dump(exclude={"plan_id"}) | {"_job_id": job_id}
                # Re-inventory in the worker; its signature must match the reviewed plan.
                def operation(_update, cancelled):
                    last_report = 0
                    def checkpoint():
                        if cancelled():
                            raise ProcessingCancelled("目录迁移已停止，原文件保留")
                    def report(detail):
                        nonlocal last_report
                        now = time.monotonic()
                        if now - last_report < .2 and detail["phase"] in {"copying", "verifying"}:
                            return
                        last_report = now
                        total = detail["bytes_total"]
                        progress = .9 * detail["bytes_completed"] / max(1, total)
                        if detail["phase"] == "completed":
                            progress = 1
                        provider.get().update_job(
                            job_id, stage=detail["phase"], progress=progress,
                            message={"copying": "正在复制文件", "verifying": "正在校验文件",
                                     "committing": "正在切换目录", "cleaning": "正在清理原文件",
                                     "completed": "目录迁移完成"}[detail["phase"]],
                            request_json=json.dumps({"migration": detail}, ensure_ascii=False),
                        )
                    try:
                        return storage_migration.migrate(
                            DIRECTORY_PROJECT_ROOT, provider.get(), values, expected_plan=payload.plan_id,
                            report=report, checkpoint=checkpoint,
                        )
                    finally:
                        storage_migrating.clear()
                storage_migrating.set()
                try:
                    jobs.submit("storage_migration", None, operation, job_id=job_id)
                except BaseException:
                    storage_migrating.clear()
                    raise
                return {"job_id": job_id}
            except (OSError, ValueError, RuntimeError) as exc:
                raise HTTPException(status_code=400, detail=f"无法迁移目录：{exc}") from exc

    @api.put("/api/settings/directories")
    def update_directories(payload: StorageDirectoriesPayload) -> dict[str, object]:
        # Retain the synchronous API for local clients; it uses the same safe mover.
        with jobs._lock:
            claimed = False
            try:
                check_directory_change(payload)
                storage_migrating.set()
                claimed = True
                storage_migration.migrate(DIRECTORY_PROJECT_ROOT, provider.get(), payload.model_dump())
                storage_migrating.clear()
                return directories_payload()
            except (OSError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=f"无法保存存储目录：{exc}") from exc
            finally:
                if claimed:
                    storage_migrating.clear()

    @api.get("/api/pickers")
    def invalid_picker_get() -> dict[str, str]:
        raise HTTPException(status_code=405, detail="请使用 POST /api/pickers 创建选择器")

    @api.post("/api/pickers")
    def create_picker(payload: PickerPayload) -> dict[str, str]:
        try:
            return pickers.create(payload.kind)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.get("/api/pickers/{picker_id}")
    def get_picker(picker_id: str) -> dict[str, object]:
        try:
            return pickers.get(picker_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="选择器不存在") from exc

    @api.delete("/api/pickers/{picker_id}")
    def cancel_picker(picker_id: str) -> dict[str, object]:
        try:
            return pickers.cancel(picker_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="选择器不存在") from exc

    @api.get("/api/health")
    def health() -> dict[str, str]:
        return {
            "status": "ok", "library_id": provider.library_id(),
            "service": "classroom-video-study", "instance_id": instance_id, "version": VERSION,
        }

    def video_file_change_locks() -> tuple[dict[int, str], str | None]:
        store = provider.get()
        locked = {}
        if storage_migrating.is_set():
            return locked, "目录正在迁移，请等待迁移完成后修改文件名"
        for row in [*store.list_active_jobs(), *store.list_paused_jobs()]:
            if row["job_type"] == "storage_migration":
                return locked, "目录正在迁移，请等待迁移完成后修改文件名"
            reason = ("该视频被资源暂停任务占用，请继续或停止任务后修改文件名"
                      if row["status"] == "resource_paused"
                      else "该视频在处理任务或批次队列中，请完成或停止任务后修改文件名")
            if row["video_id"] is not None:
                locked[int(row["video_id"])] = reason
            if row["job_type"] != "batch_video_process":
                continue
            try:
                state = json.loads(row["request_json"] or "{}")
                # Legacy queues store (ID, path) pairs; durable queues keep all entries.
                ids = [item[0] for item in state.get("items", [])]
                for entry in state.get("entries", []):
                    if entry.get("video_id"):
                        ids.append(entry["video_id"])
                        continue
                    existing = store.get_video_by_path(entry["source_path"]) if entry.get("source_path") else None
                    if existing is None and entry.get("content_hash"):
                        existing = store.get_video_by_hash(entry["content_hash"])
                    if existing is not None:
                        ids.append(existing["id"])
                for video_id in ids:
                    locked[int(video_id)] = reason
            except (ValueError, TypeError, KeyError, IndexError, AttributeError):
                return locked, "批次队列信息暂不可用，请完成或停止任务后修改文件名"
        return locked, None

    def video_payload(
        video: sqlite3.Row, importance_counts: dict[str, int] | None = None,
        file_locks: tuple[dict[int, str], str | None] | None = None,
    ) -> dict[str, object]:
        if importance_counts is None:
            importance_counts = provider.get().marker_importance_counts(int(video["id"])).get(int(video["id"]), {})
        locked, global_reason = file_locks if file_locks is not None else video_file_change_locks()
        return {
            "id": int(video["id"]),
            "name": video["display_name"] or video["filename"],
            "display_name": video["display_name"],
            "filename": video["filename"],
            "duration_ms": int(video["duration_ms"]),
            "source_path": video["video_path"],
            "video_path": video["video_path"],
            "source_folder": video["source_folder"],
            "folder_id": int(video["library_folder_id"]) if video["library_folder_id"] is not None else None,
            "library_order": int(video["library_order"] or 0),
            "status": video["status"],
            "file_change_block_reason": global_reason or locked.get(int(video["id"])),
            "transcription_model": video["transcription_model"] if "transcription_model" in video.keys() else "",
            "has_transcript": bool(video["has_transcript"]) if "has_transcript" in video.keys()
            else provider.get().has_transcript(int(video["id"])),
            "created_at": video["created_at"],
            "source_size": int(video["source_size"]),
            "source_mtime_ns": int(video["source_mtime_ns"]),
            "library_id": provider.library_id(),
            "marker_importance_counts": {level: importance_counts.get(level, 0)
                                         for level in ("standard", "important", "critical")},
        }

    def video_payloads(videos: list[sqlite3.Row]) -> list[dict[str, object]]:
        counts = provider.get().marker_importance_counts()
        file_locks = video_file_change_locks()
        return [video_payload(video, counts.get(int(video["id"]), {}), file_locks) for video in videos]

    def folder_payload(folder: sqlite3.Row) -> dict[str, object]:
        store = provider.get()
        folder_id = int(folder["id"])
        return {
            "id": folder_id,
            "name": folder["name"],
            "parent_id": int(folder["parent_id"]) if folder["parent_id"] is not None else None,
            "library_order": int(folder["library_order"] or 0),
            "created_at": folder["created_at"],
            "updated_at": folder["updated_at"],
            "video_count": int(folder["video_count"]) if "video_count" in folder.keys()
            else len(store.list_folder_videos(folder_id)),
            "marker_importance_counts": {
                level: int(folder[f"{level}_count"]) for level in ("standard", "important", "critical")
            },
            "library_id": provider.library_id(),
        }

    def folder_or_404(folder_id: int) -> sqlite3.Row:
        folder = provider.get().get_folder(folder_id)
        if folder is None:
            raise HTTPException(status_code=404, detail=f"文件夹 {folder_id} 不存在")
        return folder

    def asr_status_fingerprint(model_id: str) -> str:
        model = get_model(model_id)
        runtime_names = {
            "qwen": ("qwen-asr",), "firered": ("firered-asr2",),
            "paraformer": ("paraformer-cpu", "paraformer-gpu"),
            "nano": ("fun-asr-nano-cpu", "fun-asr-nano-gpu"),
            "dolphin": ("dolphin-cpu", "dolphin-gpu"),
        }.get(model.engine, ())
        paths = [MODEL_ROOT / model.id / "ready.json"]
        paths.extend(MODEL_ROOT.parent.parent / ".runtime" / name / "ready.json" for name in runtime_names)
        # Earlier full snapshots did not include device identity for these engines.
        signatures = ["gpu-device-v1"] if model.engine in {"qwen", "firered"} else []
        for path in paths:
            try:
                stat = path.stat()
                signatures.append((stat.st_mtime_ns, stat.st_size))
            except OSError:
                signatures.append(None)
        return hashlib.sha256(json.dumps(signatures).encode()).hexdigest()[:12]

    def asr_status_key(model_id: str) -> str:
        model = get_model(model_id)
        device = {
            "firered": selected_firered_device,
            "paraformer": selected_paraformer_device,
            "nano": selected_nano_device,
            "dolphin": selected_dolphin_device,
        }.get(model.engine, lambda: "")()
        return f"{MODEL_ROOT}|{model.id}|{device}|{asr_status_fingerprint(model.id)}"

    def invalidate_asr_status(model_id: str, *, sizes: bool = True) -> None:
        status_cache.invalidate(f"{MODEL_ROOT}|{get_model(model_id).id}|", sizes=sizes)

    def asr_model_payload(
        model_id: str, *, inspect_status: bool = True, full_check: bool = True,
        refresh_gpu: bool = False,
    ) -> dict[str, object]:
        model = get_model(model_id)
        retired = model.id in RETIRED_MODELS
        downloaded = model_is_downloaded(model.id) if inspect_status else None
        size_key = f"{MODEL_ROOT}|{model.id}|{asr_status_fingerprint(model.id)}"
        size = (
            status_cache.size(size_key + "|model-size", lambda: cache_size(model.id))
            if inspect_status and full_check else None
        )
        runtime_ready = (
            runtime_is_ready() if model.engine == "qwen" and inspect_status else
            firered_runtime_is_ready() if model.engine == "firered" and inspect_status else
            paraformer_runtime_is_ready(selected_paraformer_device())
            if model.engine == "paraformer" and inspect_status else
            nano_runtime_is_ready(selected_nano_device())
            if model.engine == "nano" and inspect_status else
            dolphin_runtime_is_ready(selected_dolphin_device())
            if model.engine == "dolphin" and inspect_status else
            True if model.engine not in {"qwen", "firered", "paraformer", "nano", "dolphin"} else None
        )
        recommendations = {
            "whisper-turbo": "日常课程，速度优先",
            "whisper-large-v3": "嘈杂音频或更看重识别准确率",
            "qwen3-asr-1.7b": "需要 Qwen 强制对齐或中文识别增强",
            "fireredasr2-aed": "官方 FireRedVAD 切段 + AED 字词时间戳，适合中文长课程",
            "paraformer-large-cpu": "中文离线转写，官方 BiCIF 字词时间戳 + FSMN-VAD",
            "fun-asr-nano-2512": "中文会议转写，官方 CTC 字级时间戳 + FSMN-VAD",
            "dolphin-small-cn": "中文与方言离线转写，CTC 时间戳 + 批量编码",
        }
        capabilities = (
            ["word_timestamps", "vad"] if model.engine in {"firered", "paraformer", "nano", "dolphin"}
            else []
        )
        gpu_probe = {
            "paraformer": paraformer_cuda_status,
            "nano": nano_cuda_status if not retired else None,
            "dolphin": dolphin_cuda_status,
        }.get(model.engine)
        gpu_status = (
            (gpu_probe(refresh=True) if refresh_gpu else gpu_probe())
            if gpu_probe and inspect_status and full_check else {}
        )
        if model.engine in {"qwen", "firered"} and inspect_status and full_check:
            python = qwen_runtime_python() if model.engine == "qwen" else firered_runtime_python()
            gpu_status = inspect_cuda_device(
                python, bool(runtime_ready), "Qwen" if model.engine == "qwen" else "FireRedASR2S",
            )
        runtime_scan = {
            "qwen": runtime_size, "firered": firered_runtime_size, "paraformer": paraformer_runtime_size,
            "nano": nano_runtime_size, "dolphin": dolphin_runtime_size,
        }.get(model.engine)
        return {
            "id": model.id,
            "model_id": model.id,
            "name": model.label,
            "label": model.label,
            "engine": model.engine,
            "selectable": not retired,
            "retired_notice": NANO_RETIRED_NOTICE if retired else None,
            "downloaded": downloaded,
            "cache_size": size,
            "cache_size_bytes": size,
            "cache_size_display": format_size(int(size or 0)) if size is not None else None,
            "qwen_runtime_ready": runtime_ready if model.engine == "qwen" else None,
            "qwen_ready": runtime_ready if model.engine == "qwen" else None,
            "firered_runtime_ready": runtime_ready if model.engine == "firered" else None,
            "paraformer_runtime_ready": runtime_ready if model.engine == "paraformer" else None,
            "nano_runtime_ready": runtime_ready if model.engine == "nano" else None,
            "dolphin_runtime_ready": runtime_ready if model.engine == "dolphin" else None,
            "batch_size": 6 if model.engine == "nano" else 8 if model.engine == "dolphin" else None,
            "timestamp_batch_mode": "sequential" if model.engine == "nano"
            else "batched-encoder" if model.engine == "dolphin" else None,
            "aed_ready": bool(runtime_ready and downloaded)
            if model.engine == "firered" and inspect_status else None,
            "supports_aed": model.engine == "firered",
            "capabilities": capabilities,
            "model_family": (
                "FireRedASR2" if model.engine == "firered"
                else model.engine
            ),
            "runtime_size_bytes": (
                status_cache.size(size_key + "|runtime-size", runtime_scan)
                if runtime_scan and inspect_status and full_check else None
            ),
            "recommended_use": recommendations.get(model.id, ""),
            "recommendation": recommendations.get(model.id, ""),
            "selected": model.id == selected_model_id(),
            "device": selected_firered_device() if model.engine == "firered"
            else selected_paraformer_device() if model.engine == "paraformer"
            else selected_nano_device() if model.engine == "nano"
            else selected_dolphin_device() if model.engine == "dolphin" else None,
            "cpu_notice": FIRERED_CPU_NOTICE if model.engine == "firered" else None,
            "cpu_available": False if model.engine == "firered" else None,
            **gpu_status,
        }

    observation_fields = {
        "downloaded", "cache_size", "cache_size_bytes", "cache_size_display", "runtime_size_bytes",
        "qwen_runtime_ready", "qwen_ready", "firered_runtime_ready", "paraformer_runtime_ready",
        "nano_runtime_ready", "dolphin_runtime_ready", "aed_ready", "gpu_available",
        "gpu_runtime_ready", "gpu_prepare_available", "gpu_unavailable_reason", "gpu_name",
        "gpu_total_memory_mib", "gpu_batch_size",
    }

    def asr_snapshot(model_id: str, active_jobs=None) -> dict[str, object]:
        payload = asr_model_payload(model_id, inspect_status=False)
        saved = status_cache.peek(asr_status_key(model_id))
        if saved:
            payload.update({key: value for key, value in saved["data"].items() if key in observation_fields})
        payload["inspection_status"] = "cached" if saved else "unknown"
        payload["status_checked_at"] = saved["checked_at"] if saved else None
        payload.update(active_job_id=None, active_job_kind=None)
        for row in active_jobs if active_jobs is not None else provider.get().list_active_jobs():
            if row["job_type"] not in {"asr_prepare", "asr_delete", "asr_download_cleanup", "asr_runtime_delete"}:
                continue
            request = json.loads(row["request_json"] or "{}")
            if request.get("model_id") == payload["id"]:
                payload.update(active_job_id=row["id"],
                               active_job_kind={"asr_prepare": "preparing", "asr_delete": "deleting",
                                                "asr_download_cleanup": "cleaning_partial",
                                                "asr_runtime_delete": "cleaning_runtime"}[row["job_type"]])
                break
        payload["download_state"] = download_snapshot(model_id, active=payload["active_job_kind"] == "preparing")
        return payload

    @api.get("/api/asr/models")
    def list_asr_models() -> list[dict[str, object]]:
        active = provider.get().list_active_jobs()
        return [asr_snapshot(model_id, active) for model_id in MODELS]

    @api.get("/api/asr/models/{model_id}/status")
    def get_asr_model_status(
        model_id: str, full: bool = Query(default=True), refresh: bool = Query(default=False),
    ) -> dict[str, object]:
        try:
            key = asr_status_key(model_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        previous = status_cache.peek(key)

        def inspect() -> dict:
            if refresh and full:
                status_cache.clear_sizes(f"{MODEL_ROOT}|{get_model(model_id).id}|")
            payload = asr_model_payload(model_id, full_check=full, refresh_gpu=refresh)
            return {
                name: value for name, value in payload.items()
                if name in observation_fields and (full or value is not None)
            }
        try:
            status_cache.inspect(key, inspect, full=full, refresh=refresh)
        except Exception as exc:
            log_exception(LOGGER, "ASR status inspection failed", exc=exc, model_id=model_id)
            raise HTTPException(status_code=503, detail="模型状态检查失败，可重新检查；上次结果仍保留") from exc
        payload = asr_snapshot(model_id)
        current = status_cache.peek(key)
        if current and payload["status_checked_at"] is not None:
            payload["inspection_status"] = (
                "cached" if previous and previous["revision"] == current["revision"] else "ready"
            )
        return payload

    @api.get("/api/asr/models/{model_id}/resource-profile")
    def get_asr_resource_profile(model_id: str) -> dict[str, object]:
        try:
            model = get_model(model_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if model.engine != "firered":
            raise HTTPException(status_code=400, detail="Resource profile is only available for FireRed")
        try:
            return resource_profile()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"FireRed resource probe failed: {exc}") from exc

    @api.put("/api/asr/selection")
    def put_asr_selection(payload: ASRSelectionPayload) -> dict[str, object]:
        try:
            model = get_model(payload.model_id)
            options = {"firered_device": payload.firered_device,
                       "paraformer_device": payload.paraformer_device}
            if payload.nano_device is not None:
                options["nano_device"] = payload.nano_device
            if payload.dolphin_device is not None:
                options["dolphin_device"] = payload.dolphin_device
            select_model(model.id, **options)
            invalidate_asr_status(model.id, sizes=False)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # Selection must not trigger a disk scan.
        return {"model_id": model.id, "selected": True,
                "firered_device": selected_firered_device(),
                "paraformer_device": selected_paraformer_device(),
                "nano_device": selected_nano_device(),
                "dolphin_device": selected_dolphin_device()}

    @api.get("/api/asr/selection")
    def get_asr_selection() -> dict[str, object]:
        return asr_snapshot(selected_model_id())

    @api.post("/api/asr/models/{model_id}/prepare", status_code=202)
    def prepare_asr(model_id: str, device: Literal["cpu", "gpu"] | None = Query(default=None)) -> dict[str, object]:
        try:
            model = get_model(model_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        try:
            require_selectable_model(model.id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if device is not None and model.engine not in {"paraformer", "nano", "dolphin"}:
            raise HTTPException(status_code=400, detail="device is only valid for Paraformer, Fun-ASR-Nano or Dolphin")
        gpu_status = (dolphin_cuda_status if model.engine == "dolphin"
                      else paraformer_cuda_status if model.engine == "paraformer" else nano_cuda_status)
        if device == "gpu" and not gpu_status()["gpu_prepare_available"]:
            raise HTTPException(status_code=400, detail="No usable NVIDIA driver/device was detected.")

        def operation(
            update: Callable[[float, str, str | None], None],
            _should_cancel: Callable[[], bool],
        ) -> object:
            invalidate_asr_status(model.id)
            progress = lambda value, message: update(value, message, "preparing")
            try:
                if device is not None and model.engine == "dolphin":
                    prepare_asr_model(model.id, progress, dolphin_device=device)
                elif device is not None and model.engine == "nano":
                    prepare_asr_model(model.id, progress, nano_device=device)
                elif device is not None:
                    prepare_asr_model(model.id, progress, paraformer_device=device)
                else:
                    prepare_asr_model(model.id, progress)
            finally:
                invalidate_asr_status(model.id)
            return {"model_id": model.id}

        try:
            job_id = jobs.submit("asr_prepare", None, operation, request={"model_id": model.id})
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"job_id": job_id, "model_id": model.id}

    @api.post("/api/asr/models/{model_id}/download/resume", status_code=202)
    def resume_asr_download(model_id: str) -> dict[str, object]:
        try:
            model = require_selectable_model(model_id)
            state = download_snapshot(model.id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if state is None:
            raise HTTPException(status_code=409, detail="没有可继续的模型下载，请重新准备模型")
        if not state["resumable"]:
            raise HTTPException(status_code=409, detail="模型已就绪，可清理遗留下载文件，无需继续下载")
        device = state.get("device")
        return prepare_asr(model.id, device=device if device in {"cpu", "gpu"} else None)

    @api.delete("/api/asr/models/{model_id}/download/incomplete", status_code=202)
    def cleanup_asr_download(model_id: str) -> dict[str, object]:
        try:
            model = get_model(model_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        def operation(update, _should_cancel):
            update(0, "正在清理未完成下载，保留已完成权重和运行环境", "cleaning")
            try:
                released = cleanup_incomplete(model.id)
            finally:
                invalidate_asr_status(model.id)
            return {"released_bytes": released}

        try:
            job_id = jobs.submit("asr_download_cleanup", None, operation, request={"model_id": model.id})
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"job_id": job_id, "model_id": model.id}

    @api.delete("/api/asr/models/{model_id}", status_code=202)
    def delete_asr(model_id: str, remove_runtime: bool = Query(default=False)) -> dict[str, object]:
        try:
            model = get_model(model_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        def operation(
            update: Callable[[float, str, str | None], None],
            _should_cancel: Callable[[], bool],
        ) -> object:
            update(0, f"释放 {model.label} 已加载模型并清理缓存", "cleaning")
            invalidate_asr_status(model.id)
            try:
                released = clear_asr_model(model.id, remove_runtime=remove_runtime)
            finally:
                invalidate_asr_status(model.id)
            return {"released_bytes": released}

        try:
            job_id = jobs.submit("asr_delete", None, operation,
                                 request={"model_id": model.id, "remove_runtime": remove_runtime})
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"job_id": job_id, "model_id": model.id}

    @api.delete("/api/asr/models/{model_id}/runtime", status_code=202)
    def delete_asr_runtime(model_id: str, device: Literal["cpu", "gpu"] | None = Query(default=None)) -> dict[str, object]:
        try:
            model = get_model(model_id)
            if model.engine not in {"qwen", "firered", "paraformer", "dolphin", "nano"}:
                raise ValueError("该模型没有可单独清理的推理环境")
            if model.engine in {"qwen", "firered"} and device == "cpu":
                raise ValueError("该模型只有 GPU 环境")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        def operation(update, _should_cancel):
            CleanupProgress(f"{model.label} 运行环境").report(
                force=True, phase="checking", message="释放已加载模型，等待文件占用结束")
            invalidate_asr_status(model.id)
            try:
                released = clear_asr_runtime(model.id, device=device)
            finally:
                invalidate_asr_status(model.id)
            return {"released_bytes": released, "message": "运行环境清理完成，模型权重已保留"}

        try:
            job_id = jobs.submit("asr_runtime_delete", None, operation,
                                 request={"model_id": model.id, "device": device})
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"job_id": job_id, "model_id": model.id}

    def ai_settings_payload() -> dict[str, object]:
        try:
            settings = load_provider_settings()
        except ValueError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        profile = settings.active_profile
        provider_name = interface_provider(profile)
        key = resolve_interface_key(profile, settings)
        return {
            "connection_id": getattr(settings, "active_connection_id", "default"),
            "connection_name": getattr(getattr(settings, "active_connection", None), "name", ""),
            "connections": [
                {"id": connection.id, "name": connection.name, "base_url": connection.profile.base_url,
                 "model": connection.profile.model,
                 "has_api_key": bool(resolve_interface_key(connection.profile, settings))}
                for connection in getattr(settings, "connections", [])
            ],
            "provider": provider_name,
            "service_type": provider_name,
            "base_url": profile.base_url,
            "model": profile.model,
            "max_tokens": profile.max_tokens,
            "output_tokens": profile.max_tokens,
            "token_parameter": profile.token_parameter,
            "json_mode": profile.json_mode,
            "timeout_seconds": profile.timeout_seconds,
            "max_retries": profile.max_retries,
            "request_concurrency": profile.request_concurrency,
            "reasoning_effort": profile.reasoning_effort,
            "has_api_key": bool(key),
            "configured": bool(profile.base_url and profile.model and key),
        }

    def profile_from_ai_payload(payload: AISettingsPayload) -> ProviderProfile:
        try:
            return ProviderProfile(
                base_url=payload.base_url,
                model=payload.model,
                max_tokens=payload.max_tokens,
                token_parameter=payload.token_parameter,
                json_mode=payload.json_mode,
                timeout_seconds=payload.timeout_seconds,
                max_retries=payload.max_retries,
                request_concurrency=payload.request_concurrency,
                reasoning_effort=payload.reasoning_effort,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @api.get("/api/settings/ai")
    def get_ai_settings() -> dict[str, object]:
        return ai_settings_payload()

    def persist_ai_connection(settings: ProviderSettings, api_key: str | None, remove_key: bool) -> None:
        """Commit encrypted credentials and configuration together, restoring on failure."""
        if api_key is not None and (not api_key.strip() or remove_key):
            raise ValueError("API Key 不能为空，也不能同时保存和移除。")
        if api_key is None and not remove_key:
            save_provider_settings(settings)
            return
        key_path = interface_key_path(settings.active_profile)
        disabled_path = key_path.with_suffix(".disabled")
        previous_key = key_path.read_bytes() if key_path.exists() else None
        was_disabled = disabled_path.exists()
        try:
            if remove_key:
                delete_api_key(key_path)
                key_path.parent.mkdir(parents=True, exist_ok=True)
                disabled_path.touch()
            else:
                save_api_key(key_path, api_key.strip())
                disabled_path.unlink(missing_ok=True)
            save_provider_settings(settings)
        except Exception:
            if previous_key is None:
                key_path.unlink(missing_ok=True)
            else:
                key_path.write_bytes(previous_key)
            if was_disabled:
                disabled_path.touch()
            else:
                disabled_path.unlink(missing_ok=True)
            raise

    @api.put("/api/settings/ai")
    def put_ai_settings(payload: AISettingsPayload) -> dict[str, object]:
        profile = profile_from_ai_payload(payload)
        try:
            with provider_settings_lock():
                settings = load_provider_settings()
                if payload.connection_id and payload.connection_id != settings.active_connection_id:
                    raise HTTPException(status_code=409, detail="当前连接已变化，请刷新后保存。")
                updated = settings.with_active_profile(profile)
                if payload.connection_name is not None:
                    updated.active_connection.name = SavedConnection(
                        id=updated.active_connection_id, name=payload.connection_name,
                        profile=updated.active_profile,
                    ).name
                persist_ai_connection(updated, payload.api_key, payload.remove_key)
        except (OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"无法保存 AI 配置：{exc}") from exc
        return ai_settings_payload()

    def find_connection(settings: ProviderSettings, connection_id: str) -> SavedConnection:
        connection = next((item for item in settings.connections if item.id == connection_id), None)
        if connection is None:
            raise HTTPException(status_code=404, detail="连接不存在，请刷新连接列表。")
        return connection

    @api.post("/api/settings/ai/connections", status_code=201)
    def create_ai_connection(payload: AIConnectionPayload) -> dict[str, object]:
        try:
            with provider_settings_lock():
                settings = load_provider_settings()
                source = find_connection(settings, payload.source_id) if payload.source_id else None
                connection_id = uuid.uuid4().hex
                profile = profile_from_ai_payload(payload.settings) if payload.settings else (
                    source.profile.model_copy(deep=True) if source else ProviderProfile()
                )
                profile.credential_id = connection_id
                connection = SavedConnection(id=connection_id, name=payload.name, profile=profile)
                copied_key = resolve_interface_key(source.profile, settings) if (
                    source and source.profile.base_url == profile.base_url and not payload.remove_key
                ) else ""
                values = settings.model_dump()
                values["connections"].append(connection.model_dump())
                values["active_connection_id"] = connection_id
                persist_ai_connection(
                    ProviderSettings.model_validate(values),
                    payload.api_key if payload.api_key is not None else copied_key or None,
                    payload.remove_key,
                )
        except (OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"无法新增连接：{exc}") from exc
        return ai_settings_payload()

    @api.post("/api/settings/ai/connections/{connection_id}/activate")
    def activate_ai_connection(connection_id: str) -> dict[str, object]:
        try:
            with provider_settings_lock():
                settings = load_provider_settings()
                find_connection(settings, connection_id)
                values = settings.model_dump()
                values["active_connection_id"] = connection_id
                save_provider_settings(ProviderSettings.model_validate(values))
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"无法切换连接：{exc}") from exc
        return ai_settings_payload()

    @api.patch("/api/settings/ai/connections/{connection_id}")
    def rename_ai_connection(connection_id: str, payload: AIConnectionRenamePayload) -> dict[str, object]:
        try:
            with provider_settings_lock():
                settings = load_provider_settings()
                connection = find_connection(settings, connection_id)
                connection.name = SavedConnection(id=connection.id, name=payload.name, profile=connection.profile).name
                save_provider_settings(settings)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"无法重命名连接：{exc}") from exc
        return ai_settings_payload()

    @api.delete("/api/settings/ai/connections/{connection_id}")
    def delete_ai_connection(connection_id: str) -> dict[str, object]:
        try:
            with provider_settings_lock():
                settings = load_provider_settings()
                find_connection(settings, connection_id)
                values = settings.model_dump()
                values["connections"] = [item for item in values["connections"] if item["id"] != connection_id]
                if settings.active_connection_id == connection_id:
                    values["active_connection_id"] = values["connections"][0]["id"] if values["connections"] else ""
                # Credentials remain encrypted locally so a task using this
                # connection can finish. Deleted connections are no longer selectable.
                save_provider_settings(ProviderSettings.model_validate(values))
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"无法删除连接：{exc}") from exc
        return ai_settings_payload()

    def profile_for_endpoint(base_url: str | None = None, connection_id: str | None = None,
                             model: str | None = None) -> tuple[ProviderProfile, ProviderSettings]:
        try:
            settings = load_provider_settings()
        except ValueError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        profile = find_connection(settings, connection_id).profile if connection_id else settings.active_profile
        if base_url is not None:
            values = profile.model_dump()
            values["base_url"] = base_url
            try:
                profile = ProviderProfile(**values)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        if model is not None:
            profile = profile.model_copy(update={"model": model.strip()})
        return profile, settings

    @api.post("/api/settings/ai/key/reveal")
    def reveal_ai_key(request: Request, payload: AIEndpointPayload | None = None) -> JSONResponse:
        origin = request.headers.get("origin")
        if request.headers.get("sec-fetch-site") == "cross-site" or (
            origin and urlsplit(origin).netloc != request.url.netloc
        ):
            raise HTTPException(status_code=403, detail="只能从本地设置页面查看 API Key")
        profile, settings = profile_for_endpoint(payload.base_url if payload else None, payload.connection_id if payload else None)
        key = resolve_interface_key(profile, settings)
        if not key:
            raise HTTPException(status_code=404, detail="当前服务尚未保存 API Key")
        return JSONResponse(
            {"api_key": key},
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    @api.post("/api/settings/ai/key")
    def put_ai_key(payload: AIKeyPayload) -> dict[str, object]:
        profile, settings = profile_for_endpoint(payload.base_url, payload.connection_id)
        try:
            save_api_key(interface_key_path(profile), payload.api_key)
            interface_key_path(profile).with_suffix(".disabled").unlink(missing_ok=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"无法保存 API Key：{exc}") from exc
        result = ai_settings_payload()
        result.update({
            "base_url": profile.base_url,
            "has_api_key": True,
            "configured": bool(profile.base_url and profile.model and payload.api_key.strip()),
        })
        return result

    @api.delete("/api/settings/ai/key")
    def delete_ai_key(payload: AIEndpointPayload | None = None, base_url: str | None = None) -> dict[str, object]:
        requested_url = base_url or (payload.base_url if payload else None)
        profile, _settings = profile_for_endpoint(requested_url, payload.connection_id if payload else None)
        try:
            deleted = delete_api_key(interface_key_path(profile))
            disabled = interface_key_path(profile).with_suffix(".disabled")
            disabled.parent.mkdir(parents=True, exist_ok=True)
            disabled.touch()
        except (OSError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=f"无法删除 API Key：{exc}") from exc
        result = ai_settings_payload()
        result.update({"base_url": profile.base_url, "has_api_key": False})
        result["deleted"] = deleted
        return result

    def endpoint_judge(payload: AIEndpointPayload | None = None, *, list_models: bool = False):
        profile, settings = profile_for_endpoint(
            payload.base_url if payload else None, payload.connection_id if payload else None,
            payload.model if payload else None,
        )
        if payload and payload.settings:
            draft_profile = profile_from_ai_payload(payload.settings)
            profile = draft_profile.model_copy(update={"credential_id": profile.credential_id})
        key = payload.api_key.strip() if payload and payload.api_key is not None else resolve_interface_key(profile, settings)
        # Listing models needs the address and credential, before a model can be chosen.
        if list_models and not profile.model:
            profile = profile.model_copy(update={"model": "model-list"})
        try:
            judge = create_judge(profile, key, interface_provider(profile))
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return judge

    @api.post("/api/settings/ai/test")
    def test_ai_endpoint(payload: AIEndpointPayload | None = None) -> dict[str, object]:
        judge = endpoint_judge(payload)
        try:
            reply = judge.test_api_connection()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"API 测试失败：{exc}") from exc
        finally:
            judge.close()
        return {"ok": True, "message": reply}

    @api.post("/api/settings/ai/models")
    def list_ai_models(payload: AIEndpointPayload | None = None) -> dict[str, object]:
        judge = endpoint_judge(payload, list_models=True)
        try:
            models = judge.list_remote_models()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"获取远程模型失败：{exc}") from exc
        finally:
            judge.close()
        return {"models": models}

    @api.get("/api/library")
    def list_library(
        background: BackgroundTasks,
        folder_id: int | None = Query(default=None),
        sort: Literal["manual", "name", "created_at", "duration"] = Query(default="manual"),
        order: Literal["asc", "desc"] = Query(default="asc"),
    ) -> dict[str, object]:
        """Return one logical directory containing mixed folders and videos."""
        store = provider.get()
        if folder_id is not None:
            folder = folder_or_404(folder_id)
            folders = store.list_folders(folder_id)
        else:
            folder = None
            folders = store.list_folders(None)
        videos = store.list_folder_videos(folder_id)
        ancestors = []
        current = folder
        while current is not None:
            ancestors.append(folder_payload(current))
            current = store.get_folder(int(current["parent_id"])) if current["parent_id"] is not None else None
        folder_items = [{"type": "folder", "folder": folder_payload(row)} for row in folders]
        video_items = [{"type": "video", "video": video} for video in video_payloads(videos)]
        items: list[dict[str, object]] = folder_items + video_items
        if sort == "manual":
            items.sort(
                key=lambda item: int(
                    (item["folder"] if item["type"] == "folder" else item["video"])["library_order"]  # type: ignore[index]
                )
            )
        else:
            reverse = order == "desc"
            def sort_key(item: dict[str, object]) -> object:
                value = item["folder"] if item["type"] == "folder" else item["video"]
                assert isinstance(value, dict)
                if sort == "name":
                    return str(value.get("name") or "").casefold()
                if sort == "created_at":
                    return str(value.get("created_at") or "")
                if sort == "duration":
                    return int(value.get("duration_ms") or 0)
                return int(value.get("library_order") or 0)
            items.sort(key=sort_key, reverse=reverse)
        result = {
            "folder": folder_payload(folder) if folder is not None else None,
            "parent_id": folder_id,
            "ancestors": list(reversed(ancestors)),
            "sort": sort,
            "order": order,
            "items": items,
        }
        if startup_snapshot_enabled and folder_id is None:
            background.add_task(save_library_snapshot, DIRECTORY_PROJECT_ROOT, provider.library_id(), result,
                                asr_storage.audio_directory(DIRECTORY_PROJECT_ROOT, provider.data_root()) / "thumbnails")
        return result

    @api.get("/api/library/search")
    def search_library_subtitles(
        q: str = Query(min_length=1, max_length=MAX_QUERY_LENGTH),
        limit: int = Query(default=20, ge=1, le=MAX_PAGE_SIZE),
        offset: int = Query(default=0, ge=0, le=MAX_SQLITE_INTEGER),
        video_id: int | None = Query(default=None, ge=1, le=MAX_SQLITE_INTEGER),
        preferred_video_id: int | None = Query(default=None, ge=1, le=MAX_SQLITE_INTEGER),
    ) -> dict[str, object]:
        try:
            query = validate_query(q)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        # Use the same database snapshot and identity for this search.
        with jobs._lock:
            store = provider.get()
            library_id = provider.library_id()
        return {
            **store.search_subtitles(query, limit=limit, offset=offset, video_id=video_id,
                                    preferred_video_id=preferred_video_id),
            "library_id": library_id,
        }

    @api.get("/api/folders")
    def list_folders(parent_id: int | None = Query(default=None)) -> list[dict[str, object]]:
        return [folder_payload(folder) for folder in provider.get().list_folders(parent_id)]

    @api.post("/api/folders", status_code=201)
    def create_folder(payload: FolderCreatePayload) -> dict[str, object]:
        try:
            folder_id = provider.get().create_folder(payload.name, payload.parent_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return folder_payload(folder_or_404(folder_id))

    @api.get("/api/folders/{folder_id}")
    def get_folder(folder_id: int) -> dict[str, object]:
        return folder_payload(folder_or_404(folder_id))

    @api.get("/api/folders/{folder_id}/videos")
    def list_folder_videos(folder_id: int) -> list[dict[str, object]]:
        folder_or_404(folder_id)
        return video_payloads(provider.get().list_folder_videos(folder_id))

    @api.patch("/api/folders/{folder_id}")
    @api.put("/api/folders/{folder_id}")
    def rename_folder(folder_id: int, payload: FolderRenamePayload) -> dict[str, object]:
        folder_or_404(folder_id)
        try:
            provider.get().update_folder(folder_id, payload.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return folder_payload(folder_or_404(folder_id))

    @api.post("/api/folders/{folder_id}/parent")
    def move_folder(folder_id: int, payload: FolderParentPayload) -> dict[str, object]:
        folder_or_404(folder_id)
        if payload.parent_id is not None:
            folder_or_404(payload.parent_id)
        try:
            provider.get().set_folder_parent(folder_id, payload.parent_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return folder_payload(folder_or_404(folder_id))

    @api.delete("/api/folders/{folder_id}")
    def delete_folder(folder_id: int) -> dict[str, object]:
        ensure_file_changes_allowed()
        try:
            result = provider.get().trash_folder(
                folder_id,
                asr_storage.video_directory(DIRECTORY_PROJECT_ROOT, provider.data_root()) / ".recycle_bin",
            )
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"id": folder_id, "deleted": True, **result, "message": "已移入回收站，可使用 Ctrl+Z 撤销"}

    @api.delete("/api/videos/{video_id}")
    def delete_video(video_id: int) -> dict[str, object]:
        ensure_file_changes_allowed()
        try:
            result = provider.get().trash_video(
                video_id,
                asr_storage.video_directory(DIRECTORY_PROJECT_ROOT, provider.data_root()) / ".recycle_bin",
            )
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"id": video_id, "deleted": True, **result, "message": "已移入回收站，可使用 Ctrl+Z 撤销"}

    @api.post("/api/recycle-bin/undo")
    def undo_recycle_bin() -> dict[str, object]:
        ensure_file_changes_allowed()
        try:
            return provider.get().undo_latest_recycle(
                asr_storage.video_directory(DIRECTORY_PROJECT_ROOT, provider.data_root()) / ".recycle_bin",
            )
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.get("/api/recycle-bin/latest")
    def latest_recycle_bin() -> dict[str, object] | None:
        return provider.get().latest_recycle()

    @api.post("/api/library/reorder")
    def reorder_library(payload: LibraryReorderPayload, background: BackgroundTasks) -> dict[str, object]:
        folder_id = payload.parent_id
        if folder_id is not None:
            folder_or_404(folder_id)
        ordered: list[tuple[str, int]] = []
        try:
            for item in payload.ordered_items:
                kind = str(item.get("type") or item.get("kind") or "")
                item_id = int(item.get("id") or item.get("folder_id") or item.get("video_id"))
                ordered.append((kind, item_id))
            provider.get().reorder_library(folder_id, ordered)
        except (TypeError, ValueError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return list_library(background, folder_id=folder_id, sort="manual", order="asc")

    @api.get("/api/videos")
    def list_videos() -> list[dict[str, object]]:
        return video_payloads(provider.get().list_videos())

    @api.post("/api/videos/register")
    def register_video(payload: VideoRegisterPayload) -> dict[str, object]:
        with jobs._lock:
            ensure_file_changes_allowed()
            try:
                video_id, _path, existed, warning = import_video(payload.path or "", payload.filename)
                if payload.display_name is not None:
                    provider.get().update_video_display_name(video_id, payload.display_name)
            except (OSError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            video = video_payload(_video_or_404(provider.get(), video_id))
            video["storage_warning"] = warning
            return {"video": video, "video_id": video_id, "existed": existed}

    @api.post("/api/videos/upload")
    async def upload_video(request: Request, filename: str = Query(min_length=1, max_length=200)) -> dict[str, object]:
        suffix = Path(filename).suffix.lower()
        if suffix not in SUPPORTED_VIDEO_SUFFIXES:
            raise HTTPException(status_code=400, detail="不支持该视频文件格式")
        try:
            video_filename(filename, Path(f"upload{suffix}"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        with jobs._lock:
            ensure_file_changes_allowed()

        # Stream browser files to disk; never buffer a classroom recording in memory.
        with TemporaryDirectory(prefix="classroom-upload-") as temporary:
            # Windows TEMP may use an 8.3 alias; canonicalize this server-owned directory.
            source = Path(temporary).resolve() / filename
            size = 0
            try:
                with source.open("wb") as output:
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > MAX_VIDEO_BYTES:
                            raise HTTPException(status_code=413, detail="视频超过 1GB，请压缩后再导入")
                        await run_in_threadpool(output.write, chunk)
                if size == 0:
                    raise HTTPException(status_code=400, detail="视频文件为空")

                def finish_import() -> dict[str, object]:
                    with jobs._lock:
                        ensure_file_changes_allowed()
                        video_id, _path, existed, warning = import_video(source, filename, browser_upload=True)
                        video = video_payload(_video_or_404(provider.get(), video_id))
                        video["storage_warning"] = warning
                        return {"video": video, "video_id": video_id, "existed": existed}

                return await run_in_threadpool(finish_import)
            except (OSError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    def ensure_file_changes_allowed() -> None:
        if storage_migrating.is_set() or jobs.is_running() or provider.get().list_paused_jobs():
            raise HTTPException(status_code=409, detail="请先完成或停止任务，再移动视频或修改文件名")

    def import_video(path: str | Path, filename: str | None = None, *, on_registered=None,
                     rename: bool = False, browser_upload: bool = False):
        root = provider.data_root()
        audio = asr_storage.audio_directory(DIRECTORY_PROJECT_ROOT, root)
        library = asr_storage.video_directory(DIRECTORY_PROJECT_ROOT, root)
        assets = asr_storage.storage_root(DIRECTORY_PROJECT_ROOT)
        asr_storage.validate_video_directory(
            library, audio, asr_storage.log_directory(DIRECTORY_PROJECT_ROOT),
            assets / "models" / "asr", assets / ".runtime", DIRECTORY_PROJECT_ROOT / ".runtime",
        )
        return register_managed_video(
            provider.get(), path, audio, library, filename, on_registered=on_registered,
            delete_source_after_import=rename or asr_storage.delete_source_after_import(DIRECTORY_PROJECT_ROOT, root),
            source_folder="" if browser_upload else None,
        )

    @api.patch("/api/videos/{video_id}/filename")
    def rename_video_file(video_id: int, payload: VideoFilenamePayload) -> dict[str, object]:
        with jobs._lock:
            video = _video_or_404(provider.get(), video_id)
            locked, global_reason = video_file_change_locks()
            reason = global_reason or locked.get(video_id)
            if reason:
                raise HTTPException(status_code=409, detail=reason)
            try:
                path = Path(video["video_path"])
                if not path.is_file():
                    raise ValueError("视频源文件不存在，请先重新选择文件")
                if provider.get().file_hash(path) != video["content_hash"]:
                    raise ValueError("视频内容已变化，请先重新导入，未修改文件名")
                _id, _path, _existed, warning = import_video(path, payload.filename, rename=True)
            except (OSError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            result = video_payload(_video_or_404(provider.get(), video_id))
            result["storage_warning"] = warning
            return result

    @api.post("/api/videos/{video_id}/folder")
    @api.patch("/api/videos/{video_id}/folder")
    def move_video_to_folder(video_id: int, payload: VideoFolderPayload) -> dict[str, object]:
        _video_or_404(provider.get(), video_id)
        if payload.folder_id is not None:
            folder_or_404(payload.folder_id)
        try:
            provider.get().set_video_folder(video_id, payload.folder_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return video_payload(_video_or_404(provider.get(), video_id))

    @api.get("/api/settings/recall-schemes")
    def list_recall_schemes() -> list[dict]:
        return provider.get().list_recall_schemes()

    @api.post("/api/settings/recall-schemes/prompt-preview")
    def recall_prompt_preview(payload: RecallPromptInput) -> dict:
        return preview_recall_prompt(payload)

    @api.post("/api/settings/recall-schemes", status_code=201)
    def create_recall_scheme(payload: RecallSchemeInput) -> dict:
        try:
            return provider.get().save_recall_scheme(payload.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.put("/api/settings/recall-schemes/{scheme_id}")
    def update_recall_scheme(scheme_id: ModeId, payload: RecallSchemeInput) -> dict:
        if provider.get().get_recall_scheme(scheme_id) is None:
            raise HTTPException(status_code=404, detail="自定义召回方案不存在")
        try:
            return provider.get().save_recall_scheme(payload.model_dump(), scheme_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.delete("/api/settings/recall-schemes/{scheme_id}")
    def archive_recall_scheme(scheme_id: ModeId) -> dict:
        try:
            provider.get().archive_recall_scheme(scheme_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"archived": True}

    def freeze_processing_options(options: VideoProcessPayload, *, restore=False):
        modes = options.analysis_modes or [options.analysis_mode]
        if restore:
            if any(mode.startswith("custom_") and mode not in options.analysis_schemes for mode in modes):
                raise HTTPException(status_code=409, detail="任务缺少自定义方案快照，请重新选择方案创建任务")
            return options
        try:
            return options.model_copy(update={"analysis_schemes": freeze_schemes(provider.get(), modes)})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def create_processing_judge(profile, key, mode, options):
        extras = {"scheme_snapshot": options.analysis_schemes.get(mode)} if mode.startswith("custom_") else {}
        return create_judge(profile, key, interface_provider(profile), mode=mode, **extras)

    @api.get("/api/videos/{video_id}/processing-options")
    def get_processing_options(video_id: int) -> dict[str, object]:
        video = _video_or_404(provider.get(), video_id)
        ai = ai_settings_payload()
        active_job = next(
            (
                JobManager._row_payload(row, provider.get())
                for row in [*provider.get().list_active_jobs(), *provider.get().list_paused_jobs()]
                if row["video_id"] is not None and int(row["video_id"]) == video_id
            ),
            None,
        )
        return {
            "video_id": video_id,
            "asr_model": asr_snapshot(selected_model_id()),
            "ai_service": {
                "provider": ai["provider"],
                "service_type": ai["service_type"],
                "base_url": ai["base_url"],
                "model": ai["model"],
                "configured": ai["configured"],
            },
            "analysis_modes": ["high_recall", "medium_recall", "low_recall",
                               *[item["id"] for item in provider.get().list_recall_schemes() if not item["archived"]]],
            "llm_chunk_seconds": 600,
            "llm_request_concurrency": ai["request_concurrency"],
            "reasoning_effort": ai["reasoning_effort"],
            "status": video["status"],
            "active_job": active_job,
        }

    def process_operation(
        video_id: int,
        options: VideoProcessPayload,
        *,
        retain_folder: bool = False,
    ) -> Callable[
        [Callable[[float, str, str | None], None], Callable[[], bool]], object
    ]:
        settings = load_provider_settings()
        profile = settings.active_profile.model_copy(deep=True)
        key = resolve_interface_key(profile, settings)
        def operation(
            update: Callable[[float, str, str | None], None],
            should_cancel: Callable[[], bool],
        ) -> object:
            modes = options.analysis_modes or [options.analysis_mode]
            counts = 0
            outcomes = []
            for index, mode in enumerate(modes):
                if should_cancel():
                    raise ProcessingCancelled("用户已请求停止处理")
                judge = create_processing_judge(profile, key, mode, options)
                try:
                    trigger_groups = (
                        {"custom": {"用户关键词": options.keywords}}
                        if options.keywords else None
                    )
                    outcome, count = process_video_once(
                        provider.get(),
                        video_id,
                        judge,
                        progress=lambda value, message: update((index + value) / len(modes), message, None),
                        use_keyword_prefilter=options.use_keyword_prefilter,
                        trigger_groups=trigger_groups,
                        analysis_mode=mode,
                        asr_model_id=options.asr_model_id,
                        filter_formula_noise=options.filter_formula_noise,
                        filter_formula=options.filter_formula,
                        should_stop=should_cancel,
                        force_retranscribe=options.force_retranscribe and index == 0,
                        qwen_options=options.qwen_options.model_dump(),
                    )
                    counts += count
                    outcomes.append(outcome)
                finally:
                    judge.close()
            if not retain_folder:
                # A single-video workflow publishes its result at the root.
                provider.get().set_video_folder(video_id, None)
            return ("skipped" if all(value == "skipped" for value in outcomes) else "completed", counts)
        return operation

    def batch_operation(state: dict, options: VideoProcessPayload, job_id: str):
        settings = load_provider_settings()
        profile = settings.active_profile.model_copy(deep=True)
        key = resolve_interface_key(profile, settings)
        @asr_session()
        @qwen_queue()
        @firered_queue()
        def operation(update, should_cancel):
            store = provider.get()

            def judge_factory(mode):
                try:
                    profile.require_ready(key)
                except ValueError as exc:
                    raise ApiUnavailable(str(exc)) from None
                return create_processing_judge(profile, key, mode, options)

            return run_web_batch(
                store, state, options,
                save=lambda: store.update_job(job_id, request_json=json.dumps(state, ensure_ascii=False)),
                update=update, should_cancel=should_cancel, import_video=import_video,
                judge_factory=judge_factory,
            )

        return operation

    def submit_batch_state(state: dict, options: VideoProcessPayload, *, restore=False) -> str:
        options = freeze_processing_options(options, restore=restore)
        options = options.model_copy(update={"asr_model_id": options.asr_model_id or selected_model_id()})
        if not state["entries"]:
            raise HTTPException(status_code=400, detail="所选文件夹中没有可处理的视频")
        if not state.get("import_only") and (options.force_retranscribe or any(
            not entry.get("video_id") or not provider.get().get_segments(entry["video_id"])
            for entry in state["entries"] if entry["status"] not in {"completed", "skipped"}
        )):
            try:
                require_selectable_model(options.asr_model_id)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        state["options"] = options.model_dump()
        job_id = uuid.uuid4().hex
        try:
            return jobs.submit(
                "batch_video_process", None, batch_operation(state, options, job_id),
                request=state, job_id=job_id,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    def submit_batch(
        items: list[tuple[int, Path]],
        options: VideoProcessPayload,
    ) -> str:
        state = normalize_batch({
            "kind": "batch", "items": [[v, str(p)] for v, p in items], "options": options.model_dump(),
        }, provider.get(), restore_completed=False)
        return submit_batch_state(state, options)

    @api.post("/api/batch/preview")
    def preview_batch(payload: BatchPreviewPayload) -> dict:
        result = preview_directories([path.strip() for path in payload.folders if path.strip()])
        return {"valid": result["valid"], "folders": [
            {key: value for key, value in folder.items() if key not in {"videos", "directories"}} for folder in result["folders"]
        ]}

    @api.get("/api/batch/latest")
    def latest_batch() -> dict | None:
        row = provider.get().latest_batch_job()
        return jobs.get(str(row["id"])) if row else None

    @api.post("/api/batch/process", status_code=202)
    def process_batch(payload: BatchProcessPayload) -> dict[str, object]:
        with jobs._lock:
            ensure_file_changes_allowed()
            preview = preview_directories(payload.folders)
            if payload.folders and not preview["valid"]:
                raise HTTPException(status_code=400, detail="；".join(
                    f"{row['path']}：{row['error']}" for row in preview["folders"] if not row["valid"]
                ))
            registered, known = [], set()
            for folder_id in payload.folder_ids:
                folder_or_404(folder_id)
                for row in provider.get().list_folder_videos(folder_id, recursive=True):
                    video_id = int(row["id"])
                    if video_id not in known:
                        registered.append({
                            "video_id": video_id, "source_path": "", "source_root": "",
                            "status": "pending_asr", "error": "",
                        })
                        known.add(video_id)
            for video_id in payload.video_ids:
                row = _video_or_404(provider.get(), video_id)
                if row["status"] == "processing":
                    raise HTTPException(status_code=409, detail=f"{row['filename']}：该视频正在处理中")
                if not Path(row["video_path"]).is_file():
                    raise HTTPException(status_code=400, detail=f"{row['filename']}：视频文件已被移动或删除")
                if video_id not in known:
                    registered.append({
                        "video_id": video_id, "video_path": row["video_path"], "label": row["filename"],
                        "source_path": "", "source_root": "", "error": "",
                        "status": "pending_ai" if provider.get().get_segments(video_id) else "pending_asr",
                    })
                    known.add(video_id)
            state = make_batch(preview, payload.model_dump(), registered)
            state["import_only"] = payload.import_only
            job_id = submit_batch_state(state, payload)
            return {
                "job_id": job_id, "status": "queued", "job_type": "batch_video_process",
                "video_ids": [entry["video_id"] for entry in registered], "total": len(state["entries"]), "errors": [],
            }

    @api.post("/api/folders/{folder_id}/process", status_code=202)
    def process_folder(folder_id: int, payload: VideoProcessPayload | None = None) -> dict[str, object]:
        """Queue all videos in a logical library folder and its descendants.

        The no-body response remains the old discovery contract for clients
        that only probe the endpoint; sending options starts the real batch.
        """
        folder = folder_or_404(folder_id)
        video_rows = provider.get().list_folder_videos(folder_id, recursive=True)
        if payload is None:
            return JSONResponse(
                status_code=501,
                content={
                    "folder_id": folder_id,
                    "video_ids": [int(row["id"]) for row in video_rows],
                    "status": "not_implemented",
                    "detail": "请提交处理选项以启动文件夹批处理",
                },
            )
        items = [(int(row["id"]), Path(row["video_path"])) for row in video_rows]
        job_id = submit_batch(items, payload)
        return {
            "job_id": job_id,
            "status": "queued",
            "job_type": "batch_video_process",
            "folder_id": folder_id,
            "folder_name": folder["name"],
            "video_ids": [video_id for video_id, _path in items],
            "errors": [],
        }

    def start_video_process(video_id: int, options: VideoProcessPayload, *, restore=False) -> dict[str, object]:
        options = freeze_processing_options(options, restore=restore)
        video_store = provider.get()
        video = _video_or_404(video_store, video_id)
        if video["status"] == "processing":
            raise HTTPException(status_code=409, detail="该视频正在处理中")
        options = options.model_copy(update={"asr_model_id": options.asr_model_id or selected_model_id()})
        if options.force_retranscribe or not video_store.get_segments(video_id):
            try:
                require_selectable_model(options.asr_model_id)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            job_id = jobs.submit("video_process", video_id, process_operation(video_id, options), request={
                "kind": "single", "video_id": video_id, "options": options.model_dump(),
            })
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"job_id": job_id, "video_id": video_id, "status": "queued", "analysis_modes": options.analysis_modes,
                "analysis_schemes": {key: {field: value[field] for field in ("id", "name", "base_mode", "revision")}
                                     for key, value in options.analysis_schemes.items()}}

    @api.post("/api/videos/{video_id}/process", status_code=202)
    def process_video(video_id: int, payload: VideoProcessPayload | None = None) -> dict[str, object]:
        return start_video_process(video_id, payload or VideoProcessPayload())

    @api.post("/api/videos/{video_id}/analysis", status_code=202)
    def analyze_saved_video(video_id: int, payload: RecallAnalysisPayload) -> dict[str, object]:
        """Add one recall mode using saved subtitles, preserving library and review state."""
        with jobs._lock:
            store = provider.get()
            _video_or_404(store, video_id)
            if not store.get_segments(video_id):
                raise HTTPException(status_code=409, detail="字幕尚未就绪，请先完成转写")
            options = freeze_processing_options(VideoProcessPayload(analysis_mode=payload.analysis_mode))
            previous = store.get_analysis_run(video_id, payload.analysis_mode)
            requested_revision = options.analysis_schemes.get(payload.analysis_mode, {}).get("revision", 0)
            if previous and previous["status"] == "completed" and previous["scheme_revision"] == requested_revision:
                raise HTTPException(status_code=409, detail="该召回方案已完成识别，请刷新结果")
            settings = load_provider_settings()
            profile = settings.active_profile.model_copy(deep=True)
            key = resolve_interface_key(profile, settings)
            try:
                profile.require_ready(key)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            mode = payload.analysis_mode

            def operation(update, should_cancel):
                if should_cancel():
                    raise ProcessingCancelled("用户已请求停止处理")
                judge = create_processing_judge(profile, key, mode, options)
                try:
                    store.set_analysis_run(video_id, mode, "processing")
                    return process_saved_analysis_once(
                        store, video_id, judge, mode,
                        progress=lambda value, message: update(value, message, "analyzing"),
                        filter_formula_noise=False, should_stop=should_cancel,
                    )
                finally:
                    judge.close()

            try:
                job_id = jobs.submit("video_analysis", video_id, operation, request={
                    "kind": "analysis", "video_id": video_id,
                    "options": options.model_dump(),
                })
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return {"job_id": job_id, "video_id": video_id, "status": "queued", "analysis_modes": [mode]}

    @api.post("/api/videos/{video_id}/transcribe", status_code=202)
    def transcribe_video(
        video_id: int,
        payload: TranscribePayload | None = None,
    ) -> dict[str, object]:
        video_store = provider.get()
        video = _video_or_404(video_store, video_id)
        options = payload or TranscribePayload()
        if video["status"] == "processing":
            raise HTTPException(status_code=409, detail="该视频正在处理中")
        options = options.model_copy(
            update={"asr_model_id": options.asr_model_id or selected_model_id()}
        )
        try:
            require_selectable_model(options.asr_model_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        def operation(
            update: Callable[[float, str, str | None], None],
            should_cancel: Callable[[], bool],
        ) -> object:
            if options.force_retranscribe:
                from classcue.video_pipeline import retranscribe_video

                count = retranscribe_video(
                    video_store,
                    video_id,
                    options.asr_model_id,
                    progress=lambda value, message: update(value, message, "transcribing"),
                    should_stop=should_cancel,
                    qwen_options=options.qwen_options.model_dump(),
                )
                return {"status": "transcribed", "segment_count": count}
            result, count = prepare_video_for_analysis(
                video_store,
                video_id,
                progress=lambda value, message: update(value, message, "transcribing"),
                asr_model_id=options.asr_model_id,
                should_stop=should_cancel,
                qwen_options=options.qwen_options.model_dump(),
            )
            video_store.set_status(video_id, "transcribed")
            return {"status": result, "segment_count": count}

        try:
            job_id = jobs.submit(
                "video_transcribe",
                video_id,
                operation,
                request={
                    "kind": "transcribe",
                    "video_id": video_id,
                    "options": options.model_dump(),
                },
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"job_id": job_id, "video_id": video_id, "status": "queued"}

    @api.post("/api/videos/{video_id}/retry", status_code=202)
    def retry_video(video_id: int, payload: VideoProcessPayload | None = None) -> dict[str, object]:
        video_store = provider.get()
        video = _video_or_404(video_store, video_id)
        if video["status"] not in {"failed", "interrupted", "processing", "resource_paused"}:
            raise HTTPException(status_code=409, detail="只有失败或中断的视频可以重试")
        if video["status"] == "processing":
            video_store.mark_interrupted_video_failed(video_id)
        return start_video_process(video_id, payload or VideoProcessPayload())

    def job_payload_or_404(job_id: str) -> dict[str, object]:
        payload = jobs.get(job_id)
        if payload is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        return payload

    @api.get("/api/jobs/paused")
    def paused_jobs() -> list[dict[str, object]]:
        return [JobManager._row_payload(row, provider.get()) for row in provider.get().list_paused_jobs()]

    @api.post("/api/jobs/{job_id}/retry", status_code=202)
    def retry_batch(job_id: str) -> dict:
        with jobs._lock:
            ensure_file_changes_allowed()
            row = provider.get().get_job(job_id)
            if row is None:
                raise HTTPException(status_code=404, detail="任务不存在")
            if row["job_type"] != "batch_video_process":
                raise HTTPException(status_code=400, detail="此入口仅支持批处理任务")
            if row["status"] in {"queued", "running", "resource_paused"}:
                raise HTTPException(status_code=409, detail="请先完成或停止当前任务")
            state = normalize_batch(json.loads(row["request_json"] or "{}"), provider.get())
            if state.get("import_only"):
                raise HTTPException(status_code=409, detail="这是仅导入批次，请在视频库选择视频后开始处理")
            batch_summary(state, provider.get())
            pending = [entry for entry in state["entries"] if entry["status"] not in {"completed", "skipped"}]
            if not pending:
                raise HTTPException(status_code=409, detail="没有需要重试的视频")
            for entry in pending:
                entry["status"] = (
                    "pending_import" if not entry.get("video_id") else
                    "pending_ai" if provider.get().get_segments(entry["video_id"]) else "pending_asr"
                )
                entry["error"] = ""
            options = VideoProcessPayload.model_validate(state.get("options", {})).model_copy(
                update={"force_retranscribe": False},
            )
            state["api_error"] = ""
            new_id = submit_batch_state(state, options, restore=True)
            return JobManager._row_payload(provider.get().get_job(new_id), provider.get())

    @api.post("/api/jobs/{job_id}/resume", status_code=202)
    def resume_job(job_id: str, payload: ResumePayload | None = None) -> dict[str, object]:
        row = provider.get().get_job(job_id)
        if row is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        if row["status"] != "resource_paused":
            raise HTTPException(status_code=409, detail="任务不是资源暂停状态")
        request_data = json.loads(row["request_json"] or "{}")
        options = VideoProcessPayload.model_validate(request_data.get("options", {}))
        skip = bool(payload and payload.skip_current)
        if request_data.get("kind") == "batch":
            state = normalize_batch(request_data, provider.get())
            if skip:
                index = state.get("current_index")
                if index is None:
                    index = next((i for i, entry in enumerate(state["entries"])
                                  if entry["status"] not in {"completed", "skipped", "failed"}), None)
                if index is not None:
                    state["entries"][index]["status"] = "skipped"
            if any(entry["status"] not in {"completed", "skipped", "failed"} for entry in state["entries"]):
                new_id = submit_batch_state(state, options.model_copy(update={"force_retranscribe": False}), restore=True)
                result = {"job_id": new_id, "status": "queued"}
            else:
                jobs.cancel(job_id)
                return job_payload_or_404(job_id)
        elif request_data.get("kind") == "single" and not skip:
            result = start_video_process(int(request_data["video_id"]), options, restore=True)
        else:
            raise HTTPException(status_code=400, detail="该任务没有可恢复的队列")
        provider.get().update_job(job_id, status="interrupted", stage="resumed", message="已创建续跑任务")
        return result

    @api.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, object]:
        return job_payload_or_404(job_id)

    @api.post("/api/jobs/{job_id}/download/pause")
    def pause_asr_download(job_id: str) -> dict[str, object]:
        try:
            return jobs.pause_preparation(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict[str, object]:
        try:
            return jobs.cancel(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.get("/api/jobs/{job_id}/transcript")
    def live_job_transcript(job_id: str):
        job = job_payload_or_404(job_id)
        return jobs.live_transcripts.snapshot(job_id, job.get("video_id"))

    @api.get("/api/jobs/{job_id}/analysis-preview")
    def live_job_analysis(job_id: str):
        job = job_payload_or_404(job_id)
        return jobs.live_analysis.snapshot(job_id, job.get("video_id"))

    @api.get("/api/jobs/{job_id}/events")
    def stream_job_events(job_id: str):
        job_payload_or_404(job_id)

        def events():
            previous: str | None = None
            deadline = time.monotonic() + 24 * 60 * 60
            while time.monotonic() < deadline:
                current = jobs.get(job_id)
                if current is None:
                    break
                encoded = json.dumps(current, ensure_ascii=False)
                if encoded != previous:
                    previous = encoded
                    yield f"event: job\ndata: {encoded}\n\n"
                if current["status"] in {"completed", "failed", "interrupted", "resource_paused", "awaiting_api"}:
                    break
                time.sleep(0.5)

        return StreamingResponse(events(), media_type="text/event-stream")

    @api.get("/api/videos/{video_id}")
    def get_video(video_id: int) -> dict[str, object]:
        video = _video_or_404(provider.get(), video_id)
        return video_payload(video)

    @api.get("/api/videos/{video_id}/subtitles")
    def get_subtitles(video_id: int) -> list[dict[str, object]]:
        video_store = provider.get()
        _video_or_404(video_store, video_id)
        return [
            {
                "id": int(segment.id),
                "start_ms": int(segment.start_ms),
                "end_ms": int(segment.end_ms),
                "text": segment.text,
            }
            for segment in video_store.get_segments(video_id)
        ]

    @api.get("/api/videos/{video_id}/markers")
    def get_markers(
        video_id: int,
        mode: str | None = Query(default=None, min_length=1),
        review_status: MarkerStatusFilter | None = Query(default=None, alias="status"),
    ) -> list[dict[str, object]]:
        video_store = provider.get()
        _video_or_404(video_store, video_id)
        return [
            _row_to_dict(row)
            for row in _marker_rows(video_store, video_id, mode, review_status)
        ]

    @api.get("/api/videos/{video_id}/analysis-runs")
    def get_analysis_runs(video_id: int) -> list[dict[str, object]]:
        video_store = provider.get()
        _video_or_404(video_store, video_id)
        with video_store.connect() as connection:
            rows = connection.execute(
                """
                SELECT r.analysis_mode, r.status, r.candidate_count, r.updated_at,
                    r.analysis_signature, r.experiment_id, r.scheme_revision,
                    s.name AS scheme_name, s.revision AS latest_scheme_revision, s.archived
                FROM video_analysis_runs r LEFT JOIN recall_schemes s ON s.id = r.analysis_mode
                WHERE r.video_id = ?
                ORDER BY r.analysis_mode
                """,
                (video_id,),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    @api.get("/api/videos/{video_id}/experiments")
    def get_experiments(video_id: int) -> list[dict[str, object]]:
        video_store = provider.get()
        _video_or_404(video_store, video_id)
        return [_row_to_dict(row) for row in video_store.list_experiments(video_id)]

    def experiment_snapshot(video_id: int, payload: ExperimentPayload) -> dict:
        video_store = provider.get()
        _video_or_404(video_store, video_id)
        try:
            config = LabConfig.model_validate(payload.model_dump(
                exclude={"name", "approved_snapshot_hash"},
            ))
            return prepare_snapshot(
                video_store, video_id, config, load_provider_settings().active_profile,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.post("/api/videos/{video_id}/experiments/preview")
    def preview_experiment(video_id: int, payload: ExperimentPayload) -> dict:
        """Render actual extraction requests without constructing an API client."""
        return experiment_snapshot(video_id, payload)

    @api.post("/api/videos/{video_id}/experiments", status_code=202)
    def start_experiment(video_id: int, payload: ExperimentPayload) -> dict[str, object]:
        video_store = provider.get()
        snapshot = experiment_snapshot(video_id, payload)
        try:
            require_approval(snapshot, payload.approved_snapshot_hash)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        profile = ProviderProfile.model_validate(snapshot["profile"])
        settings = load_provider_settings()
        key = resolve_interface_key(profile, settings)
        try:
            profile.require_ready(key)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if jobs.is_running():
            raise HTTPException(status_code=409, detail="已有任务正在运行，请等待当前任务完成。")
        experiment_id = register_run(
            video_store, payload.name, snapshot, payload.approved_snapshot_hash,
        )

        def operation(
            update: Callable[[float, str, str | None], None],
            should_cancel: Callable[[], bool],
        ) -> object:
            return execute_run(
                video_store, experiment_id, snapshot, key, payload.approved_snapshot_hash,
                progress=lambda value, message: update(value, message, "experiment"),
                should_cancel=should_cancel,
            )

        try:
            job_id = jobs.submit(
                "video_experiment",
                video_id,
                operation,
                request={
                    "kind": "experiment",
                    "experiment_id": experiment_id,
                    "video_id": video_id,
                    "options": payload.model_dump(),
                },
            )
        except Exception as exc:
            video_store.update_experiment(
                experiment_id,
                status="failed",
                error_message=str(exc),
            )
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"experiment_id": experiment_id, "video_id": video_id, "job_id": job_id, "status": "queued"}

    @api.get("/api/experiments/{experiment_id}")
    def get_experiment(experiment_id: str) -> dict[str, object]:
        video_store = provider.get()
        row = video_store.get_experiment(experiment_id)
        if row is None:
            raise HTTPException(status_code=404, detail="实验不存在")
        payload = _row_to_dict(row)
        payload["report"] = video_store.get_experiment_results(experiment_id)
        payload["markers"] = payload["report"]["results"] if payload["report"] is not None else [
            _row_to_dict(marker)
            for marker in video_store.list_markers(int(row["video_id"]), str(row["analysis_mode"]))
        ]
        return payload

    @api.get("/api/experiments/{experiment_id}/markers")
    def get_experiment_markers(experiment_id: str) -> list[dict[str, object]]:
        video_store = provider.get()
        row = video_store.get_experiment(experiment_id)
        if row is None:
            raise HTTPException(status_code=404, detail="实验不存在")
        report = video_store.get_experiment_results(experiment_id)
        if report is not None:
            return report["results"]
        return [
            _row_to_dict(marker)
            for marker in video_store.list_markers(int(row["video_id"]), str(row["analysis_mode"]))
        ]

    @api.get("/api/experiments/{experiment_id}/calls")
    def get_experiment_calls(experiment_id: str) -> list[dict]:
        video_store = provider.get()
        if video_store.get_experiment(experiment_id) is None:
            raise HTTPException(status_code=404, detail="实验不存在")
        return video_store.list_experiment_calls(experiment_id)

    @api.get("/api/experiments/{experiment_id}/snapshot")
    def get_experiment_snapshot(experiment_id: str) -> dict:
        video_store = provider.get()
        row = video_store.get_experiment(experiment_id)
        if row is None:
            raise HTTPException(status_code=404, detail="实验不存在")
        snapshot = json.loads(row["options_json"])
        if "transcript_hash" not in snapshot:
            raise HTTPException(status_code=409, detail="旧实验没有完整输入快照")
        snapshot["transcript"] = video_store.get_transcript_snapshot(snapshot["transcript_hash"])
        return snapshot

    @api.get("/api/experiments/{experiment_id}/steps")
    def get_experiment_steps(experiment_id: str) -> list[dict]:
        video_store = provider.get()
        if video_store.get_experiment(experiment_id) is None:
            raise HTTPException(status_code=404, detail="实验不存在")
        return video_store.list_experiment_steps(experiment_id)

    @api.get("/api/videos/{video_id}/thumbnail")
    def get_video_thumbnail(video_id: int, background: BackgroundTasks):
        video = _video_or_404(provider.get(), video_id)
        path = Path(str(video["video_path"])).expanduser()
        try:
            cache = asr_storage.audio_directory(DIRECTORY_PROJECT_ROOT, provider.data_root()) / "thumbnails"
            thumbnail = video_thumbnail(path, cache)
        except (OSError, ThumbnailUnavailable):
            raise HTTPException(status_code=404, detail="视频截图暂不可用")
        if startup_snapshot_enabled:
            background.add_task(save_startup_cover, DIRECTORY_PROJECT_ROOT, provider.library_id(), video_id, thumbnail)
        return FileResponse(thumbnail, media_type="image/jpeg", headers={"Cache-Control": "private, no-cache"}, background=background)

    @api.get("/api/videos/{video_id}/preview-frame")
    def get_video_preview_frame(
        video_id: int,
        time_ms: int = Query(ge=0, le=MAX_SQLITE_INTEGER),
        library_id: str = Query(min_length=1, max_length=128),
    ):
        if library_id != provider.library_id():
            raise HTTPException(status_code=409, detail="视频库连接已变化，请重新搜索")
        video = _video_or_404(provider.get(), video_id)
        path = Path(str(video["video_path"])).expanduser()
        target = min(time_ms, max(0, int(video["duration_ms"] or 0) - 1))
        try:
            cache = asr_storage.audio_directory(DIRECTORY_PROJECT_ROOT, provider.data_root()) / "search-previews"
            frame = video_preview_frame(path, cache, target)
        except (OSError, ThumbnailUnavailable):
            raise HTTPException(status_code=404, detail="该时间点的视频画面暂不可用")
        return FileResponse(frame, media_type="image/jpeg", headers={"Cache-Control": "private, no-cache"})

    @api.get("/api/videos/{video_id}/stream")
    def stream_video(video_id: int, request: Request):
        video_store = provider.get()
        video = _video_or_404(video_store, video_id)
        path = Path(str(video["video_path"])).expanduser()
        if not path.is_file():
            raise HTTPException(status_code=404, detail="视频源文件不存在")

        size = path.stat().st_size
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        common_headers = {"Accept-Ranges": "bytes"}
        range_header = request.headers.get("range")
        if not range_header:
            return FileResponse(path, media_type=media_type, headers=common_headers)

        byte_range = _parse_range(range_header, size)
        if byte_range is None:
            return JSONResponse(
                status_code=416,
                content={"detail": "请求的字节范围无效"},
                headers={**common_headers, "Content-Range": f"bytes */{size}"},
            )

        start, end = byte_range
        headers = {
            **common_headers,
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Content-Length": str(end - start + 1),
        }
        return StreamingResponse(
            _iter_file(path, start, end),
            status_code=206,
            media_type=media_type,
            headers=headers,
        )

    @api.patch("/api/markers/{marker_id}")
    def patch_marker(marker_id: int, payload: MarkerPatch) -> dict[str, object]:
        video_store = provider.get()
        _marker_or_404(video_store, marker_id)
        video_store.set_marker_review_status(marker_id, payload.resolved_status)
        return _row_to_dict(_marker_or_404(video_store, marker_id))

    @api.patch("/api/markers/{marker_id}/jump-time")
    def patch_marker_jump_time(marker_id: int, payload: MarkerJumpTimePatch) -> dict[str, object]:
        try:
            return _row_to_dict(provider.get().set_marker_jump_time(marker_id, payload.user_start_ms))
        except KeyError:
            raise HTTPException(status_code=404, detail="重点不存在")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @api.patch("/api/markers/{marker_id}/importance")
    def patch_marker_importance(marker_id: int, payload: MarkerImportancePatch) -> dict[str, object]:
        try:
            return _row_to_dict(provider.get().set_marker_importance(marker_id, payload.importance))
        except KeyError:
            raise HTTPException(status_code=404, detail="重点不存在")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    from classcue.frontend_host import mount_frontend
    mount_frontend(api, Path(__file__).resolve().parents[1])
    return api


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.getenv("CLASSROOM_API_HOST", "127.0.0.1"),
        port=int(os.getenv("CLASSROOM_API_PORT", "8765")),
    )
