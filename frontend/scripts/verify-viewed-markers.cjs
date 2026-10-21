/* Isolated UI checks: never connect to a backend or submit processing jobs. */
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");

async function main() {
  const base = process.argv[2] || "http://127.0.0.1:5174";
  const output = path.resolve(process.argv[3] || "node_modules/.cache/viewed-markers");
  await fs.mkdir(output, { recursive: true });
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const results = [];
  try {
    for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
      const context = await browser.newContext({ viewport });
      const page = await context.newPage();
      const errors = [];
      const apiRequests = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.route((url) => url.origin === new URL(base).origin && url.pathname.startsWith("/api/"), (route) => {
        apiRequests.push(route.request().url());
        return route.abort();
      });
      await page.goto(`${base}/demo`, { waitUntil: "domcontentloaded" });
      const points = page.locator(".marker-main");
      await points.first().waitFor();
      assert.equal(await points.count(), 2);
      assert.equal(await page.locator(".marker-item.is-viewed").count(), 0);
      assert.equal(await page.locator(".status-action").count(), 0);
      const before = await page.locator(".marker-item").first().evaluate((item) => getComputedStyle(item).backgroundColor);
      await points.first().click();
      assert.equal(await page.locator(".marker-item.is-viewed").count(), 1);
      const after = await page.locator(".marker-item").first().evaluate((item) => getComputedStyle(item).backgroundColor);
      assert.notEqual(before, after);
      assert.equal(await points.count(), 2);
      await points.first().click();
      assert.equal(await page.locator(".marker-item.is-viewed").count(), 1);
      await page.getByRole("button", { name: "下一个重点", exact: true }).click();
      assert.equal(await page.locator(".marker-item.is-viewed").count(), 2);
      await page.getByRole("button", { name: "上一个重点", exact: true }).click();
      assert.equal(await page.locator(".marker-item.is-viewed").count(), 2);
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.screenshot({ path: path.join(output, `viewed-${viewport.width}.png`), fullPage: true });
      const filters = page.getByRole("tablist", { name: "本次查看状态" });
      await filters.getByRole("tab", { name: "未看", exact: true }).click();
      assert.equal(await points.count(), 0);
      await filters.getByRole("tab", { name: "已看", exact: true }).click();
      assert.equal(await points.count(), 2);
      await filters.getByRole("tab", { name: "全部", exact: true }).click();
      const modes = page.getByRole("tablist", { name: "重点模式" });
      await modes.getByRole("tab", { name: /旧版考点/ }).click();
      assert.equal(await points.count(), 4);
      assert.equal(await page.locator(".marker-item.is-viewed").count(), 0);
      await points.nth(2).click();
      assert.equal(await page.locator(".marker-item.is-viewed").count(), 1);
      await modes.getByRole("tab", { name: /高召回/ }).click();
      assert.equal(await page.locator(".marker-item.is-viewed").count(), 2);
      await page.reload({ waitUntil: "domcontentloaded" });
      await points.first().waitFor();
      assert.equal(await page.locator(".marker-item.is-viewed").count(), 0);
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.screenshot({ path: path.join(output, `reopened-${viewport.width}.png`), fullPage: true });
      const dimensions = await page.evaluate(() => ({
        width: window.innerWidth, scrollWidth: document.documentElement.scrollWidth,
      }));
      assert.ok(dimensions.scrollWidth <= dimensions.width + 1, "Horizontal overflow");
      assert.deepEqual(errors, []);
      assert.deepEqual(apiRequests, []);
      results.push({ viewport, dimensions, before, after, errors, apiRequests });
      await context.close();
    }
    await fs.writeFile(path.join(output, "verification.json"), JSON.stringify(results, null, 2));
    console.log(JSON.stringify({ passed: results.length, output, results }, null, 2));
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
