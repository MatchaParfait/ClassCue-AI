// Entirely mocked public feed and native bridge; no update is installed.
const { chromium } = require("./playwright.cjs");
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");

(async () => {
  const output = path.resolve(__dirname, "../.runtime", `updates-qa-${Date.now()}`);
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ executablePath: "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", headless: true });
  const page = await browser.newPage();
  const errors = [], calls = [];
  page.on("pageerror", error => errors.push(error.message));
  let status;
  const reset = () => status = {
    version: "0.2.0", repository: "MatchaParfait/ClassCue-AI", configured: true, portable: true, busy: false,
    checking: false, downloading: false, downloaded_bytes: 0, last_checked: "2026-10-20T00:00:00Z",
    error: null, download_error: null, compatible: true, ready: true,
    preferences: { auto_check: true, auto_download: false, show_announcements: true },
    release: { version: "0.2.1", size: 8e6, notes: "改进桌面启动与视频库检索\n修复字幕显示问题，保留已有复习记录。", url: "https://github.com/MatchaParfait/ClassCue-AI/releases/download/v0.2.1/update.zip" },
    announcements: [{ id: "qa-only", title: "桌面版本维护安排与开源项目进展", body: '这是界面验收的模拟公告。\n<img src=x onerror="alert(1)">\n视频与复习记录不会上传。', published_at: "2026-10-20T00:00:00Z", level: "important", read: false }],
    install: null,
  };
  await page.addInitScript(() => {
    window.__installs = 0;
    window.pywebview = { api: {
      describe: async () => ({ name: "课堂重点复习", tray: true, dialog: null, hotkey: "" }),
      respond: async () => ({}), retry: async () => ({}), set_hotkey: async () => ({}),
      set_close_behavior: async () => ({}), set_unsaved_changes: async () => ({}),
      hide_to_tray: async () => ({}), exit_application: async () => ({}), open_logs: async () => ({}),
      install_update: async () => { window.__installs++; throw new Error("模拟安装阻止：任务已开始，请稍后重试"); },
    } };
  });
  await page.route("**/api/**", async route => {
    const request = route.request(), url = new URL(request.url()), pathname = url.pathname;
    if (!pathname.startsWith("/api/")) return route.continue();
    calls.push(`${request.method()} ${pathname}`);
    let value = [];
    if (pathname.startsWith("/api/updates")) {
      if (pathname.endsWith("/preferences")) Object.assign(status.preferences, request.postDataJSON());
      if (pathname.endsWith("/read")) for (const notice of status.announcements) if (request.postDataJSON().ids.includes(notice.id)) notice.read = true;
      if (pathname.endsWith("/pause")) status.downloading = false;
      value = status;
    } else if (pathname === "/api/library/status") value = { library_id: "updates-qa", library_scope: "updates-qa", processing: false, active_job_id: null, data_root: "fixture" };
    else if (pathname === "/api/library") value = { items: [], breadcrumbs: [], folder: null };
    else if (pathname.includes("/batch") || pathname.endsWith("/latest")) value = null;
    else if (pathname === "/api/health") value = { status: "ok", version: "0.2.0" };
    await route.fulfill({ json: value });
  });
  try {
    const origin = process.env.FRONTEND_URL || "http://127.0.0.1:5175";
    for (const width of [856, 1316, 1540, 1920]) {
      reset();
      await page.setViewportSize({ width, height: 1000 });
      await page.goto(`${origin}/settings?section=updates`);
      await page.getByRole("heading", { name: "关于与更新", exact: true }).waitFor();
      await page.getByText("当前版本", { exact: true }).waitFor();
      await page.screenshot({ path: `${output}/settings-${width}.png`, fullPage: true });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `overflow at ${width}`);
      const header = page.locator(".workbench-topbar-inner");
      for (const control of await header.locator("button,a").all()) {
        if (!(await control.isVisible())) continue;
        const box = await control.boundingBox();
        assert(box && box.x >= 0 && box.x + box.width <= width + 1, `header control outside viewport at ${width}`);
      }
      await page.getByRole("button", { name: /^公告/ }).first().click();
      const news = page.getByRole("dialog");
      await news.getByRole("button", { name: /桌面版本维护安排/ }).click();
      assert.equal(await news.locator(".announcement-body img").count(), 0);
      await page.screenshot({ path: `${output}/announcements-${width}.png` });
      await page.keyboard.press("Escape");
      await page.getByRole("button", { name: "安装并重启", exact: true }).click();
      assert.equal(await page.evaluate(() => document.activeElement.textContent), "取消");
      await page.screenshot({ path: `${output}/confirm-${width}.png` });
      await page.getByRole("dialog").getByRole("button", { name: "安装并重启", exact: true }).click();
      await page.getByRole("alert").filter({ hasText: "模拟安装阻止" }).waitFor();
      await page.getByRole("dialog").getByRole("button", { name: "取消", exact: true }).click();
    }
    reset(); status.ready = false; status.downloading = true; status.downloaded_bytes = 3e6;
    await page.reload();
    await page.getByRole("progressbar", { name: "更新包下载进度" }).waitFor();
    await page.getByRole("button", { name: "暂停下载", exact: true }).click();
    assert(calls.includes("POST /api/updates/pause"));
    reset(); status.busy = true;
    await page.reload();
    assert(await page.getByRole("button", { name: "安装并重启", exact: true }).isDisabled());
    reset(); status.portable = false; status.ready = false; status.error = "检查失败：网络不可达";
    await page.reload();
    await page.getByText("检查失败：网络不可达", { exact: true }).waitFor();
    assert.equal(await page.getByRole("button", { name: "安装并重启", exact: true }).count(), 0);
    await page.screenshot({ path: `${output}/source-offline.png`, fullPage: true });
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ passed: true, widths: [856, 1316, 1540, 1920], output, pageErrors: errors }, null, 2));
  } catch (error) {
    await page.screenshot({ path: `${output}/failure.png`, fullPage: true });
    console.error(JSON.stringify({ output, errors, text: (await page.locator("body").innerText()).slice(0, 5000) }));
    throw error;
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
