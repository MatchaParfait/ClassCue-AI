"""Local Qwen adapter with strict JSON validation and deterministic fallback."""

from __future__ import annotations

from typing import Any

from core import CandidateDecision, SemanticCandidate, SemanticResponseError, SemanticSelection, TranscriptSegment, decision_prompt, json_repair_prompt, parse_model_json, parse_semantic_json, semantic_selection_prompt, validate_decision
from runtime import qwen_backend


class LocalJudge:
    def __init__(self, model_name: str = "Qwen/Qwen2.5-3B-Instruct") -> None:
        self.model_name = model_name
        self._pipeline: Any | None = None

    def _load(self) -> Any:
        if self._pipeline is None:
            try:
                import torch
                from transformers import BitsAndBytesConfig, pipeline
            except ImportError as exc:
                raise RuntimeError("未安装 transformers/torch，无法进行本地考点判断") from exc
            backend = qwen_backend()
            kwargs: dict[str, Any] = {"task": "text-generation", "model": self.model_name}
            if backend.is_gpu:
                kwargs["device_map"] = "auto"
                kwargs["model_kwargs"] = {
                    "dtype": torch.float16,
                    "quantization_config": BitsAndBytesConfig(load_in_4bit=True),
                }
            else:
                kwargs["device"] = -1
                kwargs["model_kwargs"] = {"dtype": torch.float32}
            self._pipeline = pipeline(**kwargs)
        return self._pipeline

    def _generate(self, prompt: str, max_new_tokens: int) -> str:
        pipe = self._load()
        messages = [{"role": "user", "content": prompt}]
        output = pipe(messages, max_new_tokens=max_new_tokens, do_sample=False, return_full_text=False)[0]["generated_text"]
        if isinstance(output, list):
            output = output[-1].get("content", "")
        return str(output)

    def judge(self, window: list[TranscriptSegment]) -> CandidateDecision:
        if not window:
            return CandidateDecision(reason="没有候选转录")
        prompt = decision_prompt(window)
        try:
            decision = parse_model_json(self._generate(prompt, max_new_tokens=160))
            return validate_decision(decision, window)
        except Exception as exc:
            # A transparent fallback keeps the demo usable when the local LLM is not cached.
            anchor = next((segment for segment in window if any(term in segment.text for term in ("会考", "必考", "重点", "考点"))), window[len(window) // 2])
            return CandidateDecision(
                is_exam_point=False,
                trigger_sentence_ids=[anchor.id],
                reason=f"本地模型暂不可用：{exc}",
                confidence=0.0,
            )

    def judge_semantic_chunk(self, chunk: tuple[TranscriptSegment, ...]) -> SemanticSelection:
        """Return every high-recall candidate in a transcript chunk or raise on model failure."""
        if not chunk:
            return SemanticSelection()
        raw_output = self._generate(semantic_selection_prompt(chunk), max_new_tokens=512)
        try:
            selection = parse_semantic_json(raw_output)
        except ValueError as first_error:
            try:
                selection = parse_semantic_json(self._generate(json_repair_prompt(raw_output), max_new_tokens=512))
            except ValueError as repair_error:
                raise SemanticResponseError(f"模型 JSON 无法校验，修复重试后仍失败：{repair_error}") from first_error
        valid_ids = [segment.id for segment in chunk]
        accepted: list[SemanticCandidate] = []
        for candidate in selection.candidates:
            if candidate.start_sentence_id not in valid_ids or candidate.end_sentence_id not in valid_ids:
                continue
            if valid_ids.index(candidate.start_sentence_id) > valid_ids.index(candidate.end_sentence_id):
                continue
            accepted.append(candidate)
        return SemanticSelection(candidates=accepted)
