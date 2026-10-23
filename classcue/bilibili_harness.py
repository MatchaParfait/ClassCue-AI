"""Bounded, resumable exact-topic and course retrieval controller."""

from __future__ import annotations

import time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from classcue.bilibili import BilibiliError, video_url
from classcue.bilibili_store import digest, encode
from classcue.processing_control import ProcessingCancelled

PROTOCOL = "bilibili-harness-v2"
LIMITS = {"platform": 60, "llm": 48, "tokens": 600000, "seconds": 1800,
          "videos": 10, "attempts": 15, "verified": 5}
EXACT_LIMITS = {"platform": 30, "llm": 24, "tokens": 300000,
                "videos": 6, "attempts": 10, "verified": 3}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Requirement(Strict):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")
    text: str = Field(min_length=1, max_length=500)
    required: bool = True
    depth: Literal["concept", "method", "example"] = "example"
    sources: list[str] = Field(min_length=1, max_length=20)


ComparisonText = Annotated[str, Field(min_length=1, max_length=2000)]


class QuestionComparison(Strict):
    kind: Literal["exercise", "topic"]
    conditions: list[ComparisonText] = Field(default_factory=list, max_length=30)
    tasks: list[ComparisonText] = Field(default_factory=list, max_length=30)
    knowledge_points: list[ComparisonText] = Field(default_factory=list, max_length=12)
    inferred_methods: list[ComparisonText] = Field(default_factory=list, max_length=8)
    allowed_variations: list[ComparisonText] = Field(default_factory=list, max_length=8)
    unknowns: list[ComparisonText] = Field(default_factory=list, max_length=12)


class Target(Strict):
    course: str = Field(default="", max_length=200)
    requirements: list[Requirement] = Field(min_length=1, max_length=8)
    keywords: list[str] = Field(default_factory=list, max_length=12)
    unknowns: list[str] = Field(default_factory=list, max_length=8)
    comparison: QuestionComparison | None = None

    @model_validator(mode="after")
    def validate_requirements(self):
        if len({r.id for r in self.requirements}) != len(self.requirements) or not any(r.required for r in self.requirements):
            raise ValueError("目标必须包含唯一编号及至少一个核心要求")
        return self


class Query(Strict):
    text: str = Field(min_length=1, max_length=160, pattern=r"^[^\x00-\x1f]+$")
    purpose: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def clean(self):
        self.text = self.text.strip()
        if not self.text:
            raise ValueError("搜索词不能为空")
        return self


class Plan(Strict):
    target: Target
    exact_queries: list[Query] = Field(min_length=1, max_length=3)
    course_queries: list[Query] = Field(default_factory=list, max_length=3)


class RankedItem(Strict):
    id: str
    reason: str = Field(min_length=1, max_length=500)


class Ranking(Strict):
    items: list[RankedItem] = Field(default_factory=list, max_length=30)


class Rewrite(Strict):
    queries: list[Query] = Field(default_factory=list, max_length=3)
    reason: str = Field(min_length=1, max_length=500)


class BudgetStop(Exception):
    def __init__(self, scope="task"):
        self.scope = scope


def validate_sources(target: Target, snapshot: dict, *, allow_user=False):
    allowed = {"marker.title", "marker.note", "marker.source_quote"}
    allowed.update("context." + key for key, value in snapshot["context"].items() if value)
    allowed.update(f"classroom:{s['id']}" for s in snapshot["classroom_segments"])
    question = snapshot.get("question") or {}
    if question.get("status") == "ready":
        allowed.update(source for requirement in question["target"]["requirements"] for source in requirement["sources"])
    if allow_user:
        allowed.add("user.confirmed")
    if any(source not in allowed for req in target.requirements for source in req.sources):
        raise ValueError("目标引用了不存在的课堂依据或补充信息")


