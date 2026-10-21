// Native first-paint and lazy-chunk checks. Everything lives in a disposable installation.
const { chromium } = require("./playwright.cjs");
const { spawn, spawnSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const net = require("node:net");
const assert = require("node:assert/strict");
const root = path.resolve(__dirname, "..");
const python = path.join(root, ".venv/Scripts/python.exe");
const pythonw = path.join(root, ".venv/Scripts/pythonw.exe");
const fixtureRun = spawnSync(python, ["tests/fixtures/desktop_application.py"], { cwd: root, encoding: "utf8", windowsHide: true });
assert.equal(fixtureRun.status, 0, fixtureRun.stderr);
const fixture = JSON.parse(fixtureRun.stdout.trim().split("\n").at(-1));
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
let browser, native, env, api, observedPage, expectedHandoff;
async function until(read, predicate, timeout = 45000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    try { const value = await read(); if (predicate(value)) return value; } catch {}
    await wait(50);
  }
  throw new Error("Startup check timed out");
}
async function port() {
  const server = net.createServer();
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const value = server.address().port;
  await new Promise(resolve => server.close(resolve));
  return value;
}
async function ipc(action) {
  const state = JSON.parse(fs.readFileSync(path.join(fixture.project, ".runtime/desktop_instance.json")));
  const response = await fetch(`http://127.0.0.1:${state.port}/command`, {
    method: "POST", headers: { Authorization: `Bearer ${state.token}`, "Content-Type": "application/json" },
    body: JSON.stringify({ action }),
  });
  assert.ok(response.ok);
  return response.json();
}
async function main() {
  const backendDelayMs = Number(process.env.CLASSROOM_STARTUP_QA_DELAY_MS ?? 10000);
  assert.ok(Number.isFinite(backendDelayMs) && backendDelayMs >= 0 && backendDelayMs <= 10000);
  const apiPort = await port(), debugPort = await port(), frontendPort = await port();
  env = { ...process.env, CLASSROOM_DATA_ROOT: fixture.data, CLASSROOM_API_PORT: String(apiPort),
    CLASSROOM_FRONTEND_PORT: String(frontendPort), CLASSROOM_DESKTOP_DEBUG_PORT: String(debugPort) };
  for (const key of Object.keys(env)) if (/_API_KEY$/.test(key)) delete env[key];
  api = `http://127.0.0.1:${apiPort}`;
  const seed = spawnSync(python, ["-c", `
from pathlib import Path
from classcue.video_store import VideoStore
from classcue.asr_storage import library_identity, audio_directory
from classcue.desktop_startup import save_library_snapshot
import video_thumbnail
import os
project=Path.cwd()
database=Path(os.environ["CLASSROOM_DATA_ROOT"])/"project.db"
store=VideoStore(database)
video=dict(store.get_video(1))
cache=audio_directory(project,database.parent)/"thumbnails"
# Prepare one real cache entry before startup, then forbid all further video decoding.
video_thumbnail.video_thumbnail(Path(video["video_path"]),cache)
for index in range(2,41):
    store.create_video(f"scroll-qa-{index}",f"lesson-{index}.mp4",video["video_path"],"unused.wav",3000)
video_thumbnail._encode_frame=lambda *_args: (_ for _ in ()).throw(AssertionError("No decoding during snapshot"))
save_library_snapshot(project,library_identity(project,database),
    {"parent_id":None,"items":[{"type":"video","video":dict(row)} for row in store.list_videos()]},cache)
`], { cwd: fixture.project, env, encoding: "utf8", windowsHide: true });
  assert.equal(seed.status, 0, seed.stderr);
  const serviceFile = path.join(fixture.project, "scripts/api_service.py");
  const source = fs.readFileSync(serviceFile, "utf8");
  // Delay only this temporary backend so the first-paint preview is observable.
  fs.writeFileSync(serviceFile, source.replace("import argparse", `import time\ntime.sleep(${backendDelayMs / 1000})\nimport argparse`));
  const start = Date.now();
  native = spawn(pythonw, [path.join(fixture.project, "scripts/desktop_app.py")],
    { cwd: fixture.project, env, windowsHide: true, stdio: "ignore" });
  const disconnected = [];
  await until(async () => (await fetch(`http://127.0.0.1:${debugPort}/json/version`)).ok, Boolean);
  browser = await chromium.connectOverCDP(`http://127.0.0.1:${debugPort}`);
  const page = browser.contexts()[0].pages()[0];
  observedPage = page;
  // Force the per-card handoff to outlive the bounded whole-page cover.
  await page.route("**/api/videos/*/thumbnail?**", async route => {
    await wait(1800);
    await route.continue().catch(() => {});
  });
  await page.addInitScript(() => {
    if (window !== window.top) return;
    window.__qaStartupFrames = [];
    window.__qaStartupInputs = [];
    for (const type of ["wheel", "pointerdown", "keydown"]) window.addEventListener(type, event => {
      window.__qaStartupInputs.push({ type, time: performance.now(), trusted: event.isTrusted, deltaY: event.deltaY, key: event.key });
    }, { capture: true, passive: true });
    const sample = () => {
      window.__qaCarriedView ??= window.__classroomStartupView;
      const nativeAnchor = window.__qaCarriedView?.anchor
        ? document.querySelector(`[data-library-key="${window.__qaCarriedView.anchor}"]`) : null;
      window.__qaStartupFrames.push({
        time: performance.now(), cover: !!document.getElementById("classroom-startup-cover"),
        cards: document.querySelectorAll(".library-inner .video-card").length, scroll: scrollY,
        phase: document.getElementById("classroom-startup-cover")?.dataset.phase ?? null,
        view: window.__qaCarriedView, anchorTop: nativeAnchor?.getBoundingClientRect().top ?? null,
        width: innerWidth,
        thumbnails: [...document.querySelectorAll(".library-inner .video-card")].filter(card => {
          const bounds = card.getBoundingClientRect();
          return bounds.bottom > 0 && bounds.top < innerHeight;
        }).map(card => ({
          key: card.dataset.libraryKey,
          pixels: [...card.querySelectorAll(".video-thumbnail img")].some(image =>
            image.complete && image.naturalWidth > 0 && getComputedStyle(image).opacity !== "0"),
          cached: !!card.querySelector('.video-thumbnail img[src^="data:"]'),
        })),
      });
      if (window.__qaStartupFrames.length < 240) requestAnimationFrame(sample);
    };
    requestAnimationFrame(sample);
  });
  page.on("pageerror", error => disconnected.push(error.message));
  let previewMs = null;
  if (backendDelayMs > 0) {
    await page.getByRole("main", { name: "视频库启动预览" }).waitFor();
    await page.locator(".video strong").first().waitFor();
    assert.ok((await page.locator(".video strong").first().innerText()).includes("lesson"));
    previewMs = Date.now() - start;
    assert.equal((await ipc("status")).ready, false, "library preview precedes backend readiness");
    assert.ok((await page.locator("body").innerText()).includes("上次同步的列表"));
    assert.equal(await page.evaluate(() => document.getAnimations().length), 0);
    await page.locator(".thumbnail img").first().evaluate(image => image.decode());
    assert.ok(await page.locator(".thumbnail img").first().evaluate(image =>
      image.naturalWidth > 0 && image.naturalWidth <= 480 && image.src.startsWith("data:image/jpeg;base64,")));
    for (const width of [856, 1316, 1540, 1920]) {
      await page.setViewportSize({ width, height: 949 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      assert.equal(await page.locator(".import-actions button:enabled").count(), 0);
      await page.screenshot({ path: path.join(fixture.project, `startup-preview-${width}.png`) });
    }
    await page.evaluate(() => window.scrollTo(0, 1350));
    assert.ok(await page.evaluate(() => scrollY > 1000));
  }
  const handoff = backendDelayMs > 0 ? await page.evaluate(() => window.__classroomDesktopView()) : null;
  const cachedIds = backendDelayMs > 0 ? await page.locator('.video:has(.thumbnail img)').evaluateAll(cards =>
    cards.map(card => card.dataset.libraryKey)) : [];
  expectedHandoff = handoff;
  const filmstrip = [];
  const session = await page.context().newCDPSession(page);
  session.on("Page.screencastFrame", async event => {
    filmstrip.push(event.data);
    if (filmstrip.length > 80) filmstrip.shift();
    await session.send("Page.screencastFrameAck", { sessionId: event.sessionId }).catch(() => {});
  });
  await session.send("Page.startScreencast", { format: "jpeg", quality: 75, maxWidth: 960, maxHeight: 540, everyNthFrame: 1 });
  await until(() => ipc("status"), state => state.ready && state.tray);
  await page.locator(".library-inner .video-card").first().waitFor();
  await page.locator("#classroom-startup-cover").waitFor({ state: "detached" });
  if (handoff) {
    const visibleKey = await page.locator('.library-inner .video-card:has(img[src^="data:"])').evaluateAll(cards =>
      cards.find(card => {
        const bounds = card.querySelector(".video-thumbnail").getBoundingClientRect();
        return bounds.bottom > 0 && bounds.top < innerHeight;
      })?.dataset.libraryKey);
    assert.ok(visibleKey, "a visible cached cover survives the page handoff");
    const pending = page.locator(`.library-inner [data-library-key="${visibleKey}"] .video-thumbnail img[src^="data:"]`);
    await pending.waitFor();
    // Clip screenshots temporarily change WebView's compositor metrics; keep the
    // full viewport intact while recording the actual handoff frames.
    await page.screenshot({ path: path.join(fixture.project, "startup-cached-thumbnail.png") });
    await pending.waitFor({ state: "detached" });
    await page.screenshot({ path: path.join(fixture.project, "startup-decoded-thumbnail.png") });
  }
  fs.writeFileSync(path.join(fixture.project, "startup-handoff.json"), JSON.stringify(await page.evaluate(handoff => ({
    expected: handoff, carried: window.__qaCarriedView, scroll: scrollY,
    top: handoff?.anchor ? document.querySelector(`[data-library-key="${handoff.anchor}"]`)?.getBoundingClientRect().top : null,
    frames: window.__qaStartupFrames,
  }), handoff), null, 2));
  if (handoff) {
    await until(() => page.evaluate(() => window.pywebview?.api.get_startup_view()), value => value === null);
    const anchor = page.locator(`.library-inner [data-library-key="${handoff.anchor}"]`);
    await until(() => anchor.evaluate(element => element.getBoundingClientRect().top),
      top => Math.abs(top - handoff.offset) < 3);
    assert.ok(await page.evaluate(() => scrollY > 500), "preview scrolling survives the live-document handoff");
    await page.screenshot({ path: path.join(fixture.project, "startup-scroll-handoff.png") });
    const position = await page.evaluate(() => scrollY);
    await page.evaluate(() => { window.dispatchEvent(new Event("focus")); });
    await wait(500);
    assert.equal(await page.evaluate(() => scrollY), position, "query refresh does not replay startup restoration");
  }
  const transitionFrames = await page.evaluate(() => window.__qaStartupFrames);
  await session.send("Page.stopScreencast");
  const libraryMs = Date.now() - start;
  const filmstripFiles = filmstrip.map((data, index) => {
    const filename = path.join(fixture.project, `startup-transition-${String(index).padStart(3, "0")}.jpg`);
    fs.writeFileSync(filename, Buffer.from(data, "base64"));
    return filename;
  });
  fs.writeFileSync(path.join(fixture.project, "startup-transition-frames.json"), JSON.stringify(transitionFrames, null, 2));
  const pixels = spawnSync(python, ["-c", `
import json,sys
from PIL import Image, ImageChops
results=[]
for filename in json.loads(sys.argv[1]):
    with Image.open(filename) as image:
        red,green,blue=image.convert("RGB").split()
        maximum=ImageChops.lighter(ImageChops.lighter(red,green),blue)
        results.append({"file":filename,"ink":sum(maximum.histogram()[:140])/(image.width*image.height)})
print(json.dumps(results))
`, JSON.stringify(filmstripFiles)], { cwd: fixture.project, encoding: "utf8", windowsHide: true });
  assert.equal(pixels.status, 0, pixels.stderr);
  const pixelFrames = JSON.parse(pixels.stdout);
  fs.writeFileSync(path.join(fixture.project, "startup-transition-pixels.json"), JSON.stringify(pixelFrames, null, 2));
  if (handoff) {
    assert.ok(transitionFrames.some(frame => frame.cover), "a read-only cover appears before React loads");
    assert.ok(transitionFrames.some(frame => frame.phase === "fading"), "ready content replaces the preview with a short fade");
    assert.ok(transitionFrames.every(frame => frame.cover || frame.cards > 0), "no empty live-library frame is exposed");
    assert.ok(transitionFrames.some(frame => !frame.cover && frame.cards === 40), "the cover releases to the complete real list");
    const revealed = transitionFrames.filter(frame => !frame.cover && frame.cards === 40);
    assert.ok(revealed.some(frame => frame.thumbnails.some(image => image.cached)),
      "slow live covers retain cached thumbnails without delaying the page");
    assert.ok(revealed.every(frame => frame.thumbnails.every(image => !cachedIds.includes(image.key) || image.pixels)),
      "cached cards never expose the placeholder while live thumbnails decode");
    assert.ok(pixelFrames.length >= 3, "capture actual compositor frames across the handoff");
    assert.ok(pixelFrames.every(frame => frame.ink > .0002), "no captured frame loses all readable content");
  }
  // Production bundles, not Vite's transformed source: unloaded pages must stay unloaded.
  const assets = () => page.evaluate(() => performance.getEntriesByType("resource").map(entry => entry.name));
  const initialAssets = await assets();
  assert.ok(!initialAssets.some(url => /\/(?:SettingsPage|ReviewPage|ProcessPage|BatchProcessPage|TaskDetails|runtime)-.*\.js/.test(url)),
    "non-library pages and the Three.js runtime are not needed for first paint");
  await page.screenshot({ path: path.join(fixture.project, "startup-live-library.png") });
  const navigationCount = await page.evaluate(() => performance.getEntriesByType("navigation").length);
  await page.evaluate(() => window.pywebview.api.hide_to_tray());
  await until(() => ipc("status"), state => !state.visible);
  const wake = Date.now();
  await ipc("activate");
  await until(() => ipc("status"), state => state.visible);
  const wakeMs = Date.now() - wake;
  assert.equal(await page.evaluate(() => performance.getEntriesByType("navigation").length), navigationCount);
  await page.getByRole("link", { name: "设置", exact: true }).click();
  await page.getByRole("heading", { name: "设置", exact: true }).waitFor();
  assert.ok((await assets()).some(url => /\/SettingsPage-.*\.js/.test(url)));
  await page.reload();
  await page.getByRole("heading", { name: "设置", exact: true }).waitFor();
  assert.equal(await page.locator("#classroom-startup-cover").count(), 0, "later navigation never replays the startup cover");
  assert.equal(await page.evaluate(() => window.__classroomStartupView === undefined), true);
  assert.deepEqual(disconnected, []);
  await ipc("exit");
  await until(async () => !fs.existsSync(path.join(fixture.project, ".runtime/desktop_instance.json")), Boolean);
  const result = { passed: true, project: fixture.project, previewMs, libraryMs, artificialBackendDelayMs: backendDelayMs, wakeMs,
    transitionFrames: { sampled: transitionFrames?.length ?? 0,
      covered: transitionFrames?.filter(frame => frame.cover).length ?? 0,
      fading: transitionFrames?.filter(frame => frame.phase === "fading").length ?? 0,
      compositorFrames: pixelFrames.length },
    checks: [...(backendDelayMs > 0 ? ["read-only cached preview before backend", "4 PC widths", "no mandatory animation",
      "visible-card scroll handoff", "refresh preserves scroll", "no blank live frames", "160ms ready-content fade",
      "slow thumbnail handoff never exposes placeholder"] : []), "production lazy chunks",
      "settings load on demand", "tray restores without reload", "owned services exit safely"] };
  fs.writeFileSync(path.join(fixture.project, "startup-qa.json"), JSON.stringify(result, null, 2));
  console.log(JSON.stringify(result));
}
main().catch(async error => {
  console.error(error);
  process.exitCode = 1;
  if (observedPage) {
    try {
      const details = await observedPage.evaluate(handoff => ({
        handoff, scroll: scrollY,
        anchor: handoff?.anchor ? document.querySelector(`[data-library-key="${handoff.anchor}"]`)?.getBoundingClientRect().toJSON() : null,
        frames: window.__qaStartupFrames,
        startupView: window.__classroomStartupView,
        carriedView: window.__qaCarriedView,
        inputs: window.__qaStartupInputs,
        navigation: Object.fromEntries(Object.keys(sessionStorage).filter(key => key.startsWith("classroom-library-navigation:"))
          .map(key => [key, sessionStorage.getItem(key)])),
      }), expectedHandoff);
      fs.writeFileSync(path.join(fixture.project, "startup-failure.json"), JSON.stringify(details, null, 2));
      await observedPage.screenshot({ path: path.join(fixture.project, "startup-failure.png") });
    } catch {}
  }
}).finally(async () => {
  if (native && fs.existsSync(path.join(fixture.project, ".runtime/desktop_instance.json"))) {
    try {
      await until(() => ipc("status"), state => state.ready);
      await ipc("exit");
      await until(async () => !fs.existsSync(path.join(fixture.project, ".runtime/desktop_instance.json")), Boolean);
    } catch (error) { console.error("Disposable startup cleanup needs attention", fixture.project, error); }
  }
  if (browser) await browser.close().catch(() => {});
});
