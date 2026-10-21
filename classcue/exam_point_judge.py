"""Production recall modes backed by the logged, evidence-checked extraction engine."""

from __future__ import annotations

from typing import Callable

from classcue.ai_provider import ProviderProfile
from classcue.llm_experiments import (
    LabConfig, execute_run, load_prompt_bundle, prepare_snapshot, register_run,
)
from classcue.processing_control import ProcessingCancelled
from classcue.video_store import VideoStore
from classcue.early_extraction import EarlyExtraction, authorize_snapshot
from classcue.analysis_events import emit_analysis


RECALL_VARIANTS = {
    "high_recall": "high_recall",
    "medium_recall": "precision",
    "low_recall": "low_recall",
}


class ExamPointJudge:
    def __init__(self, profile: ProviderProfile, api_key: str, *, mode: str, client=None, scheme_snapshot=None):
        if mode not in RECALL_VARIANTS and not mode.startswith("custom_"):
            raise ValueError("Unknown recall mode")
        self.profile = profile
        self.mode = mode
        self._api_key = api_key.strip()
        self.client = client
        self.scheme_revision = int(scheme_snapshot["revision"]) if scheme_snapshot else 0
        if mode.startswith("custom_"):
            from classcue.recall_schemes import custom_prompt_bundle
            self.bundle = custom_prompt_bundle(mode, scheme_snapshot)
        else:
            self.bundle = load_prompt_bundle(variant=RECALL_VARIANTS[mode])
        self._early_extraction = None

    def close(self) -> None:
        # The extraction engine owns its per-run client; supplied clients remain caller-owned.
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def _probe(self, method: str):
        from classcue.openai_judge import OpenAICompatibleJudge

        with OpenAICompatibleJudge(
            self._api_key, self.profile.model, base_url=self.profile.base_url,
            max_tokens=self.profile.max_tokens, token_parameter=self.profile.token_parameter,
            json_mode=self.profile.json_mode, timeout_seconds=self.profile.timeout_seconds,
            max_retries=self.profile.max_retries, client=self.client,
            reasoning_effort=self.profile.reasoning_effort,
        ) as judge:
            return getattr(judge, method)()

    def test_api_connection(self):
        return self._probe("test_api_connection")

    def test_connection(self):
        return self._probe("test_connection")

    def list_remote_models(self):
        return self._probe("list_remote_models")

    list_models = list_remote_models

    def _config(self, *, use_keyword_prefilter=False, trigger_groups=None,
                filter_formula_noise=True, filter_formula=None):
        keywords = [
            word for group in (trigger_groups or {}).values()
            for words in group.values() for word in words
        ] if use_keyword_prefilter else []
        return LabConfig(
            scheme="few_shot", chunk_seconds=600, overlap_seconds=30,
            max_tokens=self.profile.max_tokens, reasoning_effort=self.profile.reasoning_effort,
            request_retries=self.profile.max_retries,
            filter_formula_noise=filter_formula_noise, filter_formula=filter_formula,
            use_keyword_prefilter=use_keyword_prefilter, keywords=keywords,
        )

    def start_early_analysis(self, store, video_id, *, should_stop=None, **options):
        stream = EarlyExtraction(
            self, store, video_id, self._config(**options), should_stop=should_stop,
        )
        self._early_extraction = stream
        return stream

    def analyze_video(
        self, store: VideoStore, video_id: int, *, progress=None,
        use_keyword_prefilter=False, trigger_groups=None, filter_formula_noise=True,
        filter_formula=None, force=False, should_stop: Callable[[], bool] | None = None,
    ) -> tuple[str, int]:
        config = self._config(
            use_keyword_prefilter=use_keyword_prefilter, trigger_groups=trigger_groups,
            filter_formula_noise=filter_formula_noise, filter_formula=filter_formula,
        )
        early = self._early_extraction
        if early is not None:
            early.finish()
        snapshot = prepare_snapshot(
            store, video_id, config, self.profile, bundle=self.bundle, allow_empty=True,
        )
        # Production authorization is the user's processing request, not a lab preview review.
        authorize_snapshot(snapshot, self.mode)
        signature = snapshot["snapshot_hash"]
        previous = store.get_analysis_run(video_id, self.mode)
        if (
            not force and previous and previous["status"] == "completed"
            and previous["analysis_signature"] == signature
        ):
            if previous["scheme_revision"] != self.scheme_revision:
                store.confirm_analysis_revision(video_id, self.mode, signature, self.scheme_revision)
            emit_analysis(video_id, self.mode, completed=len(snapshot["requests"]),
                          total=len(snapshot["requests"]), current=None, results=[
                              {**dict(marker), "reason": marker["note"]}
                              for marker in store.list_markers(video_id, self.mode)
                          ])
            if progress:
                progress(1.0, "已复用相同提示词、配置和字幕的结果")
            return "skipped", int(previous["candidate_count"])
        if should_stop and should_stop():
            raise ProcessingCancelled("用户已请求停止处理")
        run_id = (early.bind(snapshot) if early is not None else None) or register_run(
            store, f"production:{self.mode}", snapshot, signature,
        )
        try:
            report = execute_run(
                store, run_id, snapshot, self._api_key, signature, client=self.client,
                progress=(lambda value, _message: progress(
                    value, f"AI 筛选 {round(value * len(snapshot['requests']))}/{len(snapshot['requests'])}（并发 1）",
                )) if progress else None,
                should_cancel=should_stop,
                early_extraction=early,
                chunk_progress=lambda **state: emit_analysis(video_id, self.mode, **state),
            )
            if report["status"] != "completed":
                raise ValueError(
                    f"考点证据仍有未修复错误，旧结果已保留；调用记录：{run_id}"
                )
            if should_stop and should_stop():
                raise ProcessingCancelled("用户已请求停止处理")
            if snapshot["requests"]:
                store.record_ai_metrics(
                    video_id, report["latency_seconds"], len(snapshot["requests"]),
                )
            if should_stop and should_stop():
                raise ProcessingCancelled("用户已请求停止处理")
            store.publish_exam_results(video_id, self.mode, report["results"], signature, run_id,
                                       scheme_revision=self.scheme_revision)
            return "completed", report["candidate_count"]
        except Exception:
            store.set_analysis_run(video_id, self.mode, "failed")
            raise
