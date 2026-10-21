"""Run classroom video processing without keeping a Streamlit page open.

Example:
    python -m classcue.batch_process --videos "D:\\course-videos"
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from classcue.asr_models import MODELS, get_model, selected_model_id
from classcue.ai_provider import (
    PROVIDER_LABELS, ProviderProfile, create_judge, load_provider_settings, resolve_api_key,
    interface_provider, resolve_interface_key,
)
from classcue.batch_processing import format_eta, run_batch
from classcue.core import DEFAULT_TRIGGER_GROUPS, validate_trigger_groups
from classcue.app_storage import TRIGGER_LIBRARY_PATH, active_storage_paths, storage_paths
from classcue.video_pipeline import discover_videos
from classcue.video_store import VideoStore
from classcue.transcriber import clear_asr_model, prepare_asr_model


def load_trigger_groups() -> dict:
    try:
        return validate_trigger_groups(json.loads(TRIGGER_LIBRARY_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError, json.JSONDecodeError):
        return DEFAULT_TRIGGER_GROUPS


def main() -> None:
    parser = argparse.ArgumentParser(description="在后台批量处理本机课堂视频")
    parser.add_argument("--videos", help="视频文件夹，会递归扫描 mp4/mov/mkv/avi")
    parser.add_argument("--provider", choices=list(PROVIDER_LABELS), help="兼容旧版供应商配置；默认使用统一接口配置")
    parser.add_argument("--api-key", help="当前 AI 服务的 API Key；也可使用环境变量或本机记忆")
    parser.add_argument("--base-url", help="OpenAI 兼容 API 根地址，通常以 /v1 结尾")
    parser.add_argument("--model", help="上游实际模型名；默认使用所选服务的配置")
    parser.add_argument("--max-tokens", type=int, help="单次 AI 输出上限")
    parser.add_argument("--token-parameter", choices=["max_tokens", "max_completion_tokens"])
    parser.add_argument("--api-timeout", type=float, help="API 请求超时秒数")
    parser.add_argument("--no-json-mode", action="store_false", dest="json_mode", default=None)
    parser.add_argument("--mode", choices=("high_recall", "medium_recall", "low_recall", "strict_exam_intent"), default="medium_recall")
    parser.add_argument("--reasoning-effort", choices=("low", "medium", "high"))
    parser.add_argument("--keyword-prefilter", action="store_true", help="只把关键词命中附近的字幕交给 AI")
    parser.add_argument(
        "--no-formula-noise-filter",
        action="store_false",
        dest="filter_formula_noise",
        default=True,
        help="关闭发送给 AI 前的公式/低信息字幕过滤",
    )
    parser.add_argument("--asr-model", choices=list(MODELS), default=selected_model_id())
    parser.add_argument("--qwen-mode", choices=["auto", "low-resource", "manual"], default="auto")
    parser.add_argument("--qwen-batch-size", type=int, default=2)
    parser.add_argument("--qwen-chunk-seconds", type=int, default=90)
    parser.add_argument("--qwen-align-batch-size", type=int, default=2)
    parser.add_argument("--qwen-load-profile", choices=["standard", "low-memory"], default="standard")
    parser.add_argument("--qwen-no-auto-recovery", action="store_true")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--download-asr-model", action="store_true", help="只准备所选 ASR 模型及运行环境")
    actions.add_argument("--clear-asr-model", action="store_true", help="只清除所选 ASR 模型缓存")
    parser.add_argument("--clear-qwen-runtime", action="store_true", help="清除模型时同时删除 Qwen 独立环境")
    args = parser.parse_args()
    from dataclasses import asdict
    from classcue.qwen_resilience import QwenOptions
    try:
        qwen_options = asdict(QwenOptions(
            mode=args.qwen_mode, batch_size=args.qwen_batch_size,
            chunk_seconds=args.qwen_chunk_seconds, align_batch_size=args.qwen_align_batch_size,
            load_profile=args.qwen_load_profile, auto_recovery=not args.qwen_no_auto_recovery,
        ))
    except ValueError as exc:
        parser.error(str(exc))
    if args.clear_qwen_runtime and (
        not args.clear_asr_model or get_model(args.asr_model).engine != "qwen"
    ):
        parser.error("--clear-qwen-runtime 仅可与 Qwen 模型的 --clear-asr-model 一起使用")
    if args.download_asr_model:
        prepare_asr_model(args.asr_model, lambda _value, message: print(message, flush=True))
        return
    if args.clear_asr_model:
        released = clear_asr_model(args.asr_model, remove_runtime=args.clear_qwen_runtime)
        print(f"已清除 {get_model(args.asr_model).label}，释放 {released} 字节。")
        return
    if not args.videos:
        parser.error("请指定 --videos，或使用 --download-asr-model / --clear-asr-model")

    try:
        settings = load_provider_settings()
        # The runtime now has one OpenAI-compatible profile. `--provider` remains
        # accepted only so older scripts can still select their legacy key source.
        source_profile = settings.active_profile
        overrides = {
            "base_url": args.base_url, "model": args.model, "max_tokens": args.max_tokens,
            "token_parameter": args.token_parameter, "timeout_seconds": args.api_timeout,
            "json_mode": args.json_mode,
            "reasoning_effort": args.reasoning_effort,
        }
        profile = ProviderProfile.model_validate({
            **source_profile.model_dump(),
            **{key: value for key, value in overrides.items() if value is not None},
        })
        provider = interface_provider(profile)
        api_key = (
            resolve_api_key(args.provider, args.api_key) if args.provider
            else args.api_key.strip() if args.api_key is not None
            else resolve_interface_key(profile, settings)
        )
        profile.require_ready(api_key)
    except ValueError as exc:
        parser.error(str(exc))

    data_root = active_storage_paths()[0]
    data_root.mkdir(parents=True, exist_ok=True)
    database_path, audio_dir = storage_paths(data_root)
    store = VideoStore(database_path)
    paths = discover_videos(args.videos)
    if not paths:
        raise SystemExit("视频文件夹中没有找到支持的视频。")
    judge = create_judge(profile, api_key, provider, mode=args.mode)
    groups = load_trigger_groups()

    def report(event) -> None:
        print(
            f"[{event.completed} 完成 / {event.skipped} 跳过 / {event.failed} 失败] "
            f"{event.message}；剩余 {event.estimate.remaining_videos} 个，预计 {format_eta(event.estimate.seconds)}",
            flush=True,
        )

    try:
        summary = run_batch(
            store,
            paths,
            audio_dir,
            judge,
            use_keyword_prefilter=args.keyword_prefilter,
            trigger_groups=groups,
            analysis_mode=args.mode,
            on_event=report,
            asr_model_id=args.asr_model,
            filter_formula_noise=args.filter_formula_noise,
            qwen_options=qwen_options,
        )
    finally:
        judge.close()
    print(
        f"批处理结束：完成 {summary.completed}，跳过 {summary.skipped}，"
        f"失败 {summary.failed}，处理中 {summary.in_progress}。"
    )
    if summary.paused:
        print("资源不足，剩余队列已暂停；释放资源后重新运行相同命令可续跑。", flush=True)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
