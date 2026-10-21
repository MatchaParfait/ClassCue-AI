// UI verification uses an isolated browser profile and a mock desktop bridge.
// Backend requests are read-only; native shortcut writing is covered by desktop_smoke.cjs.
const { chromium } = require("./playwright.cjs");
const path = require("node:path"), fs = require("node:fs"), assert = require("node:assert/strict");
(async () => {
  const output = path.resolve(__dirname, `../.runtime/shortcuts-ui-${Date.now()}`);
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ executablePath: "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", headless: true });
  const errors = [];
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/api/**", route => route.request().method() === "GET" ? route.continue() : route.abort());
    await page.addInitScript(() => {
      localStorage.setItem("classroom.shortcuts.v1", JSON.stringify({ search: ["Ctrl+K", "Meta+K"] }));
      window.pywebview = { api: {
        describe: async () => ({ name: "QA", tray: true, hotkey: "CTRL+ALT+K", close_behavior: "ask", dialog: null }),
        respond: async () => ({}), retry: async () => ({}), set_hotkey: async value => ({ hotkey: value }),
        set_close_behavior: async value => ({ close_behavior: value }), hide_to_tray: async () => ({}),
        exit_application: async () => ({}), open_logs: async () => ({}),
      } };
    });
    await page.goto(`${process.env.FRONTEND_URL || "http://127.0.0.1:5175"}/settings?section=shortcuts`);
    const panel = page.getByRole("region", { name: "快捷键设置" });
    await panel.waitFor();
    const trigger = page.getByRole("button", { name: "全局搜索字幕", exact: true });
    assert.equal(await trigger.locator("kbd").count(), 0, "The retired defaults must not be advertised");
    await trigger.focus();
    for (const binding of ["Control+k", "Meta+k"]) {
      await page.keyboard.press(binding);
      assert.equal(await page.getByRole("dialog").count(), 0, `${binding} must not open search by default`);
    }
    await trigger.click();
    await page.getByRole("dialog").waitFor();
    await page.keyboard.press("Escape");
    await page.getByRole("dialog").waitFor({ state: "detached" });
    const next = panel.getByRole("textbox", { name: "下一个考点快捷键", exact: true });
    await next.press("K");
    await panel.getByRole("alert").filter({ hasText: "同时用于" }).waitFor();
    assert.ok(await panel.getByRole("button", { name: "保存快捷键", exact: true }).isDisabled());
    await next.press("J");
    const search = panel.getByRole("textbox", { name: "全局搜索快捷键", exact: true });
    await search.press("Control+Shift+Y");
    assert.equal(await page.getByRole("dialog").count(), 0, "Recording must not open search");
    await panel.getByRole("button", { name: "保存快捷键", exact: true }).click();
    await panel.getByText("快捷键已保存并生效。", { exact: true }).waitFor();
    await page.reload();
    await next.waitFor();
    assert.equal(await next.inputValue(), "J");
    assert.equal(await search.inputValue(), "Ctrl + Shift + Y");
    await page.getByRole("button", { name: "全局搜索字幕", exact: true }).focus();
    await page.keyboard.press("Control+k");
    assert.equal(await page.getByRole("dialog").count(), 0);
    await page.keyboard.press("Control+Shift+Y");
    await page.getByRole("dialog").waitFor();
    await page.keyboard.press("Escape");
    await page.getByRole("dialog").waitFor({ state: "detached" });
    for (const width of [856, 1316, 1540, 1920]) {
      await page.setViewportSize({ width, height: 950 });
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.waitForTimeout(250);
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `Horizontal overflow at ${width}`);
      await page.screenshot({ path: path.join(output, `shortcuts-${width}.png`), fullPage: true });
    }
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ ok: true, output, widths: [856, 1316, 1540, 1920] }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
