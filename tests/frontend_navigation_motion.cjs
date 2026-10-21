// Read-only navigation QA. Configuration replies are isolated; no business writes
// or model calls are permitted. Existing video media is used only for playback.
const { chromium } = require("./playwright.cjs");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

(async () => {
  const origin = process.env.FRONTEND_URL || "http://127.0.0.1:5175";
  const output = path.resolve(__dirname, `../.runtime/navigation-motion-${Date.now()}`);
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ executablePath: "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", headless: true });
  let context;
  const errors = [], writes = [], motions = [];
  try {
    context = await browser.newContext({ viewport: { width: 1440, height: 1000 },
      recordVideo: { dir: output, size: { width: 1440, height: 1000 } } });
    await context.addInitScript(() => {
      window.__entryMotions = [];
      const animate = Element.prototype.animate;
      Element.prototype.animate = function (frames, options) {
        const entry = { target: this.className, frames, duration: options?.duration, delay: options?.delay };
        window.__entryMotions.push(entry);
        const animation = animate.call(this, frames, options);
        queueMicrotask(() => { entry.afterSetup = animation.playState; });
        return animation;
      };
    });
    const model = { id: "whisper-turbo", name: "Whisper turbo", engine: "whisper",
      downloaded: true, selected: true, cache_size_bytes: 1024 };
    await context.route("**/api/**", route => {
      const request = route.request(), pathname = new URL(request.url()).pathname;
      if (request.method() !== "GET") { writes.push(`${request.method()} ${pathname}`); return route.abort(); }
      const json = value => route.fulfill({ json: value });
      if (pathname === "/api/asr/selection" || pathname.startsWith("/api/asr/models/")) return json(model);
      if (pathname === "/api/asr/models") return json([model]);
      if (pathname === "/api/settings/ai") return json({
        provider: "openai_compatible", model: "当前 AI 连接", base_url: "https://unused.example/v1",
        output_tokens: 16384, token_parameter: "max_tokens", json_mode: false,
        timeout_seconds: 120, max_retries: 5, has_api_key: false,
      });
      if (pathname.endsWith("/processing-options")) return json({ asr_model: model, active_job: null });
      return route.continue();
    });
    const page = await context.newPage();
    page.on("pageerror", error => errors.push(error.message));
    const collectMotions = async () => motions.push(...await page.evaluate(() => window.__entryMotions.splice(0)));
    const screen = () => page.locator(".workbench-screen:not([hidden])");
    const clickNow = locator => locator.evaluate(node => node.click());
    const back = () => clickNow(screen().locator(".page-back-row button"));
    const frame = async name => {
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${name}: overflow`);
      await page.screenshot({ path: path.join(output, `${name}.png`), fullPage: true });
    };
    await page.goto(origin);
    await page.locator(".library-heading").waitFor();
    await clickNow(page.locator('.library-heading-actions a[href="/process"]'));
    await screen().locator("#video-path").waitFor({ state: "attached" });
    assert.ok(await page.evaluate(() => window.__entryMotions.some(entry =>
      entry.target === "workbench-screen" && entry.duration === 160 && entry.afterSetup === "running")));
    await screen().locator(".video-path-details summary").click();
    await screen().locator("#video-path").fill("D:\\motion-preserved.mp4");
    await page.evaluate(() => {
      window.__draft = document.querySelector("#video-path");
      window.__processScreen = document.querySelector(".workbench-screen:not([hidden])");
      window.scrollTo(0, 100);
      window.__processScroll = scrollY;
    });
    await clickNow(page.locator('.workbench-topbar a[href="/settings"]'));
    await screen().locator(".settings-navigation").waitFor();
    await page.getByRole("button", { name: "AI 服务", exact: true }).click();
    assert.ok(await page.evaluate(() => window.__entryMotions.some(entry =>
      entry.target === "settings-section-heading" && entry.duration === 140 && entry.delay === 0)));
    await page.waitForFunction(() => window.__entryMotions.some(entry =>
      entry.target === "ai-connection-editor" && entry.duration === 140 && entry.delay === 20));
    await page.getByRole("button", { name: "ASR 模型", exact: true }).click();
    await page.getByRole("button", { name: "AI 服务", exact: true }).click();
    await frame("settings-1440");
    await back();
    await page.waitForURL("**/process");
    assert.ok(await page.evaluate(() => window.__draft === document.querySelector("#video-path")
      && window.__draft.value === "D:\\motion-preserved.mp4"
      && window.__processScreen === document.querySelector(".workbench-screen:not([hidden])")
      && Math.abs(scrollY - window.__processScroll) <= 1));
    for (let i = 0; i < 3; i++) {
      await clickNow(page.locator('.workbench-topbar a[href="/settings"]'));
      await back();
    }
    await page.waitForTimeout(220);
    assert.equal(await screen().count(), 1);
    assert.ok(await page.evaluate(() => window.__draft === document.querySelector("#video-path")));
    const pageEntries = await page.evaluate(() => window.__entryMotions.filter(entry => entry.target === "text-button"
      && entry.frames[0].transform?.startsWith("translateX")));
    assert.ok(pageEntries.some(entry => entry.frames[0].transform === "translateX(6px)"));
    assert.ok(pageEntries.some(entry => entry.frames[0].transform === "translateX(-6px)"));
    await frame("process-1440");
    await back();
    await page.waitForURL(`${origin}/`);
    assert.ok(await page.evaluate(() => window.__entryMotions.some(entry => entry.target === "library-inner" && entry.duration === 140)));
    await page.locator(".library-card-process").first().click();
    await page.locator(".library-processing-art").waitFor();
    assert.ok(await page.evaluate(() => window.__entryMotions.some(entry =>
      entry.target === "library-processing-art" && entry.duration === 160 && entry.delay === 20)));
    await frame("processing-companion-1440");
    await page.getByRole("button", { name: "收起处理选项", exact: true }).click();
    if (await page.locator(".folder-card-link").count()) {
      await page.locator(".folder-card-link").first().click();
      await page.locator(".folder-window").waitFor();
      assert.match(await page.locator(".folder-window").evaluate(node => node.style.transformOrigin), /%/);
      await frame("folder-1440");
      await page.keyboard.press("Escape");
      await page.locator(".folder-window").waitFor({ state: "detached" });
    }
    await page.getByRole("button", { name: "查看任务详情", exact: true }).click();
    await page.getByRole("button", { name: "关闭面板", exact: true }).click();
    await clickNow(page.getByRole("button", { name: "查看任务详情", exact: true }));
    await page.waitForTimeout(240);
    assert.equal(await page.locator(".workbench-panel-backdrop").evaluate(node => node.inert || node.hidden), false);
    await frame("tasks-1440");
    await page.getByRole("button", { name: "关闭面板", exact: true }).click();
    await page.locator(".workbench-panel-backdrop").waitFor({ state: "hidden" });

    let query = process.env.SEARCH_PREVIEW_QUERY || "汉诺塔";
    let search = await (await page.request.get(`${origin}/api/library/search?${new URLSearchParams({ q: query })}`)).json();
    if (!search.items?.length) {
      query = "的";
      search = await (await page.request.get(`${origin}/api/library/search?${new URLSearchParams({ q: query })}`)).json();
    }
    assert.ok(search.items?.length, "Read-only playback QA requires saved subtitles");
    const hit = search.items[0];
    await collectMotions();
    await page.goto(`${origin}/review/${hit.video_id}`);
    await page.waitForFunction(() => document.querySelector("video")?.readyState >= 1);
    await page.locator("video").evaluate(node => { node.pause(); node.currentTime = 35; window.__player = node; });
    await page.getByRole("button", { name: "全局搜索字幕", exact: true }).click();
    await page.getByRole("combobox", { name: "搜索所有视频的字幕" }).fill(query);
    const row = page.locator(".global-search-dialog [role=option]").first();
    await row.hover();
    await page.locator(".global-search-preview img.is-ready").waitFor();
    assert.ok(await page.evaluate(() => document.querySelector("video") === window.__player
      && window.__player.paused && Math.abs(window.__player.currentTime - 35) < .1));
    assert.ok(await page.evaluate(() => window.__entryMotions.some(entry => entry.target === "global-search-preview" && entry.duration === 120)));
    await frame("search-preview-1440");
    await clickNow(row);
    await page.waitForURL(url => url.pathname === `/review/${hit.video_id}` && url.searchParams.get("start_ms") === String(hit.start_ms));
    assert.equal(await page.locator(".search-page-background").evaluate(node => node.inert), false);
    await page.locator(".global-search-dialog").waitFor({ state: "detached" });
    assert.ok(await page.evaluate(() => document.querySelector("video") === window.__player));
    await frame("review-1440");

    await collectMotions();
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto(origin);
    await page.locator(".library-heading").waitFor();
    const count = await page.evaluate(() => window.__entryMotions.length);
    await clickNow(page.locator('.library-heading-actions a[href="/process"]'));
    await screen().locator("#video-path").waitFor({ state: "attached" });
    await back();
    assert.equal(await page.evaluate(() => window.__entryMotions.length), count);
    await page.emulateMedia({ reducedMotion: "no-preference" });
    for (const width of [1024, 390]) {
      await page.setViewportSize({ width, height: 900 });
      await clickNow(page.locator('.library-heading-actions a[href="/process"]'));
      await screen().locator("#video-path").waitFor({ state: "attached" });
      await frame(`process-${width}`);
      await back();
      await frame(`library-${width}`);
    }
    assert.deepEqual(errors, []);
    assert.deepEqual(writes, []);
    await collectMotions();
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ passed: true, output, errors, writes, pageEntries, motions,
      checks: ["Forward and backward entry", "Same cached form and draft", "Scroll restored", "Rapid interrupted navigation",
        "Library return fades without card replay", "Folder origin", "Drawer reopens during exit",
        "Search preview leaves player stable", "Search jump does not wait for exit", "Same-video search keeps player",
        "Reduced motion skips entries", "1440/1024/390 layouts", "No business writes or paid model calls"] }, null, 2));
    console.log(JSON.stringify({ passed: true, output, errors, writes }, null, 2));
  } finally {
    if (context) await context.close();
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
