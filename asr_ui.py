"""Shared Streamlit controls for the explicitly managed local ASR cache."""

from __future__ import annotations

import streamlit as st

from asr_models import (
    MODELS, cache_size, format_size, get_model, model_is_downloaded,
    FIRERED_CPU_NOTICE, select_model, selected_model_id, selected_firered_device,
    selected_paraformer_device, selected_dolphin_device,
)
from qwen_runtime import runtime_is_ready, runtime_size
from firered_runtime import runtime_is_ready as firered_runtime_is_ready, runtime_size as firered_runtime_size
from paraformer_runtime import (
    runtime_is_ready as paraformer_runtime_is_ready, runtime_size as paraformer_runtime_size,
    cuda_status as paraformer_cuda_status,
)
from dolphin_runtime import runtime_is_ready as dolphin_runtime_is_ready, runtime_size as dolphin_runtime_size, cuda_status as dolphin_cuda_status
from transcriber import clear_asr_model, prepare_asr_model


def reset_clear_confirmation() -> None:
    st.session_state.confirm_clear_asr = False


def _refresh_model_status(model_id: str) -> dict[str, object]:
    """Run filesystem checks only after an explicit user action."""
    model = get_model(model_id)
    status = {
        "downloaded": model_is_downloaded(model_id),
        "cache_size": cache_size(model_id),
        "runtime_ready": True,
    }
    if model.engine == "qwen":
        status["runtime_ready"] = runtime_is_ready()
    elif model.engine == "firered":
        status["runtime_ready"] = firered_runtime_is_ready()
        if status["runtime_ready"] and selected_firered_device() == "gpu":
            from firered_resilience import resource_profile
            status["resource_profile"] = resource_profile()
    elif model.engine == "paraformer":
        status["runtime_ready"] = paraformer_runtime_is_ready(selected_paraformer_device())
    elif model.engine == "dolphin":
        status["runtime_ready"] = dolphin_runtime_is_ready(selected_dolphin_device())
    st.session_state.setdefault("_asr_model_status", {})[model_id] = status
    return status


