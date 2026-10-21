// Read-only browser checks against local media. No processing or marker writes.
const { chromium } = require("./playwright.cjs");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

(async () => {
  const origin = process.env.FRONTEND_URL || "http://127.0.0.1:5175";
  const query = process.env.SEARCH_PREVIEW_QUERY || "汉诺塔";
  const search = await (await fetch(`${origin}/api/library/search?${new URLSearchParams({ q: query })}`)).json();
  assert.ok(search.items?.length >= 3, "Choose a query with at least three saved subtitle hits");
  const videoId = search.items[0].video_id;
  const output = path.resolve(__dirname, `../.runtime/search-preview-qa-${Date.now()}`);
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ executablePath: "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", headless: true });
  const errors = [], mutations = [], previewRequests = [];
  try {
    const context = await browser.newContext({ viewport: { width: 1099, height: 728 } });
    await context.route("**/api/**", route => {
      if (route.request().method() !== "GET") { mutations.push(route.request().url()); return route.abort(); }
      if (route.request().url().includes("/preview-frame?")) previewRequests.push(route.request().url());
      return route.continue();
    });
    const page = await context.newPage();
    page.on("pageerror", error => errors.push(error.message));
    await page.goto(`${origin}/review/${videoId}`);
    await page.waitForFunction(() => {
      const video = document.querySelector("video");
      return video && video.readyState >= 1;
    });
    await page.locator("video").evaluate(video => { video.pause(); video.currentTime = 35; window.__previewMainPlayer = video; });
    await page.getByRole("button", { name: "全局搜索字幕", exact: true }).click();
    const input = page.getByRole("combobox", { name: "搜索所有视频的字幕" });
    await input.fill(query);
    const rows = () => page.locator(".global-search-dialog [role=option]");
    await rows().nth(1).waitFor();
    const second = search.items[1];
    const row = page.locator(`[id="search-hit-${second.video_id}:${second.segment_id}"]`);
    const before = await row.boundingBox();
    const scroll = await page.locator(".global-search-scroll").evaluate(node => node.scrollTop);
    await row.hover();
    await page.locator(".global-search-preview img.is-ready").waitFor();
    assert.equal(new URL(await page.locator(".global-search-preview img").getAttribute("src"), origin).searchParams.get("time_ms"), String(second.start_ms));
    assert.deepEqual(await row.boundingBox(), before, "Preview must not reflow the candidate row");
    assert.equal(await page.locator(".global-search-scroll").evaluate(node => node.scrollTop), scroll);
    assert.equal(await page.locator("video").evaluate(video => video === window.__previewMainPlayer && video.paused && Math.abs(video.currentTime - 35) < .1), true);
    assert.equal(await input.evaluate(node => document.activeElement === node), true);
    const positions = [];
    for (const width of [1099, 1440, 1920, 1024]) {
      await page.setViewportSize({ width, height: 728 });
      await row.hover();
      await page.locator(".global-search-preview img.is-ready").waitFor();
      const preview = await page.locator(".global-search-preview").boundingBox();
      const anchor = await row.boundingBox();
      assert.ok(preview.x >= 0 && preview.y >= 0 && preview.x + preview.width <= width && preview.y + preview.height <= 728);
      assert.ok(preview.y + preview.height <= anchor.y || preview.y >= anchor.y + anchor.height
        || preview.x >= anchor.x + anchor.width || preview.x + preview.width <= anchor.x);
      await page.screenshot({ path: `${output}/hover-${width}.png` });
      positions.push({ width, preview, placement: await page.locator(".global-search-preview").getAttribute("data-placement") });
    }
    await page.locator(".global-search-heading").hover();
    assert.equal(await page.locator(".global-search-preview").count(), 0);

    // Slow replies for an old hover must not replace a newer frame.
    let release;
    const gate = new Promise(resolve => { release = resolve; });
    let slowStarted = false;
    const first = search.items[0];
    await page.route("**/preview-frame?*", async route => {
      if (Number(new URL(route.request().url()).searchParams.get("time_ms")) === first.start_ms) {
        slowStarted = true;
        await gate;
      }
      try { await route.fulfill({ response: await route.fetch() }); } catch { /* The old image can be cancelled after leaving its row. */ }
    });
    await rows().first().hover();
    await page.waitForFunction(() => Boolean(document.querySelector(".global-search-preview img")));
    await page.waitForTimeout(50);
    await row.hover();
    await page.locator(".global-search-preview img.is-ready").waitFor();
    release();
    await page.waitForTimeout(300);
    assert.equal(slowStarted, true);
    assert.equal(new URL(await page.locator(".global-search-preview img").getAttribute("src"), origin).searchParams.get("time_ms"), String(second.start_ms));
    await page.unroute("**/preview-frame?*");

    await page.locator(".global-search-heading").hover();
    await page.route("**/preview-frame?*", route => route.fulfill({ status: 404, json: { detail: "fixture missing frame" } }));
    await rows().nth(2).hover();
    await page.getByText("画面暂不可用，仍可点击字幕跳转", { exact: true }).waitFor();
    await page.screenshot({ path: `${output}/unavailable-frame.png` });
    await page.unroute("**/preview-frame?*");
    await input.fill(`${query}不存在`);
    assert.equal(await page.locator(".global-search-preview").count(), 0);
    await input.fill(query);
    await rows().first().waitFor();
    await input.press("ArrowDown");
    await page.locator(".global-search-preview img.is-ready").waitFor();
    assert.equal(await input.evaluate(node => document.activeElement === node), true);
    await page.screenshot({ path: `${output}/keyboard-preview.png` });
    const selectedId = await page.locator('[role="option"][aria-selected=true]').getAttribute("id");
    const selected = search.items.find(hit => selectedId === `search-hit-${hit.video_id}:${hit.segment_id}`);
    await input.press("Enter");
    await page.locator(".global-search-dialog").waitFor({ state: "detached" });
    assert.equal(await page.locator(".global-search-preview").count(), 0);
    assert.equal(new URL(page.url()).searchParams.get("start_ms"), String(selected.start_ms));

    const mobile = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
    const touch = await mobile.newPage();
    await touch.goto(origin);
    await touch.getByRole("button", { name: "全局搜索字幕", exact: true }).tap();
    await touch.getByRole("combobox", { name: "搜索所有视频的字幕" }).fill(query);
    await touch.locator(".global-search-dialog [role=option]").first().waitFor();
    await touch.locator(".global-search-dialog [role=option]").first().dispatchEvent("pointermove", { pointerType: "touch" });
    await touch.waitForTimeout(250);
    assert.equal(await touch.locator(".global-search-preview").count(), 0);
    assert.equal(await touch.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await touch.screenshot({ path: `${output}/touch-results-390.png` });
    await mobile.close();
    assert.deepEqual(errors, []);
    assert.deepEqual(mutations, []);
    fs.writeFileSync(`${output}/checks.json`, JSON.stringify({ passed: true, output, positions, previewRequests: previewRequests.length,
      checks: ["Correct subtitle timestamp", "No row or scroll reflow", "Player instance, position and paused state preserved",
        "Preview fits viewport and avoids target row", "Large outer preview on wide screens", "Mouse leave dismisses",
        "Late old frames ignored", "Unreadable frames preserve jump", "Query changes clear preview",
        "Keyboard preview and Enter jump", "Touch does not fetch hover previews", "No business mutations"] }, null, 2));
    console.log(JSON.stringify({ passed: true, output, positions }, null, 2));
    await context.close();
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
