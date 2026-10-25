"""Synthetic end-to-end controller tests using the real evidence verifier."""

from copy import deepcopy
import json

import pytest

from classcue.bilibili import BilibiliError
from classcue.bilibili_harness import BudgetStop, EXACT_LIMITS, HarnessService, LIMITS, PROTOCOL
from classcue.bilibili_study import StudyResponseError
from classcue.processing_control import ProcessingCancelled


EXACT = "合成配方法例题"
COURSE = "合成代数课程"
REWRITE = "合成代数专项练习"
BV_A = "BV1xx411c7mD"
BV_B = "BV1xx411c7mE"
BV_C = "BV1xx411c7mF"


class FakeStore:
    def __init__(self):
        self.cache, self.saved = {}, []

    def save_run(self, run):
        self.saved.append(deepcopy(run))

    def cache_get(self, key):
        return deepcopy(self.cache.get(key))

    def cache_put(self, key, value, video_id):
        assert video_id == 7
        self.cache[key] = deepcopy(value)


def video(bvid):
    return {"bvid": bvid, "title": "合成课程 " + bvid, "description": "合成简介",
            "author": "合成作者", "url": "https://www.bilibili.com/video/" + bvid}


def part(bvid, cid=101, page=1):
    return {"bvid": bvid, "cid": cid, "page": page, "part": "合成章节 " + str(page)}


class FakeClient:
    account_scope = "synthetic-account"

    def __init__(self, searches, *, metadata=None, missing=(), pause=False):
        self.searches, self.meta, self.missing = searches, metadata or {}, set(missing)
        self.pause = pause
        self.calls = []
        self.before_request = lambda: None
        self.after_subtitles = lambda: None

    def network(self, kind, value):
        self.before_request()
        self.calls.append((kind, value))
        if self.pause:
            raise BilibiliError("platform_paused", "合成平台暂停")

    def search(self, text):
        self.network("search", text)
        return [video(bvid) for bvid in self.searches.get(text, [])]

    def metadata(self, bvid):
        self.network("metadata", bvid)
        return deepcopy(self.meta.get(bvid, {"page_count": 1, "pages": [part(bvid)]}))

    def subtitles(self, unit):
        self.network("subtitles", (unit["bvid"], unit["cid"]))
        self.after_subtitles()
        if unit["bvid"] in self.missing:
            return {"status": "no_subtitles"}
        return {"status": "available", "language": "zh-CN", "automatic": False,
                "segments": [{"id": i + 1, "start_ms": i * 1000, "end_ms": (i + 1) * 1000,
                              "text": text} for i, text in enumerate(["例题条件", "配方法", "解题步骤"])]}


class FakeModel:
    def __init__(self, *, good=(), partial=(), rewrite=False, invalid_ranking=False, preferred_part=None):
        self.good, self.partial = set(good), set(partial)
        self.rewrite, self.invalid_ranking, self.preferred_part = rewrite, invalid_ranking, preferred_part
        self.calls = []

    def ask(self, task, material, schema):
        self.calls.append((schema.__name__, deepcopy(material)))
        name = schema.__name__
        if name == "Ranking":
            rows = material["candidates"]
            if self.invalid_ranking:
                return schema(items=[{"id": "not-a-candidate", "reason": "合成无效引用"}])
            if self.preferred_part and material["kind"] == "章节":
                rows = [row for row in rows if row["id"] == self.preferred_part]
            return schema(items=[{"id": row["id"], "reason": "合成取证顺序"} for row in rows])
        if name == "Rewrite":
            return schema(queries=[{"text": REWRITE, "purpose": "补充例题"}] if self.rewrite else [], reason="合成扩展")
        unit = material["unit"]
        key = (unit["bvid"], unit["cid"])
        good = key in self.good or unit["bvid"] in self.good
        partial = unit["bvid"] in self.partial
        if name == "EvidenceExtraction":
            return schema(findings=[{"requirement_id": "r1", "depth": "example" if good else "concept",
                                     "kind": "support", "reason": "合成字幕支持",
                                     "segment_ids": [1, 2, 3]}] if good or partial else [])
        assert name == "EvidenceAssessment"
        return schema(reason="综合合成字幕", assessments=[{
            "requirement_id": "r1", "status": "supported" if good or partial else "unknown",
            "depth": "example" if good else "concept", "reason": "合成综合判断",
            "evidence_ids": [item["id"] for item in material["findings"]],
            "same_example": good, "condition_ids": [1] if good else [],
            "method_ids": [2] if good else [], "step_ids": [3] if good else [],
        }])


