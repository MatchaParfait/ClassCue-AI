"""Host and web-origin boundary regressions for the local HTTP API.

The API must only serve this machine's trusted loopback host names and
same-origin web pages, while local tools without browser metadata stay able
to use it. Rejections have to happen before any business side effect runs.
"""

from __future__ import annotations

from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from api.main import create_app
from classcue.video_store import VideoStore

API_BASE = "http://127.0.0.1:8765"
PREPARE_PATH = "/api/asr/models/whisper-turbo/prepare"


@pytest.fixture
def app(tmp_path: Path):
    return create_app(VideoStore(tmp_path / "video.db"))


@pytest.fixture
def fake_prepare(monkeypatch):
    import api.main as api_main

    calls: list[str] = []
    started = Event()

    def prepare(model_id, progress, **_kwargs):
        calls.append(model_id)
        progress(1.0, "Ready")
        started.set()

    monkeypatch.setattr(api_main, "prepare_asr_model", prepare)
    return SimpleNamespace(calls=calls, started=started)


def test_cross_site_page_cannot_start_model_preparation(app, fake_prepare):
    with TestClient(app, base_url=API_BASE) as client:
        response = client.post(
            PREPARE_PATH,
            # A browser "simple" request of this shape never sends a preflight.
            headers={
                "Origin": "https://untrusted.example",
                "Sec-Fetch-Site": "cross-site",
                "Content-Type": "text/plain",
            },
        )
        assert response.status_code == 403
        assert fake_prepare.calls == []
        assert fake_prepare.started.wait(0.2) is False
        # The instrumentation itself works: a plain local request still runs it.
        assert client.post(PREPARE_PATH).status_code == 202
        assert fake_prepare.started.wait(3)
        assert fake_prepare.calls == ["whisper-turbo"]


def test_untrusted_host_cannot_read_local_data(app, fake_prepare):
    # DNS-rebinding shape: the Origin matches the attacker-controlled Host,
    # but the Host itself is not one of this service's trusted local names.
    with TestClient(app, base_url="http://untrusted.example") as rebinding:
        assert rebinding.get("/api/health").status_code == 403
        assert rebinding.get("/api/library/status").status_code == 403
        response = rebinding.post(
            PREPARE_PATH,
            headers={"Origin": "http://untrusted.example", "Sec-Fetch-Site": "cross-site"},
        )
        assert response.status_code == 403
        assert fake_prepare.calls == []


@pytest.mark.parametrize("base_url", [
    "http://127.0.0.1:5173",
    "http://127.0.0.1:5174",
    "http://localhost:5173",
])
def test_vite_proxy_requests_keep_working_on_any_local_port(app, base_url, fake_prepare):
    # Vite proxies /api with changeOrigin:false, so the API sees the
    # frontend's own Host while the browser Origin stays on that authority.
    with TestClient(app, base_url=base_url) as client:
        headers = {"Origin": base_url, "Sec-Fetch-Site": "same-origin"}
        assert client.get("/api/health", headers=headers).status_code == 200
        assert client.post(PREPARE_PATH, headers=headers).status_code == 202
    assert fake_prepare.calls == ["whisper-turbo"]


def test_desktop_page_same_origin_requests_work(app):
    with TestClient(app, base_url=API_BASE) as client:
        headers = {"Origin": API_BASE, "Sec-Fetch-Site": "same-origin"}
        assert client.get("/api/health", headers=headers).status_code == 200
    # Starlette's test transport cannot send to a bracketed IPv6 base URL,
    # so the IPv6 loopback shape is exercised through the real request headers.
    ipv6 = {"Host": "[::1]:8765", "Origin": "http://[::1]:8765"}
    with TestClient(app, base_url=API_BASE) as client:
        assert client.get("/api/health", headers=ipv6).status_code == 200
    with TestClient(app, base_url="http://localhost") as client:
        assert client.get("/api/health", headers={"Origin": "http://localhost"}).status_code == 200


@pytest.mark.parametrize("headers", [
    {"Origin": "null"},
    {"Origin": "https://untrusted.example"},
    # Another port on this machine is not the same origin as this service.
    {"Origin": "http://127.0.0.1:5173"},
    {"Origin": "http://localhost:8765"},
    # Browser origins never carry credentials, paths or another scheme.
    {"Origin": "http://user:pass@127.0.0.1:8765"},
    {"Origin": "http://127.0.0.1:8765/attacker"},
    {"Origin": "https://127.0.0.1:8765"},
    {"Origin": "http://127.0.0.1:99999"},
    # A matching Origin cannot honestly come with a cross-site statement.
    {"Origin": API_BASE, "Sec-Fetch-Site": "cross-site"},
])
def test_untrusted_origins_are_rejected_before_business_logic(app, headers):
    with TestClient(app, base_url=API_BASE) as client:
        assert client.get("/api/library/status", headers=headers).status_code == 403


@pytest.mark.parametrize("headers", [
    [("Host", "127.0.0.1:8765"), ("Host", "127.0.0.1:5173")],
    {"Host": "127.0.0.1:notaport"},
    {"Host": ""},
    {"Host": "::1:8765"},
    # Longer than any valid port, and beyond Python's int() digit limit.
    {"Host": "127.0.0.1:" + "9" * 5000},
])
def test_malformed_or_duplicate_host_headers_are_rejected(app, headers):
    with TestClient(app, base_url=API_BASE) as client:
        assert client.get("/api/health", headers=headers).status_code == 403


@pytest.mark.parametrize("headers", [
    {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Dest": "image"},
    {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"},
    {"Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Dest": "image"},
    {"Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "nested-navigate", "Sec-Fetch-Dest": "iframe"},
])
def test_page_embedded_reads_without_origin_are_rejected(app, headers):
    # No-cors subresource loads (img/iframe/form targets) send no Origin.
    with TestClient(app, base_url=API_BASE) as client:
        assert client.get("/api/library/status", headers=headers).status_code == 403


@pytest.mark.parametrize("headers", [
    # Opening the local UI from a link on another page stays possible.
    {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"},
    {"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"},
    {"Sec-Fetch-Site": "none"},
    {"Sec-Fetch-Site": "same-origin"},
])
def test_user_navigation_and_same_site_metadata_stay_available(app, headers):
    with TestClient(app, base_url=API_BASE) as client:
        assert client.get("/api/health", headers=headers).status_code == 200


def test_explicit_base_url_position_reaches_the_original_signature(app):
    # conftest replaces only TestClient's implicit default base; a base_url
    # given positionally (its slot in the original signature) must survive
    # and still decide which Host the requests are sent with.
    with TestClient(app, "http://untrusted.example") as client:
        assert client.get("/api/health").status_code == 403
    with TestClient(app, API_BASE) as client:
        assert client.get("/api/health").status_code == 200
