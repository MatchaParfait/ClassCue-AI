import base64
import logging
from types import SimpleNamespace

import httpx
import pytest

from classcue.bilibili import BilibiliAccount, BilibiliError
from classcue.bilibili_login import BilibiliQrLogin, GENERATE, POLL


QR_URL = "https://passport.bilibili.com/h5-app/passport/login/scan?auth_code=synthetic-qr"
SECRET = "synthetic-private-session"


class PassportNetwork:
    def __init__(self):
        self.codes = [86101, 86090, 0]
        self.calls = []
        self.now = 100.0
        self.url = QR_URL
        self.confirm_url = ""
        self.headers = [
            ("set-cookie", f"SESSDATA={SECRET}; Domain=.bilibili.com; Path=/; Secure; HttpOnly"),
            ("set-cookie", "buvid3=synthetic-device; Domain=.bilibili.com; Path=/; Secure"),
        ]

    def handle(self, request):
        self.calls.append(request)
        assert request.url.host == "passport.bilibili.com"
        assert request.method == "GET"
        assert not request.headers.get("cookie")
        if request.url.path == GENERATE:
            return httpx.Response(200, json={"code": 0, "data": {
                "qrcode_key": "synthetic-key", "url": self.url,
            }})
        assert request.url.path == POLL
        assert request.url.params["qrcode_key"] == "synthetic-key"
        code = self.codes.pop(0)
        return httpx.Response(200, headers=self.headers if code == 0 else [],
                              json={"code": 0, "data": {"code": code, "refresh_token": "synthetic-refresh",
                                                      "url": self.confirm_url}})

    def advance(self):
        self.now += 3


@pytest.fixture
def login():
    network = PassportNetwork()
    saved = []
    controls = {}
    account = SimpleNamespace(save=saved.append)
    repository = SimpleNamespace(set_control=controls.__setitem__)
    manager = BilibiliQrLogin(account, transport=httpx.MockTransport(network.handle), clock=lambda: network.now)
    return manager, network, repository, saved, controls


def test_qr_flow_is_ephemeral_and_success_never_returns_credentials(login):
    manager, network, repo, saved, _ = login
    value = manager.start(repo)
    assert value["status"] == "waiting_scan"
    assert "synthetic-key" not in str(value)
    assert value["image"].startswith("data:image/svg+xml;base64,")
    svg = base64.b64decode(value["image"].split(",", 1)[1])
    assert b"<svg" in svg and b"<path" in svg
    assert "http" not in value["image"]
    for expected in ("waiting_scan", "waiting_confirm", "success"):
        network.advance()
        value = manager.poll(value["session_id"], repo)
        assert value["status"] == expected
    assert saved == [f"SESSDATA={SECRET}; buvid3=synthetic-device"]
    assert SECRET not in str(value) and "synthetic-refresh" not in str(value)
    assert manager.session["key"] == "" and value["image"] == ""
    assert manager.poll(value["session_id"], repo) == value
    assert len(saved) == 1 and len(network.calls) == 4


@pytest.mark.parametrize("url", [
    QR_URL,
    "https://account.bilibili.com/h5/account-h5/auth/scan-web?auth_code=synthetic-qr",
])
def test_supports_current_and_legacy_scan_page_addresses(login, url):
    manager, network, repo, _, _ = login
    network.url = url
    assert manager.start(repo)["status"] == "waiting_scan"


@pytest.mark.parametrize("headers", [
    [],
    [("set-cookie", "buvid3=synthetic-device; Domain=.bilibili.com; Path=/")],
])
def test_confirm_url_recovers_session_without_following_url(login, headers):
    manager, network, repo, saved, _ = login
    network.codes = [0]
    network.headers = headers
    network.confirm_url = (
        "https://passport.biligame.com/crossDomain?SESSDATA=synthetic-session%2Ctoken"
        "&DedeUserID=synthetic-user&bili_jct=synthetic-csrf"
    )
    session = manager.start(repo)
    result = manager.poll(session["session_id"], repo)
    assert result["status"] == "success"
    assert "synthetic-session" not in str(result)
    assert len(network.calls) == 2
    assert saved == ["SESSDATA=synthetic-session,token" + ("; buvid3=synthetic-device" if headers else "")]