class HarnessService:
    def __init__(self, store, model, client, update=lambda *_: None, should_cancel=lambda: False):
        self.store, self.model, self.client = store, model, client
        self.update, self.should_cancel = update, should_cancel
        self.run = None
        self.last_tick = time.monotonic()

    def bind(self, run):
        self.run = run
        run.setdefault("budget", {})
        for key in LIMITS:
            run["budget"].setdefault(key, 0)
        run.setdefault("limits", dict(LIMITS))
        run.setdefault("trace", [])
        run.setdefault("phase", "exact")
        run.setdefault("completed_queries", [])
        run.setdefault("directories", [])
        run.setdefault("candidates", [])
        run.setdefault("round", 1)
        self.last_tick = time.monotonic()
        self.client.before_request = lambda: self.consume("platform")

    def persist(self):
        now = time.monotonic()
        self.run["budget"]["seconds"] += now - self.last_tick
        self.last_tick = now
        self.store.save_run(self.run)

    def event(self, action, **details):
        event = {"seq": len(self.run["trace"]) + 1, "action": action,
                 "phase": self.run["phase"], "at": time.time(), **details}
        self.run["trace"].append(event)
        self.persist()

    def checkpoint(self):
        if self.should_cancel():
            raise ProcessingCancelled("已停止 B 站讲解检索")
        if self.run["budget"]["seconds"] + time.monotonic() - self.last_tick >= self.run["limits"]["seconds"]:
            raise BudgetStop()

    def ensure(self, key, amount=1):
        self.checkpoint()
        if self.run["budget"][key] + amount > self.run["limits"][key]:
            raise BudgetStop()
        if (self.run["phase"] == "exact" and key in EXACT_LIMITS
                and self.run["budget"][key] + amount > EXACT_LIMITS[key]):
            raise BudgetStop("exact")

    def consume(self, key, amount=1):
        self.ensure(key, amount)
        self.run["budget"][key] += amount
        self.persist()

    def synthesis_reserve(self, material, output):
        from classcue.bilibili_evidence import EvidenceAssessment

        unit = material["unit"]
        saved = next((u for c in self.run["candidates"] for u in c["units"]
                      if u.get("bvid") == unit["bvid"] and u.get("cid") == unit["cid"]), {})
        chunks = saved.get("verification", {}).get("chunks", {}).values()
        findings = [f for chunk in chunks for f in chunk["findings"]]
        ids = {i for chunk in chunks for i in chunk["segment_ids"]}
        ids.update(row["id"] for row in material["segments"])
        segments = saved.get("subtitle", {}).get("segments", material["segments"])
        aggregate = {"target": material["target"], "unit": unit, "findings": findings,
                     "segments": [row for row in segments if row["id"] in ids]}
        # Estimate the next extraction output as well as the synthesis output.
        # This grows with persisted evidence instead of a fixed per-chunk reserve.
        return len(encode(aggregate)) + len(encode(EvidenceAssessment.model_json_schema())) + output * 9 + 8000

    def request(self, task, material, schema):
        self.checkpoint()
        key = digest([PROTOCOL, schema.__name__, task, self.run["input_hash"], self.run["profile"],
                      self.run.get("account_scope"), material])
        cached = self.store.cache_get(key)
        if cached is not None:
            self.event("cache", schema=schema.__name__)
            return schema.model_validate(cached)
        output = int(self.run["profile"].get("max_tokens") or 4096)
        cost = len(encode(material)) + len(task) + len(encode(schema.model_json_schema())) + output + 1000
        # An unfinished extraction must leave capacity for a separate synthesis.
        reserve = 1 if schema.__name__ == "EvidenceExtraction" else 0
        reserve_tokens = self.synthesis_reserve(material, output) if reserve else 0
        self.ensure("llm", 1 + reserve)
        self.ensure("tokens", cost + reserve_tokens)
        for attempt in range(2):
            self.consume("llm")
            self.consume("tokens", cost)
            try:
                result = self.model.ask(task, material, schema)
                self.store.cache_put(key, result.model_dump(), self.run["input"]["marker"]["video_id"])
                self.event("model", schema=schema.__name__, repair=bool(attempt))
                return result
            except ValueError:
                if attempt:
                    raise
                self.event("format_repair", schema=schema.__name__)
                self.ensure("llm", 1 + reserve)
                self.ensure("tokens", cost + reserve_tokens)
        raise AssertionError("unreachable")

    def status(self, status, message):
        self.checkpoint()
        self.run["status"] = status
        self.persist()
        progress = min(.95, .1 + self.run["budget"]["verified"] * .14)
        self.update(progress, message, status)

    def plan(self, run):
        self.bind(run)
        try:
            question = run["input"].get("question")
            if question and question.get("status") != "ready":
                raise ValueError("题目材料需先完成分析或补充信息")
            self.status("planning", "正在建立目标及两阶段搜索方案")
            result = self.request(
                "生成检索目标和两组搜索词。target包含course、requirements(id,text,required,depth,sources)、"
                "keywords和unknowns。depth为concept/method/example。sources仅引用marker.title、marker.note、"
                "marker.source_quote、非空context字段(context.course等)或classroom:字幕id。"
                "要求必须有输入依据，不编造题干。exact_queries为精准知识点搜索；course_queries为失败后"
                "使用的课程类别搜索，各最多3条(text,purpose)。课程不明确时course留空且不生成课程搜索词。"
                "有例题需求时设置example深度；没有具体题目也可要求同知识点例题，不要求原题。"
                "若输入包含question.target，按该目标生成搜索词并原样返回target，不得放宽核心要求。",
                run["input"], Plan)
            if question:
                result.target = Target.model_validate(question["target"])
            validate_sources(result.target, run["input"])
            if not result.target.course and result.course_queries:
                raise ValueError("课程类别不明确，请补充课程信息")
            run.update(version=PROTOCOL, target=result.target.model_dump(),
                       queries=[q.model_dump() for q in result.exact_queries],
                       course_queries=[q.model_dump() for q in result.course_queries], status="ready", error="")
            self.event("planned")
            return {"message": "目标与两组搜索词已生成，等待确认"}
        except Exception:
            run.update(status="interrupted" if self.should_cancel() else "failed", error="搜索方案生成未完成，请检查输入或模型连接")
            self.persist()
            raise

    def rank(self, items, kind):
        if not items:
            return []
        result = self.request(
            "根据目标按取证价值排列候选，返回items:[{id,reason}]，最多30个。"
            "标题只用于排序，不能证明覆盖；不确定项可排后，不应仅因没有同名关键词排除。"
            "选择章节时使用知识点与方法的语义关联。",
            {"target": self.run["target"], "kind": kind, "candidates": items}, Ranking)
        allowed = {item["id"] for item in items}
        if any(item.id not in allowed for item in result.items):
            raise ValueError("AI 引用了不存在的候选或章节")
        return list({item.id: item.reason for item in result.items}.items())

    def search_queries(self, queries):
        for query in queries:
            text = query["text"]
            if text in self.run["completed_queries"]:
                continue
            self.status("searching", "精确知识点搜索" if self.run["phase"] == "exact" else "课程类别搜索")
            results = self.client.search(text)
            found = {c["bvid"]: c for c in self.run["candidates"]}
            for rank, item in enumerate(results, 1):
                if item["bvid"] not in found and len(found) < 180:
                    found[item["bvid"]] = {**item, "id": item["bvid"], "status": "unverified", "units": [], "origins": []}
                if item["bvid"] in found:
                    found[item["bvid"]]["origins"].append({"phase": self.run["phase"], "query": text,
                                                          "round": self.run["round"], "rank": rank})
            self.run["candidates"] = list(found.values())
            self.run["completed_queries"].append(text)
            self.event("search", query=text, count=len(results))

    def discover(self, candidate, meta):
        collection = meta.get("collection") or {}
        if not collection.get("id"):
            return
        directories = self.run["directories"]
        if any(d["id"] == collection["id"] for d in directories):
            return
        remaining = 200 - sum(len(d["episodes"]) for d in directories)
        if len(directories) >= 3 or remaining <= 0:
            candidate["directory_unread"] = True
            return
        directory = {**collection, "episodes": collection.get("episodes", [])[:remaining], "entry_bvid": candidate["bvid"]}
        directory["complete"] = bool(collection.get("complete")) and len(directory["episodes"]) == len(collection.get("episodes", []))
        directories.append(directory)
        candidate["collection_title"] = directory["title"]
        known = {c["bvid"]: c for c in self.run["candidates"]}
        for ep in directory["episodes"]:
            bvid = ep["bvid"]
            if bvid not in known and len(known) < 180:
                new = {"id": bvid, "bvid": bvid, "title": ep["title"], "description": ep.get("section", ""),
                       "author": candidate.get("author", ""), "url": video_url(bvid), "status": "unverified",
                       "units": [], "origins": [], "collection_title": directory["title"], "no_expand_collection": True}
                known[bvid] = new
                self.run["candidates"].append(new)
            if bvid in known:
                known[bvid]["origins"].append({"phase": self.run["phase"], "collection": directory["id"],
                                              "entry_bvid": candidate["bvid"], "section": ep.get("section", "")})
        self.event("directory", id=directory["id"], complete=directory["complete"], count=len(directory["episodes"]))

    def prepare_candidate(self, candidate):
        if "metadata" not in candidate:
            self.consume("videos")
            meta = self.client.metadata(candidate["bvid"])
            candidate["metadata"] = meta
            candidate["page_count"] = meta["page_count"]
            if not candidate.get("no_expand_collection"):
                self.discover(candidate, meta)
            self.persist()
        meta = candidate["metadata"]
        if "unit_queue" not in candidate:
            units = [{**part, "id": f"{part['bvid']}:{part['cid']}", "title": candidate["title"],
                      "multipart": meta["page_count"] > 1} for part in meta["pages"]]
            by_id = {u["id"]: u for u in units}
            ranking = self.rank([{"id": u["id"], "title": u["title"], "part": u["part"]} for u in units], "章节") if len(units) > 1 else [(u["id"], "单视频直接取证") for u in units]
            candidate["unit_queue"] = [{**by_id[key], "selection_reason": reason} for key, reason in ranking]
            candidate["unchecked_pages"] = meta["page_count"]
            self.persist()

    def verify(self, candidate, unit):
        from classcue.bilibili_evidence import verify_unit

        saved = next((u for u in candidate["units"] if u["id"] == unit["id"]), None)
        if (not saved or not saved.get("verification_started")) and sum(
                bool(u.get("verification_started")) for u in candidate["units"]) >= 3:
            return False
        if saved is None:
            saved = {**unit, "status": "reading", "verification": {}}
            candidate["units"].append(saved)
        if saved["status"] in {"checked", "no_subtitles", "read_failed", "requires_login"}:
            return saved.get("verdict") == "covers"
        if "subtitle" not in saved:
            self.consume("attempts")
            self.status("reading", f"读取字幕：{unit['part'] or candidate['title']}")
            source = self.client.subtitles(unit)
            saved["source"] = {k: v for k, v in source.items() if k != "segments"}
            if source["status"] != "available":
                saved["status"] = source["status"]
                self.event("subtitle_unavailable", unit=unit["id"], status=source["status"])
                return False
            saved["subtitle"] = source
            self.persist()
        if not saved.get("verification_started"):
            if sum(bool(u.get("verification_started")) for u in candidate["units"]) >= 3:
                return False
            self.consume("verified")
            saved["verification_started"] = True
            self.persist()
        self.status("checking", f"核验字幕：{unit['part'] or candidate['title']}")
        saved["status"] = "checking"
        result = verify_unit(self.run["target"], unit, saved["subtitle"], saved["verification"],
                             request=self.request, checkpoint=self.checkpoint,
                             save_state=lambda _: self.persist())
        saved.update(result, status="checked")
        candidate["unchecked_pages"] = max(0, candidate["page_count"] - sum(u["status"] == "checked" for u in candidate["units"]))
        self.event("verified", unit=unit["id"], verdict=result["verdict"])
        return result["verdict"] == "covers"

    def drain(self):
        while True:
            self.checkpoint()
            pending = [c for c in self.run["candidates"] if c.get("processed_phase") != self.run["phase"]
                       and c.get("status") not in {"done", "read_failed"}]
            if not pending:
                return False
            self.status("screening", "按目标筛选候选及课程章节")
            ranking = self.rank([{k: c.get(k, "") for k in ("id", "title", "description", "collection_title")} for c in pending[:30]], "视频")
            selected = dict(ranking)
            for c in pending[:30]:
                if c["id"] not in selected:
                    c["processed_phase"] = self.run["phase"]
                    c["selection_reason"] = "本轮未入选，保留待选"
            self.persist()
            for key, reason in ranking:
                candidate = next(c for c in pending if c["id"] == key)
                candidate["selection_reason"] = reason
                try:
                    self.prepare_candidate(candidate)
                    for unit in candidate["unit_queue"]:
                        try:
                            if self.verify(candidate, unit):
                                return True
                        except BilibiliError as exc:
                            if exc.code in {"platform_paused", "budget_exceeded", "credential_invalid"}:
                                raise
                            saved = next((u for u in candidate["units"] if u["id"] == unit["id"]), None)
                            if saved:
                                saved.update(status="read_failed", error=str(exc))
                            self.event("subtitle_failed", unit=unit["id"])
                    candidate["status"] = "done"
                except BilibiliError as exc:
                    if exc.code in {"platform_paused", "budget_exceeded", "credential_invalid"}:
                        raise
                    candidate.update(status="read_failed", error=str(exc))
                candidate["processed_phase"] = self.run["phase"]
                self.persist()

    def finish(self, reason, status="completed"):
        self.run.update(status=status, stop_reason=reason)
        self.event("stop", reason=reason)
        return {"message": {"success": "已找到有字幕证据的讲解", "exhausted": "候选已检查，未找到满足全部要求的讲解"}.get(reason, "检索已保存")}

    def search(self, run):
        self.bind(run)
        run["error"] = ""
        try:
            if run.get("account_scope") != self.client.account_scope:
                raise ValueError("读取账号已变化，请生成新的搜索方案")
            if run.get("phase") == "exact":
                try:
                    self.search_queries(run["queries"])
                    if self.drain():
                        return self.finish("success")
                except BudgetStop as exc:
                    if exc.scope != "exact":
                        raise
                    self.event("phase_budget", reason="为课程检索保留额度")
                run.update(phase="course", round=2)
                self.event("fallback", reason="精确检索未获得合格字幕证据")
            if not run.get("course_queries"):
                return self.finish("clarification", "needs_clarification")
            self.search_queries(run["course_queries"])
            if self.drain():
                return self.finish("success")
            if run["round"] < 3:
                if "pending_queries" not in run:
                    rewrite = self.request("根据目标及失败原因生成最多3条新的课程搜索词(queries:text,purpose)，"
                                           "只补缺失知识点或方法，不能改变限制；没有有益扩展时返回空列表。返回reason。",
                                           {"target": run["target"], "used": run["completed_queries"],
                                            "outcomes": [{"title": c["title"], "status": c["status"],
                                                          "verdicts": [u.get("verdict") for u in c["units"]]} for c in run["candidates"]]}, Rewrite)
                    run["pending_queries"] = [q.model_dump() for q in rewrite.queries if q.text not in run["completed_queries"]]
                    run["rewrite_reason"] = rewrite.reason
                    self.persist()
                if run["pending_queries"]:
                    if not run.get("auto_rewrite"):
                        return self.finish("approval", "awaiting_approval")
                    run.update(round=3, course_queries=run.pop("pending_queries"))
                    self.event("rewrite", queries=run["course_queries"])
                    self.search_queries(run["course_queries"])
                    if self.drain():
                        return self.finish("success")
            partial = any(u.get("verdict") == "partial" for c in run["candidates"] for u in c["units"])
            return self.finish("partial" if partial else "exhausted")
        except BudgetStop:
            return self.finish("budget", "budget_exhausted")
        except ProcessingCancelled:
            self.finish("cancelled", "interrupted")
            raise
        except Exception as exc:
            if isinstance(exc, BilibiliError) and exc.code == "budget_exceeded":
                return self.finish("budget", "budget_exhausted")
            run["error"] = str(exc) if isinstance(exc, (BilibiliError, ValueError)) else "检索或模型服务失败，请检查后手动继续"
            self.finish("platform_pause" if isinstance(exc, BilibiliError) and exc.code == "platform_paused" else "service_error", "failed")
            raise