def render_asr_settings() -> str:
    st.subheader("本地语音识别")
    if st.session_state.get("asr_model_id") not in MODELS:
        st.session_state.asr_model_id = selected_model_id()
    if st.session_state.get("clear_asr_target") not in MODELS:
        st.session_state.clear_asr_target = selected_model_id()
        reset_clear_confirmation()
    selected = st.selectbox(
        "ASR 模型", list(MODELS), key="asr_model_id",
        format_func=lambda model_id: get_model(model_id).label,
        help="选择不会下载；准备模型或首次转写时才下载所选模型。已有字幕不会自动改写。",
    )
    if selected != selected_model_id():
        try:
            if get_model(selected).engine == "paraformer":
                select_model(selected, paraformer_device=st.session_state.get(
                    "paraformer_device", selected_paraformer_device(),
                ))
            elif get_model(selected).engine == "dolphin":
                select_model(selected, dolphin_device=st.session_state.get("dolphin_device", selected_dolphin_device()))
            else:
                select_model(selected)
        except (OSError, ValueError) as exc:
            st.error(f"无法保存模型选择：{exc}")
    model = get_model(selected)
    if model.engine == "paraformer":
        gpu = paraformer_cuda_status()
        options = ["cpu", "gpu"] if gpu["gpu_available"] else ["cpu"]
        if st.session_state.get("paraformer_device") not in options:
            saved = selected_paraformer_device()
            st.session_state.paraformer_device = saved if saved in options else "cpu"
        device = st.radio(
            "Paraformer 处理设备", options, key="paraformer_device", horizontal=True,
            format_func=lambda value: value.upper(),
        )
        if not gpu["gpu_available"]:
            st.caption(f"GPU 不可选：{gpu.get('gpu_unavailable_reason') or 'CUDA 不可用或 GPU 环境未准备。'}")
            if st.button("准备 GPU 环境", key="prepare_paraformer_gpu",
                         disabled=not gpu["gpu_prepare_available"]):
                try:
                    prepare_asr_model(selected, paraformer_device="gpu")
                    st.rerun()
                except Exception as exc:
                    st.error(f"GPU 环境准备失败：{exc}")
        if device != selected_paraformer_device():
            try:
                select_model(selected, paraformer_device=device)
            except (OSError, ValueError) as exc:
                st.error(f"无法保存处理设备：{exc}")
    if model.engine == "dolphin":
        gpu = dolphin_cuda_status()
        options = ["cpu", "gpu"] if gpu["gpu_available"] else ["cpu"]
        if st.session_state.get("dolphin_device") not in options:
            saved = selected_dolphin_device()
            st.session_state.dolphin_device = saved if saved in options else "cpu"
        device = st.radio("Dolphin 处理设备", options, key="dolphin_device", horizontal=True,
                          format_func=lambda value: value.upper())
        if not gpu["gpu_available"]:
            st.caption(f"GPU 不可选：{gpu.get('gpu_unavailable_reason') or 'CUDA 不可用或 GPU 环境未准备。'}")
            if st.button("准备 GPU 环境", key="prepare_dolphin_gpu", disabled=not gpu["gpu_prepare_available"]):
                try:
                    prepare_asr_model(selected, dolphin_device="gpu")
                    st.rerun()
                except Exception as exc:
                    st.error(f"GPU 环境准备失败：{exc}")
        if device != selected_dolphin_device():
            try:
                select_model(selected, dolphin_device=device)
            except (OSError, ValueError) as exc:
                st.error(f"无法保存处理设备：{exc}")
    if model.engine == "firered":
        st.caption("FireRed 处理设备：GPU")
        st.warning(FIRERED_CPU_NOTICE)
        if selected_firered_device() == "cpu":
            if st.button("切换到 GPU 并保存", key="firered_gpu_save"):
                try:
                    select_model(selected, firered_device="gpu")
                    st.rerun()
                except (OSError, ValueError) as exc:
                    st.error(f"无法保存处理设备：{exc}")
    if model.engine == "qwen":
        st.warning("Qwen 要求 GPU 原生显存至少 6GB；低于 6GB 请勿使用此模型。共享显存不计入。")
        render_qwen_options()
    status = st.session_state.get("_asr_model_status", {}).get(selected)
    if status:
        downloaded = bool(status["downloaded"])
        runtime_ready = bool(status["runtime_ready"])
        ready_label = "已下载" if downloaded and runtime_ready else "未就绪"
        st.caption(f"{ready_label} · 模型缓存 {format_size(int(status['cache_size']))}")
        if model.engine == "qwen":
            st.caption(f"ForcedAligner 0.6B · 独立环境{'已安装' if runtime_ready else '未安装'}")
        elif model.engine == "firered":
            st.caption(f"FireRedVAD + AED 字词时间戳 · 独立环境{'已安装' if runtime_ready else '未安装'}")
            resource = status.get("resource_profile")
            if resource:
                capacity = resource.get("gpu_total_mib")
                capacity_label = f"{capacity / 1024:.1f} GB" if capacity is not None else "未知"
                st.caption(f"原生显存 {capacity_label} · 自动 BS{resource['initial_batch_size']}")
                if resource.get("warning"):
                    st.warning(resource["warning"])
        elif model.engine == "paraformer":
            st.caption(f"{selected_paraformer_device().upper()} · BiCIF 字词时间戳 · "
                       f"独立环境{'已安装' if runtime_ready else '未安装'}")
        elif model.engine == "dolphin":
            st.caption(f"{selected_dolphin_device().upper()} · CTC 字词时间戳 · "
                       f"独立环境{'已安装' if runtime_ready else '未安装'}")
    else:
        st.caption("模型状态将在准备模型或开始转写时检查；切换模型不会扫描磁盘。")
    if st.button(
        "检查 / 准备所选模型", icon=":material/download:", key="prepare_asr",
        use_container_width=True,
    ):
        progress = st.progress(0.0)
        status = st.empty()
        try:
            prepare_asr_model(
                selected, lambda value, message: (progress.progress(value), status.info(message))
            )
            _refresh_model_status(selected)
            st.success(f"{model.label} 已就绪")
            st.rerun()
        except Exception as exc:
            st.error(f"模型准备失败：{exc}")
    with st.expander("模型缓存"):
        st.caption("缓存大小不会随页面交互自动扫描。需要查看时再手动刷新。")
        if st.button("刷新缓存状态", icon=":material/refresh:", key="refresh_asr_cache"):
            for model_id in MODELS:
                _refresh_model_status(model_id)
            st.session_state["_asr_qwen_runtime_size"] = runtime_size()
            st.session_state["_asr_firered_runtime_size"] = firered_runtime_size()
            st.session_state["_asr_paraformer_runtime_size"] = paraformer_runtime_size()
            st.session_state["_asr_dolphin_runtime_size"] = dolphin_runtime_size()
            st.rerun()
        statuses = st.session_state.get("_asr_model_status", {})
        for model_id, definition in MODELS.items():
            model_status = statuses.get(model_id)
            if model_status:
                st.caption(f"{definition.label} · {format_size(int(model_status['cache_size']))}")
            else:
                st.caption(f"{definition.label} · 未检查")
        if "_asr_qwen_runtime_size" in st.session_state:
            st.caption(
                f"Qwen 独立运行环境 · "
                f"{format_size(int(st.session_state['_asr_qwen_runtime_size']))}"
            )
        if "_asr_firered_runtime_size" in st.session_state:
            st.caption(
                f"FireRedASR2 独立运行环境 · "
                f"{format_size(int(st.session_state['_asr_firered_runtime_size']))}"
            )
        if "_asr_paraformer_runtime_size" in st.session_state:
            st.caption(
                f"Paraformer 独立运行环境 · "
                f"{format_size(int(st.session_state['_asr_paraformer_runtime_size']))}"
            )
        if "_asr_dolphin_runtime_size" in st.session_state:
            st.caption(f"Dolphin 独立运行环境 · {format_size(int(st.session_state['_asr_dolphin_runtime_size']))}")
        clear_target = st.selectbox(
            "清除对象", list(MODELS), key="clear_asr_target",
            format_func=lambda model_id: get_model(model_id).label,
            on_change=reset_clear_confirmation,
        )
        remove_runtime = False
        if get_model(clear_target).engine in {"qwen", "firered", "paraformer", "dolphin"}:
            runtime_label = (
                "FireRedASR2" if get_model(clear_target).engine == "firered"
                else "Paraformer" if get_model(clear_target).engine == "paraformer"
                else "Dolphin" if get_model(clear_target).engine == "dolphin"
                else "Qwen"
            )
            remove_runtime = st.checkbox(
                f"同时清除 {runtime_label} 独立运行环境",
                key=f"remove_{get_model(clear_target).engine}_runtime",
            )
        confirmed = st.checkbox("确认清除所选缓存", key="confirm_clear_asr")
        if st.button(
            "清除", icon=":material/delete:", key="clear_asr",
            disabled=not confirmed,
            help="只删除所选模型缓存；视频、字幕、重点及复习状态保持不变。",
            use_container_width=True,
        ):
            try:
                released = clear_asr_model(clear_target, remove_runtime=remove_runtime)
                _refresh_model_status(clear_target)
                if remove_runtime:
                    st.session_state["_asr_qwen_runtime_size"] = runtime_size()
                    st.session_state["_asr_firered_runtime_size"] = firered_runtime_size()
                    st.session_state["_asr_paraformer_runtime_size"] = paraformer_runtime_size()
                    st.session_state["_asr_dolphin_runtime_size"] = dolphin_runtime_size()
                st.success(f"已清除 {format_size(released)}")
                st.rerun()
            except Exception as exc:
                st.error(f"无法清除：{exc}")
    return selected


