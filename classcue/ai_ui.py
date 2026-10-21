"""Shared AI provider controls for management and review pages."""

from __future__ import annotations

import inspect
from pathlib import Path

import streamlit as st

from classcue.ai_provider import (
    ProviderProfile, ProviderSettings, create_judge, default_profiles, interface_key_path,
    interface_provider, load_provider_settings, resolve_interface_key, save_provider_settings,
)
from classcue.local_credentials import delete_api_key, save_api_key
from classcue.openai_judge import OpenAICompatibleJudge


def _forget_key(key_path: str) -> None:
    try:
        delete_api_key(Path(key_path))
        st.session_state["_ai_interface_key"] = ""
        st.session_state["ai_interface_key"] = ""
        st.session_state["_ai_key_message"] = "已删除本机记忆的 API Key。"
    except (OSError, RuntimeError) as exc:
        st.session_state["_ai_key_error"] = str(exc)


def _clear_models(prefix: str) -> None:
    st.session_state.pop(prefix + "models", None)
    st.session_state.pop(prefix + "model_choice", None)
    st.session_state.pop(prefix + "model_fetch_status", None)
    st.session_state.pop(prefix + "connection_status", None)


def _sync_interface_key(prefix: str) -> None:
    st.session_state["_ai_interface_key"] = st.session_state.get(prefix + "key", "").strip()
    _clear_models(prefix)


def _select_model(prefix: str, provider: str) -> None:
    value = st.session_state[prefix + "model_choice"]
    st.session_state[prefix + "model"] = value
    st.session_state["_ai_provider_drafts"][provider]["model"] = value


def _page_widget_namespace() -> str:
    for frame in inspect.stack(context=0):
        filename = Path(frame.filename).name
        if filename == "video_app.py":
            return "management"
        if filename == "review.py":
            return "review"
    return ""


