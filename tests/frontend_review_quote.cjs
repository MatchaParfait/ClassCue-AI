// Read-only layout regression: expanding evidence must reveal more wrapped text.
const { chromium } = require("./playwright.cjs");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

(async () => {
  const origin = process.env.FRONTEND_URL || "http://127.0.0.1:5175";
  const videoId = Number(process.env.REVIEW_VIDEO_ID || 4);
  const output = path.resolve(__dirname, `../.runtime/review-quote-${Date.now()}`);
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({
    ...(process.env.CLASSROOM_BROWSER_EXECUTABLE
      ? { executablePath: process.env.CLASSROOM_BROWSER_EXECUTABLE } : {}),
    headless: true,
  });
  const errors = [], writes = [], results = [];
  try {
    const page = await browser.newPage({ viewport: { width: 676, height: 872 } });
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/api/**", route => {
      if (route.request().method() !== "GET") {
        writes.push(route.request().url());
        return route.abort();
      }
      return route.continue();
    });
    await page.goto(`${origin}/review/${videoId}`);
    const toggle = page.locator(".quote-toggle").first();
    await toggle.waitFor();
    await page.locator("video").evaluate(video => video.pause());
    await page.evaluate(() => document.fonts.ready);
    const evidence = toggle.locator("..");
    const dimensions = () => evidence.evaluate(node => {
      const quote = node.querySelector(".marker-quote");
      const wrapper = node.querySelector(".animated-quote");
      return {
        height: wrapper.getBoundingClientRect().height,
        fullHeight: quote.scrollHeight,
        width: quote.clientWidth,
        fullWidth: quote.scrollWidth,
        lineHeight: parseFloat(getComputedStyle(quote).lineHeight),
      };
    });
    for (const reducedMotion of ["no-preference", "reduce"]) {
      await page.emulateMedia({ reducedMotion });
      for (const width of [676, 1440, 390]) {
        await page.setViewportSize({ width, height: 872 });
        await page.waitForTimeout(250);
        const before = await dimensions();
        assert.ok(before.height <= before.lineHeight * 2 + 1, "Collapsed evidence uses at most two lines");
        const playerTime = await page.locator("video").evaluate(video => video.currentTime);
        await toggle.click();
        await page.waitForTimeout(250);
        assert.equal(await toggle.getAttribute("aria-expanded"), "true");
        const expanded = await dimensions();
        assert.ok(expanded.height > before.height + 1, `${width}/${reducedMotion}: clicking must reveal more text`);
        assert.ok(Math.abs(expanded.height - expanded.fullHeight) <= 1, "Expanded evidence reveals the full quote");
        assert.ok(expanded.fullWidth <= expanded.width + 1, "Original text wraps within the card");
        assert.equal(await page.locator("video").evaluate(video => video.currentTime), playerTime);
        if (reducedMotion === "no-preference" && width === 676) {
          await evidence.screenshot({ path: path.join(output, "expanded.png") });
        }
        await toggle.click();
        await page.waitForTimeout(250);
        assert.equal(await toggle.getAttribute("aria-expanded"), "false");
        const collapsed = await dimensions();
        assert.ok(Math.abs(collapsed.height - before.height) <= 1, "Collapse restores the preview height");
        results.push({ width, reducedMotion, before, expanded, collapsed });
      }
    }
    assert.deepEqual(errors, []);
    assert.deepEqual(writes, []);
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ results, errors, writes }, null, 2));
    console.log(JSON.stringify({ passed: true, output, cases: results.length }));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
