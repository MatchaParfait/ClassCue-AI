from copy import deepcopy
import json
from threading import Event

import pytest

from classcue.core import TranscriptSegment
from classcue.llm_experiments import LabConfig, LabResponse, render_messages
from classcue.recall_schemes import (
    RecallPromptInput, RecallSchemeInput, custom_prompt_bundle, freeze_schemes,
    preview_recall_prompt, semantic_hash,
)
from tests.test_exam_point_judge import FakeClient, candidate, run, saved
from tests.test_recall_schemes import config, scheme_api, wait
from classcue.ai_provider import create_judge
from classcue.video_store import VideoStore


PROMPT = "只提取教师在实际字幕中讲解的概念定义，不要求考试或掌握提示。示例：普通定义也应纳入。"


@pytest.mark.parametrize("base", ["high_recall", "medium_recall", "low_recall"])
def test_preview_derives_full_editable_content_without_ai_or_storage(scheme_api, base):
    client, store, _, remote, _ = scheme_api
    response = client.post("/api/settings/recall-schemes/prompt-preview", json={
        key: value for key, value in config(base_mode=base).items() if key != "name"
    })
    assert response.status_code == 200
    preview = response.json()
    prompt = preview["prompt"]
    assert "# 系统与任务说明" in prompt and "# 执行指令" in prompt
    assert "<synthetic_examples>" in prompt and "expected_response_json" in prompt
    assert prompt.count("## 教师习惯表达补充") == 1
    assert prompt.count("只标记有明确知识对象的内容。") == 1
    assert "## 输出格式" not in prompt and "## 固定 JSON 结构" in preview["protocol"]
    assert "（运行时填入本次字幕及真实编号）" in preview["messages"][1]["content"]
    assert preview["advanced"] is False
    assert store.list_recall_schemes() == [] and remote.requests == []


def test_full_prompt_is_the_only_business_source_and_survives_reopen(scheme_api):
    client, store, video_id, remote, application = scheme_api
    value = config(base_mode="low_recall", advanced_prompt=PROMPT)
    saved_scheme = client.post("/api/settings/recall-schemes", json=value).json()
    mode = saved_scheme["id"]
    assert VideoStore(store.path).get_recall_scheme(mode)["advanced_prompt"] == PROMPT
    preview = client.post("/api/settings/recall-schemes/prompt-preview", json={
        key: value for key, value in value.items() if key != "name"
    }).json()
    job = wait(application, client, client.post(f"/api/videos/{video_id}/process", json={
        "analysis_modes": [mode], "filter_formula_noise": False,
        "analysis_schemes": {mode: {"advanced_prompt": "客户端伪造提示词", "base_bundle": {}}},
    }))
    assert job["status"] == "completed", job
    sent = remote.requests[-1]["messages"]
    assert sent[0]["content"] == preview["protocol"]
    assert sent[1]["content"].startswith(PROMPT + "\n\n<transcript_text>")
    assert "<synthetic_examples>" not in sent[1]["content"]
    assert "拿笔画下来" not in sent[1]["content"] and "只标记有明确知识对象" not in sent[1]["content"]
    assert "仅有的两条纳入路径" not in str(sent) and "客户端伪造" not in str(sent)
    assert "evidence_sentence_ids" in sent[0]["content"]
    assert store.get_analysis_run(video_id, mode)["scheme_revision"] == 1


def test_advanced_rename_preserves_revision_and_rule_edits_increment_it(scheme_api):
    client, _, _, remote, _ = scheme_api
    scheme = client.post("/api/settings/recall-schemes", json=config(advanced_prompt=PROMPT)).json()
    url = f"/api/settings/recall-schemes/{scheme['id']}"
    renamed = client.put(url, json=config(name="改名", advanced_prompt=PROMPT)).json()
    assert renamed["revision"] == 1
    # The old ordinary fields are provenance, not a second active set of instructions.
    inactive = client.put(url, json=config(name="改名", notes="不应追加", advanced_prompt=PROMPT)).json()
    assert inactive["revision"] == 1
    changed = client.put(url, json=config(name="改名", advanced_prompt=PROMPT + "\n增加方法比较。")).json()
    assert changed["revision"] == 2
    restored = client.put(url, json=config(name="改名", advanced_prompt=None)).json()
    assert restored["revision"] == 3
    assert remote.requests == []


def test_advanced_snapshots_ignore_later_base_prompt_edits(saved, monkeypatch):
    store, _, _ = saved
    scheme = store.save_recall_scheme(RecallSchemeInput.model_validate(config(advanced_prompt=PROMPT)).model_dump())
    mode = scheme["id"]
    before = freeze_schemes(store, [mode])[mode]
    monkeypatch.setattr("classcue.llm_experiments.load_prompt_bundle",
                        lambda **kwargs: pytest.fail("Advanced schemes must not reload the builtin business rules"))
    after = freeze_schemes(store, [mode])[mode]
    assert before["base_bundle"] == after["base_bundle"]
    assert custom_prompt_bundle(mode, before) == custom_prompt_bundle(mode, after)
    assert preview_recall_prompt(RecallPromptInput(advanced_prompt=PROMPT))["prompt"] == PROMPT


