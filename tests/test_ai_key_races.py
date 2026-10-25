"""Credential writer races use synthetic encrypted files and no remote calls."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event

import pytest

import api.main as api_main
import classcue.ai_provider as ai_provider
from classcue.ai_provider import ProviderSettings
from tests.test_ai_connections import connections


def key_request(client, action, value="synthetic-later-key"):
    if action == "delete":
        return client.delete("/api/settings/ai/key")
    return client.post("/api/settings/ai/key", json={"api_key": value})


@pytest.mark.parametrize("action", ["delete", "save"])
def test_failed_connection_save_does_not_revert_later_key_write(connections, monkeypatch, action):
    client = connections
    current = client.get("/api/settings/ai").json()
    entered, release, competing_lock = Event(), Event(), Event()
    real_lock = api_main.provider_settings_lock

    def fail_configuration(_settings):
        entered.set()
        assert release.wait(10), "test did not release the configuration writer"
        raise OSError("Synthetic configuration failure")

    @contextmanager
    def observed_lock():
        if entered.is_set():
            competing_lock.set()
        with real_lock():
            yield

    monkeypatch.setattr(api_main, "save_provider_settings", fail_configuration)
    monkeypatch.setattr(api_main, "provider_settings_lock", observed_lock)
    with ThreadPoolExecutor(max_workers=2) as executor:
        writing = executor.submit(client.put, "/api/settings/ai", json={
            **current, "api_key": "synthetic-uncommitted-key",
        })
        try:
            assert entered.wait(5), "connection save never reached the failure boundary"
            later = executor.submit(key_request, client, action)
            assert competing_lock.wait(2), "key writer did not enter the shared configuration lock"
            assert not later.done()
        finally:
            release.set()
        assert writing.result(timeout=10).status_code == 400
        assert later.result(timeout=10).status_code == 200
    profile = ai_provider.load_provider_settings().active_profile
    disabled = ai_provider.interface_key_path(profile).with_suffix(".disabled")
    revealed = client.post("/api/settings/ai/key/reveal")
    if action == "delete":
        assert disabled.exists()
        assert revealed.status_code == 404
    else:
        assert not disabled.exists()
        assert revealed.status_code == 200
        assert revealed.json()["api_key"] == "synthetic-later-key"


@pytest.mark.parametrize("action", ["delete", "save"])
def test_key_target_is_selected_after_acquiring_configuration_lock(connections, monkeypatch, action):
    client = connections
    created = client.post("/api/settings/ai/connections", json={
        "name": "Synthetic second connection", "source_id": "default",
    })
    assert created.status_code == 201
    second_id = created.json()["connection_id"]
    assert client.post("/api/settings/ai/connections/default/activate").status_code == 200
    attempted = Event()
    real_lock = api_main.provider_settings_lock

    @contextmanager
    def observed_lock():
        attempted.set()
        with real_lock():
            yield

    monkeypatch.setattr(api_main, "provider_settings_lock", observed_lock)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with real_lock():
            writing = executor.submit(key_request, client, action)
            assert attempted.wait(2), "key writer did not acquire the configuration lock"
            assert not writing.done()
            values = ai_provider.load_provider_settings().model_dump()
            values["active_connection_id"] = second_id
            ai_provider.save_provider_settings(ProviderSettings.model_validate(values))
        assert writing.result(timeout=10).status_code == 200
    original = client.post("/api/settings/ai/key/reveal", json={"connection_id": "default"})
    assert original.status_code == 200 and original.json()["api_key"] == "original-secret"
    selected = client.post("/api/settings/ai/key/reveal", json={"connection_id": second_id})
    if action == "delete":
        assert selected.status_code == 404
    else:
        assert selected.status_code == 200 and selected.json()["api_key"] == "synthetic-later-key"
