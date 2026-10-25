from contextlib import contextmanager
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest

import classcue.ai_provider as ai_provider
import classcue.ai_ui as ai_ui
import classcue.asr_models as asr_models
import classcue.asr_ui as asr_ui
from classcue.ai_provider import ProviderProfile, ProviderSettings, SavedConnection
from classcue.core import TranscriptSegment
import classcue.app_storage as app_storage
from classcue.video_store import VideoStore


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
        "from classcue.ai_ui import render_ai_settings\n"
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
    app = AppTest.from_file(str(app_storage.ROOT / "classcue" / "video_app.py")).run(timeout=30)
    app.text_input(key="ai_interface_management_base_url").set_value("https://relay.example/v1").run()
    app.text_input(key="ai_interface_management_model").set_value("relay-gpt").run()
    app.session_state.review_video_id = video_id
    app.switch_page("pages/review.py").run(timeout=30)
    assert not app.exception
    assert app.text_input(key="ai_interface_review_model").value == "relay-gpt"
    assert app.text_input(key="ai_interface_review_key").value == "relay-test-key"
    assert app.text_input(key="ai_interface_review_base_url").value == "https://relay.example/v1"


def _start_locked_writer(monkeypatch, mutate, order=None):
    """Compete for the real configuration lock; mutate(order) runs while the UI waits."""
    if order is None:
        order = []
    waits: list[bool] = []
    held = Event()
    ui_requested = Event()
    real_lock = ai_provider.provider_settings_lock

    @contextmanager
    def observed_lock():
        order.append("ui-requested")
        ui_requested.set()
        with real_lock():
            order.append("ui-acquired")
            yield

    monkeypatch.setattr(ai_ui, "provider_settings_lock", observed_lock, raising=False)

    def writer():
        with real_lock():
            order.append("writer-acquired")
            held.set()
            waits.append(ui_requested.wait(5))
            mutate(order)
            order.append("writer-mutated")

    thread = Thread(target=writer, daemon=True)
    thread.start()
    assert held.wait(5), "competing writer never acquired the configuration lock"
    return thread, order, waits


def test_auto_save_waits_for_shared_lock_and_keeps_concurrent_connection_switch(
    isolated_settings, monkeypatch,
):
    ai_provider.save_provider_settings(ProviderSettings(
        active_connection_id="default",
        connections=[SavedConnection(
            id="default", name="当前连接",
            profile=ProviderProfile(base_url="https://relay.test/v1", model="old-model"),
        )],
    ))

    def switch_active_connection(order):
        values = ai_provider.load_provider_settings().model_dump()
        values["connections"].append(SavedConnection(
            id="second", name="备用",
            profile=ProviderProfile(base_url="https://second.test/v1", model="second-model"),
        ).model_dump())
        values["active_connection_id"] = "second"
        ai_provider.save_provider_settings(ProviderSettings.model_validate(values))

    app = settings_app()
    thread, order, waits = _start_locked_writer(monkeypatch, switch_active_connection)
    try:
        app.text_input(key="ai_interface_model").set_value("relay-gpt")
        app.run(timeout=30)
    finally:
        thread.join(10)
    assert not thread.is_alive()
    assert waits == [True], "自动保存没有经过共享配置锁"
    assert "ui-acquired" in order and "writer-mutated" in order
    assert order.index("writer-acquired") < order.index("ui-requested")
    assert order.index("ui-requested") < order.index("writer-mutated") < order.index("ui-acquired")
    saved = ai_provider.load_provider_settings()
    assert [connection.id for connection in saved.connections] == ["default", "second"]
    assert saved.active_connection_id == "second"
    assert saved.active_profile.model == "second-model"
    edited = saved.connections[0].profile
    assert edited.model == "relay-gpt" and edited.base_url == "https://relay.test/v1"
    assert not app.exception


