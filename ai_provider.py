"""Provider profiles shared by the UI, CLI and subtitle screening."""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import tempfile
from typing import Literal
from urllib.parse import urlsplit
from filelock import FileLock

from pydantic import BaseModel, Field, field_validator, model_validator

from local_credentials import load_api_key
import app_storage


PROVIDER_LABELS = {"deepseek": "DeepSeek", "openai_compatible": "OpenAI 兼容接口"}
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_OUTPUT_TOKENS = 16384
DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_MAX_RETRIES = 5
DEFAULT_REASONING_EFFORT = "high"
SETTINGS_PATH = app_storage.DATA_DIR / "ai_providers.json"
RELAY_KEY_PATH = app_storage.DATA_DIR / "openai_compatible_api_key.dpapi"
ProviderId = Literal["deepseek", "openai_compatible"]


class ProviderProfile(BaseModel):
    credential_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    base_url: str = ""
    model: str = ""
    max_tokens: int = Field(default=DEFAULT_OUTPUT_TOKENS, ge=256, le=131072)
    token_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    json_mode: bool = True
    timeout_seconds: float = Field(default=DEFAULT_TIMEOUT_SECONDS, ge=5, le=600)
    max_retries: int = Field(default=DEFAULT_MAX_RETRIES, ge=0, le=5)
    reasoning_effort: Literal["low", "medium", "high"] = DEFAULT_REASONING_EFFORT

    @field_validator("reasoning_effort", mode="before")
    @classmethod
    def default_missing_reasoning_effort(cls, value: object) -> object:
        return DEFAULT_REASONING_EFFORT if value is None or value == "" else value

    @field_validator("base_url", "model")
    @classmethod
    def trim_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        if not value:
            return value
        value = value.rstrip("/")
        url = urlsplit(value)
        if url.scheme not in {"http", "https"} or not url.hostname:
            raise ValueError("Base URL 必须是完整的 http 或 https 地址")
        if url.username or url.password or url.query or url.fragment:
            raise ValueError("Base URL 不应包含账号、密钥、查询参数或锚点")
        if url.path.endswith(("/chat/completions", "/responses", "/models")):
            raise ValueError("请填写 API 根地址（通常以 /v1 结尾），不要填写具体请求端点")
        return value

    def require_ready(self, api_key: str) -> None:
        if not api_key.strip():
            raise ValueError("缺少 API Key，请在 AI 服务设置中输入或配置环境变量。")
        if not self.base_url:
            raise ValueError("请填写 AI 服务 Base URL。")
        if not self.model:
            raise ValueError("请填写 AI 模型名称。")


def default_profile(*, use_environment: bool = True) -> ProviderProfile:
    """Return one OpenAI-compatible profile, defaulting to DeepSeek's API."""
    if not use_environment:
        return ProviderProfile(base_url=DEFAULT_BASE_URL, model=DEFAULT_MODEL)
    relay_url = os.getenv("OPENAI_BASE_URL", "").strip()
    if relay_url:
        model = os.getenv("OPENAI_MODEL", "").strip() or os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL)
        return ProviderProfile(base_url=relay_url, model=model)
    return ProviderProfile(
        base_url=DEFAULT_BASE_URL,
        model=os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL),
    )


def default_profiles(*, use_environment: bool = True) -> dict[str, ProviderProfile]:
    """Backward-compatible view of the single active profile."""
    profile = default_profile(use_environment=use_environment)
    return {"deepseek": profile, "openai_compatible": profile}


