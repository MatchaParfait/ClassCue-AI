"""Transport-level budget and cancellation gates for real adapter sends."""

import json
from types import SimpleNamespace

import pytest

from classcue.ai_provider import ProviderProfile
from classcue.bilibili_harness import EXACT_LIMITS, LIMITS, BudgetStop, HarnessService, Ranking
from classcue.bilibili_study import StudyModel
from classcue.processing_control import ProcessingCancelled
from tests.test_bilibili_harness import FakeStore, run_state


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status_code = status


def completion(content='{"items":[]}'):
    return SimpleNamespace(choices=[
        SimpleNamespace(message=SimpleNamespace(content=content), finish_reason="stop")
    ])


UNSUPPORTED = ApiError(400, "response_format is not supported")


class FakeCompletions:
    """Scripted chat.completions transport; no network or model involved."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.on_create = lambda: None

    def create(self, **kwargs):
        self.calls.append(kwargs)
        self.on_create()
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def transport_model(outcomes):
    completions = FakeCompletions(outcomes)
    model = StudyModel(
        ProviderProfile(base_url="https://relay.example/v1", model="relay-fake", max_tokens=1024),
        "test-key", client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
    )
    return model, completions


class StubClient:
    account_scope = "synthetic-account"

    def __init__(self):
        self.before_request = lambda: None


def gated_service(model, run, **kwargs):
    service = HarnessService(FakeStore(), model, StubClient(), **kwargs)
    service.bind(run)
    return service


def ask_ranking(service):
    return service.request("合成排序请求", {"target": service.run["target"]}, Ranking)


def test_json_compatibility_resend_is_counted_as_a_second_send():
    single_run = run_state()
    single_model, single_calls = transport_model([completion()])
    assert ask_ranking(gated_service(single_model, single_run)).items == []
    run = run_state()
    model, calls = transport_model([UNSUPPORTED, completion()])
    assert ask_ranking(gated_service(model, run)).items == []
    assert (len(single_calls.calls), len(calls.calls)) == (1, 2)
    assert calls.calls[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in calls.calls[1]
    assert single_run["budget"]["llm"] == 1
    assert run["budget"]["llm"] == 2
    assert run["budget"]["tokens"] == 2 * single_run["budget"]["tokens"]


def test_total_llm_limit_stops_before_compatibility_resend():
    run = run_state()
    run["limits"] = {**LIMITS, "llm": 1}
    model, calls = transport_model([UNSUPPORTED, completion()])
    with pytest.raises(BudgetStop) as caught:
        ask_ranking(gated_service(model, run))
    assert (caught.value.scope, caught.value.resource) == ("task", "llm")
    assert (caught.value.used, caught.value.limit, caught.value.requested) == (1, 1, 1)
    assert len(calls.calls) == 1
    assert run["budget"]["llm"] == 1


def test_exact_phase_limit_stops_before_compatibility_resend():
    run = run_state()
    run["phase_limits"] = {"exact": {**EXACT_LIMITS, "llm": 1}}
    model, calls = transport_model([UNSUPPORTED, completion()])
    with pytest.raises(BudgetStop) as caught:
        ask_ranking(gated_service(model, run))
    assert caught.value.scope == "exact"
    assert (caught.value.resource, caught.value.used, caught.value.limit) == ("llm", 1, 1)
    assert len(calls.calls) == 1


def test_cancellation_observed_before_resend_blocks_second_send():
    run = run_state()
    model, calls = transport_model([UNSUPPORTED, completion()])
    state = {"cancelled": False}
    calls.on_create = lambda: state.__setitem__("cancelled", True)
    with pytest.raises(ProcessingCancelled):
        ask_ranking(gated_service(model, run, should_cancel=lambda: state["cancelled"]))
    assert len(calls.calls) == 1
    assert run["budget"]["llm"] == 1


def test_format_repair_send_still_checks_remaining_budget():
    run = run_state()
    run["limits"] = {**LIMITS, "llm": 1}
    model, calls = transport_model([completion("not valid json"), completion()])
    with pytest.raises(BudgetStop) as caught:
        ask_ranking(gated_service(model, run))
    assert (caught.value.resource, caught.value.used) == ("llm", 1)
    assert len(calls.calls) == 1
    assert any(event["action"] == "format_repair" for event in run["trace"])


@pytest.mark.parametrize("schema_name", ["EvidenceExtraction", "SubtitleLocalization"])
def test_extraction_reserve_blocks_transport_before_first_send(schema_name):
    from classcue.bilibili_evidence import EvidenceExtraction
    from classcue.bilibili_visual import SubtitleLocalization

    schema = {"EvidenceExtraction": EvidenceExtraction,
              "SubtitleLocalization": SubtitleLocalization}[schema_name]
    run = run_state()
    model, calls = transport_model([completion()])
    service = gated_service(model, run)
    run["budget"]["llm"] = LIMITS["llm"] - 1
    material = {"target": run["target"],
                "unit": {"id": "BV1xx411c7mD:1", "bvid": "BV1xx411c7mD", "cid": 1},
                "segments": [{"id": 1, "text": "合成字幕", "start_ms": 0, "end_ms": 1000}]}
    with pytest.raises(BudgetStop) as caught:
        gated_service(model, run).request("提取合成证据", material, schema)
    assert caught.value.resource == "llm" and caught.value.reserve == 1
    assert caught.value.used < caught.value.limit
    assert calls.calls == []
    assert run["budget"]["llm"] == LIMITS["llm"] - 1


def test_image_transport_is_gated_and_counted_once():
    class FakeImage:
        source, sha256 = "question.front", "0" * 64

        def content(self):
            return [{"type": "text", "text": "图片来源编号：question.front"},
                    {"type": "image_url", "image_url": {
                        "url": "data:image/png;base64,c3lu", "detail": "high"}}]

    run = run_state()
    model, calls = transport_model([completion()])
    result = gated_service(model, run).request(
        "图文核验", {"target": run["target"]}, Ranking, images=(FakeImage(),))
    assert result.items == []
    assert len(calls.calls) == 1
    parts = calls.calls[0]["messages"][1]["content"]
    assert isinstance(parts, list) and any(part["type"] == "image_url" for part in parts)
    assert run["budget"]["llm"] == 1


def test_cached_request_never_reaches_transport():
    run = run_state()
    model, calls = transport_model([completion()])
    service = gated_service(model, run)
    ask_ranking(service)
    assert len(calls.calls) == 1
    ask_ranking(service)
    assert len(calls.calls) == 1
    assert run["budget"]["llm"] == 1
    assert any(event["action"] == "cache" for event in run["trace"])


def test_adapter_runs_gate_before_each_transport_send():
    from classcue.openai_judge import OpenAICompatibleJudge

    calls = FakeCompletions([UNSUPPORTED, completion()])
    judge = OpenAICompatibleJudge(
        "test-key", "relay-fake", base_url="https://relay.example/v1",
        client=SimpleNamespace(chat=SimpleNamespace(completions=calls)),
    )
    seen = []
    raw = judge._request_content("prompt", "system", 128,
                                 before_transport=lambda: seen.append(len(calls.calls)))
    assert json.loads(raw) == {"items": []}
    assert seen == [0, 1]
    assert len(calls.calls) == 2
    assert "response_format" not in calls.calls[1]


def test_control_exceptions_keep_their_type_through_study_model():
    model, _ = transport_model([completion()])

    def budget_gate():
        raise BudgetStop("task", resource="llm", used=0, limit=0, requested=1)

    with pytest.raises(BudgetStop):
        model.ask("任务", {}, Ranking, before_transport=budget_gate)

    def cancel_gate():
        raise ProcessingCancelled("已停止 B 站讲解检索")

    with pytest.raises(ProcessingCancelled):
        model.ask("任务", {}, Ranking, before_transport=cancel_gate)