def render_ai_settings() -> tuple[str, ProviderProfile, str]:
    st.subheader("AI 服务")
    try:
        saved = load_provider_settings()
    except ValueError as exc:
        st.error(str(exc))
        saved = ProviderSettings(profile=default_profiles(use_environment=False)["deepseek"])
    st.caption("统一使用 OpenAI 兼容 Chat Completions 接口；默认地址为 DeepSeek 官方 API，也可填写任意兼容中转站。")
    st.caption("有效的 Base URL、模型和接口参数会自动保存，下次打开时恢复上次使用的配置。")

    # One draft represents the currently selected OpenAI-compatible endpoint.
    drafts = st.session_state.setdefault("_ai_provider_drafts", {})
    provider = "interface"
    draft = drafts.get(provider, saved.active_profile.model_dump())
    widget_namespace = _page_widget_namespace()
    prefix = f"ai_interface_{widget_namespace}_" if widget_namespace else "ai_interface_"

    models = st.session_state.get(prefix + "models", [])
    model = st.text_input(
        "模型名称", value=draft["model"], key=prefix + "model",
        placeholder="例如 deepseek-v4-flash 或中转站提供的模型名",
        on_change=lambda: st.session_state.pop(prefix + "connection_status", None),
    )
    effort_options = ["low", "medium", "high"]
    reasoning_effort = st.selectbox(
        "思考强度", effort_options,
        index=effort_options.index(draft.get("reasoning_effort") or "high"),
        key=prefix + "reasoning_effort",
    )
    st.caption("GPT-6 Luna、DeepSeek 4.1 Flash 建议最低使用 Medium；默认 High。")
    if models:
        if st.session_state.get(prefix + "model_choice") not in models:
            st.session_state[prefix + "model_choice"] = model if model in models else models[0]
        st.selectbox(
            "已获取的模型", models, key=prefix + "model_choice",
            on_change=_select_model, args=(prefix, provider),
        )

    base_url = st.text_input(
        "Base URL", value=draft["base_url"], key=prefix + "base_url",
        placeholder="https://api.deepseek.com 或 https://relay.example.com/v1",
        on_change=_clear_models, args=(prefix,),
    )
    try:
        base_profile = ProviderProfile(base_url=base_url, credential_id=saved.active_profile.credential_id)
    except ValueError:
        base_profile = None
    if st.session_state.get("_ai_interface_key_url") != base_url.rstrip("/"):
        st.session_state["_ai_interface_key_url"] = base_url.rstrip("/")
        resolved_key = (
            resolve_interface_key(base_profile, saved) if base_profile else ""
        )
        st.session_state["_ai_interface_key"] = resolved_key
        st.session_state[prefix + "key"] = resolved_key
        draft["_api_key"] = resolved_key
    elif draft.get("_api_key") and not st.session_state.get(prefix + "key"):
        # Streamlit may clear a widget value while switching pages; restore the
        # endpoint-scoped key kept outside the widget state.
        st.session_state.pop(prefix + "key", None)
        st.session_state[prefix + "key"] = draft["_api_key"]
    api_key = st.text_input(
        "API Key", type="password", key=prefix + "key",
        on_change=_sync_interface_key, args=(prefix,),
    ).strip()
    st.session_state["_ai_interface_key"] = api_key
    draft["_api_key"] = api_key
    current_key_path = interface_key_path(base_profile) if base_profile else None
    remember_col, forget_col = st.columns(2)
    if remember_col.button(
        "记住密钥", icon=":material/key:", key="ai_remember",
        disabled=not api_key or current_key_path is None, use_container_width=True,
    ):
        try:
            save_api_key(current_key_path, api_key)
            current_key_path.with_suffix(".disabled").unlink(missing_ok=True)
            st.success("API Key 已加密保存在本机。")
        except (OSError, RuntimeError, ValueError) as exc:
            st.error(f"无法记住 API Key：{exc}")
    forget_col.button(
        "删除记忆", icon=":material/delete:", key="ai_forget",
        disabled=current_key_path is None or not current_key_path.is_file(),
        use_container_width=True,
        on_click=_forget_key, args=(str(current_key_path) if current_key_path else "",),
    )
    if message := st.session_state.pop("_ai_key_message", None):
        st.success(message)
    if error := st.session_state.pop("_ai_key_error", None):
        st.error(f"无法删除密钥记忆：{error}")
    fetch_col, test_col = st.columns(2)
    try:
        ProviderProfile(base_url=base_url)
    except ValueError:
        base_url_valid = False
    else:
        base_url_valid = True
    fetch_valid = bool(api_key and base_url and base_url_valid)
    test_valid = bool(fetch_valid and model.strip())
    fetch_clicked = fetch_col.button(
        "获取模型", icon=":material/list:", help="从当前 Base URL 获取远端模型名称",
        key="ai_fetch_models", disabled=not fetch_valid, use_container_width=True,
    )
    test_clicked = test_col.button(
        "测试 API", icon=":material/network_check:", help="发送一次小型文本请求验证当前模型",
        key="ai_test", disabled=not test_valid, use_container_width=True,
    )
    with st.expander("接口参数"):
        max_tokens = st.number_input(
            "输出上限（tokens）", min_value=256, max_value=131072,
            value=draft["max_tokens"], step=256, key=prefix + "max_tokens",
        )
        token_parameter = st.selectbox(
            "输出上限字段", ["max_tokens", "max_completion_tokens"],
            index=["max_tokens", "max_completion_tokens"].index(draft["token_parameter"]),
            key=prefix + "token_parameter",
        )
        timeout_seconds = st.number_input(
            "请求超时（秒）", min_value=5, max_value=600,
            value=int(draft["timeout_seconds"]), key=prefix + "timeout_seconds",
        )
        json_mode = st.checkbox(
            "请求 JSON 模式", value=draft["json_mode"], key=prefix + "json_mode",
        )
    values = {
        **draft, "base_url": base_url, "model": model,
        "max_tokens": max_tokens, "token_parameter": token_parameter,
        "timeout_seconds": timeout_seconds, "json_mode": json_mode,
        "reasoning_effort": reasoning_effort,
    }
    drafts[provider] = values
    try:
        profile = ProviderProfile.model_validate(values)
    except ValueError as exc:
        st.error(str(exc))
        profile = ProviderProfile()
        valid = False
    else:
        valid = True
        provider = interface_provider(profile)
        if profile != saved.active_profile:
            try:
                # Keep the last valid endpoint/model across application restarts.
                # Credentials remain outside this ordinary JSON configuration.
                saved = saved.with_active_profile(profile)
                save_provider_settings(saved)
            except (OSError, ValueError) as exc:
                st.warning(f"当前 AI 配置未能自动保存：{exc}")
    if fetch_clicked and valid:
        client = None
        try:
            client = OpenAICompatibleJudge(
                api_key, profile.model, base_url=profile.base_url,
                timeout_seconds=profile.timeout_seconds, max_retries=profile.max_retries,
                reasoning_effort=profile.reasoning_effort,
            )
            _clear_models(prefix)
            st.session_state[prefix + "models"] = client.list_remote_models()
            if st.session_state[prefix + "models"]:
                st.session_state[prefix + "model_fetch_status"] = (
                    "success", f"已获取 {len(st.session_state[prefix + 'models'])} 个远端模型。"
                )
                st.rerun()
            st.session_state[prefix + "model_fetch_status"] = (
                "warning", "接口请求成功，但没有返回可用模型名称。"
            )
        except Exception as exc:
            message = str(exc) or "远端模型列表请求失败。"
            st.session_state[prefix + "model_fetch_status"] = (
                "error", f"获取远端模型失败：{message}"
            )
        finally:
            if client is not None:
                client.close()
    if test_clicked and valid:
        client = None
        try:
            provider = interface_provider(profile)
            client = create_judge(profile, api_key, provider)
            reply = client.test_api_connection()
            st.session_state[prefix + "connection_status"] = (
                "success", f"API 验证通过，模型返回：{reply[:160]}"
            )
        except Exception as exc:
            message = str(exc) or "API 验证失败。"
            st.session_state[prefix + "connection_status"] = (
                "error", f"API 验证失败：{message}"
            )
        finally:
            if client is not None:
                client.close()
    for state_key in ("model_fetch_status", "connection_status"):
        status = st.session_state.get(prefix + state_key)
        if not status:
            continue
        level, message = status
        if level == "success":
            st.success(message)
        elif level == "warning":
            st.warning(message)
        else:
            st.error(message)
    save_col = st.columns(3)[2]
    if save_col.button(
        "", icon=":material/save:", help="立即保存服务配置（不保存密钥）",
        key="ai_save", disabled=not valid, use_container_width=True,
    ):
        try:
            saved = saved.with_active_profile(profile)
            save_provider_settings(saved)
            st.success("服务配置已保存。")
        except (OSError, ValueError) as exc:
            st.error(f"无法保存配置：{exc}")
    if not api_key:
        st.warning("尚未配置当前服务的 API Key。")
    return provider, profile, api_key
