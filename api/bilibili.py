"""Marker-scoped Bilibili study routes; no network activity on read endpoints."""

from __future__ import annotations

import copy
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from classcue.ai_provider import load_provider_settings, resolve_interface_key
from classcue.bilibili import BilibiliAccount, BilibiliClient, BilibiliError
from classcue.bilibili_login import BilibiliQrLogin
from classcue.bilibili_store import BilibiliStore, StudyContext, digest
from classcue.bilibili_study import StudyModel
from classcue.bilibili_harness import HarnessService as StudyService, Query as SearchQuery, Target, PROTOCOL, validate_sources


class SearchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(min_length=1, max_length=64)
    queries: list[SearchQuery] = Field(min_length=1, max_length=3)
    consent: bool = Field(strict=True)
    course_queries: list[SearchQuery] = Field(default_factory=list, max_length=3)
    target: Target | None = None
    auto_rewrite: bool = False


class ContinuePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(min_length=1, max_length=64)
    consent: bool = Field(strict=True)
    queries: list[SearchQuery] | None = Field(default=None, max_length=3)
    increase_budget: bool = False


class ResumePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    acknowledge_account_risk: bool = Field(strict=True)


class PlanPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    consent: bool = Field(strict=True)


class LoginSessionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(pattern=r"^[0-9a-f]{32}$")


