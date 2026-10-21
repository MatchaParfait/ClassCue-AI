"""Connection CRUD, migration and credential isolation without external requests."""
import json
import hashlib
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import classcue.ai_provider as ai_provider
import api.main as api_main
from classcue.ai_provider import ProviderProfile, ProviderSettings, load_provider_settings
from classcue.video_store import VideoStore


@pytest.fixture
def connections(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_provider, "SETTINGS_PATH", tmp_path / "ai.json")
    monkeypatch.setattr(ai_provider.app_storage, "DATA_DIR", tmp_path)
    for variable in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"):
        monkeypatch.delenv(variable, raising=False)
    remembered = {}

    def save_key(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        encrypted = f"encrypted:{hashlib.sha256(value.encode()).hexdigest()}".encode()
        path.write_bytes(encrypted)
        remembered[encrypted] = value

    monkeypatch.setattr(api_main, "save_api_key", save_key)
    monkeypatch.setattr(ai_provider, "load_api_key", lambda path: remembered.get(path.read_bytes()) if path.is_file() else None)
    # Reproduce the file format before connection support, including its endpoint key.
    profile = ProviderProfile(base_url="https://relay.test/v1", model="old-model", max_tokens=4096)
    ai_provider.SETTINGS_PATH.write_text(json.dumps({"profile": profile.model_dump()}), encoding="utf-8")
    save_key(ai_provider.interface_key_path(profile), "original-secret")
    with TestClient(api_main.create_app(VideoStore(tmp_path / "workspace" / "project.db"))) as client:
        yield client


def test_migration_and_crud_preserve_original_and_roundtrip(connections):
    client = connections
    original = client.get("/api/settings/ai").json()
    assert original["connection_id"] == "default" and original["has_api_key"]
    assert original["connections"] == [{
        "id": "default", "name": "当前连接", "base_url": "https://relay.test/v1",
        "model": "old-model", "has_api_key": True,
    }]
    created = client.post("/api/settings/ai/connections", json={"name": "日常分析"}).json()
    new_id = created["connection_id"]
    assert new_id != "default" and len(created["connections"]) == 2
    assert created["output_tokens"] == 16384
    assert created["timeout_seconds"] == 120 and created["max_retries"] == 5
    assert created["reasoning_effort"] == "high"
    assert not created["has_api_key"]
    response = client.put("/api/settings/ai", json={
        **created, "base_url": "https://second.test/v1", "model": "second-model", "output_tokens": 8192,
        "reasoning_effort": "high",
    })
    assert response.status_code == 200, response.text
    assert client.patch(f"/api/settings/ai/connections/{new_id}", json={"name": "高精度"}).json()["connection_name"] == "高精度"
    restored = client.post("/api/settings/ai/connections/default/activate").json()
    assert restored["model"] == "old-model" and restored["output_tokens"] == 4096 and restored["has_api_key"]
    selected = client.post(f"/api/settings/ai/connections/{new_id}/activate").json()
    assert selected["model"] == "second-model" and selected["reasoning_effort"] == "high"
    assert load_provider_settings().active_connection.name == "高精度"
    assert load_provider_settings().active_profile.max_tokens == 8192
    deleted = client.delete(f"/api/settings/ai/connections/{new_id}").json()
    assert deleted["connection_id"] == "default" and len(deleted["connections"]) == 1
    assert client.post("/api/settings/ai/connections/not-found/activate").status_code == 404
    assert client.patch("/api/settings/ai/connections/default", json={"name": "   "}).status_code == 400
    text = ai_provider.SETTINGS_PATH.read_text(encoding="utf-8")
    assert "original-secret" not in text and "api_key" not in text


def test_delete_last_connection_stays_empty_after_reload_and_can_create_again(connections, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-restore")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-restore")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://relay.test/v1")
    client = connections
    deleted = client.delete("/api/settings/ai/connections/default")
    assert deleted.status_code == 200, deleted.text
    for result in (deleted.json(), client.get("/api/settings/ai").json()):
        assert result["connections"] == []
        assert result["connection_id"] == result["connection_name"] == ""
        assert result["base_url"] == result["model"] == ""
        assert not result["has_api_key"] and not result["configured"]
        assert result["output_tokens"] == 16384
    persisted = load_provider_settings()
    assert persisted.connections == [] and persisted.active_connection is None
    assert persisted.active_profile.base_url == ""
    assert client.post("/api/settings/ai/key/reveal").status_code == 404
    assert client.post("/api/settings/ai/test").status_code == 400
    assert client.post("/api/settings/ai/connections/default/activate").status_code == 404
    assert client.put("/api/settings/ai", json={
        "connection_id": "default", "base_url": "https://relay.test/v1", "model": "stale",
    }).status_code == 409
    created = client.post("/api/settings/ai/connections", json={
        "name": "重新添加",
        "settings": {"base_url": "https://relay.test/v1", "model": "new-model"},
        "api_key": "replacement-key",
    })
    assert created.status_code == 201, created.text
    assert len(created.json()["connections"]) == 1 and created.json()["has_api_key"]
    assert created.json()["connection_id"] != "default"
    assert client.post("/api/settings/ai/key/reveal").json()["api_key"] == "replacement-key"


def test_failed_last_connection_delete_preserves_original(connections, monkeypatch):
    def fail(_settings):
        raise OSError("disk full")
    monkeypatch.setattr(api_main, "save_provider_settings", fail)
    assert connections.delete("/api/settings/ai/connections/default").status_code == 400
    restored = connections.get("/api/settings/ai").json()
    assert restored["connection_id"] == "default" and restored["has_api_key"]


@pytest.mark.parametrize("effort", [None, "", "low", "medium", "high"])
def test_save_normalizes_unset_effort_and_preserves_explicit_choices(connections, effort):
    current = connections.get("/api/settings/ai").json()
    response = connections.put("/api/settings/ai", json={**current, "reasoning_effort": effort})
    assert response.status_code == 200, response.text
    expected = effort or "high"
    assert response.json()["reasoning_effort"] == expected
    assert load_provider_settings().active_profile.reasoning_effort == expected


def test_duplicate_copies_key_but_same_endpoint_accounts_remain_independent(connections):
    client = connections
    copied = client.post("/api/settings/ai/connections", json={"name": "复制", "source_id": "default"}).json()
    copy_id = copied["connection_id"]
    assert copied["has_api_key"] and copied["model"] == "old-model"
    assert client.post("/api/settings/ai/key/reveal", json={"connection_id": copy_id}).json()["api_key"] == "original-secret"
    client.post("/api/settings/ai/key", json={"connection_id": copy_id, "api_key": "copy-secret"})
    assert client.post("/api/settings/ai/key/reveal", json={"connection_id": "default"}).json()["api_key"] == "original-secret"
    other = client.post("/api/settings/ai/connections", json={"name": "同址新账号"}).json()
    other_id = other["connection_id"]
    client.put("/api/settings/ai", json={**other, "base_url": copied["base_url"], "model": "other-model"})
    assert client.get("/api/settings/ai").json()["has_api_key"] is False
    assert client.post("/api/settings/ai/key/reveal", json={"connection_id": other_id}).status_code == 404
    response = client.request("DELETE", "/api/settings/ai/key", json={"connection_id": copy_id})
    assert response.status_code == 200
    assert client.post("/api/settings/ai/key/reveal", json={"connection_id": copy_id}).status_code == 404
    assert client.post("/api/settings/ai/key/reveal", json={"connection_id": "default"}).status_code == 200
    client.post("/api/settings/ai/key", json={"connection_id": copy_id, "api_key": "replaced-secret"})
    assert client.post("/api/settings/ai/key/reveal", json={"connection_id": copy_id}).json()["api_key"] == "replaced-secret"
    # Altering the endpoint must never send a saved connection's key elsewhere.
    assert client.post("/api/settings/ai/key/reveal", json={
        "connection_id": copy_id, "base_url": "https://unrelated.test/v1",
    }).status_code == 404
    listed = client.get("/api/settings/ai").text
    assert all(secret not in listed for secret in ("original-secret", "copy-secret", "replaced-secret"))


def test_stale_save_is_rejected_and_other_connections_survive_legacy_save(connections):
    client = connections
    original = client.get("/api/settings/ai").json()
    client.post("/api/settings/ai/connections", json={"name": "备用", "source_id": "default"})
    assert client.put("/api/settings/ai", json={**original, "model": "stale"}).status_code == 409
    response = client.put("/api/settings/ai", json={
        "base_url": "https://relay.test/v1", "model": "legacy-client-update",
    })
    assert response.status_code == 200
    assert len(response.json()["connections"]) == 2
    assert client.post("/api/settings/ai/connections/default/activate").json()["model"] == "old-model"


def test_corrupt_collection_does_not_reset_or_overwrite_file(connections):
    path = ai_provider.SETTINGS_PATH
    path.write_text('{"connections": [{"id": "broken"}]}', encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    assert connections.get("/api/settings/ai").status_code == 500
    assert connections.post("/api/settings/ai/connections", json={"name": "test"}).status_code == 400
    assert path.read_text(encoding="utf-8") == before


def test_named_profiles_keep_connection_keys_out_of_legacy_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "environment-secret")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-secret")
    monkeypatch.setattr(ai_provider, "load_api_key", lambda _path: None)
    monkeypatch.setattr(ai_provider.app_storage, "DATA_DIR", tmp_path)
    for url in ("https://api.deepseek.com", "https://relay.test/v1"):
        profile = ProviderProfile(base_url=url, credential_id="independent")
        assert ai_provider.resolve_interface_key(profile, ProviderSettings(profile=profile)) == ""


def test_unified_save_updates_name_parameters_and_key_together(connections):
    original = connections.get("/api/settings/ai").json()
    response = connections.put("/api/settings/ai", json={
        **original, "connection_name": "统一保存", "model": "next-model", "api_key": "next-secret",
    })
    assert response.status_code == 200, response.text
    assert response.json()["connection_name"] == "统一保存"
    assert response.json()["model"] == "next-model" and response.json()["has_api_key"]
    assert connections.post("/api/settings/ai/key/reveal").json()["api_key"] == "next-secret"
    assert "next-secret" not in ai_provider.SETTINGS_PATH.read_text(encoding="utf-8")
    assert connections.put("/api/settings/ai", json={**response.json(), "remove_key": True}).status_code == 200
    assert connections.get("/api/settings/ai").json()["has_api_key"] is False


@pytest.mark.parametrize("remove_key", [False, True])
def test_unified_save_restores_encrypted_key_when_configuration_write_fails(connections, monkeypatch, remove_key):
    original = connections.get("/api/settings/ai").json()
    original_file = ai_provider.SETTINGS_PATH.read_bytes()

    def fail(_settings):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(api_main, "save_provider_settings", fail)
    response = connections.put("/api/settings/ai", json={
        **original, "connection_name": "should-not-save",
        **({"remove_key": True} if remove_key else {"api_key": "should-not-save-key"}),
    })
    assert response.status_code == 400
    assert ai_provider.SETTINGS_PATH.read_bytes() == original_file
    assert connections.post("/api/settings/ai/key/reveal").json()["api_key"] == "original-secret"


def test_draft_model_list_uses_new_unsaved_key_before_model_is_chosen(connections, monkeypatch):
    before = ai_provider.SETTINGS_PATH.read_bytes()
    requests = []

    def create(profile, key, *_args):
        profile.require_ready(key)
        requests.append((profile, key))
        return SimpleNamespace(list_remote_models=lambda: ["draft-model"], test_api_connection=lambda: "ok", close=lambda: None)

    monkeypatch.setattr(api_main, "create_judge", create)
    draft = {"base_url": "https://draft.test/v1", "model": "", "max_tokens": 8192}
    response = connections.post("/api/settings/ai/models", json={"settings": draft, "api_key": "unsaved-key"})
    assert response.status_code == 200 and response.json()["models"] == ["draft-model"]
    assert requests[-1][0].base_url == "https://draft.test/v1"
    assert requests[-1][0].max_tokens == 8192 and requests[-1][1] == "unsaved-key"
    response = connections.post("/api/settings/ai/test", json={
        "settings": {**draft, "model": "draft-model"}, "api_key": "unsaved-key",
    })
    assert response.status_code == 200 and requests[-1][0].model == "draft-model"
    assert ai_provider.SETTINGS_PATH.read_bytes() == before
    assert connections.post("/api/settings/ai/key/reveal").json()["api_key"] == "original-secret"


def test_new_connection_can_save_complete_draft_and_copy_never_transfers_key_to_changed_address(connections):
    response = connections.post("/api/settings/ai/connections", json={
        "name": "完整连接", "api_key": "new-account-key",
        "settings": {"base_url": "https://new.test/v1", "model": "new-model", "max_tokens": 8192},
    })
    assert response.status_code == 201, response.text
    assert response.json()["model"] == "new-model" and response.json()["has_api_key"]
    assert connections.post("/api/settings/ai/key/reveal").json()["api_key"] == "new-account-key"
    copied = connections.post("/api/settings/ai/connections", json={
        "name": "改地址的副本", "source_id": "default",
        "settings": {"base_url": "https://different.test/v1", "model": "another-model"},
    })
    assert copied.status_code == 201 and copied.json()["has_api_key"] is False
