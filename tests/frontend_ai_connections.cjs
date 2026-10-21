// Full UI -> disposable Python API -> disk/DPAPI; no real configuration writes or remote requests.
const { chromium } = require("./playwright.cjs");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");
const net = require("node:net");

async function main() {
  const root = path.resolve(__dirname, "..");
  const output = path.join(root, ".runtime", `ai-connections-qa-${Date.now()}`);
  fs.mkdirSync(output, { recursive: true });
  const port = await new Promise(resolve => {
    const server = net.createServer();
    server.listen(0, "127.0.0.1", () => {
      const number = server.address().port;
      server.close(() => resolve(number));
    });
  });
  const origin = `http://127.0.0.1:${port}`;
  const logs = fs.openSync(path.join(output, "api.log"), "a");
  const service = spawn(path.join(root, ".venv/Scripts/python.exe"),
    [path.join(root, "tests/fixtures/ai_connections_server.py"), output, String(port)],
    { cwd: root, windowsHide: true, stdio: ["ignore", logs, logs] });
  let browser;
  try {
    let ready = false;
    for (let attempt = 0; attempt < 100; attempt++) {
      try { if ((await fetch(`${origin}/api/health`)).ok) { ready = true; break; } } catch {}
      if (service.exitCode !== null) throw new Error("Fixture API failed; inspect api.log");
      await new Promise(resolve => setTimeout(resolve, 200));
    }
    assert.ok(ready, "Fixture API startup timed out");
    browser = await chromium.launch({ executablePath: "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", headless: true });
    const page = await browser.newPage({ viewport: { width: 1338, height: 1038 } });
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/api/**", async route => {
      const url = new URL(route.request().url());
      if (!url.pathname.startsWith("/api/")) return route.continue();
      const headers = { ...route.request().headers(), origin };
      delete headers.host;
      const response = await route.fetch({ url: origin + url.pathname + url.search, headers });
      await route.fulfill({ response });
    });
    await page.goto("http://127.0.0.1:5175/settings?section=ai");
    const section = page.getByRole("region", { name: "分析服务连接" });
    const connections = section.getByRole("combobox", { name: "已保存连接" });
    await connections.waitFor();
    assert.equal(await connections.inputValue(), "default");
    assert.match(await connections.innerText(), /当前连接/);
    const model = section.getByRole("textbox", { name: "模型名称", exact: true });
    assert.equal(await model.inputValue(), "fixture-fast");
    const effort = section.getByRole("combobox", { name: "思考强度", exact: true });
    assert.equal(await effort.isVisible(), true);
    assert.equal(await effort.inputValue(), "high");
    assert.deepEqual(await effort.locator("option").allTextContents(), ["Low", "Medium", "High"]);
    assert.equal(await section.locator("details").getByRole("combobox", { name: "思考强度" }).count(), 0);
    await section.getByText("GPT-6 Luna、DeepSeek 4.1 Flash 建议最低使用 Medium；默认 High。", { exact: true }).waitFor();
    const name = section.getByRole("textbox", { name: "连接名称", exact: true });
    const save = section.getByRole("button", { name: "保存连接", exact: true });
    const keyInput = section.locator('input[aria-label="API Key"]');
    await effort.selectOption("medium");
    await name.fill("日常分析");
    await save.click();
    await page.waitForFunction(() => !document.querySelector(".ai-connection-editor").disabled);
    await section.getByRole("button", { name: "复制当前连接" }).click();
    assert.equal(await connections.inputValue(), "__new__");
    assert.equal((await (await fetch(`${origin}/api/settings/ai`)).json()).connection_id, "default");
    await name.fill("精细分析");
    await model.fill("fixture-accurate");
    await keyInput.fill("fixture-copy-key");
    await section.getByRole("button", { name: "测试连接", exact: true }).click();
    await section.getByText("隔离测试接口已连接", { exact: true }).waitFor();
    await save.click();
    await page.waitForFunction(() => document.querySelector('select[aria-label="已保存连接"]').value !== "__new__"
      && !document.querySelector(".ai-connection-editor").disabled);
    const copyId = await connections.inputValue();
    assert.notEqual(copyId, "default");
    await model.fill("unsaved-model");
    await connections.selectOption("default");
    let dialog = page.getByRole("dialog");
    await dialog.getByRole("button", { name: "取消", exact: true }).click();
    assert.equal(await model.inputValue(), "unsaved-model");
    await connections.selectOption("default");
    await page.getByRole("dialog").getByRole("button", { name: "放弃修改并继续" }).click();
    await page.getByRole("dialog").waitFor({ state: "hidden" });
    assert.equal(await model.inputValue(), "fixture-fast");
    await section.getByRole("button", { name: "显示 API Key" }).click();
    await page.waitForFunction(() => document.querySelector('input[aria-label="API Key"]').value === "fixture-primary-key");
    await section.getByRole("button", { name: "隐藏 API Key" }).click();
    await connections.selectOption(copyId);
    await page.waitForFunction(id => document.querySelector('select[aria-label="已保存连接"]').value === id
      && !document.querySelector(".ai-connection-editor").disabled, copyId);
    assert.equal(await model.inputValue(), "fixture-accurate");
    await section.getByRole("button", { name: "测试连接", exact: true }).click();
    await section.getByText("隔离测试接口已连接", { exact: true }).waitFor();
    await section.getByRole("button", { name: "获取模型", exact: true }).click();
    await page.getByRole("dialog").getByRole("button", { name: "fixture-accurate", exact: true }).click();
    await model.fill("saved-before-switch");
    await connections.selectOption("default");
    await page.getByRole("dialog").getByRole("button", { name: "保存并继续" }).click();
    await page.getByRole("dialog").waitFor({ state: "hidden" });
    await connections.selectOption(copyId);
    await page.waitForFunction(() => document.querySelector('.ai-connection-editor').disabled === false);
    assert.equal(await model.inputValue(), "saved-before-switch");
    assert.equal(await effort.inputValue(), "medium");
    await page.reload();
    await connections.waitFor();
    assert.equal(await connections.inputValue(), copyId);
    assert.equal(await model.inputValue(), "saved-before-switch");
    for (const width of [1338, 856, 390]) {
      await page.setViewportSize({ width, height: 1038 });
      await page.evaluate(() => document.fonts.ready);
      await page.evaluate(() => scrollTo(0, 0));
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false, `overflow at ${width}`);
      await page.screenshot({ path: path.join(output, `connections-${width}.png`), fullPage: true });
    }
    await section.getByRole("button", { name: "新增", exact: true }).click();
    assert.equal(await connections.inputValue(), "__new__");
    assert.equal((await (await fetch(`${origin}/api/settings/ai`)).json()).connection_id, copyId);
    assert.equal(await model.inputValue(), "");
    assert.equal(await keyInput.inputValue(), "");
    assert.equal(await effort.inputValue(), "high");
    await name.fill("全新账号");
    await section.locator("#ai-base-url").fill("https://fixture.invalid/v1");
    await keyInput.fill("fixture-new-key");
    await section.getByRole("button", { name: "获取模型", exact: true }).click();
    await page.getByRole("dialog").getByRole("button", { name: "fixture-fast", exact: true }).click();
    assert.equal(await model.inputValue(), "fixture-fast");
    await save.click();
    await page.waitForFunction(() => document.querySelector('select[aria-label="已保存连接"]').value !== "__new__"
      && !document.querySelector(".ai-connection-editor").disabled);
    await section.getByRole("button", { name: "删除当前连接" }).click();
    await page.getByRole("dialog").getByRole("button", { name: "确认删除连接" }).click();
    await page.getByRole("dialog").waitFor({ state: "hidden" });
    assert.equal(await connections.inputValue(), "default");
    await connections.selectOption(copyId);
    await page.waitForFunction(() => document.querySelector('.ai-connection-editor').disabled === false);
    await section.getByRole("button", { name: "删除当前连接" }).click();
    await page.getByRole("dialog").getByRole("button", { name: "确认删除连接" }).click();
    await page.getByRole("dialog").waitFor({ state: "hidden" });
    assert.equal(await section.getByRole("button", { name: "删除当前连接" }).isDisabled(), false);
    await section.getByRole("button", { name: "删除当前连接" }).click();
    await page.getByRole("dialog").getByText(/将不再有已保存连接/).waitFor();
    await page.getByRole("dialog").getByRole("button", { name: "确认删除连接" }).click();
    await page.getByRole("dialog").waitFor({ state: "hidden" });
    assert.equal(await connections.inputValue(), "");
    assert.equal(await section.getByRole("button", { name: "删除当前连接" }).isDisabled(), true);
    assert.equal(await section.getByRole("button", { name: "复制当前连接" }).isDisabled(), true);
    assert.equal(await keyInput.count(), 0);
    await page.reload();
    await section.getByText(/尚未保存 AI 服务连接/).waitFor();
    let persisted = JSON.parse(fs.readFileSync(path.join(output, "ai.json"), "utf8"));
    assert.equal(persisted.connections.length, 0);
    assert.equal(persisted.active_connection_id, "");
    for (const width of [1338, 856, 390]) {
      await page.setViewportSize({ width, height: 1038 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
      await page.screenshot({ path: path.join(output, `connections-empty-${width}.png`), fullPage: true });
    }
    await section.getByRole("button", { name: "新增", exact: true }).click();
    await name.fill("重新添加");
    await section.locator("#ai-base-url").fill("https://fixture.invalid/v1");
    await keyInput.fill("fixture-replacement-key");
    await section.getByRole("button", { name: "获取模型", exact: true }).click();
    await page.getByRole("dialog").getByRole("button", { name: "fixture-fast", exact: true }).click();
    await save.click();
    await page.waitForFunction(() => document.querySelector('select[aria-label="已保存连接"]').value !== "__new__"
      && !document.querySelector(".ai-connection-editor").disabled);
    await page.reload();
    await name.waitFor();
    assert.equal(await name.inputValue(), "重新添加");
    persisted = JSON.parse(fs.readFileSync(path.join(output, "ai.json"), "utf8"));
    assert.equal(persisted.connections.length, 1);
    assert.equal(persisted.profile.reasoning_effort, "high");
    assert.ok(!JSON.stringify(persisted).includes("fixture-primary-key"));
    assert.ok(!JSON.stringify(persisted).includes("fixture-copy-key"));
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ result: "passed", screenshots: output, checks: [
      "legacy migration", "unified name/settings/key save", "copy and independent DPAPI key", "draft test and model list",
      "unsaved cancel/discard/save", "switch and reload", "new connection stays inactive until saved", "delete all, reload empty and recreate",
      "desktop/tablet/mobile overflow",
      "visible three-level effort control", "default high and saved medium roundtrip",
    ] }, null, 2));
  } finally {
    if (browser) await browser.close();
    service.kill();
    await new Promise(resolve => service.exitCode !== null ? resolve() : service.once("exit", resolve));
    fs.closeSync(logs);
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