def router(provider, jobs, account_directory: Path) -> APIRouter:
    routes = APIRouter()
    account = BilibiliAccount(account_directory)
    login = BilibiliQrLogin(account)

    def repository():
        return BilibiliStore(provider.get())

    def idle():
        if jobs.is_running():
            raise HTTPException(status_code=409, detail="已有任务正在运行，请等待或停止当前任务")

    def model_settings():
        settings = load_provider_settings()
        profile = settings.active_profile.model_copy(deep=True)
        key = resolve_interface_key(profile, settings)
        try:
            profile.require_ready(key)
        except ValueError:
            raise HTTPException(status_code=400, detail="请先配置可用的 AI 服务连接") from None
        return profile, key

    def submit(repo, run, profile, key, kind, cookie=""):
        run["job_id"] = uuid.uuid4().hex
        repo.save_run(run)

        def operation(update, should_cancel):
            with StudyModel(profile, key) as model, BilibiliClient(
                repo, cookie, should_cancel=should_cancel,
            ) as client:
                service = StudyService(repo, model, client, update, should_cancel)
                return service.plan(run) if kind == "bilibili_plan" else service.search(run)

        try:
            job_id = jobs.submit(
                kind, run["input"]["marker"]["video_id"], operation,
                request={"bilibili_run_id": run["id"]}, job_id=run["job_id"],
            )
        except RuntimeError:
            run.update(status="failed", error="任务未能启动，请稍后重试")
            repo.save_run(run)
            raise HTTPException(status_code=409, detail="任务未能启动，请等待当前任务完成后重试") from None
        return {"run_id": run["id"], "job_id": job_id}

    @routes.get("/api/bilibili/account")
    def account_status():
        return {
            "configured": account.path.is_file(),
            "pause": repository().control("platform_pause"),
        }

    def login_request(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        origin = request.headers.get("origin")
        try:
            parsed = urlsplit(origin) if origin else None
            invalid = bool(parsed and (
                parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.username or parsed.password
            ))
        except ValueError:
            invalid = True
        if invalid or request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(status_code=403, detail="只允许从本机应用发起 B 站登录")
        idle()

    def login_result(operation):
        try:
            return operation()
        except BilibiliError as error:
            status = {"login_not_found": 404, "login_throttled": 429, "platform_paused": 409}.get(error.code, 502)
            raise HTTPException(status_code=status, detail=str(error)) from None

    @routes.post("/api/bilibili/login/start")
    def start_login(request: Request, response: Response):
        login_request(request, response)
        return login_result(lambda: login.start(repository()))

    @routes.post("/api/bilibili/login/poll")
    def poll_login(payload: LoginSessionPayload, request: Request, response: Response):
        login_request(request, response)
        return login_result(lambda: login.poll(payload.session_id, repository(), before_save=idle))

    @routes.post("/api/bilibili/login/cancel")
    def cancel_login(payload: LoginSessionPayload, request: Request, response: Response):
        login_request(request, response)
        return login.cancel(payload.session_id)

    @routes.delete("/api/bilibili/account")
    def delete_account():
        idle()
        login.cancel()
        account.delete()
        return account_status()

    @routes.post("/api/bilibili/resume")
    def resume(payload: ResumePayload):
        idle()
        if not payload.acknowledge_account_risk:
            raise HTTPException(status_code=400, detail="请先检查账号状态并确认恢复读取")
        repository().set_control("platform_pause", None)
        return account_status()

    @routes.get("/api/markers/{marker_id}/bilibili")
    def study_state(marker_id: int):
        repo = repository()
        try:
            return {"context": repo.context(marker_id), "runs": repo.runs(marker_id)}
        except KeyError:
            raise HTTPException(status_code=404, detail="考点不存在或已被重新分析替换") from None

    @routes.put("/api/markers/{marker_id}/bilibili/context")
    def save_context(marker_id: int, payload: StudyContext):
        idle()
        try:
            return repository().save_context(marker_id, payload)
        except KeyError:
            raise HTTPException(status_code=404, detail="考点不存在") from None

    @routes.post("/api/markers/{marker_id}/bilibili/plan", status_code=202)
    def plan(marker_id: int, payload: PlanPayload):
        idle()
        if not payload.consent:
            raise HTTPException(status_code=400, detail="请确认向所选 AI 服务发送考点依据及补充信息")
        repo = repository()
        try:
            snapshot = repo.snapshot(marker_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="考点不存在") from None
        profile, key = model_settings()
        run = repo.create_run(snapshot, profile.model_dump(exclude={"credential_id"}))
        return submit(repo, run, profile, key, "bilibili_plan")

    @routes.post("/api/markers/{marker_id}/bilibili/search", status_code=202)
    def search(marker_id: int, payload: SearchPayload):
        idle()
        if not payload.consent:
            raise HTTPException(status_code=400, detail="请确认发送搜索词并将候选字幕提交所选 AI 服务")
        repo = repository()
        try:
            source = repo.get_run(marker_id, payload.run_id)
            snapshot = repo.snapshot(marker_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="考点或搜索方案不存在") from None
        if source["input_hash"] != digest(snapshot):
            raise HTTPException(status_code=409, detail="考点、字幕或补充信息已变化，请重新生成搜索方案")
        if source["status"] != "ready" or not source["queries"]:
            raise HTTPException(status_code=409, detail="搜索方案尚未生成完成")
        if source.get("version") != PROTOCOL or not source.get("target"):
            raise HTTPException(status_code=409, detail="旧版记录需要重新生成目标与搜索方案")
        if repo.control("platform_pause"):
            raise HTTPException(status_code=409, detail="B 站读取已暂停，请检查账号后手动恢复")
        profile, key = model_settings()
        identity = profile.model_dump(exclude={"credential_id"})
        if identity != source["profile"]:
            raise HTTPException(status_code=409, detail="AI 连接设置已变化，请重新生成搜索方案")
        try:
            cookie = account.load()
        except (BilibiliError, ValueError):
            raise HTTPException(status_code=400, detail="B 站登录状态不可用，请重新扫码或退出登录") from None
        target = payload.target or Target.model_validate(source["target"])
        try:
            validate_sources(target, snapshot, allow_user=True)
        except ValueError:
            raise HTTPException(status_code=400, detail="目标引用无效，请重新生成方案") from None
        if payload.course_queries and not target.course:
            raise HTTPException(status_code=400, detail="请明确课程类别后再搜索课程")
        run = repo.create_run(snapshot, identity)
        run.update(status="searching", queries=[query.model_dump() for query in payload.queries],
                   version=PROTOCOL, target=target.model_dump(),
                   course_queries=[q.model_dump() for q in payload.course_queries], auto_rewrite=payload.auto_rewrite,
                   parent_run_id=source["id"], account_scope=digest(cookie) if cookie else "anonymous",
                   budget=copy.deepcopy(source.get("budget", {})))
        return submit(repo, run, profile, key, "bilibili_search", cookie)

    @routes.post("/api/markers/{marker_id}/bilibili/continue", status_code=202)
    def continue_search(marker_id: int, payload: ContinuePayload):
        idle()
        if not payload.consent:
            raise HTTPException(status_code=400, detail="请确认继续检索及字幕核验")
        repo = repository()
        try:
            run = repo.get_run(marker_id, payload.run_id)
            snapshot = repo.snapshot(marker_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="考点或搜索记录不存在") from None
        if run.get("version") != PROTOCOL or run["input_hash"] != digest(snapshot):
            raise HTTPException(status_code=409, detail="目标或材料已变化，请重新生成方案")
        if run["status"] not in {"failed", "interrupted", "awaiting_approval", "budget_exhausted"}:
            raise HTTPException(status_code=409, detail="该任务当前不能继续")
        if repo.control("platform_pause"):
            raise HTTPException(status_code=409, detail="B 站读取已暂停，请先处理平台提示")
        profile, key = model_settings()
        if profile.model_dump(exclude={"credential_id"}) != run["profile"]:
            raise HTTPException(status_code=409, detail="AI 设置已变化，请重新生成方案")
        try:
            cookie = account.load()
        except (BilibiliError, ValueError):
            raise HTTPException(status_code=400, detail="读取凭据不可用") from None
        if run.get("account_scope") != (digest(cookie) if cookie else "anonymous"):
            raise HTTPException(status_code=409, detail="读取账号已变化，请重新生成方案")
        if run["status"] == "awaiting_approval":
            if not payload.queries:
                raise HTTPException(status_code=400, detail="请确认新的课程搜索词")
            queries = [q.model_dump() for q in payload.queries if q.text not in run["completed_queries"]]
            if not queries:
                raise HTTPException(status_code=400, detail="这些搜索词已执行")
            run.update(course_queries=queries, round=3)
            run.pop("pending_queries", None)
        if run["status"] == "budget_exhausted":
            if not payload.increase_budget:
                raise HTTPException(status_code=400, detail="预算已耗尽，需明确确认追加预算")
            if run.get("budget_extended"):
                raise HTTPException(status_code=409, detail="本任务已经追加过预算，请结束并检查现有结果")
            run["limits"] = {name: limit * 2 for name, limit in run["limits"].items()}
            run["budget_extended"] = True
        run.update(status="searching", error="")
        return submit(repo, run, profile, key, "bilibili_search", cookie)

    return routes
