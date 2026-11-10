"""Desktop-first playback uses synthetic libraries and isolated desktop IPC."""

from html.parser import HTMLParser
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
from threading import Thread
from urllib.parse import parse_qs, urlsplit

import pytest

from classcue import course_connection
from classcue.desktop_runtime import SingleInstance
from classcue.playback_links import desktop_review_url, playback_url
from tests.test_course_tools import fixture, video, call


ORIGIN = "http://127.0.0.1:9999"


def page_target(html):
    class Parser(HTMLParser):
        reading = False
        data = ""

        def handle_starttag(self, tag, attrs):
            self.reading = tag == "script" and dict(attrs).get("id") == "target"

        def handle_endtag(self, tag):
            if tag == "script":
                self.reading = False

        def handle_data(self, data):
            if self.reading:
                self.data += data

    parser = Parser()
    parser.feed(html)
    return json.loads(parser.data)


def landing(f, identity, seconds=12.345):
    url = f.service._url(identity, seconds)
    page = f.client.get(url)
    assert page.status_code == 200, page.text
    return page, page_target(page.text)


def post(f, payload, **kwargs):
    return f.client.post(ORIGIN + "/api/desktop-links/open", json=payload, **kwargs)


def test_get_and_link_preview_do_not_open_desktop_and_escape_course_names(fixture, monkeypatch):
    f = fixture
    identity = video(f, "synthetic-lesson")
    with f.store.connect() as db:
        db.execute("UPDATE videos SET display_name=? WHERE id=?", ('</script><img src=x onerror=alert(1)>', identity))
    monkeypatch.setattr(course_connection, "navigate_desktop", lambda *_a, **_k: pytest.fail("GET must be read-only"))
    page, payload = landing(f, identity)
    assert "&lt;/script&gt;&lt;img" in page.text and "<img src=x" not in page.text
    assert payload["start_ms"] == 12345 and payload["video_id"] == identity
    assert payload["library_id"] == f.app.state.jobs.provider.library_id()
    assert course_connection.connection_token(f.root / "app") not in page.text
    assert page.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["referrer-policy"] == "no-referrer"
    assert 'id="web" hidden' in page.text
    assert f.client.get(ORIGIN + "/api/desktop-links/client.js").status_code == 200
    assert f.client.get(ORIGIN + "/api/desktop-links/icon.png").content.startswith(b"\x89PNG")


def test_navigation_credentials_are_created_only_for_a_valid_handoff(fixture):
    f = fixture
    identity = video(f, "synthetic-lesson")
    token = f.root / "app/.runtime/course_tools_auth.json"
    assert not token.exists()
    response = f.client.get(f"{ORIGIN}/open/review/{identity}?library_id=wrong")
    assert response.status_code == 409 and not token.exists()
    landing(f, identity)
    assert token.is_file()


def test_click_reaches_real_isolated_desktop_ipc_and_preserves_milliseconds(fixture, monkeypatch):
    f = fixture
    identity = video(f, "synthetic-lesson")
    commands = []

    def dispatch(command):
        commands.append(command)
        return {"ready": True, "window_ready": True} if command == "status" else {"ok": True}

    instance = SingleInstance(f.root / "app/.runtime", dispatch)
    assert instance.acquire()
    monkeypatch.setattr(course_connection.subprocess, "Popen", lambda *_a, **_k: pytest.fail("reuse the desktop"))
    try:
        _, payload = landing(f, identity)
        response = post(f, payload)
        assert response.status_code == 200 and response.json() == {"navigation": "requested"}
        assert commands == ["status", {"action": "navigate", "url":
            playback_url(ORIGIN, identity, 12345, payload["library_id"], desktop=False)}]
    finally:
        instance.close()


def test_mcp_open_keeps_the_clickable_link_but_sends_a_review_route_to_desktop(fixture, monkeypatch):
    f = fixture
    identity = video(f, "synthetic-lesson")
    result = call(f, "open_video", video_id=identity, start_seconds=12.345)
    calls = []
    instance = SingleInstance(f.root / "app/.runtime", lambda command:
        {"ready": True, "window_ready": True} if command == "status" else calls.append(command) or {"ok": True})
    assert instance.acquire()
    try:
        opened = course_connection.navigate_desktop(result["play_url"], project=f.root / "app")
        assert opened["play_url"] == result["play_url"]
        assert urlsplit(result["play_url"]).path == f"/open/review/{identity}"
        assert urlsplit(calls[0]["url"]).path == f"/review/{identity}"
        assert parse_qs(urlsplit(calls[0]["url"]).query)["start_ms"] == ["12345"]
        assert desktop_review_url(calls[0]["url"]) == calls[0]["url"]
    finally:
        instance.close()


def test_unsaved_desktop_is_activated_without_losing_edits_and_web_is_only_an_option(fixture):
    f = fixture
    identity = video(f, "synthetic-lesson")
    commands = []
    instance = SingleInstance(f.root / "app/.runtime", lambda command:
        {"ready": True, "window_ready": True, "unsaved_changes": True}
        if command == "status" else commands.append(command) or {"ok": True})
    assert instance.acquire()
    try:
        _, payload = landing(f, identity)
        response = post(f, payload)
        assert response.status_code == 503
        assert response.json()["code"] == "desktop_unavailable"
        assert "未保存" in response.json()["message"]
        assert response.json()["web_url"].startswith(f"/review/{identity}?")
        assert commands == ["activate"]
    finally:
        instance.close()


