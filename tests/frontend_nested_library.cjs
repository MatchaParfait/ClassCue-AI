// Real recursive imports and UI navigation against disposable data and simulated ASR/AI.
const { chromium } = require("./playwright.cjs");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const net = require("node:net");
const assert = require("node:assert/strict");

(async () => {
  const root = path.resolve(__dirname, "..");
  const output = path.join(root, ".runtime", `nested-library-${Date.now()}`);
  const fixture = path.join(output, "application");
  fs.mkdirSync(fixture, { recursive: true });
  for (const name of fs.readdirSync(root)) {
    if (name.endsWith(".py")) fs.copyFileSync(path.join(root, name), path.join(fixture, name));
  }
  for (const name of ["pyproject.toml", "release_config.json", "uv.lock"]) fs.copyFileSync(path.join(root, name), path.join(fixture, name));
  for (const name of ["api", "scripts"]) fs.cpSync(path.join(root, name), path.join(fixture, name), {
    recursive: true, filter: file => fs.statSync(file).isDirectory() ? !file.includes("__pycache__") : file.endsWith(".py"),
  });
  const listener = net.createServer();
  await new Promise(resolve => listener.listen(0, "127.0.0.1", resolve));
  const port = listener.address().port;
  await new Promise(resolve => listener.close(resolve));
  const base = `http://127.0.0.1:${port}`;
  const origin = process.env.FRONTEND_URL || base;
  let logs = "", browser;
  const server = spawn(path.join(root, ".venv/Scripts/python.exe"), [
    path.join(root, "tests/fixtures/web_batch_server.py"), output, String(port),
  ], { cwd: fixture, windowsHide: true, env: {
    ...process.env, PYTHONUTF8: "1", QA_FRONTEND_PROJECT_ROOT: root,
  } });
  for (const stream of [server.stdout, server.stderr]) stream.on("data", data => { logs = (logs + data).slice(-8000); });
  const request = async (endpoint, payload) => {
    const response = await fetch(`${base}${endpoint}`, payload === undefined ? {} : {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    const value = await response.json();
    assert.ok(response.ok, JSON.stringify(value));
    return value;
  };
  try {
    for (let index = 0; ; index++) {
      assert.ok(index < 150 && server.exitCode === null, logs || "Fixture startup failed");
      try { if ((await fetch(`${base}/api/health`)).ok) break; } catch {}
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    const state = await request("/__qa__/state");
    await request("/__qa__/repair", {});
    const preview = await request("/api/batch/preview", { folders: [state.paths.nested] });
    assert.equal(preview.valid, true);
    assert.equal(preview.folders[0].video_count, 3);
    assert.equal(preview.folders[0].subfolder_count, 3);
    const started = await request("/api/batch/process", { folders: [state.paths.nested] });
    for (let index = 0; ; index++) {
      const job = await request(`/api/jobs/${started.job_id}`);
      if (job.status === "completed") break;
      assert.ok(index < 200 && !["failed", "cancelled"].includes(job.status), JSON.stringify(job));
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    await request("/__qa__/seed-markers", {});
    const top = (await request("/api/library")).items.find(item => item.type === "folder").folder;
    assert.equal(top.video_count, 3);
    assert.deepEqual(top.marker_importance_counts, { standard: 3, important: 3, critical: 3 });
    const topContents = await request(`/api/library?folder_id=${top.id}`);
    const child = topContents.items.find(item => item.folder?.name === "child").folder;
    const empty = topContents.items.find(item => item.folder?.name === "empty").folder;
    const lesson = (await request(`/api/library?folder_id=${child.id}`)).items.find(item => item.type === "folder").folder;
    browser = await chromium.launch({
      executablePath: "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", headless: true,
    });
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/api/**", async route => {
      const url = new URL(route.request().url());
      const response = await route.fetch({ url: `${base}${url.pathname}${url.search}` });
      await route.fulfill({ response });
    });
    await page.goto(origin);
    const open = id => page.locator(`[data-library-key="folder-${id}"] .folder-card-link`).click();
    const topCard = page.locator(`[data-library-key="folder-${top.id}"]`);
    await topCard.getByText("3 个视频", { exact: true }).waitFor();
    assert.deepEqual(await topCard.locator(".video-marker-count").allTextContents(), ["3", "3", "3"]);
    for (const width of [1440, 1024, 390]) {
      await page.setViewportSize({ width, height: 1000 });
      assert.ok(await topCard.evaluate(card => [...card.querySelectorAll(".video-marker-count")].every(badge => {
        const a = card.getBoundingClientRect(), b = badge.getBoundingClientRect();
        return b.left >= a.left && b.right <= a.right && b.bottom <= a.bottom;
      })), `${width}: folder badges overflow`);
      await page.screenshot({ path: path.join(output, `folder-totals-${width}.png`), fullPage: true });
    }
    await page.setViewportSize({ width: 1440, height: 1000 });
    await open(top.id);
    await page.locator(`.folder-window [data-library-key="folder-${child.id}"]`).waitFor();
    const childCard = page.locator(`.folder-window [data-library-key="folder-${child.id}"]`);
    await childCard.getByText("2 个视频", { exact: true }).waitFor();
    assert.deepEqual(await childCard.locator(".video-marker-count").allTextContents(), ["2", "2", "2"]);
    await childCard.scrollIntoViewIfNeeded();
    await page.waitForTimeout(400);
    await page.screenshot({ path: path.join(output, "child-folder-totals.png"), fullPage: true });
    assert.equal(await page.locator(".library-inner .folder-card").count(), 1);
    await open(child.id);
    await open(lesson.id);
    await page.locator(".folder-window .video-card").waitFor();
    assert.ok((await page.locator(".folder-breadcrumbs").textContent()).includes("nestedchildlesson"));
    for (const width of [1440, 1024, 390]) {
      await page.setViewportSize({ width, height: 1000 });
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${width}: page overflow`);
      assert.ok(await page.locator(".folder-window").evaluate(node => node.scrollWidth <= node.clientWidth + 1), `${width}: dialog overflow`);
      assert.ok(await page.locator("#folder-window-title").evaluate(node => {
        const rect = node.getBoundingClientRect();
        const target = document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2);
        return target === node || node.contains(target);
      }), `${width}: folder heading covered`);
      await page.screenshot({ path: path.join(output, `nested-${width}.png`), fullPage: true });
    }
    await page.getByRole("button", { name: "返回上一级", exact: true }).click();
    await page.locator(`.folder-window [data-library-key="folder-${lesson.id}"]`).waitFor();
    await page.getByRole("button", { name: "返回上一级", exact: true }).click();
    await open(empty.id);
    await page.getByText("这个文件夹还没有视频", { exact: true }).waitFor();
    await page.getByRole("button", { name: "返回上一级", exact: true }).click();
    await page.locator(".folder-window-toolbar").getByRole("button", { name: "新建文件夹", exact: true }).click();
    await page.locator(".folder-dialog input").fill("新子文件夹");
    await page.locator(".folder-dialog").getByRole("button", { name: "创建", exact: true }).click();
    await page.getByRole("button", { name: "打开文件夹“新子文件夹”", exact: true }).waitFor();
    await page.keyboard.press("Escape");
    await page.locator(".folder-window").waitFor({ state: "detached" });
    assert.equal(await page.locator(".library-inner .folder-card").count(), 1);
    // A non-video file several levels deep rejects the entire multi-root request.
    fs.writeFileSync(path.join(state.paths.nested, "child", "lesson", "notes.txt"), "not a video");
    const before = await request("/__qa__/state");
    const blocked = await fetch(`${base}/api/batch/process`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folders: [state.paths.pure, state.paths.nested] }),
    });
    assert.equal(blocked.status, 400);
    assert.deepEqual((await request("/__qa__/state")).videos, before.videos);
    assert.equal(fs.readdirSync(state.paths.pure).length, 3);
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({
      passed: true, checks: ["Recursive real import", "Preserved folder tree and empty folders", "Nested navigation",
        "Create nested folder", "1440/1024/390 layouts", "Non-video rejection before any imports"], errors,
    }, null, 2));
    console.log(JSON.stringify({ passed: true, output }, null, 2));
  } finally {
    if (browser) await browser.close();
    if (server.exitCode === null) {
      const exited = new Promise(resolve => server.once("exit", resolve));
      server.kill();
      await exited;
    }
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
