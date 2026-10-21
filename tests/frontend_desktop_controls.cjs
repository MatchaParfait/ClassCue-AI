// Mock the native bridge; never minimize or exit a real application.
const { chromium } = require("./playwright.cjs");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

(async () => {
  const output = path.resolve(__dirname, "..", ".runtime", `desktop-controls-qa-${Date.now()}`);
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({
    executablePath: "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", headless: true,
  });
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  try {
    await page.route("**/api/**", route => {
      const pathname = new URL(route.request().url()).pathname;
      if (!pathname.startsWith("/api/")) return route.continue();
      assert.equal(route.request().method(), "GET", "No real settings or processing writes");
      const body = pathname === "/api/health" ? { status: "ok" }
        : pathname === "/api/library/status" ? { library_id: "desktop-qa", data_root: "desktop-qa", video_count: 0 }
        : pathname === "/api/batch/latest" ? null : [];
      return route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
    });
    await page.addInitScript(() => {
      window.__desktopResponses = [];
      window.pywebview = { api: {
        describe: async () => ({ name: "测试桌面", tray: true, dialog: null, hotkey: "" }),
        respond: async (...args) => { window.__desktopResponses.push(args); return {}; },
        retry: async () => ({}), set_hotkey: async () => ({}), set_close_behavior: async () => ({}),
      } };
    });
    await page.goto(process.env.FRONTEND_URL || "http://127.0.0.1:5175");
    await page.locator(".workbench-brand").waitFor();
    const show = (tray = true) => page.evaluate(tray => {
      window.dispatchEvent(new CustomEvent("classroom-desktop-dialog", {
        detail: { kind: "close-choice", message: "请选择关闭窗口后的操作。", tray },
      }));
    }, tray);
    for (const width of [856, 1316, 1540, 1920]) {
      await page.setViewportSize({ width, height: 949 });
      await show();
      const dialog = page.getByRole("dialog", { name: "关闭窗口", exact: true });
      await dialog.waitFor();
      const minimize = dialog.getByRole("button", { name: "最小化到托盘" });
      const exit = dialog.getByRole("button", { name: "退出应用" });
      const cancel = dialog.getByRole("button", { name: "取消关闭" });
      assert.equal(await cancel.evaluate(node => node === document.activeElement), true);
      const a = await minimize.boundingBox(), b = await exit.boundingBox(), c = await cancel.boundingBox();
      assert.ok(Math.abs(a.width - b.width) < 1 && Math.abs(a.height - b.height) < 1);
      assert.ok(Math.abs(a.y - b.y) < 1 && b.x >= a.x + a.width);
      assert.ok(a.width + b.width > (await dialog.boundingBox()).width * .8, "Peer choices fill the action row");
      assert.ok(c.y >= a.y + a.height + 20, "Cancel is visually separated");
      assert.equal(await minimize.evaluate(node => getComputedStyle(node).backgroundColor),
        await exit.evaluate(node => getComputedStyle(node).backgroundColor));
      assert.equal(await dialog.getByRole("group", { name: "关闭方式" }).getByRole("button").count(), 2);
      assert.ok(await dialog.evaluate(node => node.scrollWidth <= node.clientWidth));
      await dialog.screenshot({ path: path.join(output, `close-choice-${width}.png`) });
      await page.keyboard.press("Escape");
      await dialog.waitFor({ state: "detached" });
    }
    await show(false);
    assert.equal(await page.getByRole("button", { name: "最小化到托盘" }).isDisabled(), true);
    await page.getByRole("button", { name: "取消关闭" }).click();
    await show();
    await page.getByRole("button", { name: "最小化到托盘" }).click();
    await page.getByRole("dialog").waitFor({ state: "detached" });
    await show();
    await page.getByRole("button", { name: "退出应用" }).click();
    await page.getByRole("dialog").waitFor({ state: "detached" });
    const responses = await page.evaluate(() => window.__desktopResponses);
    assert.deepEqual(responses, [
      ["cancel"], ["cancel"], ["cancel"], ["cancel"], ["cancel"], ["background", false], ["exit", false],
    ]);
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, "report.json"), JSON.stringify({ passed: true, responses, errors }, null, 2));
    console.log(output);
  } catch (error) {
    await page.screenshot({ path: path.join(output, "failure.png"), fullPage: true });
    fs.writeFileSync(path.join(output, "errors.json"), JSON.stringify(errors));
    throw error;
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
