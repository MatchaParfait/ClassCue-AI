"""Short-lived Bilibili web QR login; credentials never enter API responses."""

from __future__ import annotations

import base64
import io
import json
import logging
import re
import threading
import time
import uuid
from http.cookies import SimpleCookie
from urllib.parse import parse_qsl, urlsplit

import httpx
import qrcode
from qrcode.image.svg import SvgPathImage

from classcue.bilibili import BilibiliAccount, BilibiliError, normalize_cookie
from classcue.diagnostics import redact_text, register_secret


PASSPORT = "https://passport.bilibili.com"
GENERATE = "/x/passport-login/web/qrcode/generate"
POLL = "/x/passport-login/web/qrcode/poll"
WAITING = {"waiting_scan", "waiting_confirm"}
POLL_SECONDS = 3
LIFETIME_SECONDS = 180
SCAN_PAGES = {
    ("passport.bilibili.com", "/h5-app/passport/login/scan"),
    ("account.bilibili.com", "/h5/account-h5/auth/scan-web"),
}


class _RedactLoginLog(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        record.msg = ("HTTP session headers omitted" if "cookie" in message.lower()
                      else redact_text(message))
        record.args = ()
        return True


def qr_image(url: str) -> str:
    image = qrcode.make(url, image_factory=SvgPathImage, box_size=6, border=4)
    output = io.BytesIO()
    image.save(output)
    return "data:image/svg+xml;base64," + base64.b64encode(output.getvalue()).decode("ascii")


def confirmation_cookie(raw: object) -> dict[str, str]:
    """Read credentials from the successful poll response without following its URL."""
    if not isinstance(raw, str) or len(raw) > 8192 or any(ord(char) < 32 for char in raw):
        raise ValueError("Invalid confirmation URL")
    parsed = urlsplit(raw)
    host = parsed.hostname or ""
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or parsed.port not in {None, 443} or parsed.fragment
            or not any(host == domain or host.endswith("." + domain)
                       for domain in ("bilibili.com", "biligame.com"))):
        raise ValueError("Invalid confirmation URL")
    cookies = {}
    for key, value in parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=32):
        if key not in {"SESSDATA", "buvid3"}:
            continue
        register_secret(value)
        if key in cookies and cookies[key] != value:
            raise ValueError("Conflicting confirmation credentials")
        cookies[key] = value
    return cookies


