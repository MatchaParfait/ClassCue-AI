/* Synthetic-only browser acceptance against material_browser_server.py. */
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium } = require("./playwright.cjs");
let activePage;
let activeBrowser;

async function main() {
  const base = process.env.CLASSROOM_MATERIALS_QA_URL || "http://127.0.0.1:8879";
  const output = path.resolve(__dirname, "../.runtime/materials-browser");
  await fs.mkdir(output, { recursive: true });
  const browser = await chromium.launch({ headless: true, channel: process.env.CLASSROOM_BROWSER_CHANNEL || "msedge" });
  activeBrowser = browser;
  const errors = [], failed = [];
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  activePage = page;
  const status = await (await page.request.get(`${base}/api/library/status`)).json();
  await page.addInitScript(scope => {
    localStorage.setItem(`classroom-onboarding:v1:${scope}`, JSON.stringify({ dismissed: true }));
  }, status.library_scope || "default");
  page.on("pageerror", error => errors.push(error.message));
  page.on("response", response => {
    if (response.status() >= 400 && !response.url().includes("/favicon.ico")) failed.push(`${response.status()} ${response.url()}`);
  });
  await page.goto(`${base}/materials`);
  await page.getByRole("heading", { name: "复习资料", exact: true }).waitFor();
  await page.getByRole("button", { name: "绘制实体联系图（E-R 图）" }).click();
  await page.locator(".material-item-row").first().waitFor();
  assert.equal(await page.locator(".material-item-row").count(), 3);
  await page.locator(".material-item-row > button").first().click();
  await page.locator(".material-reading-heading h2").waitFor();
  const beforeVersion = Number((await page.locator(".material-origin").innerText()).match(/版本 (\d+)/)[1]);
  await page.locator(".material-question-image").first().evaluate(image => image.decode());
  assert.match(await page.locator(".material-shared").innerText(), /共享题干/);
  await page.screenshot({ path: path.join(output, "desktop-library.png"), fullPage: true });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await page.getByRole("button", { name: "原文定位", exact: true }).click();
  await page.locator(".material-highlight").waitFor();
  assert.ok((await page.locator(".material-highlight").boundingBox()).height > 10);
  await page.screenshot({ path: path.join(output, "desktop-source.png"), fullPage: true });
  await page.getByRole("button", { name: "返回题目", exact: true }).click();
  await page.getByRole("button", { name: "已掌握", exact: true }).click();
  await page.waitForFunction(() => document.querySelector('.material-study-control button[aria-pressed="true"]')?.textContent.includes("已掌握"));
  await page.reload();
  await page.locator(".material-reading-heading h2").waitFor();
  assert.match(await page.locator('.material-study-control button[aria-pressed="true"]').innerText(), /已掌握/);
  await page.getByRole("button", { name: "修正题目", exact: true }).click();
  await page.getByRole("dialog", { name: "修正题目" }).waitFor();
  await page.getByLabel("标题", { exact: true }).fill("图书借阅系统 E-R 图 · 人工标题");
  await page.getByRole("button", { name: "保存题目", exact: true }).click();
  await page.getByRole("dialog").waitFor({ state: "hidden" });
  assert.match(await page.locator('.material-study-control button[aria-pressed="true"]').innerText(), /已掌握/);
  await page.getByRole("button", { name: "修正题目", exact: true }).click();
  await page.getByLabel("修正文字", { exact: true }).fill(`人工补充：借阅包含借出日期和归还日期。版本验收 ${Date.now()}`);
  await page.getByRole("button", { name: "保存题目", exact: true }).click();
  await page.getByRole("dialog").waitFor({ state: "hidden" });
  await page.waitForFunction(() => document.querySelector('.material-study-control button[aria-pressed="true"]')?.textContent.includes("未复习"));
  assert.ok((await page.locator(".material-origin").innerText()).includes(`版本 ${beforeVersion + 1}`));
  for (const viewport of [{ width: 1280, height: 720 }, { width: 768, height: 1024 }, { width: 390, height: 844 }]) {
    await page.setViewportSize(viewport);
    await page.screenshot({ path: path.join(output, `${viewport.width}-reader.png`), fullPage: true });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `overflow ${viewport.width}`);
    if (viewport.width <= 950) {
      await page.getByRole("button", { name: "返回题目列表", exact: true }).click();
      await page.getByRole("button", { name: "返回目录", exact: true }).click();
      await page.getByRole("button", { name: "绘制实体联系图（E-R 图）" }).click();
      assert.equal(await page.locator(".material-item-row").count(), 3);
      await page.locator(".material-item-row > button").first().click();
    }
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByRole("tab", { name: "按原始资料" }).click();
  await page.locator(".material-document-row").filter({ hasText: "公式与 SQL.md" }).locator(".material-directory-row").click();
  await page.locator(".material-item-row > button").first().click();
  await page.locator(".material-prose table").waitFor();
  await page.locator(".material-prose .katex").waitFor();
  await page.screenshot({ path: path.join(output, "markdown-math.png"), fullPage: true });
  await page.getByRole("tab", { name: "待处理", exact: true }).click();
  await page.locator(".material-pending-documents button").filter({ hasText: "待补图.md" }).click();
  await page.locator(".material-pending-block").filter({ hasText: "图片" }).waitFor();
  assert.match(await page.locator(".material-list-scroll").innerText(), /图片/);
  await page.screenshot({ path: path.join(output, "pending.png"), fullPage: true });
  assert.deepEqual(errors, []);
  assert.deepEqual(failed, []);
  await fs.writeFile(path.join(output, "result.json"), JSON.stringify({ passed: true, errors, failed, viewports: [1440, 1280, 768, 390] }, null, 2));
  await browser.close();
  console.log("Material browser acceptance passed: classification, source regions, progress, revisions, responsive reading, Markdown/math, pending sources.");
}
main().catch(async error => {
  console.error(error);
  if (activePage) {
    await activePage.screenshot({ path: path.resolve(__dirname, "../.runtime/materials-browser/failure.png"), fullPage: true });
    console.error(await activePage.locator("body").innerText());
  }
  await activeBrowser?.close();
  process.exitCode = 1;
});
