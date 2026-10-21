"""OpenAI-compatible Chat Completions adapter for classroom screening."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ai_provider import (
    DEFAULT_OUTPUT_TOKENS, DEFAULT_TIMEOUT_SECONDS, DEFAULT_MAX_RETRIES,
    DEFAULT_REASONING_EFFORT, ProviderProfile,
)
from core import (
    SemanticResponseError, SemanticSelection, TranscriptSegment,
    parse_semantic_json, semantic_json_repair_prompt, semantic_selection_prompt,
    strict_exam_intent_prompt,
)


def _response_format_unsupported(error: Exception) -> bool:
    status = getattr(error, "status_code", None)
    message = str(error).lower()
    return status in {400, 422} and any(
        field in message for field in ("response_format", "json_object")
    ) and any(
        word in message for word in (
            "unsupported", "not supported", "does not support", "not support",
            "unknown", "unrecognized", "not allowed", "not permitted", "unexpected", "不支持",
        )
    )


class OpenAICompatibleJudge:
    def __init__(
        self, api_key: str, model: str, *, base_url: str,
        provider: str = "openai_compatible", mode: str = "high_recall",
        max_tokens: int = DEFAULT_OUTPUT_TOKENS, token_parameter: str = "max_tokens",
        json_mode: bool = True, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES, client: Any | None = None,
        reasoning_effort: str | None = DEFAULT_REASONING_EFFORT,
    ) -> None:
        self.profile = ProviderProfile(
            base_url=base_url, model=model, max_tokens=max_tokens,
            token_parameter=token_parameter, json_mode=json_mode,
            timeout_seconds=timeout_seconds, max_retries=max_retries,
            reasoning_effort=reasoning_effort,
        )
        if not api_key.strip() or not self.profile.base_url:
            raise ValueError("缺少 API Key 或 Base URL。")
        if mode not in {"high_recall", "strict_exam_intent"}:
            raise ValueError("未知 AI 判定模式。")
        self._owns_client = client is None
        if client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise RuntimeError("未安装 openai，请使用项目运行环境或安装 requirements.txt。") from exc

            client = OpenAI(
                api_key=api_key.strip(), base_url=self.profile.base_url,
                timeout=self.profile.timeout_seconds, max_retries=self.profile.max_retries,
            )
        self.client = client
        self.model = self.profile.model
        self.provider = provider
        self.mode = mode

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> OpenAICompatibleJudge:
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    @property
    def cache_identity(self) -> str:
        profile = self.profile
        payload = {
            "provider": self.provider, "base_url": profile.base_url,
            "model": self.model, "mode": self.mode, "max_tokens": profile.max_tokens,
            "token_parameter": profile.token_parameter, "json_mode": profile.json_mode,
            "reasoning_effort": profile.reasoning_effort,
            "prompt_version": "semantic-prompts-v1",
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
        return f"openai-compatible:{digest}"

    def list_remote_models(self) -> list[str]:
        return sorted({model.id for model in self.client.models.list() if model.id})

    def list_models(self) -> list[str]:
        """Backward-compatible alias for callers using the original method name."""
        return self.list_remote_models()

    def test_api_connection(self) -> str:
        """Probe the configured model with a minimal ordinary text request."""
        return self._request_content(
            "只回复 OK，不要输出其他内容。",
            "You are a connectivity test. Reply with a short non-empty text response.",
            self.profile.max_tokens,
            json_mode=False,
        )

    def test_connection(self) -> None:
        raw = self._request_content(
            '接口测试：只返回 JSON 对象 {"candidates":[]}，不要添加其他内容。',
            "You are a JSON API test. Return only a valid JSON object.",
            self.profile.max_tokens,
        )
        parse_semantic_json(raw)

    def judge_semantic_chunk(self, chunk: tuple[TranscriptSegment, ...]) -> SemanticSelection:
        if not chunk:
            return SemanticSelection()
        strict_mode = self.mode == "strict_exam_intent"
        prompt = strict_exam_intent_prompt(chunk) if strict_mode else semantic_selection_prompt(chunk)
        system_prompt = (
            "你是严格的期末考试意向审核器。必须只输出合法 JSON 对象，不能输出 Markdown 或解释。"
            if strict_mode
            else "你是严谨的课堂字幕考点筛选器。必须只输出合法 JSON 对象，不能输出 Markdown 或解释。"
        )
        try:
            raw = self._request_content(prompt, system_prompt, self.profile.max_tokens)
        except SemanticResponseError:
            raw = self._request_content(prompt, system_prompt, self.profile.max_tokens)
        try:
            selection = parse_semantic_json(raw)
        except ValueError:
            selection = self._repair_selection(raw, strict_mode)
        return self._validate_selection(selection, chunk, strict_mode)

    def _request_content(
        self, prompt: str, system_prompt: str, max_tokens: int,
        *, json_mode: bool | None = None,
    ) -> str:
        if not self.model:
            raise ValueError("请填写 AI 模型名称。")
        request = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            self.profile.token_parameter: max_tokens,
            "reasoning_effort": self.profile.reasoning_effort,
        }
        use_json_mode = self.profile.json_mode if json_mode is None else json_mode
        if use_json_mode:
            request["response_format"] = {"type": "json_object"}
        try:
            response = self.client.chat.completions.create(**request)
        except Exception as exc:
            if "response_format" not in request or not _response_format_unsupported(exc):
                raise
            del request["response_format"]
            response = self.client.chat.completions.create(**request)
        try:
            choice = response.choices[0]
            content = choice.message.content
        except (AttributeError, IndexError, TypeError) as exc:
            raise SemanticResponseError("AI 没有返回可读取的 JSON 内容") from exc
        if getattr(choice, "finish_reason", None) == "length":
            raise RuntimeError("AI 输出达到长度上限，未保存该块；请提高输出上限后重试。")
        if not isinstance(content, str) or not content.strip():
            raise SemanticResponseError("AI 返回了空的 JSON 内容")
        return content

    def _repair_selection(self, raw: str, strict_mode: bool) -> SemanticSelection:
        try:
            repaired = self._request_content(
                semantic_json_repair_prompt(raw, strict_mode),
                "你是 JSON 修复器。只能输出合法 JSON 对象，不得补充新事实。",
                self.profile.max_tokens,
            )
            return parse_semantic_json(repaired)
        except ValueError as exc:
            raise SemanticResponseError(f"AI 的初次 JSON 无效且修复失败：{exc}") from exc

    def _validate_selection(
        self, selection: SemanticSelection, chunk: tuple[TranscriptSegment, ...], strict_mode: bool
    ) -> SemanticSelection:
        positions = {segment.id: index for index, segment in enumerate(chunk)}
        return SemanticSelection(candidates=[
            candidate for candidate in selection.candidates
            if (not strict_mode or candidate.level == "confirmed")
            and candidate.start_sentence_id in positions
            and candidate.end_sentence_id in positions
            and positions[candidate.start_sentence_id] <= positions[candidate.end_sentence_id]
        ])