class BilibiliQrLogin:
    def __init__(self, account: BilibiliAccount, *, transport=None, clock=time.monotonic):
        for name in ("httpx", "httpcore.http11", "httpcore.http2", "httpcore.connection"):
            logger = logging.getLogger(name)
            if not any(isinstance(item, _RedactLoginLog) for item in logger.filters):
                logger.addFilter(_RedactLoginLog())
        self.account = account
        self.transport, self.clock = transport, clock
        self.lock = threading.RLock()
        self.session: dict | None = None
        self.last_start = float("-inf")

    def _request(self, path: str, params: dict, repository) -> tuple[dict, str]:
        if path not in {GENERATE, POLL}:
            raise BilibiliError("url_denied", "拒绝访问未经允许的登录接口")
        try:
            with httpx.Client(timeout=httpx.Timeout(15, connect=8), follow_redirects=False,
                              transport=self.transport, trust_env=False) as client:
                with client.stream("GET", PASSPORT + path, params=params, headers={
                    "User-Agent": "Mozilla/5.0 ClassCue/1.0",
                    "Referer": "https://www.bilibili.com/",
                }) as response:
                    if response.status_code in {401, 403, 412, 429}:
                        repository.set_control("platform_pause", {
                            "reason": "login_http_" + str(response.status_code), "at": int(time.time()),
                        })
                        raise BilibiliError("platform_paused", "B 站登录受限，请检查平台提示后手动重试")
                    if response.status_code != 200:
                        raise BilibiliError("login_failed", "B 站登录暂不可用，请稍后重试")
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > 65536:
                            raise BilibiliError("login_failed", "B 站登录响应超过上限")
                    cookies = {}
                    for cookie in response.cookies.jar:
                        if cookie.domain.lstrip(".") not in {"bilibili.com", "passport.bilibili.com"}:
                            continue
                        if cookie.name in {"SESSDATA", "buvid3"}:
                            register_secret(cookie.value)
                            if cookie.name in cookies and cookies[cookie.name] != cookie.value:
                                raise BilibiliError("login_failed", "B 站登录会话冲突，请重新扫码")
                            cookies[cookie.name] = cookie.value
        except httpx.HTTPError:
            raise BilibiliError("login_failed", "B 站登录连接失败或超时，请手动重试") from None
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        data = payload.get("data")
        message = (str(payload.get("message", "")) + " " + (
            str(data.get("message", "")) if isinstance(data, dict) else ""
        )).lower()
        if (payload.get("code") in {-101, -111, -352, -403, -412, -509}
                or isinstance(data, dict) and data.get("code") in {-101, -111, -352, -403, -412, -509}
                or any(word in message for word in ("captcha", "验证码", "风控", "账号异常"))
                or isinstance(data, dict) and data.get("v_voucher")
                or any(word in body[:8192].decode("utf-8", errors="ignore").lower()
                       for word in ("captcha", "验证码"))):
            repository.set_control("platform_pause", {"reason": "login_challenge", "at": int(time.time())})
            raise BilibiliError("platform_paused", "B 站要求验证，已停止登录，请先检查平台提示")
        if payload.get("code") != 0 or not isinstance(data, dict):
            raise BilibiliError("login_failed", "B 站未返回有效的登录结果")
        for field in ("url", "qrcode_key", "refresh_token", "token"):
            if isinstance(data.get(field), str):
                register_secret(data[field])
        return data, "; ".join(f"{key}={value}" for key, value in cookies.items())

    def _finish(self, status: str):
        if self.session:
            self.session.update(status=status, key="", image="")

    def _public(self) -> dict:
        session = self.session
        return {
            "session_id": session["id"], "status": session["status"],
            "image": session["image"] if session["status"] in WAITING else "",
            "expires_at": session["expires_at"], "poll_after_ms": POLL_SECONDS * 1000,
        }

    def start(self, repository) -> dict:
        with self.lock:
            now = self.clock()
            if now - self.last_start < 5:
                raise BilibiliError("login_throttled", "二维码刷新过快，请稍后再试")
            self.last_start = now
            self._finish("cancelled")
            data, _ = self._request(GENERATE, {"source": "main_web"}, repository)
            url, key = data.get("url"), data.get("qrcode_key")
            try:
                parsed = urlsplit(url) if isinstance(url, str) else None
                valid = (
                    parsed and parsed.scheme == "https"
                    and not parsed.username and not parsed.password and parsed.port in {None, 443}
                    and (parsed.hostname, parsed.path.rstrip("/")) in SCAN_PAGES and not parsed.fragment
                    and isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,256}", key)
                    and len(url) <= 4096
                )
            except ValueError:
                valid = False
            if not valid:
                raise BilibiliError("login_failed", "B 站登录二维码地址无效")
            self.session = {
                "id": uuid.uuid4().hex, "key": key, "status": "waiting_scan", "image": qr_image(url),
                "deadline": self.clock() + LIFETIME_SECONDS,
                "expires_at": int(time.time()) + LIFETIME_SECONDS, "last_poll": float("-inf"),
            }
            return self._public()

    def poll(self, session_id: str, repository, *, before_save=lambda: None) -> dict:
        with self.lock:
            if not self.session or self.session["id"] != session_id:
                raise BilibiliError("login_not_found", "登录二维码已关闭或被刷新，请重新扫码")
            if self.session["status"] not in WAITING:
                return self._public()
            now = self.clock()
            if now >= self.session["deadline"]:
                self._finish("expired")
                return self._public()
            if now - self.session["last_poll"] < POLL_SECONDS:
                return self._public()
            self.session["last_poll"] = now
            try:
                data, cookie = self._request(POLL, {
                    "qrcode_key": self.session["key"], "source": "main_web",
                }, repository)
                code = data.get("code")
                if type(code) is not int:
                    raise BilibiliError("login_failed", "B 站返回未知登录状态，请重新扫码")
                if code == 0:
                    parsed_cookie = SimpleCookie()
                    parsed_cookie.load(cookie)
                    if "SESSDATA" not in parsed_cookie or not parsed_cookie["SESSDATA"].value:
                        try:
                            fallback = confirmation_cookie(data.get("url"))
                            for name, value in fallback.items():
                                if name not in parsed_cookie or not parsed_cookie[name].value:
                                    parsed_cookie[name] = value
                            cookie = normalize_cookie("; ".join(
                                f"{name}={item.value}" for name, item in parsed_cookie.items()
                            ))
                        except ValueError:
                            raise BilibiliError("login_failed", "登录未返回有效会话，请重新扫码") from None
                    if "SESSDATA" not in parsed_cookie or not parsed_cookie["SESSDATA"].value:
                        raise BilibiliError("login_failed", "登录未返回有效会话，请重新扫码")
                    before_save()
                    self.account.save(normalize_cookie(cookie))
                    self._finish("success")
                elif code == 86101:
                    self.session["status"] = "waiting_scan"
                elif code == 86090:
                    self.session["status"] = "waiting_confirm"
                elif code == 86038:
                    self._finish("expired")
                else:
                    raise BilibiliError("login_failed", "B 站未确认登录，请重新扫码")
            except (OSError, ValueError, RuntimeError) as error:
                self._finish("failed")
                if isinstance(error, BilibiliError):
                    raise
                raise BilibiliError("login_failed", "无法保存 B 站登录状态，请重新扫码") from None
            except Exception:
                self._finish("failed")
                raise
            return self._public()

    def cancel(self, session_id: str | None = None) -> dict:
        with self.lock:
            if self.session and (session_id is None or self.session["id"] == session_id):
                self._finish("cancelled")
            return {"status": "cancelled"}