@pytest.mark.parametrize("reason", [RuntimeError("synthetic unavailable"), OSError("synthetic launch failure")])
def test_failed_desktop_provides_a_validated_web_alternative_without_redirecting(fixture, monkeypatch, reason):
    f = fixture
    identity = video(f, "synthetic-lesson")
    _, payload = landing(f, identity)

    def fail(*_args, **_kwargs):
        raise reason

    monkeypatch.setattr(course_connection, "navigate_desktop", fail)
    response = post(f, payload)
    assert response.status_code == 503 and "location" not in response.headers
    assert response.json()["web_url"] == playback_url("", identity, 12345, payload["library_id"], desktop=False)


@pytest.mark.parametrize("mutation", ["library", "video", "time", "proof", "extra"])
def test_tampered_or_arbitrary_navigation_never_reaches_desktop(fixture, monkeypatch, mutation):
    f = fixture
    identity = video(f, "synthetic-lesson")
    _, payload = landing(f, identity)
    payload.update({
        "library": {"library_id": "another-library"}, "video": {"video_id": identity + 1},
        "time": {"start_ms": 55555}, "proof": {"proof": "0" * 64},
        "extra": {"url": "https://example.test/settings"},
    }[mutation])
    monkeypatch.setattr(course_connection, "navigate_desktop", lambda *_a, **_k: pytest.fail("tampered request"))
    response = post(f, payload)
    assert response.status_code in {403, 422} and "web_url" not in response.json()


@pytest.mark.parametrize("query", [
    "?library_id=other&start_ms=0",
    "?library_id={library}&start_ms=120000",
    "?library_id={library}&start_ms=-1",
    "?library_id={library}&start_ms=1.5",
    "?library_id={library}&start_ms=0&start_ms=1",
    "?library_id={library}&start_ms=0&url=https://example.test",
])
def test_invalid_link_never_opens_desktop(fixture, monkeypatch, query):
    f = fixture
    identity = video(f, "synthetic-lesson")
    library = f.app.state.jobs.provider.library_id()
    monkeypatch.setattr(course_connection, "navigate_desktop", lambda *_a, **_k: pytest.fail("invalid link"))
    response = f.client.get(f"{ORIGIN}/open/review/{identity}" + query.format(library=library))
    assert response.status_code in {409, 422}
    if response.headers["content-type"].startswith("text/html"):
        assert page_target(response.text) == {"error": True}
        assert "回放暂时不可用" in response.text


@pytest.mark.parametrize("change", ["library", "file", "video"])
def test_changed_target_cannot_fall_back_to_the_wrong_video(fixture, monkeypatch, change):
    f = fixture
    identity = video(f, "synthetic-lesson")
    _, payload = landing(f, identity)
    if change == "library":
        monkeypatch.setattr(f.app.state.jobs.provider, "library_id", lambda: "changed")
    elif change == "file":
        Path(f.store.get_video(identity)["video_path"]).unlink()
    else:
        with f.store.connect() as db:
            db.execute("DELETE FROM videos WHERE id=?", (identity,))
    monkeypatch.setattr(course_connection, "navigate_desktop", lambda *_a, **_k: pytest.fail("stale target"))
    response = post(f, payload)
    assert response.status_code in {404, 409} and "web_url" not in response.json()


def test_cross_site_embedding_and_post_are_rejected(fixture, monkeypatch):
    f = fixture
    identity = video(f, "synthetic-lesson")
    _, payload = landing(f, identity)
    monkeypatch.setattr(course_connection, "navigate_desktop", lambda *_a, **_k: pytest.fail("cross-site call"))
    assert post(f, payload, headers={"Origin": "https://example.test"}).status_code == 403
    embedded = f.client.get(f.service._url(identity), headers={
        "Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "iframe",
    })
    assert embedded.status_code == 403


def test_real_browser_clicks_open_desktop_and_only_offer_web_after_failure(fixture, monkeypatch):
    import uvicorn
    f = fixture
    project = Path(__file__).resolve().parents[1]
    node = shutil.which("node")
    playwright = os.getenv("CLASSROOM_PLAYWRIGHT_MODULE") or os.getenv("PLAYWRIGHT_MODULE") or str(
        project / "frontend/node_modules/playwright")
    if not node or not Path(playwright).exists():
        pytest.skip("Node and installed Playwright are required")
    identity = video(f, "synthetic-browser-lesson")
    commands = []
    instance = SingleInstance(f.root / "app/.runtime", lambda command:
        {"ready": True, "window_ready": True} if command == "status" else commands.append(command) or {"ok": True})
    assert instance.acquire()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(f.app, lifespan="off", log_level="error"))
    thread = Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    try:
        result = subprocess.run([
            node, str(project / "tests/desktop_links_browser.cjs"), origin,
            playback_url(origin, identity, 12345, f.app.state.jobs.provider.library_id()),
            str(f.root / "browser-evidence"),
        ], env={**os.environ, "CLASSROOM_PLAYWRIGHT_MODULE": playwright},
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout)["passed"] is True
        assert len(commands) == 2
        assert all(urlsplit(command["url"]).path == f"/review/{identity}" for command in commands)
    finally:
        server.should_exit = True
        thread.join(10)
        listener.close()
        instance.close()
        assert not thread.is_alive()