def test_manual_save_reports_when_rendered_connection_disappears(isolated_settings, monkeypatch):
    ai_provider.save_provider_settings(ProviderSettings(
        active_connection_id="default",
        connections=[
            SavedConnection(id="default", name="当前连接",
                            profile=ProviderProfile(base_url="https://relay.test/v1", model="old-model")),
            SavedConnection(id="second", name="备用",
                            profile=ProviderProfile(base_url="https://second.test/v1", model="second-model")),
        ],
    ))

    def delete_rendered_connection(order):
        values = ai_provider.load_provider_settings().model_dump()
        values["connections"] = [item for item in values["connections"] if item["id"] != "default"]
        values["active_connection_id"] = "second"
        ai_provider.save_provider_settings(ProviderSettings.model_validate(values))

    app = settings_app()
    thread, order, waits = _start_locked_writer(monkeypatch, delete_rendered_connection)
    try:
        app.button(key="ai_save").click()
        app.run(timeout=30)
    finally:
        thread.join(10)
    assert not thread.is_alive()
    assert waits == [True], "手动保存没有经过共享配置锁"
    assert order.index("ui-requested") < order.index("writer-mutated") < order.index("ui-acquired")
    assert any("已不存在" in error.value for error in app.error)
    saved = ai_provider.load_provider_settings()
    assert [connection.id for connection in saved.connections] == ["second"]
    assert saved.active_connection_id == "second"
    assert saved.active_profile.model == "second-model"
    assert not app.exception


def test_auto_save_from_empty_collection_still_creates_default_connection(isolated_settings):
    ai_provider.save_provider_settings(ProviderSettings(connections=[]))
    app = settings_app()
    app.text_input(key="ai_interface_base_url").set_value("https://relay.example/v1").run()
    app.text_input(key="ai_interface_model").set_value("relay-gpt").run()
    saved = ai_provider.load_provider_settings()
    assert [connection.id for connection in saved.connections] == ["default"]
    assert saved.active_connection_id == "default"
    assert saved.active_profile.model == "relay-gpt"
    assert not app.exception


def test_forget_key_waits_for_shared_lock_and_survives_locked_rollback(isolated_settings, monkeypatch):
    monkeypatch.setattr(app_storage, "DATA_DIR", isolated_settings)
    key_path = ai_provider.interface_key_path(ProviderProfile(base_url="https://api.deepseek.com"))
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(b"synthetic-original-secret")
    order: list[str] = []

    def fake_delete(path):
        Path(path).unlink()
        order.append("ui-delete")

    monkeypatch.setattr(ai_ui, "delete_api_key", fake_delete)

    def rollback_new_key(order):
        previous = key_path.read_bytes()
        key_path.write_bytes(b"synthetic-replaced-secret")
        # Same locked sequence as the API: write the key, then restore the
        # previous bytes when the configuration commit fails.
        key_path.write_bytes(previous)
        order.append("writer-restored")

    app = settings_app()
    thread, order, waits = _start_locked_writer(monkeypatch, rollback_new_key, order)
    try:
        app.button(key="ai_forget").click()
        app.run(timeout=30)
    finally:
        thread.join(10)
    assert not thread.is_alive()
    assert waits == [True], "删除密钥记忆没有经过共享配置锁"
    assert order.index("ui-requested") < order.index("writer-restored") < order.index("ui-acquired")
    assert order.index("ui-delete") > order.index("ui-acquired")
    assert not key_path.exists()
    assert not key_path.with_suffix(".disabled").exists()
    assert any("已删除" in message.value for message in app.success)
    assert not app.exception


def test_remember_key_writes_inside_shared_lock_and_clears_disabled_marker(
    isolated_settings, monkeypatch,
):
    monkeypatch.setattr(app_storage, "DATA_DIR", isolated_settings)
    key_path = ai_provider.interface_key_path(ProviderProfile(base_url="https://api.deepseek.com"))
    key_path.parent.mkdir(parents=True, exist_ok=True)
    order: list[str] = []

    def fake_save(path, value):
        Path(path).write_bytes(value.encode())
        order.append("ui-save")

    monkeypatch.setattr(ai_ui, "save_api_key", fake_save)

    def set_disabled_marker(order):
        key_path.with_suffix(".disabled").touch()
        order.append("writer-marked")

    app = settings_app()
    thread, order, waits = _start_locked_writer(monkeypatch, set_disabled_marker, order)
    try:
        app.button(key="ai_remember").click()
        app.run(timeout=30)
    finally:
        thread.join(10)
    assert not thread.is_alive()
    assert waits == [True], "记住密钥没有经过共享配置锁"
    assert order.index("ui-requested") < order.index("writer-marked") < order.index("ui-acquired")
    assert order.index("ui-save") > order.index("ui-acquired")
    assert key_path.read_bytes() == b"deepseek-test-key"
    assert not key_path.with_suffix(".disabled").exists()
    assert any("已加密保存" in message.value for message in app.success)
    assert not app.exception
