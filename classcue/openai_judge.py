"""OpenAI-compatible Chat Completions adapter for classroom screening."""

from __future__ import annotations

import hashlib
import json
import math
import random
import time
from email.utils import mktime_tz, parsedate_tz
from typing import Any, Callable

from classcue.ai_provider import (
    DEFAULT_OUTPUT_TOKENS, DEFAULT_TIMEOUT_SECONDS, DEFAULT_MAX_RETRIES,
    DEFAULT_REASONING_EFFORT, DEFAULT_REQUEST_CONCURRENCY, ProviderProfile,
)
from classcue.core import (
    SemanticResponseError, SemanticSelection, TranscriptSegment,
    parse_semantic_json, semantic_json_repair_prompt, semantic_selection_prompt,
    strict_exam_intent_prompt,
)
from classcue.processing_control import ProcessingCancelled


def _check_stop(should_stop: Callable[[], bool] | None) -> None:
    if should_stop and should_stop():
        raise ProcessingCancelled("用户已请求停止 AI 筛选")


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


def _retry_after_seconds(headers) -> float | None:
    if headers is None:
        return None
    try:
        return float(headers.get("retry-after-ms")) / 1000
    except (TypeError, ValueError):
        pass
    value = headers.get("retry-after")
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        parsed = parsedate_tz(value)
        return float(mktime_tz(parsed) - time.time()) if parsed is not None else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _transport_retry_delay(error: Exception, attempt: int) -> float | None:
    response = getattr(error, "response", None)
    headers = response.headers if response is not None else None
    retry_after = _retry_after_seconds(headers)
    if response is not None:
        # Match the locked SDK's server directives and retryable status codes.
        if retry_after is not None and math.isfinite(retry_after) and retry_after > 120:
            return None
        directive = headers.get("x-should-retry")
        if directive == "false":
            return None
        if directive != "true" and response.status_code not in {408, 409, 429} and response.status_code < 500:
            return None
    if retry_after is not None and math.isfinite(retry_after) and 0 < retry_after <= 120:
        return retry_after
    return min(0.5 * 2 ** min(attempt, 4), 8.0) * (1 - 0.25 * random.random())


def _wait_for_retry(delay: float, should_stop: Callable[[], bool]) -> None:
    deadline = time.monotonic() + delay
    while True:
        _check_stop(should_stop)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.05))


class OpenAICompatibleJudge:
    def __init__(
        self, api_key: str, model: str, *, base_url: str,
        provider: str = "openai_compatible", mode: str = "high_recall",
        max_tokens: int = DEFAULT_OUTPUT_TOKENS, token_parameter: str = "max_tokens",
        json_mode: bool = True, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES, client: Any | None = None,
        reasoning_effort: str | None = DEFAULT_REASONING_EFFORT,
        request_concurrency: int = DEFAULT_REQUEST_CONCURRENCY,
    ) -> None:
        self.profile = ProviderProfile(
            base_url=base_url, model=model, max_tokens=max_tokens,
            token_parameter=token_parameter, json_mode=json_mode,
            timeout_seconds=timeout_seconds, max_retries=max_retries,
            reasoning_effort=reasoning_effort,
            request_concurrency=request_concurrency,
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

    def judge_semantic_chunk(
        self, chunk: tuple[TranscriptSegment, ...], *,
        should_stop: Callable[[], bool] | None = None,
    ) -> SemanticSelection:
        _check_stop(should_stop)
        if not chunk:
            return SemanticSelection()
        strict_mode = self.mode == "strict_exam_intent"
        prompt = strict_exam_intent_prompt(chunk) if strict_mode else semantic_selection_prompt(chunk)
        system_prompt = (
            "你是严格的期末考试意向审核器。必须只输出合法 JSON 对象，不能输出 Markdown 或解释。"
            if strict_mode
            else "你是严谨的课堂字幕考点筛选器。必须只输出合法 JSON 对象，不能输出 Markdown 或解释。"
        )
        cancel_options = {"should_stop": should_stop} if should_stop is not None else {}
        try:
            raw = self._request_content(prompt, system_prompt, self.profile.max_tokens, **cancel_options)
        except SemanticResponseError:
            raw = self._request_content(prompt, system_prompt, self.profile.max_tokens, **cancel_options)
        try:
            selection = parse_semantic_json(raw)
        except ValueError:
            selection = self._repair_selection(raw, strict_mode, **cancel_options)
        return self._validate_selection(selection, chunk, strict_mode)

    def _request_content(
        self, prompt: str | list[dict[str, Any]], system_prompt: str, max_tokens: int,
        *, json_mode: bool | None = None, should_stop: Callable[[], bool] | None = None,
        before_transport: Callable[[], None] | None = None,
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

        def send():
            return self._send_completion(request, should_stop, before_transport)

        try:
            response = send()
        except Exception as exc:
            if "response_format" not in request or not _response_format_unsupported(exc):
                raise
            del request["response_format"]
            response = send()
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

    def _send_completion(
        self, request: dict[str, Any], should_stop: Callable[[], bool] | None,
        before_transport: Callable[[], None] | None,
    ):
        client = self.client
        retry_errors = ()
        max_retries = 0
        if should_stop is not None:
            try:
                from openai import APIConnectionError, APIStatusError, OpenAI
            except ImportError:
                pass
            else:
                if isinstance(client, OpenAI):
                    # The SDK cannot poll cancellation during its internal retries.
                    # This wrapper shares the borrowed transport; do not close it.
                    max_retries = client.max_retries
                    client = client.with_options(max_retries=0)
                    retry_errors = (APIConnectionError, APIStatusError)
        for attempt in range(max_retries + 1):
            _check_stop(should_stop)
            if before_transport is not None:
                before_transport()
            _check_stop(should_stop)
            try:
                return client.chat.completions.create(**request)
            except retry_errors as exc:
                _check_stop(should_stop)
                if attempt == max_retries:
                    raise
                delay = _transport_retry_delay(exc, attempt)
                if delay is None:
                    raise
                _wait_for_retry(delay, should_stop)

    def _repair_selection(
        self, raw: str, strict_mode: bool, *, should_stop: Callable[[], bool] | None = None,
    ) -> SemanticSelection:
        try:
            repaired = self._request_content(
                semantic_json_repair_prompt(raw, strict_mode),
                "你是 JSON 修复器。只能输出合法 JSON 对象，不得补充新事实。",
                self.profile.max_tokens,
                **({"should_stop": should_stop} if should_stop is not None else {}),
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