@pytest.mark.parametrize("url", [
    "https://evil.example/?SESSDATA=synthetic-session",
    "https://bilibili.com.evil.example/?SESSDATA=synthetic-session",
    "http://passport.bilibili.com/?SESSDATA=synthetic-session",
    "https://user@passport.bilibili.com/?SESSDATA=synthetic-session",
    "https://passport.bilibili.com:8443/?SESSDATA=synthetic-session",
    "https://passport.bilibili.com/?SESSDATA=synthetic-session#fragment",
    "https://passport.bilibili.com/?SESSDATA=first&SESSDATA=second",
    "https://passport.bilibili.com/?SESSDATA=bad%0D%0Acookie",
    "https://passport.bilibili.com/?SESSDATA=",
])
def test_invalid_confirm_url_never_saves_a_session(login, url):
    manager, network, repo, saved, _ = login
    network.codes, network.headers, network.confirm_url = [0], [], url
    session = manager.start(repo)
    with pytest.raises(BilibiliError, match="有效会话"):
        manager.poll(session["session_id"], repo)
    assert saved == [] and len(network.calls) == 2
    assert manager.session["status"] == "failed"


def test_session_headers_take_precedence_over_confirmation_url(login):
    manager, network, repo, saved, _ = login
    network.codes = [0]
    network.confirm_url = "https://evil.example/?SESSDATA=untrusted"
    session = manager.start(repo)
    assert manager.poll(session["session_id"], repo)["status"] == "success"
    assert saved == [f"SESSDATA={SECRET}; buvid3=synthetic-device"]


def test_poll_is_server_throttled_and_qr_refresh_is_rate_limited(login):
    manager, network, repo, _, _ = login
    session = manager.start(repo)
    manager.poll(session["session_id"], repo)
    manager.poll(session["session_id"], repo)
    assert len(network.calls) == 2
    with pytest.raises(BilibiliError, match="刷新过快"):
        manager.start(repo)
    network.now += 6
    other = manager.start(repo)
    assert other["session_id"] != session["session_id"]
    with pytest.raises(BilibiliError, match="已关闭"):
        manager.poll(session["session_id"], repo)
    manager.cancel(session["session_id"])
    assert manager.session["status"] == "waiting_scan"


def test_qr_key_is_redacted_even_if_httpx_request_logging_is_enabled(login, caplog):
    manager, _, repo, _, _ = login
    with caplog.at_level(logging.INFO, logger="httpx"):
        session = manager.start(repo)
        manager.poll(session["session_id"], repo)
    assert "qrcode/poll" in caplog.text
    assert "synthetic-key" not in caplog.text
    assert "[REDACTED]" in caplog.text


def test_debug_headers_cannot_log_a_fresh_cookie_before_it_has_been_registered(login, caplog):
    with caplog.at_level(logging.DEBUG, logger="httpcore.http11"):
        logging.getLogger("httpcore.http11").debug(
            "receive_response_headers.complete Set-Cookie: SESSDATA=%s", "synthetic-unregistered-session",
        )
    assert "synthetic-unregistered-session" not in caplog.text
    assert "session headers omitted" in caplog.text


@pytest.mark.parametrize("cancel", [False, True])
def test_cancel_or_local_expiry_stops_all_further_platform_requests(login, cancel):
    manager, network, repo, saved, _ = login
    session = manager.start(repo)
    if cancel:
        manager.cancel(session["session_id"])
    else:
        network.now += 181
    result = manager.poll(session["session_id"], repo)
    assert result["status"] == ("cancelled" if cancel else "expired")
    assert not result["image"] and not manager.session["key"]
    assert len(network.calls) == 1 and saved == []


def test_platform_expiry_requires_a_new_qr(login):
    manager, network, repo, saved, _ = login
    network.codes = [86038]
    session = manager.start(repo)
    assert manager.poll(session["session_id"], repo)["status"] == "expired"
    assert not saved


