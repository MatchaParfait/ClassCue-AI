// Read-only QA against the current installation. Never creates, moves or
// processes user videos, saves configuration, or invokes an AI provider.
const { chromium } = require("./playwright.cjs");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

(async () => {
  const origin = process.env.FRONTEND_URL || "http://127.0.0.1:5175";
  const read = async endpoint => {
    const response = await fetch(`${origin}${endpoint}`);
    assert.ok(response.ok, endpoint);
    return response.json();
  };
  const status = await read("/api/library/status");
  const videos = await read("/api/videos");
  const directories = await read("/api/settings/directories");
  assert.ok(videos.length > 0);
  assert.equal(videos.length, status.video_count);
  assert.ok(videos.every(video => video.library_id === status.library_id));
  assert.equal(directories.library_id, status.library_id);
  assert.equal((await fetch(`${origin}/api/settings/workspace`)).status, 404);
  const output = path.resolve(__dirname, `../.runtime/single-library-qa-${Date.now()}`);
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({
    executablePath: "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", headless: true,
  });
  const errors = [], writes = [], failedApi = [];
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    page.on("pageerror", error => errors.push(error.message));
    page.on("response", response => {
      if (response.url().includes("/api/") && response.status() >= 400) failedApi.push(response.url());
    });
    await page.route("**/api/**", route => {
      if (route.request().method() !== "GET") {
        writes.push(route.request().url());
        return route.abort();
      }
      return route.continue();
    });
    await page.goto(`${origin}/settings`);
    await page.getByLabel("视频与音频存储目录", { exact: true }).waitFor();
    assert.deepEqual(await page.locator(".settings-navigation button").allTextContents(),
      ["存储与导入", "ASR 模型", "AI 服务"]);
    assert.equal(await page.locator(".workspace-caption").count(), 0);
    assert.equal(await page.getByRole("button", { name: "新建工作区", exact: true }).count(), 0);
    assert.ok((await page.locator(".storage-derived-paths").first().textContent()).includes(status.data_root));
    for (const width of [1440, 1024, 390]) {
      await page.setViewportSize({ width, height: 1000 });
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${width}: overflow`);
      await page.screenshot({ path: path.join(output, `storage-${width}.png`), fullPage: true });
    }
    const cache = page.getByLabel("视频与音频存储目录", { exact: true });
    const originalCache = await cache.inputValue();
    await cache.fill(`${originalCache}-unsaved`);
    await page.getByRole("button", { name: "AI 服务", exact: true }).click();
    await page.getByRole("button", { name: "存储与导入", exact: true }).click();
    assert.equal(await cache.inputValue(), `${originalCache}-unsaved`);
    await page.getByRole("button", { name: "撤销未保存的目录修改", exact: true }).click();
    assert.equal(await cache.inputValue(), originalCache);

    const uploaded = videos.find(video => video.status === "uploaded");
    if (uploaded) {
      await page.goto(`${origin}/process?videoId=${uploaded.id}`);
      const preview = page.locator(".process-preview-player");
      await preview.waitFor();
      assert.equal(await preview.getAttribute("src"), `/api/videos/${uploaded.id}/stream`);
      assert.equal(await preview.evaluate(video => video.controls && video.paused), true);
    }
    const reviewed = videos.find(video => video.has_transcript);
    if (reviewed) {
      const subtitles = await read(`/api/videos/${reviewed.id}/subtitles`);
      const rows = Array.isArray(subtitles) ? subtitles : subtitles.items ?? subtitles.subtitles;
      const query = rows.map(row => row.text?.match(/[\u4e00-\u9fff]{2,5}/)?.[0]).find(Boolean);
      assert.ok(query, "Requires one saved subtitle for search QA");
      await page.goto(origin);
      await page.getByRole("button", { name: "全局搜索字幕", exact: true }).click();
      await page.getByRole("combobox", { name: "搜索所有视频的字幕", exact: true }).fill(query);
      const result = page.locator(".global-search-hit").first();
      await result.waitFor();
      await result.click();
      await page.waitForURL(url => url.pathname.startsWith("/review/") && url.searchParams.get("library_id") === status.library_id);
      await page.locator(".video-frame").waitFor();
    }
    assert.deepEqual(errors, []);
    assert.deepEqual(writes, []);
    assert.deepEqual(failedApi, []);
    const after = await read("/api/library/status");
    assert.equal(after.library_id, status.library_id);
    assert.deepEqual(await read("/api/videos"), videos);
    assert.deepEqual(await read("/api/settings/directories"), directories);
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({
      passed: true, video_count: status.video_count, library_id: status.library_id, errors, writes, failedApi,
      checks: ["Single library status", "Removed workspace endpoint", "Three settings categories",
        "Default storage page", "1440/1024/390 layout", "Directory drafts persist", "Playable unprocessed video",
        "Real subtitle search and library-scoped review navigation", "No data/config writes"],
    }, null, 2));
    console.log(JSON.stringify({ passed: true, output, video_count: status.video_count }, null, 2));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
