"""Derived recall schemes with optional full prompts and a fixed output protocol."""

from copy import deepcopy
import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

BUILTIN_MODES = ("high_recall", "medium_recall", "low_recall")
BASE_VARIANTS = {"high_recall": "high_recall", "medium_recall": "precision", "low_recall": "low_recall"}
ModeId = Annotated[str, Field(pattern=r"^(high_recall|medium_recall|low_recall|strict_exam_intent|custom_[a-f0-9]{32})$")]


class TeacherRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phrase: str = Field(min_length=1, max_length=60)
    meaning: Literal["emphasis", "ordinary", "deemphasize"] = "emphasis"

    @field_validator("phrase")
    @classmethod
    def clean_phrase(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("表达不能为空")
        return value


class RecallPromptInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_mode: Literal["high_recall", "medium_recall", "low_recall"] = "medium_recall"
    rules: list[TeacherRule] = Field(default_factory=list, max_length=20)
    notes: str = Field(default="", max_length=500)
    advanced_prompt: str | None = Field(default=None, min_length=1, max_length=60000)

    @field_validator("notes")
    @classmethod
    def clean_text(cls, value):
        return value.strip()

    @field_validator("advanced_prompt")
    @classmethod
    def clean_advanced_prompt(cls, value):
        if value is not None:
            value = value.strip()
            if not value:
                raise ValueError("完整提示词不能为空；如需恢复基础编辑，请关闭完整编辑模式")
        return value

    @model_validator(mode="after")
    def validate_expressions(self):
        phrases = [rule.phrase.casefold() for rule in self.rules]
        if len(set(phrases)) != len(phrases):
            raise ValueError("同一方案内的教师表达不能重复")
        return self


class RecallSchemeInput(RecallPromptInput):
    name: str = Field(min_length=1, max_length=60)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        return value.strip()

    @model_validator(mode="after")
    def validate_name(self):
        if not self.name or self.name in {"高召回", "中召回", "低召回"}:
            raise ValueError("请填写与内置方案不同的方案名称")
        return self


def semantic_hash(payload):
    value = (
        {"advanced_prompt": payload["advanced_prompt"]}
        if payload.get("advanced_prompt") is not None
        else {key: payload[key] for key in ("base_mode", "rules", "notes")}
    )
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def freeze_schemes(store, modes):
    from classcue.llm_experiments import load_prompt_bundle

    snapshots = {}
    for mode in modes:
        if not mode.startswith("custom_"):
            continue
        scheme = store.get_recall_scheme(mode)
        if scheme is None or scheme["archived"]:
            raise ValueError("自定义召回方案不存在或已停用，请重新选择")
        # The base prompt is frozen too, so recovery never silently changes rules.
        bundle = (
            advanced_prompt_bundle(scheme["advanced_prompt"])
            if scheme.get("advanced_prompt") is not None
            else load_prompt_bundle(variant=BASE_VARIANTS[scheme["base_mode"]])
        )
        snapshots[mode] = {**scheme, "base_bundle": bundle}
    return snapshots


def _quoted_preference(text):
    text = json.dumps(text, ensure_ascii=False)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def custom_prompt_bundle(mode, snapshot):
    from classcue.llm_experiments import digest

    if not snapshot or snapshot.get("id") != mode:
        raise ValueError("自定义召回方案缺少有效的任务快照")
    validated = RecallSchemeInput.model_validate({
        key: snapshot[key] for key in ("name", "base_mode", "rules", "notes")
    } | {"advanced_prompt": snapshot.get("advanced_prompt")}).model_dump()
    bundle = deepcopy(snapshot["base_bundle"])
    if validated["advanced_prompt"] is not None:
        bundle["rules"] = validated["advanced_prompt"]
        bundle.pop("version", None)
        bundle["version"] = digest(bundle)
        return bundle
    meanings = {
        "emphasis": "表示要求重点掌握",
        "ordinary": "属于一般提示，不单独表示重点要求",
        "deemphasize": "通常不表示重点要求",
    }
    if validated["rules"] or validated["notes"]:
        lines = ["## 教师习惯表达补充", "结合上下文参考，不改变上述规则。"]
        lines.extend(
            f'- {_quoted_preference(rule["phrase"])}：{meanings[rule["meaning"]]}。'
            for rule in validated["rules"]
        )
        if validated["notes"]:
            lines.append(f'补充偏好：{_quoted_preference(validated["notes"])}')
        bundle["rules"] += "\n\n" + "\n".join(lines)
    # Display names and revisions must not invalidate semantically identical results.
    bundle.pop("version", None)
    bundle["version"] = digest(bundle)
    return bundle


def advanced_prompt_bundle(prompt):
    from classcue.llm_experiments import PROMPT_DIRECTORY, digest

    bundle = {
        "system": (PROMPT_DIRECTORY / "custom_protocol.md").read_text(encoding="utf-8").strip(),
        "rules": prompt,
        "task": "按上述自定义提示词处理本次实际字幕，遵守固定输出与字幕引用协议。",
        "examples": [],
        "review": "按上述自定义提示词复核当前候选。只返回该候选的复核结果，遵守固定输出与字幕引用协议。",
        "repair": (PROMPT_DIRECTORY / "repair.md").read_text(encoding="utf-8").strip(),
        "evidence_repair": (PROMPT_DIRECTORY / "evidence_repair.md").read_text(encoding="utf-8").strip(),
    }
    bundle["version"] = digest(bundle)
    return bundle


def preview_recall_prompt(value: RecallPromptInput):
    from classcue.llm_experiments import LabConfig, load_prompt_bundle, render_examples, render_messages

    if value.advanced_prompt is not None:
        prompt = value.advanced_prompt
        effective = advanced_prompt_bundle(prompt)
        protocol = effective["system"]
    else:
        mode = "custom_" + "0" * 32
        base = load_prompt_bundle(variant=BASE_VARIANTS[value.base_mode])
        ordinary = custom_prompt_bundle(mode, {
            **value.model_dump(), "name": "提示词预览", "id": mode, "base_bundle": base,
        })
        # Keep business instructions and examples editable; publish the protocol separately.
        business_rules = base["rules"].split("## 证据与范围", 1)[0].rstrip()
        supplements = ordinary["rules"][len(base["rules"]):]
        prompt = "\n\n".join([
            "# 系统与任务说明", base["system"],
            business_rules + supplements,
            "# 判断示例", render_examples(base["examples"]),
            "# 执行指令", base["task"],
        ])
        effective = ordinary
        protocol = advanced_prompt_bundle(prompt)["system"]
    messages = render_messages(effective, LabConfig(scheme="few_shot"), [])
    prefix, _, suffix = messages[1]["content"].rpartition("<transcript_text>\n\n</transcript_text>")
    messages[1]["content"] = (
        prefix + "<transcript_text>\n（运行时填入本次字幕及真实编号）\n</transcript_text>" + suffix
    )
    return {
        "prompt": prompt, "protocol": protocol, "messages": messages,
        "repair": effective["repair"], "evidence_repair": effective["evidence_repair"],
        "advanced": value.advanced_prompt is not None,
    }
