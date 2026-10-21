from types import SimpleNamespace
import sys

import pytest

import ai_provider
import batch_process


@pytest.fixture
def cli_environment(tmp_path, monkeypatch):
    for name in ("AI_PROVIDER", "OPENAI_BASE_URL", "OPENAI_MODEL", "DEEPSEEK_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ai_provider, "SETTINGS_PATH", tmp_path / "ai.json")
    monkeypatch.setattr(ai_provider, "load_api_key", lambda _path: None)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-cli-key")
    monkeypatch.setenv("OPENAI_API_KEY", "relay-cli-key")
    monkeypatch.setattr(batch_process, "VideoStore", lambda _path: object())
    monkeypatch.setattr(batch_process, "discover_videos", lambda _folder: ["lesson.mp4"])
    calls = []

    def factory(profile, api_key, provider, **kwargs):
        calls.append((profile, api_key, provider, kwargs))
        return SimpleNamespace(close=lambda: calls.append("closed"))

    monkeypatch.setattr(batch_process, "create_judge", factory)
    monkeypatch.setattr(batch_process, "run_batch", lambda *args, **kwargs: SimpleNamespace(
        completed=1, skipped=0, failed=0, in_progress=0, paused=False,
    ))
    return tmp_path, calls


def run_cli(monkeypatch, tmp_path, *arguments):
    monkeypatch.setattr(sys, "argv", [
        "batch_process.py", "--videos", str(tmp_path),
        *arguments,
    ])
    batch_process.main()


def test_cli_uses_saved_relay_profile_and_provider_key(cli_environment, monkeypatch):
    tmp_path, calls = cli_environment
    settings = ai_provider.ProviderSettings(profile=ai_provider.ProviderProfile(
        base_url="https://relay.example/v1", model="saved-model",
    ))
    ai_provider.save_provider_settings(settings)
    run_cli(monkeypatch, tmp_path)
    profile, key, provider, kwargs = calls[0]
    assert (profile.model, key, provider) == ("saved-model", "relay-cli-key", "openai_compatible")
    assert kwargs["mode"] == "medium_recall"
    assert calls[-1] == "closed"


def test_cli_overrides_are_not_persisted(cli_environment, monkeypatch):
    tmp_path, calls = cli_environment
    run_cli(
        monkeypatch, tmp_path, "--base-url", "https://relay.example/api/v1",
        "--model", "explicit-model", "--api-key", "explicit-key", "--max-tokens", "4096",
        "--token-parameter", "max_completion_tokens", "--api-timeout", "30", "--no-json-mode",
    )
    profile, key, provider, _ = calls[0]
    assert (key, provider) == ("explicit-key", "openai_compatible")
    assert profile.model == "explicit-model"
    assert profile.max_tokens == 4096
    assert profile.token_parameter == "max_completion_tokens"
    assert profile.timeout_seconds == 30
    assert not profile.json_mode
    assert not ai_provider.SETTINGS_PATH.exists()


def test_cli_deepseek_default_still_works(cli_environment, monkeypatch):
    tmp_path, calls = cli_environment
    run_cli(monkeypatch, tmp_path)
    profile, key, provider, _ = calls[0]
    assert (key, provider) == ("deepseek-cli-key", "deepseek")
    assert profile.base_url == "https://api.deepseek.com"


def test_asr_only_commands_do_not_require_ai_configuration(cli_environment, monkeypatch):
    tmp_path, calls = cli_environment
    prepared = []
    monkeypatch.setenv("AI_PROVIDER", "invalid-provider")
    monkeypatch.setattr(batch_process, "prepare_asr_model", lambda model, _callback: prepared.append(model))
    monkeypatch.setattr(sys, "argv", [
        "batch_process.py", "--asr-model", "whisper-turbo", "--download-asr-model",
    ])
    batch_process.main()
    assert prepared == ["whisper-turbo"]
    assert calls == []