class SavedConnection(BaseModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    name: str = Field(min_length=1, max_length=80)
    profile: ProviderProfile

    @field_validator("name")
    @classmethod
    def trim_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("连接名称不能为空")
        return value.strip()


class ProviderSettings(BaseModel):
    # Connections are authoritative; profile and provider aliases keep existing
    # callers and old configuration files compatible with the selected entry.
    profile: ProviderProfile = Field(default_factory=default_profile)
    connections: list[SavedConnection] = Field(default_factory=list)
    active_connection_id: str = "default"
    selected_provider: ProviderId = "deepseek"
    profiles: dict[str, ProviderProfile] = Field(
        default_factory=lambda: default_profiles(use_environment=False)
    )
    interface_profile: ProviderProfile | None = None
    legacy_openai_base_url: str | None = Field(default=None, repr=False)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_profiles(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        if "profile" not in data and not any(
            key in data for key in ("profiles", "interface_profile", "selected_provider")
        ):
            return data
        defaults = default_profile(use_environment=False).model_dump()
        legacy_profiles = data.get("profiles") or {}
        selected = data.get("selected_provider", "deepseek")
        candidate = data.get("profile")
        if candidate is None:
            candidate = data.get("interface_profile")
        if candidate is None and isinstance(legacy_profiles, dict):
            candidate = legacy_profiles.get(selected) or legacy_profiles.get("deepseek")
        if isinstance(candidate, ProviderProfile):
            candidate = candidate.model_dump()
        elif not isinstance(candidate, dict):
            candidate = {}
        data["profile"] = {**defaults, **candidate}
        if not data.get("legacy_openai_base_url") and isinstance(legacy_profiles, dict):
            old_relay = legacy_profiles.get("openai_compatible") or {}
            if isinstance(old_relay, dict):
                data["legacy_openai_base_url"] = old_relay.get("base_url")
        return data

    @model_validator(mode="after")
    def normalize_unified_profile(self) -> ProviderSettings:
        if not self.connections and "connections" not in self.model_fields_set:
            self.connections = [SavedConnection(id="default", name="当前连接", profile=self.profile)]
            self.active_connection_id = "default"
        if not self.connections:
            # An explicit empty collection must survive reload without restoring
            # a deleted profile or picking up environment credentials.
            self.active_connection_id = ""
            self.profile = ProviderProfile()
            self.selected_provider = "openai_compatible"
            self.interface_profile = self.profile
            self.profiles = {"deepseek": self.profile, "openai_compatible": self.profile}
            self.legacy_openai_base_url = None
            return self
        ids = [connection.id for connection in self.connections]
        if len(ids) != len(set(ids)) or self.active_connection_id not in ids:
            raise ValueError("已保存连接的 ID 或当前连接无效")
        self.profile = self.active_connection.profile
        self.selected_provider = interface_provider(self.profile)  # type: ignore[assignment]
        self.interface_profile = self.profile
        self.profiles = {"deepseek": self.profile, "openai_compatible": self.profile}
        return self

    @property
    def active_profile(self) -> ProviderProfile:
        return self.profile

    @property
    def active_connection(self) -> SavedConnection | None:
        return next((connection for connection in self.connections if connection.id == self.active_connection_id), None)

    def with_active_profile(self, profile: ProviderProfile) -> ProviderSettings:
        values = self.model_dump()
        if not self.connections:
            values["connections"] = [SavedConnection(id="default", name="当前连接", profile=profile).model_dump()]
            values["active_connection_id"] = "default"
            return ProviderSettings.model_validate(values)
        profile = profile.model_copy(update={"credential_id": self.active_profile.credential_id})
        for connection in values["connections"]:
            if connection["id"] == self.active_connection_id:
                connection["profile"] = profile.model_dump()
        return ProviderSettings.model_validate(values)


def provider_settings_lock() -> FileLock:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    return FileLock(str(SETTINGS_PATH) + ".lock", timeout=10)


def load_provider_settings(path: Path | None = None) -> ProviderSettings:
    target = path or SETTINGS_PATH
    if not target.is_file():
        return ProviderSettings()
    try:
        return ProviderSettings.model_validate_json(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"无法读取 AI 服务配置：{exc}") from exc


def save_provider_settings(settings: ProviderSettings, path: Path | None = None) -> None:
    target = path or SETTINGS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    # Keep the active profile for older readers. Secret values stay outside JSON.
    payload = {
        "profile": settings.active_profile.model_dump(),
        "connections": [connection.model_dump() for connection in settings.connections],
        "active_connection_id": settings.active_connection_id,
        "legacy_openai_base_url": settings.legacy_openai_base_url,
    }
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=target.parent, suffix=".tmp", delete=False
        ) as output:
            temporary = Path(output.name)
            json.dump(payload, output, ensure_ascii=False, indent=2)
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def provider_key_path(provider: str) -> Path:
    if provider == "deepseek":
        return app_storage.DEEPSEEK_API_KEY_PATH
    if provider == "openai_compatible":
        return RELAY_KEY_PATH
    raise ValueError(f"未知 AI 服务：{provider}")


def resolve_api_key(provider: str, explicit: str | None = None) -> str:
    if explicit is not None:
        return explicit.strip()
    variable = "DEEPSEEK_API_KEY" if provider == "deepseek" else "OPENAI_API_KEY"
    return (load_api_key(provider_key_path(provider)) or os.getenv(variable, "")).strip()


def interface_provider(profile: ProviderProfile) -> str:
    return "deepseek" if profile.base_url == DEFAULT_BASE_URL else "openai_compatible"


def interface_key_path(profile: ProviderProfile) -> Path:
    identity = f"{profile.credential_id}:{profile.base_url}" if profile.credential_id else profile.base_url
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return app_storage.DATA_DIR / "ai_keys" / f"{digest}.dpapi"


def resolve_interface_key(profile: ProviderProfile, settings: ProviderSettings) -> str:
    if not settings.connections:
        return ""
    if interface_key_path(profile).with_suffix(".disabled").exists():
        return ""
    remembered = load_api_key(interface_key_path(profile))
    if remembered is not None:
        return remembered.strip()
    if profile.credential_id:
        return ""
    # Legacy credentials are only reused for their recorded endpoint.
    if profile.base_url == "https://api.deepseek.com":
        return resolve_api_key("deepseek")
    if profile.base_url and profile.base_url == settings.legacy_openai_base_url:
        return resolve_api_key("openai_compatible")
    env_url = os.getenv("OPENAI_BASE_URL", "").strip().rstrip("/")
    if profile.base_url and (profile.base_url == env_url or not env_url):
        return os.getenv("OPENAI_API_KEY", "").strip()
    return ""


def create_judge(
    profile: ProviderProfile, api_key: str, provider: str | None = None,
    mode: str = "strict_exam_intent", *, client=None, scheme_snapshot=None,
):
    if mode in {"high_recall", "medium_recall", "low_recall"} or mode.startswith("custom_"):
        from exam_point_judge import ExamPointJudge

        profile.require_ready(api_key)
        return ExamPointJudge(profile, api_key, mode=mode, client=client, scheme_snapshot=scheme_snapshot)
    from openai_judge import OpenAICompatibleJudge

    profile.require_ready(api_key)
    return OpenAICompatibleJudge(
        api_key, profile.model, base_url=profile.base_url, provider=provider,
        mode=mode, max_tokens=profile.max_tokens, token_parameter=profile.token_parameter,
        json_mode=profile.json_mode, timeout_seconds=profile.timeout_seconds,
        max_retries=profile.max_retries, reasoning_effort=profile.reasoning_effort, client=client,
    )
