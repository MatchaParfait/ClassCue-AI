"""Read-only Bilibili transport, WBI signing and timestamped subtitle discovery.

Portions adapted from dsh-bili-humanism, Copyright (c) 2026
InariNGT-Ofiicial (MIT). See docs/dsh_bili_humanism_LICENSE.txt.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import tempfile
import time
from html.parser import HTMLParser
from http.cookies import SimpleCookie, CookieError
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx

from classcue.bilibili_store import BilibiliStore, VERSION, digest
from classcue.local_credentials import load_api_key, save_api_key, delete_api_key
from classcue.processing_control import ProcessingCancelled
from classcue.diagnostics import register_secret


API = "https://api.bilibili.com"
PATHS = {
    "/x/web-interface/nav", "/x/web-interface/wbi/search/type",
    "/x/web-interface/view", "/x/player/wbi/v2",
}
MIXIN = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
    26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
    20, 34, 44, 52,
)


class BilibiliError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def wbi_sign(params: dict, img_url: str, sub_url: str, timestamp: int | None = None) -> dict:
    stem = lambda value: urlsplit(value).path.rsplit("/", 1)[-1].split(".")[0]
    raw = stem(img_url) + stem(sub_url)
    if len(raw) < 64:
        raise BilibiliError("invalid_response", "B 站签名参数不可用")
    mix = "".join(raw[index] for index in MIXIN)[:32]
    values = {
        key: re.sub(r"[!'()*]", "", str(value))
        for key, value in sorted({**params, "wts": int(time.time()) if timestamp is None else timestamp}.items())
    }
    values["w_rid"] = hashlib.md5((urlencode(values) + mix).encode()).hexdigest()
    return values


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def plain_text(value, limit=2000) -> str:
    parser = _PlainText()
    parser.feed(str(value or "")[:10000])
    return "".join(parser.parts).strip()[:limit]


def video_url(bvid: str, page: int = 1, at_ms: int | None = None) -> str:
    if not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid) or page < 1:
        raise BilibiliError("invalid_response", "视频标识无效")
    query = {"p": page}
    if at_ms is not None:
        query["t"] = max(0, at_ms // 1000)
    return f"https://www.bilibili.com/video/{bvid}/?{urlencode(query)}"


def normalize_cookie(raw: str) -> str:
    if not raw or len(raw) > 16384 or any(ord(char) < 32 or ord(char) > 126 for char in raw):
        raise ValueError("Cookie 格式无效，请填写 Cookie 请求头中的值")
    try:
        cookies = SimpleCookie()
        cookies.load(raw)
    except CookieError:
        raise ValueError("Cookie 格式无效") from None
    selected = [(key, cookies[key].value) for key in ("SESSDATA", "buvid3") if key in cookies]
    if not selected or any(not value or re.search(r'[\s;"\\]', value) for _, value in selected):
        raise ValueError("Cookie 中需要 SESSDATA 或 buvid3")
    value = "; ".join(f"{key}={item}" for key, item in selected)
    register_secret(value)
    for _, secret in selected:
        register_secret(secret)
    return value


class BilibiliAccount:
    def __init__(self, directory: Path):
        self.path = directory / "bilibili_cookie.dpapi"

    def load(self) -> str:
        if not self.path.exists():
            return ""
        value = load_api_key(self.path)
        if not value:
            raise BilibiliError("credential_invalid", "无法读取 B 站登录状态，请重新扫码或退出后使用匿名模式")
        return normalize_cookie(value)

    def save(self, raw: str) -> None:
        value = normalize_cookie(raw)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix=".bilibili-", suffix=".tmp", delete=False) as file:
            temporary = Path(file.name)
        try:
            save_api_key(temporary, value)
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def delete(self) -> None:
        delete_api_key(self.path)


class BilibiliClient:
    def __init__(self, store: BilibiliStore, cookie: str = "", *, transport=None, should_cancel=lambda: False):
        self.store = store
        self.cookie = normalize_cookie(cookie) if cookie else ""
        self.account_scope = digest(self.cookie) if self.cookie else "anonymous"
        self.should_cancel = should_cancel
        self.requests = 0
        self.started = time.monotonic()
        self.keys = None
        self.before_request = None
        self.client = httpx.Client(
            timeout=httpx.Timeout(15, connect=8), follow_redirects=False, transport=transport,
        )

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.client.close()

    def checkpoint(self):
        if self.should_cancel():
            raise ProcessingCancelled("已停止 B 站检索")
        if self.store.control("platform_pause"):
            raise BilibiliError("platform_paused", "B 站读取已暂停，请检查账号和平台提示后手动恢复")
        if self.before_request is None and time.monotonic() - self.started > 1800:
            raise BilibiliError("budget_exceeded", "本次 B 站读取已达到请求或时间预算")

    def pause(self, reason: str):
        self.store.set_control("platform_pause", {"reason": reason, "at": int(time.time())})
        raise BilibiliError("platform_paused", "B 站返回登录失效、验证码或风控提示，已暂停后续读取")

    def get(self, path: str, params: dict | None = None, *, subtitle=False):
        self.checkpoint()
        url = path if subtitle else API + path
        parsed = urlsplit(url)
        allowed = (
            parsed.hostname in {"aisubtitle.hdslb.com", "i0.hdslb.com"}
            and (parsed.path.endswith(".json") or parsed.path.startswith("/bfs/ai_subtitle/"))
            if subtitle else parsed.hostname == "api.bilibili.com" and parsed.path in PATHS
        )
        if (not allowed or parsed.scheme != "https" or parsed.username or parsed.password
                or parsed.port not in {None, 443} or parsed.fragment):
            raise BilibiliError("url_denied", "拒绝访问未经允许的 B 站资源地址")
        headers = {"User-Agent": "Mozilla/5.0 ClassCue/1.0", "Referer": "https://www.bilibili.com/"}
        if self.cookie and not subtitle:
            headers["Cookie"] = self.cookie
        # Never let an HTTP client's cookie jar forward a subtitle/CDN cookie.
        self.client.cookies.clear()
        if self.before_request is not None:
            self.before_request()
        elif self.requests >= 60:
            raise BilibiliError("budget_exceeded", "本次 B 站读取已达到请求预算")
        self.requests += 1
        try:
            with self.client.stream("GET", url, params=params, headers=headers) as response:
                if response.status_code in {401, 403, 412, 429}:
                    self.pause("http_" + str(response.status_code))
                if response.status_code != 200:
                    raise BilibiliError("read_failed", "B 站读取失败，请稍后手动重试")
                body = bytearray()
                for chunk in response.iter_bytes():
                    self.checkpoint()
                    body.extend(chunk)
                    if len(body) > 4 * 1024 * 1024:
                        raise BilibiliError("response_limit", "B 站返回内容超过读取上限")
        except httpx.HTTPError:
            raise BilibiliError("read_failed", "B 站连接失败或超时") from None
        self.checkpoint()
        try:
            result = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            if any(word in body.decode("utf-8", errors="ignore")[:8192].lower()
                   for word in ("captcha", "验证码", "验证页面")):
                self.pause("challenge")
            raise BilibiliError("read_failed", "B 站未返回可用的 JSON 内容") from None
        if not isinstance(result, dict):
            raise BilibiliError("read_failed", "B 站响应结构无效")
        if not subtitle:
            anonymous_keys = (
                path == "/x/web-interface/nav" and "SESSDATA=" not in self.cookie
                and result.get("code") == -101 and isinstance(result.get("data"), dict)
                and bool(result["data"].get("wbi_img"))
            )
            if not anonymous_keys and result.get("code") in {-101, -111, -352, -412, -509, -403}:
                self.pause("api_" + str(result["code"]))
            message = str(result.get("message", ""))
            if (any(word in message.lower() for word in ("captcha", "验证码", "风控", "账号异常"))
                    or isinstance(result.get("data"), dict) and result["data"].get("v_voucher")):
                self.pause("challenge")
            if result.get("code") != 0 and not anonymous_keys:
                raise BilibiliError("read_failed", "B 站接口暂不可用")
            return result.get("data") or {}
        return result

    def signed(self, path: str, params: dict):
        if self.keys is None:
            self.keys = self.get("/x/web-interface/nav").get("wbi_img", {})
        return self.get(path, wbi_sign(params, self.keys.get("img_url", ""), self.keys.get("sub_url", "")))

    def cached(self, operation: str, params: dict, load):
        self.checkpoint()
        key = digest([VERSION, operation, self.account_scope, params])
        cached = self.store.cache_get(key)
        if cached is not None:
            return cached
        result = load()
        self.checkpoint()
        self.store.cache_put(key, result)
        return result

    def search(self, keyword: str) -> list[dict]:
        def load():
            data = self.signed("/x/web-interface/wbi/search/type", {
                "keyword": keyword, "search_type": "video", "page": 1, "page_size": 20,
            })
            result = []
            for row in (data.get("result") or [])[:20]:
                bv = str(row.get("bvid", ""))
                if not re.fullmatch(r"BV[0-9A-Za-z]{10}", bv):
                    continue
                result.append({
                    "bvid": bv, "title": plain_text(row.get("title"), 300),
                    "description": plain_text(row.get("description")),
                    "author": plain_text(row.get("author"), 100), "url": video_url(bv),
                })
            return result
        return self.cached("search", {"keyword": keyword}, load)

    def metadata(self, bvid: str) -> dict:
        video_url(bvid)
        def load():
            data = self.get("/x/web-interface/view", {"bvid": bvid})
            all_pages = data.get("pages") or []
            pages = []
            for item in all_pages[:100]:
                duration = float(item.get("duration") or 0)
                cid, page = int(item["cid"]), int(item["page"])
                if cid <= 0 or page <= 0 or not math.isfinite(duration) or duration <= 0:
                    continue
                pages.append({
                    "bvid": bvid, "page": page, "cid": cid, "duration_ms": round(duration * 1000),
                    "part": plain_text(item.get("part"), 300), "url": video_url(bvid, page),
                })
            season = data.get("ugc_season") or {}
            episodes = []
            for section in season.get("sections") or []:
                for episode in section.get("episodes") or []:
                    bv = str(episode.get("bvid") or (episode.get("arc") or {}).get("bvid") or "")
                    if not re.fullmatch(r"BV[0-9A-Za-z]{10}", bv):
                        continue
                    episodes.append({"bvid": bv, "title": plain_text(episode.get("title"), 300),
                                     "section": plain_text(section.get("title"), 300),
                                     "episode_id": str(episode.get("id") or "")})
            total = int(season.get("ep_count") or len(episodes))
            return {"title": plain_text(data.get("title"), 300), "pages": pages,
                    "page_count": len(all_pages), "pages_complete": len(pages) == len(all_pages),
                    "collection": {"id": str(season.get("id") or ""),
                                   "title": plain_text(season.get("title"), 300),
                                   "episodes": episodes[:200], "total": total,
                                   "complete": total == len(episodes) and len(episodes) <= 200}}
        return self.cached("metadata", {"bvid": bvid}, load)

    def subtitles(self, unit: dict) -> dict:
        def load():
            data = self.signed("/x/player/wbi/v2", {"bvid": unit["bvid"], "cid": unit["cid"]})
            tracks = (data.get("subtitle") or {}).get("subtitles") or []
            needs_login = bool(data.get("need_login_subtitle")
                               or (data.get("subtitle") or {}).get("need_login_subtitle"))
            if not tracks:
                return {"status": "requires_login" if needs_login else "no_subtitles", "segments": []}
            tracks = [track for track in tracks if isinstance(track, dict)
                      and isinstance(track.get("subtitle_url"), str) and track["subtitle_url"].strip()]
            if not tracks:
                return {"status": "requires_login" if needs_login or "SESSDATA=" not in self.cookie
                        else "read_failed", "segments": []}
            def preference(track):
                language = str(track.get("lan", ""))
                chinese = "zh" in language
                automatic = language.startswith("ai") or bool(track.get("ai_type"))
                return (not chinese, automatic)
            selected = sorted(tracks, key=preference)[0]
            raw_url = selected["subtitle_url"].strip()
            body = self.get("https:" + raw_url if raw_url.startswith("//") else raw_url, subtitle=True)
            raw = body.get("body")
            if not isinstance(raw, list) or len(raw) > 30000:
                raise BilibiliError("read_failed", "字幕内容结构无效或超过条数上限")
            segments = []
            for row in raw:
                text = str(row.get("content", "")).strip()
                if not text:
                    continue
                try:
                    start, end = float(row["from"]) * 1000, float(row["to"]) * 1000
                except (KeyError, TypeError, ValueError):
                    raise BilibiliError("read_failed", "字幕缺少有效时间戳") from None
                if (not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start
                        or end > unit["duration_ms"] + 5000 or len(text) > 20000):
                    raise BilibiliError("read_failed", "字幕时间戳或文本超出有效范围")
                segments.append({"start_ms": round(start), "end_ms": round(end), "text": text})
            segments.sort(key=lambda item: (item["start_ms"], item["end_ms"]))
            for index, segment in enumerate(segments, 1):
                segment["id"] = index
            language = str(selected.get("lan", ""))
            return {
                "status": "available" if segments else "no_subtitles", "segments": segments,
                "language": language,
                "track_id": str(selected.get("id_str") or selected.get("id") or "")[:128],
                "automatic": language.startswith("ai") or bool(selected.get("ai_type")),
            }
        return self.cached("subtitles", {key: unit[key] for key in (
            "bvid", "cid", "page", "duration_ms",
        )}, load)