@pytest.mark.parametrize("url", [
    "https://evil.example/scan",
    "http://passport.bilibili.com/h5-app/passport/login/scan",
    "https://passport.bilibili.com:8443/h5-app/passport/login/scan",
    "https://user@passport.bilibili.com/h5-app/passport/login/scan",
    "https://passport.bilibili.com/another-path",
])
def test_qr_url_is_restricted_to_the_web_login_page(login, url):
    manager, network, repo, saved, _ = login
    network.url = url
    with pytest.raises(BilibiliError, match="地址无效"):
        manager.start(repo)
    assert saved == []


@pytest.mark.parametrize("headers", [
    [],
    [("set-cookie", "buvid3=synthetic-device; Domain=.bilibili.com; Path=/")],
    [("set-cookie", "buvid3=SESSDATA=synthetic-device; Domain=.bilibili.com; Path=/")],
    [("set-cookie", f"SESSDATA={SECRET}; Domain=.evil.example; Path=/")],
])
def test_success_without_a_valid_session_cookie_is_not_saved(login, headers):
    manager, network, repo, saved, _ = login
    network.codes = [0]
    network.headers = headers
    session = manager.start(repo)
    with pytest.raises(BilibiliError, match="有效会话"):
        manager.poll(session["session_id"], repo)
    assert saved == []
    assert manager.session["status"] == "failed"
    assert manager.session["key"] == ""


@pytest.mark.parametrize("response", [
    httpx.Response(412),
    httpx.Response(429),
    httpx.Response(200, json={"code": -352, "data": {}}),
    httpx.Response(200, json={"code": 0, "data": {"code": -352}}),
    httpx.Response(200, json={"code": 0, "data": {"v_voucher": "synthetic-voucher"}}),
    httpx.Response(200, text="<html>captcha</html>"),
])
def test_risk_or_captcha_is_persisted_and_stops_login(login, response):
    manager, network, repo, saved, controls = login
    session = manager.start(repo)
    manager.transport = httpx.MockTransport(lambda _: response)
    with pytest.raises(BilibiliError) as error:
        manager.poll(session["session_id"], repo)
    assert error.value.code == "platform_paused"
    assert controls["platform_pause"]
    assert manager.session["status"] == "failed" and saved == []


@pytest.mark.parametrize("response", [
    httpx.Response(302, headers={"Location": "https://evil.example/"}),
    httpx.Response(200, content=b"x" * 65537),
    httpx.Response(200, json={"code": 0, "data": {"code": 9999}}),
    httpx.Response(200, text="not-json"),
])
def test_unknown_or_oversized_responses_do_not_leak_platform_content(login, response):
    manager, network, repo, saved, _ = login
    session = manager.start(repo)
    manager.transport = httpx.MockTransport(lambda _: response)
    with pytest.raises(BilibiliError):
        manager.poll(session["session_id"], repo)
    assert manager.session["status"] == "failed" and saved == []
    assert manager.poll(session["session_id"], repo)["status"] == "failed"


def test_timeout_does_not_expose_request_key_or_existing_credentials(login):
    manager, network, repo, saved, _ = login
    session = manager.start(repo)

    def timeout(request):
        raise httpx.ReadTimeout("synthetic-key private-session upstream", request=request)

    manager.transport = httpx.MockTransport(timeout)
    with pytest.raises(BilibiliError) as error:
        manager.poll(session["session_id"], repo)
    assert "synthetic-key" not in str(error.value)
    assert "upstream" not in str(error.value) and saved == []


def test_task_started_during_poll_prevents_account_switch(login):
    manager, network, repo, saved, _ = login
    network.codes = [0]
    session = manager.start(repo)

    def busy():
        raise RuntimeError("Synthetic processing task active")

    with pytest.raises(BilibiliError):
        manager.poll(session["session_id"], repo, before_save=busy)
    assert saved == []


def test_failed_encryption_preserves_old_account_file(tmp_path, monkeypatch):
    account = BilibiliAccount(tmp_path)
    account.path.write_bytes(b"synthetic-old-encrypted-state")

    def fail(_path, _value):
        raise OSError("Synthetic encryption failure")

    monkeypatch.setattr("classcue.bilibili.save_api_key", fail)
    with pytest.raises(OSError):
        account.save("SESSDATA=synthetic-new-session")
    assert account.path.read_bytes() == b"synthetic-old-encrypted-state"
    assert list(tmp_path.iterdir()) == [account.path]
