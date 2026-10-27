/* Synthetic-only acceptance against material_browser_server.py, default disabled build. */
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium } = require("./playwright.cjs");

async function main() {
  const base = process.env.CLASSROOM_MATERIALS_QA_URL || "http://127.0.0.1:8879";
  const output = path.resolve(__dirname, "../.runtime/materials-paused-browser");
  await fs.mkdir(output, { recursive: true });
  const browser = await chromium.launch({ headless: true, channel: process.env.CLASSROOM_BROWSER_CHANNEL || "msedge" });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const status = await (await page.request.get(`${base}/api/library/status`)).json();
    const before = await (await page.request.get(`${base}/api/material-collections`)).json();
    assert.ok(before.some(collection => collection.total > 0), "Synthetic materials must exist before testing the gate");
    const errors = [], materialRequests = [];
    page.on("pageerror", error => errors.push(error.message));
    page.on("request", request => {
      if (/\/api\/material-/.test(request.url())) materialRequests.push(request.url());
    });
    await page.addInitScript(scope => {
      localStorage.setItem(`classroom-onboarding:v1:${scope}`, JSON.stringify({ dismissed: true }));
      localStorage.setItem("classcue-materials:paused-qa", JSON.stringify({ collection: "saved", item: "saved-question" }));
    }, status.library_scope || "default");
    await page.goto(base);
    await page.locator(".library-heading h1").waitFor();
    assert.equal(await page.locator('a[href="/materials"]').count(), 0);
    assert.equal(await page.getByRole("link", { name: "导入视频", exact: true }).count(), 1);
    await page.screenshot({ path: path.join(output, "paused-library.png"), fullPage: true });
    for (const route of ["/materials", "/materials?collection=saved#saved-question"]) {
      await page.goto(`${base}${route}`);
      await page.waitForURL(`${base}/`);
      await page.locator(".library-heading h1").waitFor();
      assert.equal(await page.locator(".materials-page").count(), 0);
      assert.deepEqual(await page.evaluate(() => JSON.parse(localStorage.getItem("classcue-materials:paused-qa"))),
        { collection: "saved", item: "saved-question" });
    }
    assert.deepEqual(materialRequests, [], "Paused UI must not fetch or process materials");
    assert.deepEqual(errors, []);
    const after = await (await page.request.get(`${base}/api/material-collections`)).json();
    assert.deepEqual(after, before, "Hiding the UI must not modify existing materials");
    await fs.writeFile(path.join(output, "result.json"), JSON.stringify({ passed: true, errors, materialRequests,
      collectionsPreserved: true, navigationPreserved: true }, null, 2));
    console.log("Paused materials acceptance passed: hidden entry, replaced routes, no material requests, data and navigation preserved.");
  } finally {
    await browser.close();
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