def run_state():
    return {"id": "synthetic-run", "version": PROTOCOL, "input_hash": "synthetic-input",
            "input": {"marker": {"video_id": 7}}, "profile": {"model": "fake", "max_tokens": 1024},
            "account_scope": "synthetic-account", "status": "ready",
            "target": {"course": "代数", "requirements": [{"id": "r1", "text": "配方法例题",
                      "required": True, "depth": "example", "sources": ["marker.title"]}], "keywords": ["配方法"]},
            "queries": [{"text": EXACT, "purpose": "精确知识点"}],
            "course_queries": [{"text": COURSE, "purpose": "课程备用"}]}


def execute(run, client, model, store=None, **kwargs):
    store = store or FakeStore()
    HarnessService(store, model, client, **kwargs).search(run)
    return store


def searches(client):
    return [value for kind, value in client.calls if kind == "search"]


def test_search_planning_asks_for_short_topic_queries_not_instance_conditions():
    run = run_state()
    run["input"].update(marker={"video_id": 7, "title": "属性闭包"}, context={"course": "数据库"},
                        classroom_segments=[])
    store = FakeStore()
    client = FakeClient({})

    class Planner:
        def ask(self, task, material, schema):
            assert schema.__name__ == "Plan"
            assert "不得把原题的实例字母、数值、完整条件列表、中间步骤和最终答案拼进搜索词" in task
            assert "不是搜索词模板或原题相等约束" in task
            assert "不能机械删除所有数字或数学符号" in task
            assert "至少包含一条最简洁" in task
            assert "course_queries仅检索课程或较宽的章节" in task
            return schema(target={
                "course": "数据库", "requirements": [{"id": "r1", "text": "属性闭包计算例题",
                "depth": "example", "sources": ["marker.title"]}], "keywords": ["属性闭包"],
            }, exact_queries=[{"text": "数据库 属性闭包 例题", "purpose": "同知识点例题"}],
                course_queries=[{"text": "数据库原理 课程", "purpose": "课程目录"}])

    HarnessService(store, Planner(), client).plan(run)
    assert run["version"] == PROTOCOL
    assert run["queries"][0]["text"] == "数据库 属性闭包 例题"
    assert run["course_queries"][0]["text"] == "数据库原理 课程"
    assert client.calls == []


def test_exact_success_stops_before_other_candidate_or_course():
    run, client = run_state(), FakeClient({EXACT: [BV_A, BV_B], COURSE: [BV_C]})
    execute(run, client, FakeModel(good=[BV_A, BV_B]))
    assert run["stop_reason"] == "success"
    assert searches(client) == [EXACT]
    assert ("metadata", BV_B) not in client.calls
    assert run["budget"]["verified"] == 1
    assert run["candidates"][0]["units"][0]["evidence"][1]["start_ms"] == 1000


def test_format_repair_receives_validation_feedback_within_budget_gate():
    class RepairingModel(FakeModel):
        def ask(self, task, material, schema):
            result = super().ask(task, material, schema)
            if schema.__name__ == "EvidenceAssessment":
                if "format_feedback" not in material:
                    invalid = result.model_dump()
                    invalid["assessments"][0]["step_ids"] = [3] * 25
                    try:
                        schema.model_validate(invalid)
                    except ValueError as error:
                        raise StudyResponseError(json.dumps(invalid), error) from None
                feedback = material["format_feedback"]
                assert feedback["errors"] == [{
                    "type": "too_long", "field": ["assessments", 0, "step_ids"],
                }]
                assert "previous_response" in feedback
                assert "上次返回未通过JSON结构校验" in task
            return result

    run, model = run_state(), RepairingModel(good=[BV_A])
    store = execute(run, FakeClient({EXACT: [BV_A]}), model)
    assert run["stop_reason"] == "success"
    assert run["budget"]["llm"] == 4
    assert any(event["action"] == "format_repair" for event in run["trace"])
    assert all("format_feedback" not in saved for saved in store.saved)


def test_exact_failure_falls_back_to_plain_course_video():
    run, client = run_state(), FakeClient({EXACT: [BV_A], COURSE: [BV_B]})
    execute(run, client, FakeModel(good=[BV_B]))
    assert searches(client) == [EXACT, COURSE]
    assert run["stop_reason"] == "success"
    assert run["phase"] == "course"
    assert run["candidates"][1]["units"][0]["multipart"] is False


