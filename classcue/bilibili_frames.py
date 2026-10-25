"""Run-owned candidate frames and an ephemeral, read-only Edge player."""

from __future__ import annotations

import io
import base64
import json
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image, ImageStat

from classcue.asr_storage import library_scope
from classcue.bilibili import BilibiliError, normalize_cookie, video_url
from classcue.bilibili_store import BilibiliStore
from classcue.diagnostics import get_logger, log_exception
from classcue.paths import PROJECT_ROOT
from classcue.question_analysis import MAX_IMAGE_BYTES, QuestionImage
from classcue.qwen_process import WindowsJob


LOGGER = get_logger(__name__)

PLAYER_API_PATHS = {
    "/x/web-interface/nav", "/x/web-interface/view", "/x/web-interface/view/detail",
    "/x/web-interface/wbi/view/detail", "/x/player/v2", "/x/player/wbi/v2",
    "/x/player/playurl", "/x/player/wbi/playurl", "/x/player/pagelist",
}


class FrameStore:
    def __init__(self, store):
        self.store = store
        self.base = Path(library_scope(PROJECT_ROOT, store.path))
        self.root = self.base / "bilibili-frames"

    def path(self, frame_id):
        if not re.fullmatch(r"[0-9a-f]{32}", frame_id):
            raise ValueError("候选截图编号无效")
        if not self.root.resolve().is_relative_to(self.base.resolve()):
            raise ValueError("候选截图目录无效")
        path = (self.root / (frame_id + ".image")).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError("候选截图路径无效")
        return path

    def get(self, marker_id, run_id, frame_id):
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM bili_frames WHERE id=? AND marker_id=? AND run_id=?",
                (frame_id, marker_id, run_id),
            ).fetchone()
        if row is None:
            raise KeyError("候选截图不存在或不属于此考点和运行")
        return dict(row)

    def find(self, marker_id, run_id, unit_id, requested_ms):
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM bili_frames WHERE marker_id=? AND run_id=? AND unit_id=? AND requested_ms=?",
                (marker_id, run_id, unit_id, requested_ms),
            ).fetchone()
        return dict(row) if row else None

    def image(self, record):
        try:
            with self.path(record["id"]).open("rb") as source:
                image = QuestionImage.from_bytes("frame:" + record["id"], source.read(MAX_IMAGE_BYTES + 1))
        except OSError:
            raise ValueError("候选截图文件不可用，请重新取证") from None
        if image.sha256 != record["image_hash"]:
            raise ValueError("候选截图内容已变化")
        return image

    def add(self, run, unit, requested_ms, captured_ms, data, time_source):
        marker = run["input"]["marker"]
        persisted = BilibiliStore(self.store).get_run(marker["id"], run["id"])
        owned = next((item for candidate in persisted["candidates"] for item in candidate["units"]
                      if item["id"] == unit["id"]), None)
        if owned is None or any(owned.get(key) != unit.get(key) for key in ("bvid", "cid", "page")):
            raise ValueError("候选截图与当前运行的取证单元不一致")
        if (type(requested_ms) is not int or type(captured_ms) is not int or requested_ms < 0
                or captured_ms < 0 or abs(captured_ms - requested_ms) > 1500
                or (unit.get("duration_ms") and max(requested_ms, captured_ms) >= unit["duration_ms"])
                or time_source not in {"decoded_frame", "player_current_time"}):
            raise ValueError("候选截图时间无效或定位偏差过大")
        image = QuestionImage.from_bytes("candidate", data)
        if image.width < 640 or image.height < 360:
            raise ValueError("候选截图分辨率不足，无法可靠核验题目")
        with Image.open(io.BytesIO(data)) as decoded:
            if max(ImageStat.Stat(decoded.convert("RGB").resize((128, 72))).stddev) < 1.5:
                raise ValueError("候选截图为空白或黑屏，不用于核验")
        existing = self.find(marker["id"], run["id"], unit["id"], requested_ms)
        if existing:
            self.image(existing)
            return existing
        frame_id = uuid.uuid4().hex
        path = self.path(frame_id)
        self.root.mkdir(parents=True, exist_ok=True)
        self.collect()
        try:
            with self.store.connect() as connection:
                with path.open("xb") as output:
                    output.write(data)
                connection.execute(
                    """INSERT INTO bili_frames
                    (id,marker_id,video_id,run_id,unit_id,bvid,cid,page,requested_ms,captured_ms,
                     time_source,image_hash,mime_type,width,height) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (frame_id, marker["id"], marker["video_id"], run["id"], unit["id"], unit["bvid"],
                     unit["cid"], unit["page"], requested_ms, captured_ms, time_source,
                     image.sha256, image.mime_type, image.width, image.height),
                )
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return self.get(marker["id"], run["id"], frame_id)

    def collect(self):
        with self.store.connect() as connection:
            for row in connection.execute("SELECT id FROM bili_frame_garbage").fetchall():
                try:
                    self.path(row["id"]).unlink(missing_ok=True)
                except (OSError, ValueError):
                    continue
                connection.execute("DELETE FROM bili_frame_garbage WHERE id=?", (row["id"],))


def browser_url_allowed(url):
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and not parsed.username and not parsed.password
                and parsed.port in {None, 443}
                and any(parsed.hostname == domain or (parsed.hostname or "").endswith("." + domain)
                        for domain in ("bilibili.com", "bilivideo.com", "hdslb.com")))
    except ValueError:
        return False


IDENTITY_SCRIPT = """expected => {
  const state = window.__INITIAL_STATE__ || {};
  const data = state.videoData || {};
  let info = {};
  try { info = window.player?.getVideoInfo?.() || {}; } catch {}
  const cid = info.cid || state.cid || (data.pages?.length === 1 ? data.cid : null);
  const bvid = info.bvid || data.bvid;
  return bvid === expected.bvid && Number(cid) === expected.cid
    && Number(new URL(location.href).searchParams.get('p') || 1) === expected.page;
}"""

SEEK_SCRIPT = """(video, target) => {
  video.pause(); video.muted = true;
  if (!Number.isFinite(video.duration) || target >= video.duration) throw Error('unavailable');
  for (let node = video.parentElement; node && node !== document.documentElement; node = node.parentElement) {
    node.style.setProperty('overflow','visible','important');
    node.style.setProperty('contain','none','important');
    node.style.setProperty('transform','none','important');
    node.style.setProperty('clip-path','none','important');
    node.style.setProperty('z-index','2147483646','important');
  }
  video.style.cssText = 'position:fixed!important;left:0!important;top:0!important;'
    + 'z-index:2147483647!important;width:1600px!important;max-width:none!important;'
    + 'height:' + Math.round(1600 * video.videoHeight / video.videoWidth) + 'px!important;'
    + 'object-fit:contain!important;background:black!important;opacity:1!important';
  if (video.__classcueCallback !== undefined && video.cancelVideoFrameCallback)
    video.cancelVideoFrameCallback(video.__classcueCallback);
  video.__classcueDecoded = null;
  if (video.requestVideoFrameCallback) {
    video.__classcueCallback = video.requestVideoFrameCallback((_, metadata) => {
      video.__classcueDecoded = metadata.mediaTime;
    });
  }
  if (Math.abs(video.currentTime-target) >= 0.03) video.currentTime = target;
}"""

SEEK_READY_SCRIPT = """({video, target}) =>
  !video.seeking && video.readyState >= 2 && Math.abs(video.currentTime-target) <= 1.5"""

FRAME_TIME_SCRIPT = """video => {
  const decoded = video.__classcueDecoded;
  if (video.__classcueCallback !== undefined && video.cancelVideoFrameCallback)
    video.cancelVideoFrameCallback(video.__classcueCallback);
  return {captured_ms:Math.round((decoded !== null ? decoded : video.currentTime)*1000),
    time_source:decoded !== null ? 'decoded_frame' : 'player_current_time'};
}"""

FRAME_STAGES = {"加载播放器", "核对视频和分 P", "等待可解码画面", "定位截图时间",
                "等待定位画面", "读取截图像素", "校验截图时间"}


class EdgeFrameSession:
    """No user browser profile, downloads, account actions, or stored browser state."""

    def __init__(self, repo, cookie="", *, checkpoint=lambda: None, consume=lambda *_: None):
        self.repo, self.cookie = repo, normalize_cookie(cookie) if cookie else ""
        self.checkpoint_callback, self.consume = checkpoint, consume
        self.playwright = self.browser = self.context = self.page = None
        self.unit_id = None
        self.pending_error = None
        self.stage = "准备取帧"
        self.stage_callback = lambda _: None
        self.response_bytes = {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        try:
            if self.browser:
                self.browser.close()
        finally:
            if self.playwright:
                self.playwright.stop()

    def checkpoint(self):
        self.checkpoint_callback()
        if self.pending_error:
            raise self.pending_error
        if self.repo.control("platform_pause"):
            raise BilibiliError("platform_paused", "B 站读取已暂停，请处理平台提示后手动恢复")

    def set_stage(self, value):
        self.stage = value
        self.stage_callback(value)

    def fail_pause(self, reason):
        self.repo.set_control("platform_pause", {"reason": reason})
        self.pending_error = BilibiliError("platform_paused", "B 站播放器返回风控提示，已暂停读取")

    def route(self, route):
        request = route.request
        parsed = urlsplit(request.url)
        if (request.method not in {"GET", "HEAD"} or not browser_url_allowed(request.url)
                or getattr(request, "resource_type", "") in {"image", "font"}
                or (parsed.hostname == "api.bilibili.com" and parsed.path not in PLAYER_API_PATHS)):
            route.abort()
            return
        try:
            self.checkpoint()
            self.consume("browser_requests")
        except Exception as error:
            self.pending_error = error
            route.abort()
            return
        # Authentication is confined to Bilibili, never forwarded to media CDNs.
        headers = dict(request.headers)
        if parsed.hostname != "bilibili.com" and not (parsed.hostname or "").endswith(".bilibili.com"):
            headers.pop("cookie", None)
        route.continue_(headers=headers)

    def block_web_socket(self, socket):
        # Keep the intercepted socket local; synchronous close can stall the driver callback.
        socket.on_message(lambda _: None)

    def response(self, response):
        if response.status in {401, 403, 412, 429} and browser_url_allowed(response.url):
            self.fail_pause("browser_http_" + str(response.status))
        try:
            length = int(response.headers.get("content-length", "0"))
        except (ValueError, TypeError):
            length = 0
        if length > 32 * 1024 * 1024:
            self.pending_error = BilibiliError("response_limit", "播放器单项响应超过读取上限，已停止本次取帧")
            if self.page:
                self.page.close()
            return
    def request_finished(self, request):
        parsed = urlsplit(request.url)
        if parsed.hostname == "api.bilibili.com" and parsed.path in PLAYER_API_PATHS:
            try:
                response = request.response()
                if response is None or int(response.headers.get("content-length", "0")) > 4 * 1024 * 1024:
                    return
                payload = response.json()
            except Exception:
                return
            if not isinstance(payload, dict):
                return
            code = payload.get("code")
            message = str(payload.get("message", "")).lower()
            if (code in {-111, -352, -412, -509, -403} or code == -101 and bool(self.cookie)
                    or any(word in message for word in ("captcha", "验证码", "风控", "账号异常"))
                    or isinstance(payload.get("data"), dict) and payload["data"].get("v_voucher")):
                self.fail_pause("browser_api_" + str(code))

    def data_received(self, event):
        try:
            size = max(0, int(event.get("encodedDataLength") or event.get("dataLength") or 0))
            request_id = event.get("requestId")
            self.response_bytes[request_id] = self.response_bytes.get(request_id, 0) + size
            self.consume("browser_bytes", size)
            if self.response_bytes[request_id] > 32 * 1024 * 1024:
                raise BilibiliError("response_limit", "播放器单项响应超过读取上限，已停止本次取帧")
        except Exception as error:
            self.pending_error = error
            try:
                if self.page and not self.page.is_closed():
                    self.page.close()
            except Exception:
                pass

    def start(self):
        if self.context:
            return
        try:
            from playwright.sync_api import sync_playwright
            self.playwright = sync_playwright().start()
            self.browser = self.playwright.chromium.launch(channel="msedge", headless=True)
            self.context = self.browser.new_context(
                viewport={"width": 1680, "height": 1100}, accept_downloads=False,
                service_workers="block",
            )
            self.context.route("**/*", self.route)
            self.context.route_web_socket("**/*", self.block_web_socket)
            self.context.on("response", self.response)
            self.context.on("requestfinished", self.request_finished)
            if self.cookie:
                self.context.add_cookies([
                    {"name": name, "value": value, "domain": ".bilibili.com", "path": "/",
                     "secure": True, "httpOnly": True, "sameSite": "Lax"}
                    for item in self.cookie.split("; ") for name, value in [item.split("=", 1)]
                ])
        except Exception:
            raise BilibiliError("capture_unavailable", "无法启动 Edge 取帧，请确认已安装 Microsoft Edge 和浏览器取帧组件") from None

    def capture(self, unit, requested_ms):
        self.checkpoint()
        self.start()
        try:
            if self.unit_id != unit["id"]:
                self.set_stage("加载播放器")
                if self.page:
                    self.page.close()
                self.page = self.context.new_page()
                self.response_bytes.clear()
                session = self.context.new_cdp_session(self.page)
                session.send("Network.enable")
                session.on("Network.dataReceived", self.data_received)
                session.on("Network.loadingFinished", lambda event: self.response_bytes.pop(event["requestId"], None))
                session.on("Network.loadingFailed", lambda event: self.response_bytes.pop(event["requestId"], None))
                self.page.set_default_timeout(15000)
                self.page.goto(video_url(unit["bvid"], unit["page"]), wait_until="domcontentloaded", timeout=25000)
                self.checkpoint()
                self.check_risk()
                self.set_stage("核对视频和分 P")
                self.page.wait_for_function(IDENTITY_SCRIPT, arg=unit, timeout=15000)
                self.unit_id = unit["id"]
            self.checkpoint()
            if not self.page.evaluate(IDENTITY_SCRIPT, unit):
                raise BilibiliError("capture_failed", "播放器当前视频或分 P 已变化，未保存截图")
            self.check_risk()
            self.set_stage("等待可解码画面")
            video = self.page.locator(".bpx-player-video-wrap video, .bpx-player-video-wrap bwp-video").first
            video.wait_for(state="visible")
            video.evaluate("video => { video.muted=true; const p=video.play(); if(p?.catch) p.catch(()=>{}); }")
            self.page.wait_for_function(
                "() => { const v=document.querySelector('.bpx-player-video-wrap video, .bpx-player-video-wrap bwp-video');"
                " return v && v.readyState>=2 && v.videoWidth>0 && v.videoHeight>0; }",
                timeout=15000,
            )
            video.evaluate("video => video.pause()")
            self.checkpoint()
            self.set_stage("定位截图时间")
            video.evaluate(SEEK_SCRIPT, requested_ms / 1000)
            self.set_stage("等待定位画面")
            handle = video.element_handle()
            self.page.wait_for_function(
                SEEK_READY_SCRIPT, arg={"video": handle, "target": requested_ms / 1000},
                polling=100, timeout=12000,
            )
            self.checkpoint()
            timing = video.evaluate(FRAME_TIME_SCRIPT)
            if abs(timing["captured_ms"] - requested_ms) > 1500:
                raise BilibiliError("capture_failed", "解码画面与目标时间不一致，未保存截图")
            self.set_stage("读取截图像素")
            data = video.screenshot(type="jpeg", quality=90, animations="disabled", timeout=10000)
            self.set_stage("校验截图时间")
            after = video.evaluate("v => ({time:Math.round(v.currentTime*1000),seeking:v.seeking})")
            self.checkpoint()
            if (after["seeking"] or abs(after["time"] - timing["captured_ms"]) > 300
                    or not self.page.evaluate(IDENTITY_SCRIPT, unit)):
                raise BilibiliError("capture_failed", "候选视频画面仍在变化，未保存为截图证据")
            return {**timing, "data": data}
        except Exception as error:
            try:
                self.check_risk()
                if isinstance(error, BilibiliError):
                    raise
                raise BilibiliError("capture_failed", f"候选取帧未完成（{self.stage}）；可能需要登录、无播放权限或页面结构变化") from None
            finally:
                if not self.pending_error or (isinstance(self.pending_error, BilibiliError)
                                              and self.pending_error.code not in {"platform_paused", "credential_invalid"}):
                    self.pending_error = None
                    self.unit_id = None

    def check_risk(self):
        if self.page and not self.page.is_closed():
            path = urlsplit(self.page.url).path.lower()
            challenge = self.page.locator(".geetest_panel, .bili-captcha, #bili-captcha").filter(visible=True).count()
            if challenge or any(word in path for word in ("/captcha", "/risk", "/security")):
                self.fail_pause("browser_challenge")
        self.checkpoint()


MAX_FRAME_SECONDS = 60
MAX_WORKER_REPLY = 9 * 1024 * 1024


class BrowserFrameReader:
    """Supervise browser calls out of process so cancellation cannot hang a job.

    The worker is created suspended and joins a kill-on-close Windows job
    before it runs, so its Edge descendants are reaped even when the worker
    itself exits or crashes first.
    """

    def __init__(self, repo, cookie="", *, checkpoint=lambda: None, consume=lambda *_: None):
        self.repo, self.cookie = repo, normalize_cookie(cookie) if cookie else ""
        self.checkpoint_callback, self.consume = checkpoint, consume
        self.process = self.reader_thread = self.job = None
        self.cleanup_errors = []
        self.messages = queue.Queue()
        self.stage = "准备取帧"

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        try:
            self.stop()
        except Exception:
            # Cleanup failures stay logged and retained on the reader; the
            # body's own exception must keep propagating.
            if exc_type is None:
                raise

    def checkpoint(self):
        self.checkpoint_callback()
        if self.repo.control("platform_pause"):
            raise BilibiliError("platform_paused", "B 站读取已暂停，请检查平台提示后手动恢复")

    def send(self, value):
        try:
            self.process.stdin.write(json.dumps(value, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
        except (OSError, ValueError, AttributeError):
            raise BilibiliError("capture_failed", "候选取帧进程已退出") from None

    def read_messages(self, process, messages):
        try:
            while True:
                line = process.stdout.readline(MAX_WORKER_REPLY + 1)
                if not line:
                    break
                if len(line) > MAX_WORKER_REPLY:
                    messages.put({"event": "error", "code": "response_limit"})
                    return
                try:
                    message = json.loads(line)
                except ValueError:
                    break
                if not isinstance(message, dict):
                    break
                messages.put(message)
        except (OSError, ValueError):
            pass
        messages.put({"event": "error", "code": "capture_failed"})

    def wait(self, expected, seconds):
        deadline = time.monotonic() + seconds
        while True:
            self.checkpoint()
            if time.monotonic() >= deadline:
                raise BilibiliError("capture_unavailable",
                                   f"候选播放器取帧超时（{self.stage}），辅助进程已停止；请检查后手动重试")
            try:
                message = self.messages.get(timeout=min(.1, max(.001, deadline - time.monotonic())))
            except queue.Empty:
                continue
            event = message.get("event")
            if event == expected:
                return message
            if event == "stage":
                if message.get("stage") in FRAME_STAGES:
                    self.stage = message["stage"]
                continue
            if event in {"consume", "checkpoint", "pause"}:
                try:
                    if event == "consume":
                        if message.get("key") not in {"browser_requests", "browser_bytes"}:
                            raise BilibiliError("capture_failed", "取帧进程请求了无效的资源额度")
                        amount = message.get("amount")
                        if type(amount) is not int or amount < 0:
                            raise BilibiliError("capture_failed", "取帧进程返回了无效用量")
                        self.consume(message["key"], amount)
                    elif event == "pause":
                        reason = message.get("reason", "")
                        if not isinstance(reason, str) or not re.fullmatch(r"browser_(?:http|api)_-?\d+|browser_challenge", reason):
                            reason = "browser_challenge"
                        self.repo.set_control("platform_pause", {"reason": reason, "at": int(time.time())})
                    self.send({"ok": True})
                except Exception:
                    self.send({"ok": False})
                    raise
                if event == "pause":
                    raise BilibiliError("platform_paused", "B 站播放器返回登录或风控提示，已停止读取")
            elif event == "error":
                codes = {"capture_failed", "capture_unavailable", "platform_paused", "response_limit"}
                code = message.get("code") if message.get("code") in codes else "capture_failed"
                # Worker errors are fixed application messages, never provider/driver exceptions.
                raise BilibiliError(code, f"候选取帧未完成（{self.stage}），请检查 Edge、浏览器组件及播放权限后重试")

    def start(self):
        if self.process:
            return
        # 4 = CREATE_SUSPENDED so the job owns the whole tree before the worker runs.
        flags = ((subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP | 4)
                 if sys.platform == "win32" else 0)
        self.process = subprocess.Popen(
            [sys.executable, "-m", "classcue.bilibili_frame_worker"], cwd=PROJECT_ROOT,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", bufsize=1, creationflags=flags,
        )
        self.job = None
        try:
            if sys.platform == "win32":
                self.job = WindowsJob(self.process)
                self.job.resume(self.process)
            self.reader_thread = threading.Thread(target=self.read_messages, args=(self.process, self.messages),
                                                  name="candidate-frame-reader", daemon=True)
            self.reader_thread.start()
            self.send({"action": "init", "cookie": self.cookie})
            self.wait("ready", 10)
        except BaseException:
            try:
                self.stop(force=True)
            except Exception:
                # stop() already logged the cleanup failure; the startup error is primary.
                pass
            raise

    def capture(self, unit, requested_ms):
        try:
            self.checkpoint()
            self.start()
            self.stage = "受控播放器取帧"
            self.send({"action": "capture", "unit": unit, "requested_ms": requested_ms})
            result = self.wait("frame", MAX_FRAME_SECONDS)
            try:
                data = base64.b64decode(result["image"], validate=True)
            except (KeyError, ValueError, TypeError):
                raise BilibiliError("capture_failed", "取帧进程返回了无效图片") from None
            if len(data) > MAX_IMAGE_BYTES:
                raise BilibiliError("response_limit", "候选截图超过读取上限")
            return {"data": data, "captured_ms": result.get("captured_ms"), "time_source": result.get("time_source")}
        except Exception:
            try:
                self.stop(force=True)
            except Exception:
                # The capture error below is primary; cleanup failures stay
                # logged and retained on the reader for later handling.
                pass
            raise

    def stop(self, *, force=False):
        """Close the owned worker tree, then its pipes, keeping failures visible."""
        process, job, reader = self.process, self.job, self.reader_thread
        if process is None:
            return
        errors = []

        def record(stage, error):
            errors.append((stage, error))
            try:
                log_exception(LOGGER, "Frame worker cleanup failed", exc=error,
                              stage=stage, pid=process.pid)
            except Exception:
                pass  # Diagnostics must never abort the remaining cleanup steps.

        def cleanup(stage, operation):
            try:
                operation()
            except Exception as error:
                record(stage, error)

        if not force:
            try:
                if process.poll() is None:
                    self.send({"action": "close"})
                    process.wait(timeout=3)
            except (BilibiliError, subprocess.TimeoutExpired):
                force = True
            except Exception as error:
                # An abnormal graceful-shutdown failure must not skip the
                # independent force cleanup below; it stays recorded for
                # re-raising after every step has run.
                record("graceful_close", error)
                force = True

        def close_tree():
            # The job still owns the descendants when the worker parent has
            # already exited; killing only by PID would both risk reuse and
            # miss the orphaned tree.
            if job is not None:
                job.close()
            elif process.poll() is None:
                process.kill()

        def kill_direct():
            if process.poll() is None:
                process.kill()

        def join_reader():
            reader.join(timeout=1)
            if reader.is_alive():
                # join() returning does not mean the thread exited; stdout
                # must stay open while readline() still holds it.
                raise RuntimeError("frame reader thread did not exit with the worker tree")

        cleanup("process_tree", close_tree)
        cleanup("kill", kill_direct)
        cleanup("wait", lambda: process.wait(timeout=5))
        if reader is not None and reader.ident is not None:
            cleanup("reader_join", join_reader)
        if process.stdin:
            cleanup("stdin_close", process.stdin.close)
        if process.stdout and (reader is None or not reader.is_alive()):
            cleanup("stdout_close", process.stdout.close)
        if errors:
            # Keep the un-reaped references for an explicit later retry.
            self.cleanup_errors.extend(errors)
            raise errors[0][1]
        self.process = self.job = self.reader_thread = None
        self.messages = queue.Queue()
        self.cleanup_errors = []
