from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest

import ai_provider
import ai_ui
import asr_models
import asr_ui
from core import TranscriptSegment
import app_storage
from video_store import VideoStore


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    for name in ("AI_PROVIDER", "OPENAI_BASE_URL", "OPENAI_MODEL", "DEEPSEEK_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ai_provider, "SETTINGS_PATH", tmp_path / "ai.json")
    monkeypatch.setattr(ai_provider, "load_api_key", lambda _path: None)
    monkeypatch.setattr(app_storage, "DEEPSEEK_API_KEY_PATH", tmp_path / "deepseek.dpapi")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "relay-test-key")
    return tmp_path


def settings_app():
    return AppTest.from_string(
        "from ai_ui import render_ai_settings\n"
        "provider, profile, key = render_ai_settings()\n"
    ).run()


def test_unified_settings_default_to_deepseek_and_have_one_model_editor(isolated_settings):
    app = settings_app()
    assert not app.exception
    assert app.text_input(key="ai_interface_model").value == "deepseek-v4-flash"
    assert app.text_input(key="ai_interface_base_url").value == "https://api.deepseek.com"
    assert app.selectbox(key="ai_interface_reasoning_effort").value == "high"
    assert app.selectbox(key="ai_interface_reasoning_effort").options == ["low", "medium", "high"]
    with pytest.raises(KeyError):
        app.selectbox(key="ai_provider")


def test_relay_profile_uses_same_controls_and_saves_without_credentials(isolated_settings):
    app = settings_app()
    app.text_input(key="ai_interface_model").set_value("relay-gpt").run()
    app.text_input(key="ai_interface_base_url").set_value("https://relay.example/v1").run()
    app.number_input(key="ai_interface_max_tokens").set_value(4096).run()
    assert app.text_input(key="ai_interface_key").value == "relay-test-key"
    app.button(key="ai_save").click().run()
    saved = ai_provider.load_provider_settings()
    assert saved.selected_provider == "openai_compatible"
    assert saved.active_profile.model == "relay-gpt"
    assert saved.active_profile.max_tokens == 4096
    assert "test-key" not in ai_provider.SETTINGS_PATH.read_text(encoding="utf-8")


def test_model_list_selection_and_connection_test_use_current_unsaved_profile(
    isolated_settings, monkeypatch
):
    calls = []

    class FakeJudge:
        def __init__(self, key, model, **kwargs):
            calls.append((key, model, kwargs))

        def list_remote_models(self):
            return ["relay-model-one", "relay-model-two"]

        def close(self):
            pass

    monkeypatch.setattr(ai_ui, "OpenAICompatibleJudge", FakeJudge)
    monkeypatch.setattr(
        ai_ui,
        "create_judge",
        lambda profile, key, provider: SimpleNamespace(
            test_api_connection=lambda: calls.append((profile.model, key, provider)) or "OK",
            close=lambda: None,
        ),
    )
    app = settings_app()
    app.text_input(key="ai_interface_base_url").set_value("https://relay.example/v1").run()
    assert not app.button(key="ai_fetch_models").disabled
    app.button(key="ai_fetch_models").click().run()
    app.selectbox(key="ai_interface_model_choice").select("relay-model-two").run()
    assert app.text_input(key="ai_interface_model").value == "relay-model-two"
    app.button(key="ai_test").click().run()
    assert not app.exception
    assert calls[-1] == ("relay-model-two", "relay-test-key", "openai_compatible")
    saved = ai_provider.load_provider_settings()
    assert saved.active_profile.base_url == "https://relay.example/v1"
    assert saved.active_profile.model == "relay-model-two"


