// Isolated browser profile; all settings writes go to in-memory fixtures.
const { chromium } = require("./playwright.cjs");
const path = require("node:path"), fs = require("node:fs"), assert = require("node:assert/strict");
(async () => {
  const output = path.resolve(__dirname, `../.runtime/unsaved-ui-${Date.now()}`);
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ executablePath: "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = []; page.on("pageerror", error => errors.push(error.message));
    let ai = { provider: "openai_compatible", connection_id: "fixture", connection_name: "测试连接",
      base_url: "https://fixture.test/v1", model: "saved-model", output_tokens: 16384, token_parameter: "max_tokens",
      timeout_seconds: 120, max_retries: 5, json_mode: false, has_api_key: true, connections: [
        { id: "fixture", name: "测试连接", model: "saved-model", base_url: "https://fixture.test/v1", has_api_key: true },
      ] };
    let failSave = true, writes = 0;
    await page.route("**/api/**", async route => {
      const request = route.request();
      if (new URL(request.url()).pathname === "/api/settings/ai") {
        if (request.method() !== "GET") {
          writes++;
          if (failSave) return route.fulfill({ status: 500, json: { detail: "测试：保存失败，请重试" } });
          ai = { ...ai, ...request.postDataJSON() };
        }
        return route.fulfill({ json: ai });
      }
      return request.method() === "GET" ? route.continue() : route.abort();
    });
    await page.goto(`${process.env.FRONTEND_URL || "http://127.0.0.1:5175"}/settings?section=ai`);
    const model = page.getByRole("textbox", { name: "模型名称", exact: true });
    await model.fill("draft-model");
    await page.getByRole("button", { name: "快捷键", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "有未保存的修改" });
    await dialog.waitFor();
    assert.equal(await page.evaluate(() => document.activeElement.textContent), "继续编辑");
    await dialog.getByRole("button", { name: "保存并离开" }).click();
    await dialog.getByRole("alert").filter({ hasText: "保存失败" }).waitFor();
    assert.equal(await model.inputValue(), "draft-model");
    for (const width of [1440, 1024, 390]) {
      await page.setViewportSize({ width, height: 950 });
      assert.ok(await dialog.evaluate(element => element.getBoundingClientRect().right <= innerWidth), `Dialog overflow: ${width}`);
      await page.screenshot({ path: path.join(output, `unsaved-${width}.png`) });
    }
    failSave = false;
    await dialog.getByRole("button", { name: "保存并离开" }).click();
    await page.waitForURL("**/settings?section=shortcuts");
    assert.equal(writes, 2);
    await page.getByRole("button", { name: "AI 服务", exact: true }).click();
    await model.fill("discard-model");
    await page.getByRole("link", { name: "课堂重点复习", exact: true }).click();
    await dialog.getByRole("button", { name: "放弃修改", exact: true }).click();
    await page.waitForURL(/\/$/);
    await page.goBack();
    await model.waitFor(); assert.equal(await model.inputValue(), "draft-model");
    await model.fill("history-draft");
    // POP navigation is restored until the user confirms.
    await page.evaluate(() => history.forward());
    await dialog.waitFor();
    await dialog.getByRole("button", { name: "继续编辑", exact: true }).click();
    assert.equal(await model.inputValue(), "history-draft");
    await page.evaluate(() => history.forward());
    await dialog.getByRole("button", { name: "放弃修改", exact: true }).click();
    await page.waitForURL(/\/$/);
    await page.goBack(); await model.waitFor();
    await model.fill("reload-draft");
    const beforeUnload = page.waitForEvent("dialog");
    const reload = page.reload().catch(() => {});
    const warning = await beforeUnload;
    assert.equal(warning.type(), "beforeunload"); await warning.dismiss(); await reload;
    assert.equal(await model.inputValue(), "reload-draft");
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ ok: true, output, widths: [1440, 1024, 390], writes }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
