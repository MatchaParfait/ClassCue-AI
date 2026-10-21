// Read-only browser checks. Never confirm migration against the user's library.
const { chromium } = require("./playwright.cjs");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

(async () => {
  const output = path.resolve(__dirname, "..", ".runtime", `application-data-ui-qa-${Date.now()}`);
  fs.mkdirSync(output, { recursive: true });
  const destination = path.join(os.tmpdir(), `classroom-data-preview-${Date.now()}`);
  const browser = await chromium.launch({
    executablePath: "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", headless: true,
  });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
  const errors = [], writes = [];
  page.on("pageerror", error => errors.push(error.message));
  // Guard against any accidental migration or unrelated settings writes.
  await page.route("**/api/**", async route => {
    const request = route.request();
    if (["GET", "HEAD"].includes(request.method()) ||
      (request.method() === "POST" && new URL(request.url()).pathname === "/api/settings/application-data/preview")) {
      return route.continue();
    }
    writes.push(`${request.method()} ${new URL(request.url()).pathname}`);
    await route.abort();
  });
  try {
    await page.goto(`${process.env.FRONTEND_URL || "http://127.0.0.1:5175"}/settings?section=storage`);
    const input = page.getByLabel("应用数据目录", { exact: true });
    await input.fill(destination);
    const preview = page.getByRole("button", { name: "检查迁移范围", exact: true });
    await preview.click();
    const dialog = page.getByRole("dialog", { name: "确认迁移并立即重启？" });
    await dialog.waitFor();
    await page.waitForFunction(() => [...document.querySelectorAll('[role="dialog"]')]
      .some(element => getComputedStyle(element).opacity === "1"));
    assert.ok((await dialog.textContent()).includes("修改应用数据目录后，软件会自动立即重启"));
    assert.ok((await dialog.textContent()).includes(path.join(destination, "library")));
    assert.ok((await dialog.textContent()).includes(path.join(destination, "config")));
    assert.equal(await page.evaluate(() => document.activeElement?.textContent), "取消");
    await page.keyboard.press("Tab");
    assert.equal(await page.evaluate(() => document.activeElement?.textContent), "迁移并立即重启");
    for (const width of [1440, 1024, 390]) {
      await page.setViewportSize({ width, height: 900 });
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${width}: overflow`);
      assert.ok(await dialog.getByRole("button", { name: "取消", exact: true }).evaluate(element => {
        const bounds = element.getBoundingClientRect();
        return bounds.top >= 0 && bounds.bottom <= innerHeight && bounds.left >= 0 && bounds.right <= innerWidth;
      }));
      await page.screenshot({ path: path.join(output, `restart-warning-${width}.png`) });
    }
    await page.keyboard.press("Escape");
    await dialog.waitFor({ state: "detached" });
    assert.equal(await page.evaluate(() => document.activeElement?.textContent), "检查迁移范围");
    assert.equal(await input.inputValue(), destination);
    await preview.click();
    await dialog.waitFor();
    await dialog.getByRole("button", { name: "取消", exact: true }).click();
    await dialog.waitFor({ state: "detached" });
    assert.ok(!fs.existsSync(destination));
    assert.deepEqual(writes, []);
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({
      passed: true, errors, writes, checks: [
        "explicit immediate restart warning", "target subdirectories shown", "initial focus on cancel",
        "keyboard focus trapped", "Escape returns focus", "cancel keeps draft", "preview creates no files",
        "1440/1024/390px layout", "no migration or settings writes",
      ],
    }, null, 2));
    console.log(output);
  } catch (error) {
    await page.screenshot({ path: path.join(output, "failure.png"), fullPage: true });
    throw error;
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