def test_changing_api_key_clears_remote_models_and_connection_status(
    isolated_settings, monkeypatch
):
    class FakeJudge:
        def __init__(self, key, model, **kwargs):
            pass

        def list_remote_models(self):
            return ["relay-model"]

        def test_api_connection(self):
            return "OK"

        def close(self):
            pass

    monkeypatch.setattr(ai_ui, "OpenAICompatibleJudge", FakeJudge)
    monkeypatch.setattr(
        ai_ui,
        "create_judge",
        lambda profile, key, provider: FakeJudge(key, profile.model, provider=provider),
    )
    app = settings_app()
    app.text_input(key="ai_interface_base_url").set_value("https://relay.example/v1").run()
    app.text_input(key="ai_interface_model").set_value("relay-model").run()
    app.button(key="ai_fetch_models").click().run()
    assert app.selectbox(key="ai_interface_model_choice").options == ["relay-model"]
    app.button(key="ai_test").click().run()
    assert app.success
    app.text_input(key="ai_interface_key").set_value("new-relay-key").run()
    assert not app.exception
    with pytest.raises(KeyError):
        app.selectbox(key="ai_interface_model_choice")


def test_invalid_url_prevents_save_and_request(isolated_settings):
    app = settings_app()
    app.text_input(key="ai_interface_base_url").set_value(
        "https://relay.example/v1/chat/completions"
    ).run()
    assert not app.exception
    assert app.button(key="ai_save").disabled
    assert app.button(key="ai_fetch_models").disabled
    assert app.button(key="ai_test").disabled


def test_invalid_environment_url_shows_error_without_crashing(isolated_settings, monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://relay.example/v1/chat/completions")
    app = settings_app()
    assert not app.exception
    assert app.error
    app.text_input(key="ai_interface_base_url").set_value("https://relay.example/v1").run()
    app.text_input(key="ai_interface_model").set_value("relay-gpt").run()
    app.button(key="ai_save").click().run()
    assert not app.exception
    assert ai_provider.load_provider_settings().active_profile.model == "relay-gpt"


def test_valid_relay_profile_is_restored_without_clicking_save(isolated_settings):
    app = settings_app()
    app.text_input(key="ai_interface_base_url").set_value("https://relay.example/v1").run()
    app.text_input(key="ai_interface_model").set_value("relay-gpt").run()
    saved = ai_provider.load_provider_settings()
    assert saved.active_profile.base_url == "https://relay.example/v1"
    assert saved.active_profile.model == "relay-gpt"

    reopened = settings_app()
    assert reopened.text_input(key="ai_interface_base_url").value == "https://relay.example/v1"
    assert reopened.text_input(key="ai_interface_model").value == "relay-gpt"
    assert reopened.text_input(key="ai_interface_key").value == "relay-test-key"


def test_management_and_review_share_unsaved_unified_profile(isolated_settings, monkeypatch):
    tmp_path = isolated_settings
    workspace = tmp_path / "course"
    monkeypatch.setenv("CLASSROOM_DATA_ROOT", str(workspace))
    monkeypatch.setattr(app_storage, "TRIGGER_LIBRARY_PATH", tmp_path / "triggers.json")
    monkeypatch.setattr(asr_models, "MODEL_ROOT", tmp_path / "models")
    monkeypatch.setattr(asr_models, "SETTINGS_PATH", tmp_path / "asr.json")
    monkeypatch.setattr(asr_ui, "runtime_size", lambda: 0)
    store = VideoStore(workspace / "project.db")
    path = tmp_path / "video.mp4"
    path.write_bytes(b"local video fixture")
    video_id = store.create_video("hash", path.name, str(path), str(tmp_path / "audio.wav"), 1000)
    store.mark_transcribed(video_id, [TranscriptSegment(1, 0, 1000, "课堂字幕")])
    store.set_status(video_id, "review_ready")
    app = AppTest.from_file(str(app_storage.ROOT / "video_app.py")).run(timeout=30)
    app.text_input(key="ai_interface_management_base_url").set_value("https://relay.example/v1").run()
    app.text_input(key="ai_interface_management_model").set_value("relay-gpt").run()
    app.session_state.review_video_id = video_id
    app.switch_page("pages/review.py").run(timeout=30)
    assert not app.exception
    assert app.text_input(key="ai_interface_review_model").value == "relay-gpt"
    assert app.text_input(key="ai_interface_review_key").value == "relay-test-key"
    assert app.text_input(key="ai_interface_review_base_url").value == "https://relay.example/v1"
