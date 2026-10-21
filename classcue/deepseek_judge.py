"""Backward-compatible DeepSeek entry point for the shared API adapter."""

from __future__ import annotations

from typing import Any, Literal

from classcue.openai_judge import OpenAICompatibleJudge


class DeepSeekJudge(OpenAICompatibleJudge):
    def __init__(
        self,
        api_key: str,
        model: str = "deepseek-v4-flash",
        client: Any | None = None,
        mode: Literal["high_recall", "strict_exam_intent"] = "high_recall",
    ) -> None:
        super().__init__(
            api_key, model, client=client, mode=mode,
            base_url="https://api.deepseek.com", provider="deepseek",
        )