def test_course_optional_parts_select_nonfirst_part():
    run = run_state()
    client = FakeClient({EXACT: [], COURSE: [BV_B]}, metadata={BV_B: {
        "page_count": 3, "pages": [part(BV_B, 201 + i, i + 1) for i in range(3)]}})
    execute(run, client, FakeModel(good=[(BV_B, 203)], preferred_part=f"{BV_B}:203"))
    assert run["stop_reason"] == "success"
    assert ("subtitles", (BV_B, 203)) in client.calls
    assert ("subtitles", (BV_B, 201)) not in client.calls
    assert run["candidates"][0]["units"][0]["evidence"][0]["url"].endswith("p=3&t=0")
    assert run["candidates"][0]["unchecked_pages"] == 2


def test_related_collection_video_is_discovered_and_verified():
    run = run_state()
    client = FakeClient({EXACT: [], COURSE: [BV_A]}, missing=[BV_A], metadata={BV_A: {
        "page_count": 1, "pages": [part(BV_A)], "collection": {
            "id": 51, "title": "合成课程合集", "complete": True,
            "episodes": [{"bvid": BV_A, "title": "课程介绍"}, {"bvid": BV_B, "title": "配方法例题"}]}}})
    execute(run, client, FakeModel(good=[BV_B]))
    assert run["stop_reason"] == "success"
    found = next(c for c in run["candidates"] if c["bvid"] == BV_B)
    assert found["origins"][0]["entry_bvid"] == BV_A
    assert found["units"][0]["verdict"] == "covers"
    assert len(run["directories"]) == 1


def test_no_subtitles_backfills_without_spending_verified_slot():
    run, client = run_state(), FakeClient({EXACT: [BV_A, BV_B]}, missing=[BV_A])
    execute(run, client, FakeModel(good=[BV_B]))
    assert run["stop_reason"] == "success"
    assert run["budget"]["attempts"] == 2
    assert run["budget"]["verified"] == 1
    assert run["candidates"][0]["units"][0]["status"] == "no_subtitles"


def test_duplicate_candidates_share_reads_and_preserve_query_origins():
    run = run_state()
    second = "合成第二精确词"
    run["queries"].append({"text": second})
    client = FakeClient({EXACT: [BV_A, BV_A], second: [BV_A], COURSE: [BV_A, BV_B]}, missing=[BV_A])
    execute(run, client, FakeModel(good=[BV_B]))
    assert len(run["candidates"]) == 2
    assert client.calls.count(("metadata", BV_A)) == 1
    assert client.calls.count(("subtitles", (BV_A, 101))) == 1
    assert len(run["candidates"][0]["origins"]) == 4


def test_platform_pause_never_falls_back_and_keeps_budget():
    run, client = run_state(), FakeClient({EXACT: [BV_A]}, pause=True)
    with pytest.raises(BilibiliError, match="暂停"):
        execute(run, client, FakeModel())
    assert run["stop_reason"] == "platform_pause"
    assert searches(client) == [EXACT]
    assert run["budget"]["platform"] == 1


def test_cancel_preserves_finished_no_subtitle_result_and_resumes():
    run, client, store, cancelled = run_state(), FakeClient({EXACT: [BV_A, BV_B]}, missing=[BV_A]), FakeStore(), False

    def cancel_after_read():
        nonlocal cancelled
        cancelled = True

    client.after_subtitles = cancel_after_read
    with pytest.raises(ProcessingCancelled):
        execute(run, client, FakeModel(good=[BV_B]), store, should_cancel=lambda: cancelled)
    assert run["stop_reason"] == "cancelled"
    assert run["candidates"][0]["units"][0]["status"] == "no_subtitles"
    spent = deepcopy(run["budget"])
    client.after_subtitles = lambda: None
    execute(run, client, FakeModel(good=[BV_B]), store)
    assert run["stop_reason"] == "success"
    assert client.calls.count(("subtitles", (BV_A, 101))) == 1
    assert run["budget"]["platform"] > spent["platform"]


