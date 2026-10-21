// Uses an existing unprocessed video read-only. Never imports, processes or
// modifies a video; block all API writes and verify the status stays unchanged.
const { chromium } = require("./playwright.cjs");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

(async () => {
  const origin = process.env.FRONTEND_URL || "http://127.0.0.1:5175";
  const raw = await (await fetch(`${origin}/api/videos`)).json();
  const videos = Array.isArray(raw) ? raw : raw.videos;
  const id = Number(process.env.VIDEO_PREVIEW_ID || 3);
  const source = videos.find(video => video.id === id && video.status === "uploaded")
    ?? videos.find(video => video.status === "uploaded");
  assert.ok(source, "Playback QA requires an imported, unprocessed video");
  const output = path.resolve(__dirname, `../.runtime/process-preview-${Date.now()}`);
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ executablePath: "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", headless: true });
  const errors = [], writes = [], streams = [];
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    page.on("pageerror", error => errors.push(error.message));
    page.on("response", response => {
      if (new URL(response.url()).pathname === `/api/videos/${source.id}/stream`) streams.push(response.status());
    });
    await page.route("**/api/**", route => {
      if (route.request().method() !== "GET") { writes.push(route.request().url()); return route.abort(); }
      return route.continue();
    });
    await page.goto(origin);
    if (source.folder_id) await page.locator(`[data-library-key="folder-${source.folder_id}"] .folder-card-link`).click();
    await page.locator(`[data-library-key="video-${source.id}"] .library-thumbnail-link`).click();
    const preview = page.locator(".workbench-screen:not([hidden]) .process-preview-player");
    await preview.waitFor();
    await preview.evaluate(node => new Promise(resolve => {
      if (node.readyState >= 1) return resolve();
      node.addEventListener("loadedmetadata", resolve, { once: true });
    }));
    assert.equal(await preview.getAttribute("src"), `/api/videos/${source.id}/stream`);
    assert.equal(await preview.evaluate(node => node.controls && node.paused && !node.autoplay), true);
    await preview.evaluate(async node => { node.muted = true; window.__processPreview = node; await node.play(); });
    await page.waitForFunction(() => window.__processPreview.currentTime > .2 && !window.__processPreview.paused);
    await preview.evaluate(node => { node.currentTime = Math.min(30, node.duration / 2); });
    await page.waitForFunction(() => !window.__processPreview.seeking && window.__processPreview.currentTime > 1);
    await page.locator('.workbench-topbar a[href="/settings"]').click();
    await page.waitForFunction(() => window.__processPreview.paused);
    const pausedAt = await page.evaluate(() => window.__processPreview.currentTime);
    await page.locator(".workbench-screen:not([hidden]) .page-back-row button").click();
    await preview.waitFor();
    assert.equal(await preview.evaluate(node => node === window.__processPreview && node.paused), true);
    assert.ok(Math.abs(await preview.evaluate(node => node.currentTime) - pausedAt) < .1);
    await preview.evaluate(node => { node.dispatchEvent(new Event("error")); });
    await page.getByRole("alert").filter({ hasText: "视频暂时无法播放" }).waitFor();
    await page.getByRole("button", { name: "重新加载视频", exact: true }).click();
    await page.waitForFunction(() => window.__processPreview.readyState >= 2);
    assert.equal(await page.locator(".process-preview-error").count(), 0);
    await preview.evaluate(async node => { node.muted = true; await node.play(); });
    await page.waitForFunction(() => !window.__processPreview.paused && window.__processPreview.currentTime > .2);
    await preview.evaluate(node => node.pause());
    for (const width of [1440, 1024, 390]) {
      await page.setViewportSize({ width, height: 1000 });
      const rect = await preview.boundingBox();
      assert.ok(Math.abs(rect.width / rect.height - 16 / 9) < .02);
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${width}: overflow`);
      await page.screenshot({ path: path.join(output, `process-video-${width}.png`), fullPage: true });
    }
    assert.ok(streams.includes(206), "Seeking should use the existing HTTP range stream");
    assert.deepEqual(errors, []);
    assert.deepEqual(writes, []);
    const after = await (await fetch(`${origin}/api/videos/${source.id}`)).json();
    assert.equal(after.status, source.status);
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ passed: true, output, videoId: source.id, streams, errors, writes,
      checks: ["Unprocessed library card opens playable video", "Real playback", "Seek through HTTP range",
        "No autoplay", "Pause on settings navigation", "Same player and position after return",
        "Error and local reload", "Playback after retry", "1440/1024/390 layout", "Original status unchanged"] }, null, 2));
    console.log(JSON.stringify({ passed: true, output, videoId: source.id, streams, errors, writes }, null, 2));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
