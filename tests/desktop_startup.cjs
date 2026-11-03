// Optional native acceptance harness; all media, settings and services are disposable.
const { chromium } = require("./playwright.cjs");
const { spawn, spawnSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const net = require("node:net");
const assert = require("node:assert/strict");
const root = path.resolve(__dirname, "..");
const python = path.join(root, ".venv/Scripts/python.exe");
const pythonw = path.join(root, ".venv/Scripts/pythonw.exe");
const fixtureRun = spawnSync(python, ["tests/fixtures/desktop_application.py"],
  { cwd: root, encoding: "utf8", windowsHide: true });
assert.equal(fixtureRun.status, 0, fixtureRun.stderr);
const fixture = JSON.parse(fixtureRun.stdout.trim().split("\n").at(-1));
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
let browser, native;

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
  const env = { ...process.env, CLASSROOM_DATA_ROOT: fixture.data, CLASSROOM_API_PORT: String(apiPort),
    CLASSROOM_FRONTEND_PORT: String(frontendPort), CLASSROOM_DESKTOP_DEBUG_PORT: String(debugPort) };
  for (const key of Object.keys(env)) if (/_API_KEY$/.test(key)) delete env[key];
  const seed = spawnSync(python, ["-c", `
from pathlib import Path
from classcue.video_store import VideoStore
import os
store=VideoStore(Path(os.environ["CLASSROOM_DATA_ROOT"])/"project.db")
video=dict(store.get_video(1))
for index in range(2,13):
    store.create_video(f"startup-qa-{index}",f"lesson-{index}.mp4",video["video_path"],"unused.wav",3000)
`], { cwd: fixture.project, env, encoding: "utf8", windowsHide: true });
  assert.equal(seed.status, 0, seed.stderr);
  const serviceFile = path.join(fixture.project, "scripts/api_service.py");
  fs.writeFileSync(serviceFile, fs.readFileSync(serviceFile, "utf8").replace(
    "import argparse", `import time\ntime.sleep(${backendDelayMs / 1000})\nimport argparse`));
  const started = Date.now();
  native = spawn(pythonw, [path.join(fixture.project, "scripts/desktop_app.py")],
    { cwd: fixture.project, env, windowsHide: true, stdio: "ignore" });
  await until(async () => (await fetch(`http://127.0.0.1:${debugPort}/json/version`)).ok, Boolean);
  browser = await chromium.connectOverCDP(`http://127.0.0.1:${debugPort}`);
  const page = browser.contexts()[0].pages()[0];
  const pageErrors = [];
  page.on("pageerror", error => pageErrors.push(error.message));
  await page.route("**/api/videos/*/thumbnail?**", async route => {
    await wait(1500);
    await route.continue().catch(() => {});
  });
  await page.addInitScript(() => {
    if (window !== window.top) return;
    window.__qaStartupFrames = [];
    const sample = () => {
      window.__qaStartupFrames.push({
        time: performance.now(),
        cover: !!document.getElementById("classroom-startup-cover"),
        phase: document.getElementById("classroom-startup-cover")?.dataset.phase ?? null,
        cards: document.querySelectorAll(".library-inner .video-card").length,
        visibleImages: [...document.querySelectorAll(".library-inner .video-thumbnail img")].filter(image => {
          const bounds = image.getBoundingClientRect();
          return bounds.bottom > 0 && bounds.top < innerHeight;
        }).map(image => ({ complete: image.complete, width: image.naturalWidth })),
      });
      if (window.__qaStartupFrames.length < 360) requestAnimationFrame(sample);
    };
    requestAnimationFrame(sample);
  });
  let artworkSource = null, sceneId = null, artworkMs = null;
  if (backendDelayMs > 0) {
    await page.getByRole("main", { name: "应用启动画面" }).waitFor();
    const artwork = page.locator(".startup-art");
    await artwork.evaluate(image => image.decode());
    artworkSource = await artwork.getAttribute("src");
    sceneId = await page.locator(".startup-screen").getAttribute("data-scene-id");
    artworkMs = Date.now() - started;
    assert.ok(artworkSource.startsWith("data:image/jpeg;base64,"));
    assert.ok(sceneId);
    assert.equal(await page.locator(".startup-art").count(), 1);
    assert.equal(await page.locator("[data-library-key]").count(), 0);
    assert.equal(await page.evaluate(() => document.getAnimations().length), 0);
    await wait(500);
    assert.equal(await artwork.getAttribute("src"), artworkSource, "one launch keeps one image");
    await page.screenshot({ path: path.join(fixture.project, "startup-artwork.png") });
  }
  await until(() => ipc("status"), value => value.ready && value.window_ready);
  if (artworkSource) {
    const cover = page.locator("#classroom-startup-cover");
    await cover.waitFor();
    assert.ok((await cover.locator("iframe").getAttribute("srcdoc")).includes(artworkSource),
      "the same artwork crosses navigation");
  }
  await page.locator(".library-inner .video-card").first().waitFor();
  await page.locator("#classroom-startup-cover").waitFor({ state: "detached", timeout: 30000 });
  assert.equal(await page.locator(".library-inner .video-card").count(), 12);
  assert.equal(await page.evaluate(() => !!document.getElementById("root").inert), false);
  const frames = await page.evaluate(() => window.__qaStartupFrames);
  if (artworkSource) {
    assert.ok(frames.some(frame => frame.cover));
    assert.ok(frames.every(frame => frame.cover || frame.cards > 0), "no partial empty page is exposed");
    assert.ok(frames.filter(frame => !frame.cover && frame.cards > 0)
      .every(frame => frame.visibleImages.every(image => image.complete && image.width > 0)),
      "visible thumbnails finish before reveal");
  }
  await page.screenshot({ path: path.join(fixture.project, "startup-live-library.png") });
  const libraryMs = Date.now() - started;
  const resources = () => page.evaluate(() => performance.getEntriesByType("resource").map(entry => entry.name));
  assert.ok(!(await resources()).some(url =>
    /\/(?:SettingsPage|ReviewPage|ProcessPage|BatchProcessPage|runtime)-.*\.js/.test(url)));
  const navigationCount = await page.evaluate(() => performance.getEntriesByType("navigation").length);
  await until(() => ipc("status"), state => state.tray);
  await page.evaluate(() => window.pywebview.api.hide_to_tray());
  await until(() => ipc("status"), state => !state.visible);
  const wake = Date.now();
  await ipc("activate");
  await until(() => ipc("status"), state => state.visible);
  const wakeMs = Date.now() - wake;
  assert.equal(await page.evaluate(() => performance.getEntriesByType("navigation").length), navigationCount);
  await page.getByRole("link", { name: "设置", exact: true }).click();
  await page.getByRole("heading", { name: "设置", exact: true }).waitFor();
  await page.reload();
  await page.getByRole("heading", { name: "设置", exact: true }).waitFor();
  assert.equal(await page.locator("#classroom-startup-cover").count(), 0);
  assert.deepEqual(pageErrors, []);
  await ipc("exit");
  await until(async () => !fs.existsSync(path.join(fixture.project, ".runtime/desktop_instance.json")), Boolean);
  const result = { passed: true, project: fixture.project, artworkMs, libraryMs, artificialBackendDelayMs: backendDelayMs,
    sceneId, wakeMs, sampledFrames: frames.length,
    checks: ["one image per launch", "same artwork across navigation", "ready images before reveal",
      "no blank live frames", "production lazy chunks", "tray restore without reload", "owned services stop"] };
  fs.writeFileSync(path.join(fixture.project, "startup-qa.json"), JSON.stringify(result, null, 2));
  console.log(JSON.stringify(result));
}

main().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
  if (native && fs.existsSync(path.join(fixture.project, ".runtime/desktop_instance.json"))) {
    try {
      await until(() => ipc("status"), state => state.ready);
      await ipc("exit");
      await until(async () => !fs.existsSync(path.join(fixture.project, ".runtime/desktop_instance.json")), Boolean);
    } catch (error) { console.error("Disposable startup cleanup needs attention", fixture.project, error); }
  }
  if (browser) await browser.close().catch(() => {});
});