def test_task_budget_continuation_inherits_spent_requests():
    run, client, store = run_state(), FakeClient({EXACT: [BV_A]}), FakeStore()
    run["limits"] = {**LIMITS, "platform": 1}
    execute(run, client, FakeModel(good=[BV_A]), store)
    assert run["stop_reason"] == "budget"
    assert run["budget"]["platform"] == 1
    stopped = deepcopy(run["budget_stop"])
    assert stopped == {"scope": "task", "resource": "platform", "phase": "exact",
                       "used": 1, "limit": 1, "requested": 1, "reserve": 0}
    assert run["trace"][-1]["budget_stop"] == stopped
    run["limits"]["platform"] = 10
    execute(run, client, FakeModel(good=[BV_A]), store)
    assert run["stop_reason"] == "success"
    assert searches(client) == [EXACT]
    assert run["budget"]["platform"] == 3
    assert "budget_stop" not in run
    assert any(event.get("budget_stop") == stopped for event in run["trace"])


@pytest.mark.parametrize("resource", ["platform", "llm", "tokens", "seconds", "videos", "attempts", "verified"])
def test_every_task_limit_records_resource_and_keeps_other_balances(resource):
    run = run_state()
    run.update(phase="course", limits={**LIMITS, resource: 0})
    client = FakeClient({COURSE: [BV_A]})
    store = execute(run, client, FakeModel())
    stop = run["budget_stop"]
    assert run["status"] == "budget_exhausted"
    assert stop["scope"] == "task" and stop["resource"] == resource
    assert stop["phase"] == "course" and stop["limit"] == 0
    assert stop["used"] >= 0 and stop["requested"] >= 0
    assert run["trace"][-1]["budget_stop"] == stop
    assert store.saved[-1]["budget_stop"] == stop
    assert run["phase_limits"]["exact"] == EXACT_LIMITS
    assert run["budget"]["verified"] == 0


@pytest.mark.parametrize("resource", ["llm", "tokens"])
def test_next_step_reservation_can_stop_with_remaining_budget_without_charging(resource):
    from classcue.bilibili_evidence import EvidenceExtraction

    run = run_state()
    run["phase"] = "course"
    model = FakeModel()
    service = HarnessService(FakeStore(), model, FakeClient({}))
    service.bind(run)
    run["budget"][resource] = LIMITS[resource] - 1
    before = deepcopy(run["budget"])
    material = {"target": run["target"], "unit": part(BV_A),
                "segments": [{"id": 1, "text": "合成字幕", "start_ms": 0, "end_ms": 1000}]}
    with pytest.raises(BudgetStop) as caught:
        service.request("提取合成证据", material, EvidenceExtraction)
    detail = service.budget_detail(caught.value)
    assert detail["resource"] == resource
    assert detail["used"] < detail["limit"]
    assert detail["used"] + detail["requested"] > detail["limit"]
    assert detail["reserve"] > 0
    assert detail["requested"] > detail["reserve"]
    assert model.calls == []
    assert run["budget"] == before


def test_exact_phase_limit_records_transition_reason_not_task_exhaustion():
    run = run_state()
    run["phase_limits"] = {"exact": {**EXACT_LIMITS, "videos": 0}}
    client = FakeClient({EXACT: [BV_A], COURSE: [BV_B]})
    execute(run, client, FakeModel(good=[BV_B]))
    event = next(row for row in run["trace"] if row["action"] == "phase_budget")
    assert event["budget_stop"] == {"scope": "exact", "resource": "videos", "phase": "exact",
                                    "used": 0, "limit": 0, "requested": 1, "reserve": 0}
    assert run["stop_reason"] == "success"
    assert run["phase"] == "course"
    assert "budget_stop" not in run


def test_planning_budget_failure_records_limit_instead_of_only_model_failure():
    run = run_state()
    run["limits"] = {**LIMITS, "llm": 0}
    model = FakeModel()
    store = FakeStore()
    with pytest.raises(BudgetStop):
        HarnessService(store, model, FakeClient({})).plan(run)
    assert run["status"] == "failed"
    assert "运行额度限制" in run["error"]
    assert run["budget_stop"]["resource"] == "llm"
    assert any(row["action"] == "budget_stop" for row in run["trace"])
    assert model.calls == []


def test_unapproved_rewrite_is_saved_but_never_searched():
    run, client, store = run_state(), FakeClient({EXACT: [], COURSE: [], REWRITE: [BV_B]}), FakeStore()
    model = FakeModel(good=[BV_B], rewrite=True)
    execute(run, client, model, store)
    assert run["status"] == "awaiting_approval"
    assert run["pending_queries"][0]["text"] == REWRITE
    assert searches(client) == [EXACT, COURSE]
    execute(run, client, model, store)
    assert searches(client) == [EXACT, COURSE]
    run["auto_rewrite"] = True
    execute(run, client, model, store)
    assert searches(client) == [EXACT, COURSE, REWRITE]
    assert run["stop_reason"] == "success"