def render_qwen_options():
    from dataclasses import asdict
    from qwen_resilience import QwenOptions

    mode = st.selectbox("Qwen 资源模式", ["auto", "low-resource", "manual"],
                        format_func=lambda value: {"auto": "自动", "low-resource": "节省资源",
                                                   "manual": "手动"}[value], key="qwen_resource_mode")
    settings = {"mode": mode}
    if mode == "manual":
        settings.update(
            batch_size=int(st.number_input("ASR BS", min_value=1, max_value=8, value=2)),
            chunk_seconds=int(st.number_input("目标分块秒数", min_value=30, max_value=1200, value=90)),
            align_batch_size=int(st.number_input("对齐 BS", min_value=1, max_value=8, value=2)),
            load_profile=st.selectbox("加载方式", ["standard", "low-memory"]),
            auto_recovery=st.checkbox("自动资源降级", value=False),
        )
    st.session_state["qwen_options"] = asdict(QwenOptions(**settings))


def render_retranscription(store, video, model_id: str) -> bool:
    from video_pipeline import retranscribe_video

    with st.expander("重新转写"):
        st.warning("成功后将替换旧字幕，并清除该视频的 AI 重点、已掌握和已移除状态。失败时保留原结果。")
        confirmed = st.checkbox(
            "确认替换字幕和重点", key=f"confirm_retranscribe_{video['id']}"
        )
        if st.button(
            f"使用 {get_model(model_id).label} 重新转写",
            icon=":material/refresh:", disabled=not confirmed,
            key=f"retranscribe_{video['id']}",
        ):
            progress = st.progress(0.0)
            status = st.empty()
            try:
                count = retranscribe_video(
                    store, int(video["id"]), model_id,
                    progress=lambda value, message: (progress.progress(value), status.info(message)),
                    qwen_options=st.session_state.get("qwen_options"),
                )
                st.session_state.pop("last_deleted_marker", None)
                st.session_state.pop("study_marker_id", None)
                st.success(f"已更新 {count} 段字幕。")
                return True
            except Exception as exc:
                st.error(str(exc))
    return False
