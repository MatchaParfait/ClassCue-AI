// Exercise the standalone handoff with a synthetic API and desktop IPC.
const { chromium } = require("./playwright.cjs");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const [origin, target, output] = process.argv.slice(2);

(async () => {
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  let posts = 0;
  page.on("request", request => {
    if (request.url() === origin + "/api/desktop-links/open" && request.method() === "POST") posts++;
  });
  const checkLayout = async () => {
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
    assert.equal(await page.locator("header img").evaluate(image => image.complete && image.naturalWidth > 0), true);
    const [heading, targetBox, message] = await Promise.all([
      page.locator("h1").boundingBox(), page.locator(".target").boundingBox(), page.locator("#message").boundingBox(),
    ]);
    assert.ok(heading.y + heading.height <= targetBox.y && targetBox.y + targetBox.height <= message.y);
  };
  try {
    await page.goto(target);
    await page.getByRole("heading", { name: "已发送到桌面版" }).waitFor();
    assert.equal(posts, 1);
    assert.equal(await page.locator("#web").isVisible(), false);
    assert.equal(await page.locator("#retry").isVisible(), false);
    await checkLayout();
    await page.screenshot({ path: path.join(output, "desktop-opened.png") });

    const requestPath = "**/api/desktop-links/open";
    await page.route(requestPath, async route => {
      const payload = route.request().postDataJSON();
      const query = new URLSearchParams({ start_ms: String(payload.start_ms), library_id: payload.library_id });
      await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({
        code: "desktop_unavailable", message: "桌面有未保存的修改，请先保存后再打开视频。",
        web_url: `/review/${payload.video_id}?${query}`,
      }) });
    });
    await page.goto(target);
    await page.getByRole("heading", { name: "暂时无法打开桌面版" }).waitFor();
    assert.equal(await page.getByRole("link", { name: "在网页中继续" }).isVisible(), true);
    assert.equal(new URL(page.url()).pathname, new URL(target).pathname);
    await checkLayout();
    await page.screenshot({ path: path.join(output, "desktop-blocked.png") });
    await page.setViewportSize({ width: 320, height: 640 });
    await checkLayout();
    await page.screenshot({ path: path.join(output, "mobile-blocked.png") });
    const fallback = await page.getByRole("link", { name: "在网页中继续" }).getAttribute("href");
    await page.route(origin + "/review/**", route => route.fulfill({
      contentType: "text/html", body: "<h1>synthetic web fallback</h1>",
    }));
    await page.getByRole("link", { name: "在网页中继续" }).click();
    await page.getByRole("heading", { name: "synthetic web fallback" }).waitFor();
    assert.equal(page.url(), origin + fallback);
    await page.goto(target);
    await page.getByRole("heading", { name: "暂时无法打开桌面版" }).waitFor();
    await page.unroute(requestPath);
    await page.getByRole("button", { name: "重试打开桌面版" }).click();
    await page.getByRole("heading", { name: "已发送到桌面版" }).waitFor();
    assert.equal(await page.locator("#web").isVisible(), false);

    await page.route(requestPath, route => route.fulfill({
      status: 409, contentType: "application/json",
      body: JSON.stringify({ detail: { code: "library_changed", message: "视频库已变化，请重新检索回放。" } }),
    }));
    await page.goto(target);
    await page.getByRole("alert").filter({ hasText: "视频库已变化" }).waitFor();
    assert.equal(await page.locator("#web").isVisible(), false);
    assert.equal(await page.locator("#retry").isVisible(), false);

    await page.unroute(requestPath);
    await page.route(requestPath, route => route.abort());
    await page.goto(target);
    await page.getByRole("heading", { name: "尚未确认是否打开" }).waitFor();
    assert.equal(await page.locator("#web").isVisible(), false);
    assert.equal(await page.locator("#retry").isVisible(), true);
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ passed: true, posts, screenshots: 3, output }));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