def test_fallback_rewrite_keeps_same_generalized_search_rules():
    run = run_state()
    client = FakeClient({EXACT: [], COURSE: []})
    model = FakeModel(rewrite=True)
    original = model.ask

    def ask(task, material, schema):
        if schema.__name__ == "Rewrite":
            assert "不得把原题的实例字母、数值、完整条件列表、中间步骤和最终答案拼进搜索词" in task
            assert "不把原题实例条件补回搜索词" in task
        return original(task, material, schema)

    model.ask = ask
    execute(run, client, model)
    assert run["status"] == "awaiting_approval"
    assert searches(client) == [EXACT, COURSE]


def test_invalid_ranked_candidate_never_triggers_metadata_read():
    run, client = run_state(), FakeClient({EXACT: [BV_A]})
    with pytest.raises(ValueError, match="不存在"):
        execute(run, client, FakeModel(invalid_ranking=True))
    assert all(kind != "metadata" for kind, _ in client.calls)
    assert run["stop_reason"] == "service_error"


def test_partial_evidence_does_not_stop_as_success():
    run, client = run_state(), FakeClient({EXACT: [BV_A], COURSE: [BV_B]})
    execute(run, client, FakeModel(partial=[BV_A, BV_B]))
    assert searches(client) == [EXACT, COURSE]
    assert run["stop_reason"] == "partial"
    assert all(u["verdict"] == "partial" for c in run["candidates"] for u in c["units"])


def test_transport_budget_is_reported_as_budget_stop():
    run, client = run_state(), FakeClient({EXACT: [BV_A]})

    def budget_stop(text):
        raise BilibiliError("budget_exceeded", "合成读取预算已用尽")

    client.search = budget_stop
    execute(run, client, FakeModel())
    assert run["status"] == "budget_exhausted"
    assert run["stop_reason"] == "budget"
    assert run["budget_stop"]["scope"] == "transport"
    assert run["budget_stop"]["resource"] is None


def test_cancel_during_model_response_does_not_repeat_completed_request():
    run, client, store = run_state(), FakeClient({EXACT: [BV_A]}), FakeStore()
    model, cancelled = FakeModel(good=[BV_A]), False
    original = model.ask

    def ask(task, material, schema):
        nonlocal cancelled
        result = original(task, material, schema)
        if schema.__name__ == "EvidenceExtraction":
            cancelled = True
        return result

    model.ask = ask
    with pytest.raises(ProcessingCancelled):
        execute(run, client, model, store, should_cancel=lambda: cancelled)
    assert run["stop_reason"] == "cancelled"
    model.ask = original
    execute(run, client, model, store)
    assert run["stop_reason"] == "success"
    assert sum(name == "EvidenceExtraction" for name, _ in model.calls) == 1


def test_resume_third_part_does_not_stop_at_first_already_checked_part():
    run, store = run_state(), FakeStore()
    client = FakeClient({EXACT: [BV_A], COURSE: []}, metadata={BV_A: {
        "page_count": 3, "pages": [part(BV_A, 201 + i, i + 1) for i in range(3)]}})
    model, cancelled = FakeModel(good=[(BV_A, 203)]), False
    original = model.ask

    def ask(task, material, schema):
        nonlocal cancelled
        result = original(task, material, schema)
        if schema.__name__ == "EvidenceExtraction" and material["unit"]["cid"] == 203:
            cancelled = True
        return result

    model.ask = ask
    with pytest.raises(ProcessingCancelled):
        execute(run, client, model, store, should_cancel=lambda: cancelled)
    assert run["budget"]["verified"] == 3
    assert run["candidates"][0]["units"][2]["status"] == "checking"
    model.ask = original
    execute(run, client, model, store)
    assert run["stop_reason"] == "success"
    assert run["candidates"][0]["units"][2]["verdict"] == "covers"
    assert run["budget"]["verified"] == 3


def test_same_bv_cid_in_duplicate_page_entries_is_read_once():
    run = run_state()
    client = FakeClient({EXACT: [BV_A]}, metadata={BV_A: {
        "page_count": 3, "pages": [part(BV_A, 201, 1), part(BV_A, 201, 1), part(BV_A, 203, 3)]}})
    execute(run, client, FakeModel(good=[(BV_A, 203)]))
    assert run["stop_reason"] == "success"
    assert client.calls.count(("subtitles", (BV_A, 201))) == 1
    assert run["budget"]["verified"] == 2
