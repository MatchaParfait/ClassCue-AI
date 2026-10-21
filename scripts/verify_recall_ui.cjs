/* Read-only browser checks; never submit processing jobs or reveal API keys. */
const { chromium } = require("../tests/playwright.cjs");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");

async function main() {
  const base = process.argv[2] || "http://127.0.0.1:5174";
  const output = path.resolve(process.argv[3] || "data/recall-integration");
  await fs.mkdir(output, { recursive: true });
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const report = [];
  try {
    for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
      const context = await browser.newContext({ viewport });
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.route("**/api/**", (route) => {
        if (route.request().method() !== "GET") return route.abort();
        return route.continue();
      });
      for (const route of ["/process", "/batch-process", "/demo", "/settings"]) {
        await page.goto(base + route);
        if (route === "/process" || route === "/batch-process") {
          const select = page.getByLabel("考点召回", { exact: true });
          await select.waitFor();
          assert.equal(await select.inputValue(), "medium_recall");
          assert.deepEqual(await select.locator("option").evaluateAll((options) => options.map((option) => option.value)),
            ["high_recall", "medium_recall", "low_recall"]);
          for (const value of ["high_recall", "low_recall", "medium_recall"]) {
            await select.selectOption(value);
            assert.equal(await select.inputValue(), value);
          }
        } else if (route === "/demo") {
          const tabs = page.getByRole("tablist", { name: "重点模式" });
          await tabs.getByRole("tab", { name: /低召回/ }).click();
          assert.equal(await tabs.getByRole("tab", { name: /低召回/ }).getAttribute("aria-selected"), "true");
          assert.equal(await tabs.getByRole("tab").count(), 4);
        } else {
          const select = page.getByLabel("思考强度", { exact: true });
          await select.waitFor();
          await select.selectOption("high");
          assert.equal(await select.inputValue(), "high");
        }
        await page.waitForTimeout(250);
        const dimensions = await page.evaluate(() => ({
          width: window.innerWidth, scrollWidth: document.documentElement.scrollWidth,
        }));
        assert.ok(dimensions.scrollWidth <= dimensions.width + 1, `Horizontal overflow on ${route}`);
        const file = `${route.slice(1)}-${viewport.width}.png`;
        await page.screenshot({ path: path.join(output, file), fullPage: true });
        report.push({ route, viewport, dimensions, screenshot: file });
      }
      assert.deepEqual(errors, []);
      await context.close();
    }
    await fs.writeFile(path.join(output, "ui-verification.json"), JSON.stringify(report, null, 2));
    console.log(JSON.stringify({ passed: report.length, output }));
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