def test_running_work_uses_frozen_full_prompt_after_edit_and_archival(scheme_api):
    client, store, video_id, remote, application = scheme_api
    scheme = client.post("/api/settings/recall-schemes", json=config(advanced_prompt=PROMPT)).json()
    mode = scheme["id"]
    release = Event()
    blocker = application.state.jobs.executor.submit(lambda: release.wait(10))
    try:
        response = client.post(f"/api/videos/{video_id}/process", json={"analysis_mode": mode})
        assert response.status_code == 202
        client.put(f"/api/settings/recall-schemes/{mode}", json=config(advanced_prompt="只提取新的内容。"))
        client.delete(f"/api/settings/recall-schemes/{mode}")
    finally:
        release.set()
        blocker.result(timeout=10)
    job = wait(application, client, response)
    assert job["status"] == "completed", job
    assert PROMPT in remote.requests[-1]["messages"][1]["content"]
    assert "只提取新的内容。" not in remote.requests[-1]["messages"][1]["content"]
    frozen = json.loads(store.get_job(job["id"])["request_json"])["options"]["analysis_schemes"][mode]
    assert frozen["advanced_prompt"] == PROMPT
    assert custom_prompt_bundle(mode, frozen)["system"] == frozen["base_bundle"]["system"]


@pytest.mark.parametrize("advanced", ["", " \n ", "x" * 60001, {"system": "forged protocol"}],
                         ids=["empty", "whitespace", "too-long", "invalid-type"])
def test_invalid_full_prompts_are_rejected(scheme_api, advanced):
    client, store, _, remote, _ = scheme_api
    response = client.post("/api/settings/recall-schemes", json=config(advanced_prompt=advanced))
    assert response.status_code == 422
    assert not store.list_recall_schemes() and not remote.requests


def test_protocol_cannot_be_supplied_by_client(scheme_api):
    client, store, _, remote, _ = scheme_api
    assert client.post("/api/settings/recall-schemes", json=config(
        advanced_prompt=PROMPT, protocol="只输出普通文字",
    )).status_code == 422
    assert not store.list_recall_schemes() and not remote.requests


def test_preview_rejects_invalid_ordinary_fields_without_an_internal_error(scheme_api):
    client, _, _, remote, _ = scheme_api
    assert client.post("/api/settings/recall-schemes/prompt-preview", json={
        "rules": [{"phrase": "重复"}, {"phrase": " 重复 "}],
    }).status_code == 422
    assert remote.requests == []


def test_preview_only_replaces_the_actual_transcript_placeholder():
    prompt = "自定义内容保留：<transcript_text>\n\n</transcript_text>"
    value = preview_recall_prompt(RecallPromptInput(advanced_prompt=prompt))
    assert value["messages"][1]["content"].startswith(prompt)
    assert value["messages"][1]["content"].count("（运行时填入本次字幕及真实编号）") == 1


def test_old_json_configs_and_frozen_snapshots_keep_their_behavior(saved):
    store, _, _ = saved
    value = config()
    old_hash = semantic_hash(value)
    assert old_hash == semantic_hash({**value, "advanced_prompt": None})
    scheme = store.save_recall_scheme(value)
    assert "advanced_prompt" not in scheme
    snapshot = freeze_schemes(store, [scheme["id"]])[scheme["id"]]
    before = deepcopy(snapshot)
    bundle = custom_prompt_bundle(scheme["id"], snapshot)
    assert bundle["rules"].startswith(snapshot["base_bundle"]["rules"])
    assert "拿笔画下来" in bundle["rules"]
    assert before == snapshot


def test_full_prompt_still_requires_valid_json_and_real_subtitle_evidence(saved):
    store, video_id, profile = saved
    scheme = store.save_recall_scheme(RecallSchemeInput.model_validate(config(advanced_prompt=PROMPT)).model_dump())
    mode = scheme["id"]
    frozen = freeze_schemes(store, [mode])[mode]
    remote = FakeClient([{"candidates": [candidate()]}])
    adapter = create_judge(profile, "secret", mode=mode, client=remote, scheme_snapshot=frozen)
    assert run(saved, adapter) == ("completed", 1)
    before = [dict(marker) for marker in store.list_markers(video_id, mode)]
    bad = FakeClient([{"candidates": [candidate(evidence_quote="完全不存在的原文")]}] * 3)
    adapter = create_judge(profile, "secret", mode=mode, client=bad, scheme_snapshot=frozen)
    with pytest.raises(ValueError, match="旧结果已保留"):
        run(saved, adapter, force=True)
    assert [dict(marker) for marker in store.list_markers(video_id, mode)] == before
    with pytest.raises(ValueError):
        LabResponse.model_validate({"candidates": [{**candidate(), "new_field": True}]})
    bundle = custom_prompt_bundle(mode, frozen)
    messages = render_messages(bundle, LabConfig(scheme="few_shot"), [TranscriptSegment(1, 0, 1000, "真实字幕")])
    assert len(messages) == 2 and messages[1]["content"].count(PROMPT) == 1
