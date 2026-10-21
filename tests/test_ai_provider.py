import json

import pytest

import classcue.ai_provider as ai_provider
from classcue.ai_provider import (
    ProviderProfile, ProviderSettings, create_judge, load_provider_settings,
    provider_key_path, resolve_api_key, save_provider_settings,
)
from classcue.deepseek_judge import DeepSeekJudge


@pytest.fixture(autouse=True)
def isolate_provider_defaults(monkeypatch):
    for name in ("AI_PROVIDER", "OPENAI_BASE_URL", "OPENAI_MODEL", "DEEPSEEK_MODEL"):
        monkeypatch.delenv(name, raising=False)


def test_defaults_use_one_deepseek_compatible_profile_and_support_relay_environment(tmp_path, monkeypatch):
    settings = load_provider_settings(tmp_path / "missing.json")
    assert settings.selected_provider == "deepseek"
    assert settings.active_profile.base_url == "https://api.deepseek.com"
    assert settings.active_profile.model == "deepseek-v4-flash"
    assert settings.active_profile.max_tokens == 16384
    assert settings.active_profile.timeout_seconds == 120
    assert settings.active_profile.max_retries == 5
    assert settings.active_profile.reasoning_effort == "high"
    monkeypatch.setenv("OPENAI_BASE_URL", "https://relay.example/api/v1/")
    monkeypatch.setenv("OPENAI_MODEL", "relay-gpt-model")
    settings = load_provider_settings(tmp_path / "missing.json")
    assert settings.selected_provider == "openai_compatible"
    assert settings.active_profile.base_url == "https://relay.example/api/v1"
    assert settings.active_profile.model == "relay-gpt-model"


def test_unified_profile_roundtrip_without_credentials(tmp_path):
    settings = ProviderSettings(profile=ProviderProfile(
        base_url="https://relay.example/v1", model="relay-gpt",
        max_tokens=4096, token_parameter="max_completion_tokens",
    ))
    path = tmp_path / "settings.json"
    save_provider_settings(settings, path)
    loaded = load_provider_settings(path)
    assert loaded == settings
    assert loaded.active_profile.max_tokens == 4096
    assert loaded.selected_provider == "openai_compatible"
    assert "secret" not in path.read_text(encoding="utf-8")
    assert not list(tmp_path.glob("*.tmp"))


def test_saved_profiles_override_environment_and_complete_old_configs(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "env-model")
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "selected_provider": "openai_compatible",
        "profiles": {"openai_compatible": {"model": "saved-model"}},
    }), encoding="utf-8")
    settings = load_provider_settings(path)
    assert settings.active_profile.model == "saved-model"
    assert settings.active_profile.base_url == "https://api.deepseek.com"
    assert settings.active_profile.max_tokens == 16384
    assert settings.active_profile.timeout_seconds == 120
    assert settings.active_profile.max_retries == 5


def test_invalid_saved_config_reports_error_instead_of_resetting(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("not JSON", encoding="utf-8")
    with pytest.raises(ValueError, match="无法读取"):
        load_provider_settings(path)
    assert path.read_text(encoding="utf-8") == "not JSON"


@pytest.mark.parametrize("url", [
    "relay.example/v1", "ftp://relay.example/v1",
    "https://relay.example/v1/chat/completions",
    "https://relay.example/v1/responses",
    "https://relay.example/v1/models",
    "https://relay.example/v1?api_key=secret",
    "https://user:secret@relay.example/v1",
])
def test_profile_rejects_wrong_endpoint_or_embedded_credentials(url):
    with pytest.raises(ValueError):
        ProviderProfile(base_url=url)


def test_keys_are_isolated_and_legacy_deepseek_path_is_reused(tmp_path, monkeypatch):
    deepseek_path = tmp_path / "old-deepseek.dpapi"
    relay_path = tmp_path / "relay.dpapi"
    monkeypatch.setattr(ai_provider.app_storage, "DEEPSEEK_API_KEY_PATH", deepseek_path)
    monkeypatch.setattr(ai_provider, "RELAY_KEY_PATH", relay_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-deepseek")
    monkeypatch.setenv("OPENAI_API_KEY", "env-relay")
    remembered = {deepseek_path: "remembered-deepseek"}
    monkeypatch.setattr(ai_provider, "load_api_key", remembered.get)
    assert provider_key_path("deepseek") == deepseek_path
    assert resolve_api_key("deepseek") == "remembered-deepseek"
    assert resolve_api_key("openai_compatible") == "env-relay"
    assert resolve_api_key("openai_compatible", "explicit") == "explicit"
    assert resolve_api_key("openai_compatible", "") == ""
    assert resolve_api_key("deepseek") != resolve_api_key("openai_compatible")


def test_unified_interface_key_uses_endpoint_specific_memory_or_environment(tmp_path, monkeypatch):
    profile = ProviderProfile(base_url="https://relay.example/v1", model="relay-gpt")
    settings = ProviderSettings(profile=profile)
    monkeypatch.setenv("OPENAI_API_KEY", "relay-env")
    monkeypatch.setattr(ai_provider, "load_api_key", lambda _path: None)
    assert ai_provider.resolve_interface_key(profile, settings) == "relay-env"


def test_factory_and_direct_adapter_agree_without_reusing_legacy_cache():
    client = object()
    settings = ProviderSettings()
    judge = create_judge(settings.profiles["deepseek"], "key", "deepseek", client=client)
    direct = DeepSeekJudge("key", client=client, mode="strict_exam_intent")
    assert judge.profile.max_tokens == direct.profile.max_tokens == 16384
    assert judge.cache_identity == direct.cache_identity
    legacy = create_judge(
        settings.active_profile.model_copy(update={"max_tokens": 2048}),
        "key", "deepseek", client=client,
    )
    assert legacy.cache_identity != "deepseek:deepseek-v4-flash:strict_exam_intent"
    assert judge.cache_identity != legacy.cache_identity


@pytest.mark.parametrize("value", [None, ""])
def test_missing_effort_in_saved_profiles_defaults_to_high(tmp_path, value):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"profile": {
        "base_url": "https://relay.example/v1", "model": "relay-gpt",
        "reasoning_effort": value,
    }}), encoding="utf-8")
    settings = load_provider_settings(path)
    assert settings.active_profile.reasoning_effort == "high"
    save_provider_settings(settings, path)
    assert json.loads(path.read_text(encoding="utf-8"))["profile"]["reasoning_effort"] == "high"


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_factory_preserves_explicit_reasoning_effort(effort):
    profile = ProviderProfile(base_url="https://relay.example/v1", model="model", reasoning_effort=effort)
    judge = create_judge(profile, "key", mode="strict_exam_intent", client=object())
    assert judge.profile.reasoning_effort == effort


def test_factory_requires_model_address_and_key():
    for profile, key in [
        (ProviderProfile(base_url="https://relay.example/v1"), "key"),
        (ProviderProfile(model="model"), "key"),
        (ProviderProfile(base_url="https://relay.example/v1", model="model"), ""),
    ]:
        with pytest.raises(ValueError):
            create_judge(profile, key, "openai_compatible", client=object())
