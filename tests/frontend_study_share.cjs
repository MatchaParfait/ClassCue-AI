// Real local sharing endpoints; all files/databases are disposable and no processing is allowed.
const { chromium } = require("./playwright.cjs");
const { spawn } = require("node:child_process");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const net = require("node:net");
const path = require("node:path");

(async () => {
  const root = path.resolve(__dirname, "..");
  const output = path.join(root, ".runtime", `study-share-qa-${Date.now()}`);
  fs.mkdirSync(output, { recursive: true });
  const socket = net.createServer();
  await new Promise(resolve => socket.listen(0, "127.0.0.1", resolve));
  const port = socket.address().port;
  await new Promise(resolve => socket.close(resolve));
  const base = `http://127.0.0.1:${port}`;
  const origin = process.env.FRONTEND_URL || "http://127.0.0.1:5177";
  const server = spawn(path.join(root, ".venv/Scripts/python.exe"), [
    path.join(root, "tests/fixtures/study_share_server.py"), output, String(port),
  ], { cwd: output, windowsHide: true, env: { ...process.env, PYTHONUTF8: "1" } });
  let logs = "", browser, page;
  for (const stream of [server.stdout, server.stderr]) stream.on("data", data => { logs = (logs + data).slice(-8000); });
  const errors = [], writes = [];
  let failImport = true;
  try {
    for (let attempt = 0; attempt < 200; attempt++) {
      if (server.exitCode !== null) throw new Error(logs);
      try { if ((await fetch(`${base}/api/health`)).ok) break; } catch {}
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    browser = await chromium.launch({
      executablePath: "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", headless: true,
    });
    page = await browser.newPage({ viewport: { width: 1316, height: 949 } });
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/api/**", async route => {
      const request = route.request(), url = new URL(request.url());
      if (!url.pathname.startsWith("/api/")) return route.continue();
      assert.ok(!/\/(process|analyze|transcribe|prepare)$/.test(url.pathname), `Unexpected processing: ${url.pathname}`);
      if (request.method() !== "GET") {
        writes.push(`${request.method()} ${url.pathname}`);
        assert.ok(url.pathname.startsWith("/api/study-shares/"), `Unexpected write: ${url.pathname}`);
      }
      if (url.pathname === "/api/study-shares/import" && failImport) {
        failImport = false;
        return route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ detail: "模拟校验失效，请重新校验" }) });
      }
      return route.fulfill({ response: await route.fetch({ url: `${base}${url.pathname}${url.search}` }) });
    });
    await page.goto(origin);
    await page.locator('[data-library-key="video-1"]').waitFor();
    await page.getByRole("button", { name: "导入分享包", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "导入分享包", exact: true });
    await dialog.getByLabel("分享包文件").setInputFiles(path.join(output, "lesson.studyshare"));
    await dialog.getByText("匹配的本地视频", { exact: true }).waitFor();
    assert.equal((await (await fetch(`${base}/api/videos/1`)).json()).status, "uploaded");
    assert.equal(await dialog.getByRole("button", { name: "导入并复习", exact: true }).isEnabled(), true);
    for (const width of [856, 1316, 1540, 1920]) {
      await page.setViewportSize({ width, height: 949 });
      const bounds = await dialog.boundingBox();
      assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= width);
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await page.screenshot({ path: path.join(output, `preview-${width}.png`), fullPage: true, animations: "disabled" });
    }
    await dialog.getByRole("button", { name: "导入并复习", exact: true }).click();
    await dialog.getByRole("alert").filter({ hasText: "模拟校验失效" }).waitFor();
    assert.equal(await dialog.getByRole("button", { name: "导入并复习", exact: true }).isDisabled(), true);
    await dialog.getByRole("button", { name: "重新校验", exact: true }).click();
    await dialog.getByRole("button", { name: "导入并复习", exact: true }).click();
    await page.waitForURL("**/review/1");
    await page.locator(".marker-item").first().waitFor();
    assert.equal(await page.locator(".marker-item").count(), 2);
    assert.ok((await page.locator(".marker-item").first().textContent()).includes("分治算法"));
    await page.screenshot({ path: path.join(output, "imported-review.png"), fullPage: true, animations: "disabled" });
    await page.getByRole("tab", { name: /低召回/ }).click();
    await page.getByText("低召回已完成识别", { exact: true }).waitFor();
    await page.getByRole("tab", { name: /老师的自定义方案/ }).click();
    await page.getByText("老师的自定义方案已完成识别", { exact: true }).waitFor();
    await page.reload();
    await page.locator(".video-frame").waitFor();
    await page.getByRole("link", { name: "返回视频库", exact: true }).click();
    await page.getByRole("button", { name: "导入分享包", exact: true }).click();
    await dialog.getByLabel("分享包文件").setInputFiles(path.join(output, "lesson.studyshare"));
    await dialog.getByText("此分享包已导入，不会重复添加或覆盖。", { exact: true }).waitFor();
    await dialog.getByRole("button", { name: "取消", exact: true }).click();
    await page.locator('[data-library-key="video-1"]').getByRole("button", { name: /^管理视频/ }).click();
    const download = page.waitForEvent("download");
    await page.getByRole("menuitem", { name: "导出分享包", exact: true }).click();
    const result = await download;
    assert.ok(result.suggestedFilename().endsWith(".studyshare"));
    await result.saveAs(path.join(output, "reexport.studyshare"));
    const reexport = JSON.parse(fs.readFileSync(path.join(output, "reexport.studyshare"), "utf8"));
    assert.equal(reexport.markers.length, 2);
    assert.equal(reexport.subtitles.length, 3);
    assert.equal(reexport.analysis_runs.length, 3);
    assert.equal(reexport.markers[0].user_start_ms, 400);
    assert.equal(reexport.markers[0].importance, "critical");
    assert.deepEqual(errors, []);
    const report = { passed: true, output, errors, writes, checks: [
      "PC 856/1316/1540/1920", "preview is read-only", "fresh preview after failed commit", "direct review without ASR/AI",
      "subtitle and marker persistence", "completed empty builtin/custom modes", "idempotent duplicate", "browser download",
    ] };
    fs.writeFileSync(path.join(output, "report.json"), JSON.stringify(report, null, 2));
    console.log(JSON.stringify(report, null, 2));
  } catch (error) {
    if (page) await page.screenshot({ path: path.join(output, "failure.png"), fullPage: true });
    console.error(logs);
    throw error;
  } finally {
    if (browser) await browser.close();
    if (server.exitCode === null) {
      const exited = new Promise(resolve => server.once("exit", resolve));
      server.kill(); await exited;
    }
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
