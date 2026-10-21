// Exercise real copies and failures only in a disposable installation.
const { chromium } = require("./playwright.cjs");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const net = require("node:net");
const { spawn } = require("node:child_process");

(async () => {
  const root = path.resolve(__dirname, "..");
  const output = path.join(root, ".runtime", `storage-migration-qa-${Date.now()}`);
  fs.mkdirSync(output, { recursive: true });
  const socket = net.createServer();
  await new Promise(resolve => socket.listen(0, "127.0.0.1", resolve));
  const port = socket.address().port;
  await new Promise(resolve => socket.close(resolve));
  const api = `http://127.0.0.1:${port}`;
  const server = spawn(path.join(root, ".venv", "Scripts", "python.exe"),
    [path.join(__dirname, "fixtures", "storage_migration_server.py"), output, String(port)],
    { cwd: root, windowsHide: true, stdio: ["ignore", "pipe", "pipe"] });
  const logs = [];
  server.stdout.on("data", value => logs.push(String(value)));
  server.stderr.on("data", value => logs.push(String(value)));
  let browser;
  let page;
  const errors = [];
  try {
    let ready = false;
    for (let i = 0; i < 100; i++) {
      try { ready = (await fetch(`${api}/api/health`)).ok; } catch {}
      if (ready) break;
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    assert.ok(ready, logs.join(""));
    browser = await chromium.launch({
      executablePath: "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", headless: true,
    });
    page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/api/**", async route => {
      const url = new URL(route.request().url());
      if (!url.pathname.startsWith("/api/")) return route.continue();
      const response = await route.fetch({ url: `${api}${url.pathname}${url.search}` });
      await route.fulfill({ response });
    });
    await page.goto(`${process.env.FRONTEND_URL || "http://127.0.0.1:5175"}/settings?section=storage`);
    const cache = page.getByLabel("视频与音频存储目录", { exact: true });
    await cache.waitFor();
    await cache.fill(path.join(output, "blocked-media"));
    await page.getByRole("button", { name: "检查并迁移", exact: true }).click();
    await page.getByRole("heading", { name: "确认迁移范围" }).waitFor();
    await page.getByRole("button", { name: "开始迁移", exact: true }).click();
    await page.locator(".storage-migration-progress .field-error").waitFor();
    assert.equal(fs.readFileSync(path.join(output, "blocked-media", "videos", "fixture", "lesson.mp4"), "utf8"), "do not overwrite");
    assert.ok(fs.existsSync(path.join(output, "library", "temporary_audio", "lesson.wav")));
    await page.screenshot({ path: path.join(output, "failed-migration.png"), fullPage: true });
    await page.getByRole("button", { name: "关闭提示", exact: true }).click();
    await cache.fill(path.join(output, "new-media"));
    assert.equal(await page.locator('input[aria-label="视频库目录"]').count(), 0);
    assert.equal(await page.locator('input[aria-label="临时音频与转写缓存"]').count(), 0);
    await page.getByLabel("ASR 资源目录", { exact: true }).fill(path.join(output, "new-assets"));
    await page.getByRole("button", { name: "检查并迁移", exact: true }).click();
    await page.getByRole("heading", { name: "确认迁移范围" }).waitFor();
    for (const width of [856, 1316, 1540, 1920]) {
      await page.setViewportSize({ width, height: 1100 });
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${width}: overflow`);
      await page.screenshot({ path: path.join(output, `preview-${width}.png`), fullPage: true });
    }
    await page.setViewportSize({ width: 1440, height: 1100 });
    await page.getByRole("button", { name: "开始迁移", exact: true }).click();
    await page.waitForFunction(() => document.querySelector(".storage-migration-progress")?.textContent.includes("正在复制"));
    await page.screenshot({ path: path.join(output, "progress.png"), fullPage: true });
    await page.reload();
    await page.waitForFunction(() => document.querySelector(".storage-migration-progress")?.textContent.includes("目录迁移完成"));
    await page.waitForFunction(expected => document.querySelector('input[aria-label="视频与音频存储目录"]')?.value === expected, path.join(output, "new-media"));
    await page.screenshot({ path: path.join(output, "completed.png"), fullPage: true });
    const data = await (await fetch(`${api}/api/settings/directories`)).json();
    const videos = await (await fetch(`${api}/api/videos`)).json();
    assert.equal(data.migration_job.status, "completed");
    assert.equal(videos.length, 1);
    assert.ok(videos[0].video_path.startsWith(path.join(output, "new-media", "videos")));
    assert.equal(data.media_storage_root, path.join(output, "new-media"));
    assert.equal(data.media_storage_bound, true);
    assert.equal(data.audio_cache_dir, path.join(output, "new-media", "audio"));
    assert.ok(fs.existsSync(path.join(output, "new-media", "audio", "lesson.wav")));
    assert.equal(fs.readFileSync(videos[0].video_path, "utf8"), "fixture video");
    assert.ok(!fs.existsSync(path.join(output, "library", "temporary_audio", "lesson.wav")));
    assert.ok(fs.existsSync(path.join(output, "assets", ".runtime", "qwen-asr", "ready.json")));
    assert.ok(fs.existsSync(path.join(output, "new-assets", "models", "asr", "fixture", "weights.bin")));
    await page.reload();
    await cache.waitFor();
    assert.equal(await cache.inputValue(), path.join(output, "new-media"));
    assert.equal(await page.locator(".storage-migration-progress").count(), 0);
    await page.screenshot({ path: path.join(output, "completed-after-reload.png"), fullPage: true });
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ passed: true, errors,
      checks: ["one parent derives videos/audio", "conflict preserves both sources and target", "preview at 856/1316/1540/1920", "real copy progress",
        "reload resumes active status", "completed progress hidden after reload", "verified file migration", "video reference updated", "old environment retained"] }, null, 2));
    console.log(output);
  } catch (error) {
    if (page) {
      await page.screenshot({ path: path.join(output, "failure.png"), fullPage: true });
      fs.writeFileSync(path.join(output, "failure.html"), await page.content());
    }
    fs.writeFileSync(path.join(output, "errors.json"), JSON.stringify(errors));
    throw error;
  } finally {
    if (browser) await browser.close();
    server.kill();
    fs.writeFileSync(path.join(output, "server.log"), logs.join(""));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
