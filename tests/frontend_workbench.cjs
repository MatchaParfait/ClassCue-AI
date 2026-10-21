// Run against a Vite server; all data writes stay in a disposable application.
const { chromium } = require("./playwright.cjs");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const net = require("node:net");
const path = require("node:path");
const assert = require("node:assert/strict");

const root = path.resolve(__dirname, "..").replaceAll("\\", "/");
const output = `${root}/.runtime/workbench-qa-${Date.now()}`;
const origin = process.env.FRONTEND_URL || "http://127.0.0.1:5175";
let server, browser, page, logs = "";
const requests = [];
async function chooseRecall(scope, modes) {
  const choices = { high_recall: "高召回", medium_recall: "中召回", low_recall: "低召回" };
  for (const mode of modes) await scope.getByRole("checkbox", { name: choices[mode], exact: true }).check();
  for (const [mode, label] of Object.entries(choices)) {
    if (!modes.includes(mode)) await scope.getByRole("checkbox", { name: label, exact: true }).uncheck();
  }
}
async function checkedRecall(scope) {
  return scope.locator(".recall-mode-control input:checked").evaluateAll(nodes => nodes.map(node => node.value));
}
async function stopServer() {
  if (server && server.exitCode === null) {
    const exited = new Promise((resolve) => server.once("exit", resolve));
    server.kill();
    await exited;
  }
}
async function main() {
  const fixture = `${output}/application`;
  fs.mkdirSync(fixture, { recursive: true });
  for (const file of fs.readdirSync(root)) if (file.endsWith(".py")) fs.copyFileSync(`${root}/${file}`, `${fixture}/${file}`);
  for (const file of ["pyproject.toml", "release_config.json", "uv.lock"]) fs.copyFileSync(`${root}/${file}`, `${fixture}/${file}`);
  for (const dir of ["api", "scripts"]) fs.cpSync(`${root}/${dir}`, `${fixture}/${dir}`, {
    recursive: true, filter: (item) => fs.statSync(item).isDirectory() ? !item.includes("__pycache__") : item.endsWith(".py"),
  });
  const protectedFiles = ["data/asr_settings.json", "data/asr_storage.json", "data/video_workspace.json", ".runtime/frontend_state.json"];
  const originals = protectedFiles.map((file) => fs.existsSync(`${root}/${file}`) ? fs.readFileSync(`${root}/${file}`) : null);
  const socket = net.createServer();
  await new Promise((resolve) => socket.listen(0, "127.0.0.1", resolve));
  const port = socket.address().port;
  await new Promise((resolve) => socket.close(resolve));
  const base = `http://127.0.0.1:${port}`;
  async function startServer() {
    server = spawn(`${root}/.venv/Scripts/python.exe`, [`${root}/tests/fixtures/workbench_server.py`, output, String(port)], {
      cwd: fixture, windowsHide: true, env: { ...process.env, PYTHONUTF8: "1" },
    });
    for (const stream of [server.stdout, server.stderr]) stream.on("data", (data) => { logs = (logs + data).slice(-6000); });
    for (let attempt = 0; attempt < 200; attempt++) {
      if (server.exitCode !== null) throw new Error(logs);
      try { if ((await fetch(`${base}/api/health`)).ok) return; } catch {}
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    throw new Error(`Fixture did not start: ${logs}`);
  }
  try {
    await startServer();
    browser = await chromium.launch({ executablePath: "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", headless: true });
    page = await browser.newPage({ viewport: { width: 1316, height: 949 },
      recordVideo: process.env.THEME_QA || process.env.SEARCH_QA ? { dir: output, size: { width: 1440, height: 960 } } : undefined });
    await page.addInitScript(() => {
      const connect = AudioNode.prototype.connect;
      AudioNode.prototype.connect = function (destination, ...ports) {
        const result = connect.call(this, destination, ...ports);
        if (destination === this.context.destination) {
          const splitter = this.context.createChannelSplitter(2);
          const meters = [this.context.createAnalyser(), this.context.createAnalyser()];
          connect.call(this, splitter);
          meters.forEach((meter, channel) => splitter.connect(meter, channel));
          window.__audioMeters = meters;
        }
        return result;
      };
    });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    let job = null, failMove = false, failRetry = false, configSaves = 0, starts = 0, stops = 0, retries = 0;
    let failSelectedBatch = false;
    const selectedBatchRequests = [];
    const asr = { id: "whisper-turbo", name: "Whisper turbo", engine: "whisper", selected: true,
      downloaded: true, cache_size_bytes: 1024, inspection_status: "ready" };
    let ai = { connection_name: "测试连接", provider: "deepseek", base_url: "https://api.example.test", model: "fixture-model",
      output_tokens: 2048, token_parameter: "max_tokens", json_mode: false, timeout_seconds: 60, max_retries: 2, has_api_key: true };
    const workspace = `${output}/workspace`;
    const directories = { library_id: "workbench-fixture", data_root: workspace, asr_storage_root: `${output}/assets`, audio_cache_dir: `${workspace}/audio`,
      audio_cache_default_dir: `${workspace}/temporary_audio`, video_library_dir: `${workspace}/videos`,
      video_library_default_dir: `${workspace}/videos`, log_dir: `${output}/logs`, active_log_dir: `${output}/logs`,
      model_dir: `${output}/assets/models`, runtime_dir: `${output}/assets/runtime`, package_cache_dir: `${output}/assets/cache`,
      active_asr_storage_root: `${output}/assets`, config_dir: `${fixture}/data`, restart_required: false, busy: false,
      has_paused_jobs: false, delete_source_after_import: true };
    await page.route("**/api/**", async (route) => {
      const url = new URL(route.request().url()), pathname = url.pathname, method = route.request().method();
      if (!pathname.startsWith("/api/")) return route.continue();
      requests.push(`${method} ${pathname}${route.request().postData() ? ` ${route.request().postData()}` : ""}`);
      const json = (body, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
      if (process.env.SEARCH_QA && pathname === "/api/library/status") return route.fulfill({ response: await route.fetch({ url: `${base}${pathname}` }) });
      if (pathname === "/api/library/status") return json({ library_id: "workbench-fixture", data_root: workspace, database_path: `${workspace}/project.db`,
        temp_audio_dir: directories.audio_cache_dir, video_count: 24, active_job_id: job && !["awaiting_api", "completed", "failed", "resource_paused", "interrupted"].includes(job.status) ? job.id : null });
      if (pathname === "/api/settings/directories") return json(directories);
      if (pathname === "/api/asr/selection") {
        if (method === "PUT") Object.assign(asr, { id: route.request().postDataJSON().model_id, name: "Whisper small" });
        return json(asr);
      }
      if (pathname === "/api/asr/models") return json([
        { ...asr, id: "whisper-turbo", name: "Whisper turbo", selected: asr.id === "whisper-turbo" },
        { ...asr, id: "whisper-small", name: "Whisper small", selected: asr.id === "whisper-small" },
      ]);
      if (pathname.includes("/api/asr/models/")) {
        const id = pathname.split("/")[4];
        return json({ ...asr, id, name: id === "whisper-small" ? "Whisper small" : "Whisper turbo" });
      }
      if (pathname === "/api/settings/ai") {
        if (method === "PUT") { ai = { ...ai, ...route.request().postDataJSON() }; configSaves++; }
        return json(ai);
      }
      if (pathname === "/api/settings/ai/models") return json(["fixture-model", "fixture-model-two"]);
      if (pathname === "/api/jobs/paused") return json(job?.status === "resource_paused" ? [job] : []);
      if (pathname === "/api/batch/latest") return json(job?.job_type === "batch_video_process" ? job : null);
      if (pathname.startsWith("/api/jobs/")) {
        if (pathname.endsWith("/cancel")) { stops++; job = { ...job, status: "interrupted", message: "用户停止" }; }
        if (pathname.endsWith("/retry") || pathname.endsWith("/resume")) {
          if (failRetry) { failRetry = false; return json({ detail: "fixture retry failed" }, 503); }
          retries++; job = { ...job, id: `retry-${retries}`, job_id: `retry-${retries}`, status: "running", progress: 20, message: "复用字幕，继续 AI" };
        }
        return json(job);
      }
      if (pathname.endsWith("/processing-options")) return json({ asr_model: asr,
        ai_service: { model: ai.model, provider: "deepseek", configured: true },
        active_job: job?.video_id === Number(pathname.split("/")[3]) ? job : null });
      if (/\/api\/videos\/\d+\/process$/.test(pathname)) {
        starts++; job = { id: "single", job_id: "single", video_id: Number(pathname.split("/")[3]),
          job_type: "video_process", status: "running", progress: 22, message: "正在转写" };
        return json(job, 202);
      }
      if (pathname === "/api/batch/process" && method === "POST" && route.request().postDataJSON().video_ids) {
        const payload = route.request().postDataJSON();
        selectedBatchRequests.push(payload);
        if (failSelectedBatch) {
          failSelectedBatch = false;
          return json({ detail: "fixture selected queue unavailable" }, 503);
        }
        const rows = await (await fetch(`${base}/api/videos`)).json();
        job = { id: `selected-${selectedBatchRequests.length}`, job_id: `selected-${selectedBatchRequests.length}`,
          job_type: "batch_video_process", status: "running", progress: 12, message: "正在处理选中视频",
          batch_summary: { total: payload.video_ids.length, completed: 0, skipped: 0, failed: 0, awaiting_ai: 0, pending_asr: payload.video_ids.length },
          batch_entries: payload.video_ids.map((id) => ({ video_id: id, label: rows.find((row) => row.id === id)?.display_name, status: "pending_asr" })) };
        return json({ job_id: job.id, status: "queued", job_type: job.job_type, video_ids: payload.video_ids, total: payload.video_ids.length }, 202);
      }
      if (/\/api\/videos\/\d+\/folder$/.test(pathname) && failMove) { failMove = false; return json({ detail: "fixture move failed" }, 503); }
      const response = await route.fetch({ url: `${base}${pathname}${url.search}` });
      await route.fulfill({ response });
    });
    const visible = () => page.locator(".workbench-screen:not([hidden])");
    const card = (id, folder = false) => page.locator(`${folder ? ".folder-window " : ".library-inner "}[data-library-key="video-${id}"]`);
    const closePanel = async () => {
      if (new URL(page.url()).pathname === "/tasks") await page.getByRole("button", { name: "关闭面板", exact: true }).click();
      else await visible().locator(".page-back-row button").click();
      await page.waitForURL(`${origin}/`);
    };
    const openImport = async (kind) => { await page.getByRole("link", { name: `导入${kind}`, exact: true }).click(); };
    async function geometry(width) {
      await page.setViewportSize({ width, height: 949 });
      await require("./frontend_header_feedback.cjs")({ page });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      assert.ok(Math.abs((await page.locator(".library-inner").boundingBox()).width - Math.min(width - 64, 1640)) < 1);
      for (const item of await page.locator(".library-inner .video-card").all()) {
        const thumbnail = await item.locator(".video-thumbnail").boundingBox();
        const body = await item.locator(".library-card-body").boundingBox();
        const actions = await item.locator(".library-card-actions").boundingBox();
        assert.ok(Math.abs(thumbnail.width / thumbnail.height - 16 / 9) < .02);
        assert.ok(body.y >= thumbnail.y + thumbnail.height - 1);
        assert.ok(body.y + body.height <= actions.y + 1);
        for (const label of await item.locator(".status-badge, .video-marker-count").all()) {
          const box = await label.boundingBox(); assert.ok(box.x + box.width <= actions.x + 1);
        }
        assert.equal(await item.evaluate((element) => getComputedStyle(element).opacity), "1");
      }
      const columns = await page.locator('.library-inner > .library-grid-list').evaluate((element) => getComputedStyle(element).gridTemplateColumns.split(" ").length);
      assert.equal(columns, ({ 856: 2, 1316: 3, 1540: 4, 1920: 5 })[width]);
      assert.equal(await page.locator('.library-folder-section .video-card').count(), 0);
    }
    async function panelScreenshots(name) {
      for (const width of [856, 1316, 1540, 1920]) {
        await page.setViewportSize({ width, height: 949 });
        await page.evaluate(() => window.scrollTo(0, 0));
        await page.waitForTimeout(200);
        assert.equal(await page.locator('[aria-modal="true"]:visible').count(), 0);
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
        assert.equal((await page.locator('.workbench-topbar').boundingBox()).y, 0);
        assert.ok((await visible().locator('h1').boundingBox()).y >= 64);
        await require("./frontend_header_feedback.cjs").returnBorder(visible().locator(".page-back-row button"));
        if (width >= 1100) {
          const title = await visible().locator('h1').boundingBox();
          const backRow = await visible().locator('.page-back-row').boundingBox();
          assert.ok(Math.abs(title.x - backRow.x - 24) < 1, 'Page heading must align with the return control');
        }
        if (name === "settings") {
          const atmosphere = visible().locator(".settings-page > .warm-atmosphere");
          assert.equal(await atmosphere.getAttribute("data-moving"), "true");
          assert.equal((await atmosphere.boundingBox()).y, 64);
          assert.equal((await atmosphere.boundingBox()).width, width);
          assert.deepEqual(await atmosphere.locator("span").evaluateAll(nodes =>
            nodes.map(node => getComputedStyle(node).animationDuration)), ["48s", "64s"]);
          const before = await atmosphere.evaluate(node => node.getAnimations({ subtree: true }).map(a => a.currentTime));
          await page.waitForTimeout(150);
          const after = await atmosphere.evaluate(node => node.getAnimations({ subtree: true }).map(a => a.currentTime));
          assert.ok(after.every((time, index) => time > before[index]));
          await page.emulateMedia({ reducedMotion: "reduce" });
          assert.deepEqual(await atmosphere.locator("span").evaluateAll(nodes =>
            nodes.map(node => getComputedStyle(node).animationName)), ["none", "none"]);
          await page.emulateMedia({ reducedMotion: "no-preference" });
          await require("./frontend_settings_herons.cjs")({ page, scope: visible(), width });
        }
        await page.screenshot({ path: `${output}/${name}-${width}.png`, fullPage: true, animations: "disabled" });
      }
      await page.setViewportSize({ width: 1316, height: 949 });
      if (name === "settings") {
        await page.setViewportSize({ width: 1920, height: 949 });
        for (const [category, selector] of [["存储与导入", ".storage-directories-section"], ["桌面设置", ".settings-navigation"]]) {
          await visible().getByRole("button", { name: category, exact: true }).click();
          await visible().locator(selector).waitFor();
          await page.waitForTimeout(250);
          await require("./frontend_settings_herons.cjs")({ page, scope: visible(), width: 1920 });
          await page.screenshot({ path: `${output}/settings-herons-${category === "存储与导入" ? "long" : "short"}.png`, fullPage: true, animations: "disabled" });
        }
        await visible().getByRole("button", { name: "AI 服务", exact: true }).click();
        await visible().locator("#ai-settings-title").waitFor();
        await page.setViewportSize({ width: 1316, height: 949 });
      }
    }
    await page.goto(origin);
    if (process.env.SEARCH_QA) {
      await require("./frontend_search.cjs")({ page, origin, output, requests, base });
      assert.deepEqual(errors, []);
      console.log(JSON.stringify({ output, passed: true, checks: "Global subtitle search against disposable real API" }));
      return;
    }
    await card(2).waitFor();
    await page.evaluate(async () => { await Promise.all([400, 500, 600].map((weight) => document.fonts.load(`${weight} 14px "Noto Sans SC"`))); });
    assert.ok(await page.evaluate(() => [...document.fonts].some((font) => font.family === "Noto Sans SC" && font.status === "loaded")));
    assert.ok(await page.locator('body').evaluate((body) => getComputedStyle(body).fontFamily.startsWith('"Noto Sans SC"')));
    for (const thumbnail of await page.locator('.library-inner .video-thumbnail img').all()) {
      await thumbnail.evaluate((image) => image.decode());
      assert.deepEqual(await thumbnail.evaluate((image) => [image.naturalWidth, image.naturalHeight]), [640, 360]);
    }
    for (const width of [856, 1316, 1540, 1920]) {
      await geometry(width);
      await page.screenshot({ path: `${output}/library-${width}.png`, fullPage: true, animations: "disabled" });
    }
    await require("./frontend_library_atmosphere.cjs")({ page, output });
    await page.setViewportSize({ width: 1316, height: 949 });
    await card(2).getByRole("button", { name: /^管理视频/ }).focus();
    await page.keyboard.press("ArrowDown");
    await page.keyboard.press("ArrowDown");
    assert.equal(await page.evaluate(() => document.activeElement.textContent), "算法课程");
    await page.keyboard.press("Escape");
    assert.equal(await page.getByRole("menu").count(), 0);
    // The current workbench no longer exposes the old More menu; the demo route remains supported.
    await page.goto(`${origin}/demo`);
    await page.locator(".video-frame").waitFor();
    await page.goBack();
    await card(2).waitFor();

    // A failed move must remain actionable without opening the video.
    failMove = true;
    await card(2).getByRole("button", { name: /^管理视频/ }).click();
    await card(2).getByRole("menuitem", { name: "算法课程", exact: true }).click();
    await page.locator(".library-operation-error").waitFor();
    await page.locator(".library-operation-error").getByRole("button", { name: "重试", exact: true }).click();
    await card(2).waitFor({ state: "detached" });
    assert.equal(new URL(page.url()).pathname, "/");
    await page.getByRole("button", { name: "打开文件夹“算法课程”" }).click();
    await card(2, true).waitFor();
    await card(2, true).locator(".library-card-body").dragTo(page.locator(".folder-window-backdrop"), { targetPosition: { x: 8, y: 100 } });
    await card(2, true).waitFor({ state: "detached" });
    await page.getByRole("button", { name: "关闭文件夹" }).click();
    await card(2).waitFor();
    await page.getByLabel("视频库排序").selectOption("name");
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.waitForTimeout(300);
    const sourceHandle = card(2).locator(".drag-handle");
    await sourceHandle.scrollIntoViewIfNeeded();
    const sourceBox = await sourceHandle.boundingBox();
    await page.mouse.move(sourceBox.x + sourceBox.width / 2, sourceBox.y + sourceBox.height / 2);
    await page.mouse.down();
    await page.mouse.move(sourceBox.x + sourceBox.width / 2 - 12, sourceBox.y + sourceBox.height / 2 - 12, { steps: 4 });
    await page.evaluate(() => window.scrollTo(0, 0));
    const targetBox = await page.locator(".folder-card-link").boundingBox();
    await page.mouse.move(targetBox.x + 20, targetBox.y + 20, { steps: 8 });
    await page.mouse.up();
    await card(2).waitFor({ state: "detached" });
    assert.equal(new URL(page.url()).pathname, "/");

    // Folder rename, sorting and scroll survive both panels and review.
    await page.getByRole("button", { name: "重命名文件夹“算法课程”" }).click();
    await page.locator(".folder-dialog input").fill("算法课程 · 已整理");
    await page.locator(".folder-dialog").getByRole("button", { name: "保存", exact: true }).click();
    await page.getByRole("button", { name: "打开文件夹“算法课程 · 已整理”" }).click();
    await page.getByLabel("文件夹内排序").selectOption("name");
    await card(14, true).waitFor();
    await card(14, true).scrollIntoViewIfNeeded();
    const folderScroll = await page.locator(".folder-window-content").evaluate((element) => element.scrollTop);
    await card(14, true).locator(".library-thumbnail-link").click();
    await visible().locator(".video-info-selected").waitFor();
    assert.equal(await page.locator('[aria-modal="true"]:visible').count(), 0);
    assert.equal(await page.locator(".folder-window").count(), 0);
    await visible().getByRole("button", { name: "修改文件名", exact: true }).click();
    await visible().locator("#video-filename").fill("重命名后的课程.mp4");
    await visible().getByRole("button", { name: "保存文件名", exact: true }).click();
    await visible().getByRole("heading", { name: "重命名后的课程.mp4", exact: true }).waitFor();
    await chooseRecall(visible(), ["low_recall"]);
    await panelScreenshots("process");
    await visible().locator('a[href="/settings?section=ai"]').click();
    await visible().locator("#ai-settings-title").waitFor();
    await panelScreenshots("settings");
    const modelField = visible().locator(".settings-field").filter({ hasText: "模型名称" }).locator("input");
    await modelField.fill("draft-model");
    await visible().getByRole("button", { name: "获取模型", exact: true }).click();
    await page.getByRole("button", { name: "关闭模型列表", exact: true }).waitFor();
    await page.keyboard.press("Escape");
    assert.equal(await page.locator(".models-dialog").count(), 0);
    assert.equal(new URL(page.url()).pathname, "/settings");
    await modelField.fill("saved-model");
    await visible().getByRole("button", { name: "保存连接", exact: true }).click();
    await page.waitForURL(`${origin}/process?videoId=14`);
    assert.equal(configSaves, 1);
    await visible().getByRole("heading", { name: "重命名后的课程.mp4", exact: true }).waitFor();
    await visible().locator(".readonly-config strong").filter({ hasText: "saved-model" }).waitFor();
    assert.deepEqual(await checkedRecall(visible()), ["low_recall"]);
    await closePanel();
    await page.locator(".folder-window").waitFor();
    assert.equal(await page.getByLabel("文件夹内排序").inputValue(), "name");
    assert.ok(Math.abs(await page.locator(".folder-window-content").evaluate((element) => element.scrollTop) - folderScroll) < 5);
    await page.goForward();
    await page.waitForURL(`${origin}/process?videoId=14`);
    await page.goBack();
    await page.locator(".folder-window").waitFor();
    await page.reload();
    await page.locator(".folder-window").waitFor();
    await card(13, true).locator(".library-thumbnail-link").click();
    await page.waitForURL(`${origin}/review/13`);
    await page.locator(".topbar").getByRole("link", { name: "返回视频库", exact: false }).click();
    await page.locator(".folder-window").waitFor();
    assert.equal(await page.getByLabel("文件夹内排序").inputValue(), "name");
    await page.getByRole("button", { name: "关闭文件夹" }).click();

    // Import uses a real temporary file; a mock task continues after panel dismissal.
    await openImport("视频");
    await visible().locator(".video-file-picker").waitFor();
    await visible().locator(".readonly-config").filter({ hasText: "当前 AI 服务" })
      .locator("strong").filter({ hasText: ai.model }).waitFor();
    await panelScreenshots("import");
    await visible().getByText("手动输入路径", { exact: true }).click();
    await visible().locator("#video-path").fill(`${output}/import.mp4`);
    await visible().getByRole("button", { name: "导入视频库", exact: true }).click();
    await visible().locator(".video-info-selected").waitFor();
    const importedId = Number(new URL(page.url()).searchParams.get("videoId"));
    assert.ok(importedId > 24);
    await page.reload();
    await visible().locator(".video-info-selected").waitFor();
    await visible().getByRole("button", { name: "开始处理", exact: true }).click();
    await visible().locator(".job-status").waitFor();
    assert.equal(starts, 1);
    await closePanel();
    assert.equal(stops, 0);
    await page.getByRole("button", { name: "查看任务详情" }).click();
    await page.locator(".task-details-title h3").filter({ hasText: "处理中" }).waitFor();
    await page.locator(".task-danger-zone").getByRole("button", { name: "停止任务", exact: true }).click();
    await page.locator(".task-details-title h3").filter({ hasText: "已停止" }).waitFor();
    assert.equal(stops, 1);
    await closePanel();

    // Import drafts stay in memory; unsaved settings require an explicit leave decision.
    await openImport("文件夹");
    await visible().locator(".batch-folder-row input").first().fill(`${output}/mixed`);
    await visible().getByText("该文件夹或其子文件夹包含非视频文件", { exact: false }).waitFor();
    assert.equal(await visible().getByRole("button", { name: "导入并开始批处理", exact: true }).isDisabled(), true);
    await visible().locator(".batch-folder-row input").first().fill(`${output}/pure`);
    await visible().getByRole("button", { name: "导入并开始批处理", exact: true }).waitFor();
    await page.waitForFunction(() => ![...document.querySelectorAll(".workbench-screen:not([hidden]) button")].find((button) => button.textContent.includes("导入并开始批处理"))?.disabled);
    await panelScreenshots("batch");
    await visible().locator('a[href="/settings?section=asr"]').click();
    await visible().locator("#asr-settings-title").waitFor();
    await panelScreenshots("settings-asr");
    await visible().getByRole("button", { name: "存储与导入", exact: true }).click();
    await visible().getByRole("button", { name: "ASR 模型", exact: true }).click();
    await visible().locator(".asr-more-models > summary").click();
    const engineCard = visible().locator('[data-model-id="whisper-small"]');
    await page.mouse.move(0, 0);
    const engineRest = await engineCard.evaluate(node => getComputedStyle(node).backgroundColor);
    await engineCard.locator(".asr-model-meta").hover();
    await page.waitForTimeout(180);
    assert.notEqual(await engineCard.evaluate(node => getComputedStyle(node).backgroundColor), engineRest,
      "unselected engine cards must visibly respond to hover");
    await panelScreenshots("settings-asr-expanded");
    const selectionWrites = requests.filter(request => request.startsWith("PUT /api/asr/selection")).length;
    await engineCard.locator(".asr-model-meta").click();
    assert.equal(await engineCard.locator('input[name="asr-model"]').isChecked(), true);
    assert.equal(requests.filter(request => request.startsWith("PUT /api/asr/selection")).length, selectionWrites,
      "clicking the card selects a draft but must not save immediately");
    await page.keyboard.press("Tab");
    await engineCard.locator('input[name="asr-model"]').focus();
    assert.equal(await engineCard.evaluate(node => getComputedStyle(node).outlineStyle), "solid");
    await visible().getByRole("button", { name: "保存模型选择", exact: true }).click();
    await page.waitForURL(`${origin}/batch-process`);
    assert.equal(await visible().locator(".batch-folder-row input").first().inputValue(), `${output}/pure`);
    await closePanel();
    await page.getByRole("link", { name: "设置", exact: true }).click();
    await visible().getByRole("button", { name: "快捷键", exact: true }).click();
    await visible().getByRole("heading", { name: "快捷键设置", exact: true }).waitFor();
    assert.equal(await visible().locator(".shortcut-group h3").filter({ hasText: "字幕位置" }).count(), 0);
    assert.equal(await visible().locator(".shortcut-row").filter({ hasText: "字幕向" }).count(), 0);
    await panelScreenshots("settings-shortcuts");
    await visible().getByRole("button", { name: "AI 服务", exact: true }).click();
    await visible().locator("#ai-settings-title").waitFor();
    const key = visible().locator('input[type="password"]');
    const savedKeyDisplay = await key.inputValue();
    const unsaved = page.getByRole("dialog", { name: "有未保存的修改", exact: true });
    const draftKey = "not-a-real-key-draft";
    const savesBeforeDraft = configSaves;
    await key.fill(draftKey);
    await page.keyboard.press("Escape");
    assert.equal(new URL(page.url()).pathname, "/settings");
    assert.equal(await key.inputValue(), draftKey);
    assert.equal(await page.evaluate(value => JSON.stringify({ ...localStorage, ...sessionStorage }).includes(value), draftKey), false);
    await visible().getByRole("button", { name: "存储与导入", exact: true }).click();
    await unsaved.waitFor();
    assert.equal(new URL(page.url()).search, "?section=ai");
    assert.equal(await page.evaluate(() => document.activeElement.textContent), "继续编辑");
    await unsaved.getByRole("button", { name: "继续编辑", exact: true }).click();
    await unsaved.waitFor({ state: "detached" });
    assert.equal(await key.inputValue(), draftKey);
    assert.equal(configSaves, savesBeforeDraft);
    await visible().getByRole("button", { name: "存储与导入", exact: true }).click();
    await unsaved.waitFor();
    await unsaved.getByRole("button", { name: "放弃修改", exact: true }).click();
    await page.waitForURL(`${origin}/settings?section=storage`);
    await unsaved.waitFor({ state: "detached" });
    await panelScreenshots("settings-storage");
    await visible().getByRole("button", { name: "AI 服务", exact: true }).click();
    assert.equal(await key.inputValue(), savedKeyDisplay);

    // Cancelling a page-level leave also keeps the key, while discarding clears it.
    await key.fill(draftKey);
    await visible().locator(".page-back-row button").click();
    await unsaved.waitFor();
    await unsaved.getByRole("button", { name: "继续编辑", exact: true }).click();
    await unsaved.waitFor({ state: "detached" });
    assert.equal(new URL(page.url()).search, "?section=ai");
    assert.equal(await key.inputValue(), draftKey);
    assert.equal(await page.evaluate(value => JSON.stringify({ ...localStorage, ...sessionStorage }).includes(value), draftKey), false);
    await visible().locator(".page-back-row button").click();
    await unsaved.waitFor();
    await unsaved.getByRole("button", { name: "放弃修改", exact: true }).click();
    await page.waitForURL(`${origin}/`);
    await unsaved.waitFor({ state: "detached" });
    assert.equal(configSaves, savesBeforeDraft);
    assert.equal(requests.some(request => request.includes(draftKey)), false);
    await page.getByRole("link", { name: "设置", exact: true }).click();
    await visible().getByRole("button", { name: "AI 服务", exact: true }).click();
    assert.equal(await key.inputValue(), savedKeyDisplay);
    await closePanel();

    // Restore the recent batch, distinguish API/resource failures and retry only on click.
    job = { id: "batch-wait", job_id: "batch-wait", status: "awaiting_api", progress: 100, job_type: "batch_video_process",
      message: "字幕已保存，等待 AI 恢复", batch_summary: { total: 3, completed: 1, skipped: 0, failed: 0, awaiting_ai: 2, pending_asr: 0 },
      batch_entries: [{ video_id: 1, label: "已完成课程", status: "completed" }, { video_id: 2, label: "等待课程", status: "pending_ai" }] };
    await page.reload();
    await page.getByRole("button", { name: "查看任务详情" }).filter({ hasText: "等待 AI 恢复" }).waitFor();
    assert.equal(retries, 0);
    await page.getByRole("button", { name: "查看任务详情" }).click();
    await page.locator(".task-summary").waitFor();
    await page.screenshot({ path: `${output}/awaiting-api.png`, animations: "disabled" });
    failRetry = true;
    await page.getByRole("button", { name: "重试未完成视频", exact: true }).click();
    await page.locator(".task-details .field-error").filter({ hasText: "fixture retry failed" }).waitFor();
    await page.getByRole("button", { name: "重试未完成视频", exact: true }).click();
    await page.locator(".task-details-title h3").filter({ hasText: "处理中" }).waitFor();
    assert.equal(retries, 1);
    await closePanel();
    job = { ...job, status: "completed", progress: 100, message: "全部处理完成",
      batch_summary: { total: 3, completed: 3, skipped: 0, failed: 0, awaiting_ai: 0, pending_asr: 0 } };
    await page.getByRole("button", { name: "查看任务详情" }).filter({ hasText: "处理完成" }).waitFor();
    assert.equal(new URL(page.url()).pathname, "/");
    assert.equal(await page.locator(".task-indicator .lucide-circle-check").count(), 1);
    for (const width of [856, 1316, 1540, 1920]) {
      await page.setViewportSize({ width, height: 949 });
      await require("./frontend_header_feedback.cjs")({ page });
      await page.screenshot({ path: `${output}/header-completed-${width}.png`, animations: "disabled" });
    }
    await page.getByRole("button", { name: "查看任务详情" }).click();
    await page.locator(".task-details-title h3").filter({ hasText: "处理完成" }).waitFor();
    await closePanel();
    job = { ...job, status: "resource_paused", progress: 45, message: "内存不足，已保存进度" };
    await page.reload();
    await page.getByRole("button", { name: "查看任务详情" }).filter({ hasText: "资源暂停" }).click();
    await page.getByRole("button", { name: "继续任务", exact: true }).waitFor();
    await page.keyboard.press("Escape");
    assert.equal(stops, 1);
    job = null;

    // Preserve review navigation, marker colors and manual time changes.
    await page.goto(`${origin}/review/1`);
    await page.locator(".marker-item").nth(2).waitFor();
    await page.locator("video").evaluate((video) => new Promise((resolve) => {
      if (video.readyState >= 1) resolve(); else video.addEventListener("loadedmetadata", resolve, { once: true });
    }));
    await require("./frontend_review_tabs.cjs")({ page, output, base, origin });
    for (const width of [856, 1316, 1540, 1920, 2560]) {
      await page.setViewportSize({ width, height: 949 });
      await require("./frontend_header_feedback.cjs")({ page, review: true });
      await require("./frontend_header_feedback.cjs").returnBorder(page.locator(".back-link"));
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      const settingsButton = page.getByRole("button", { name: "打开复习设置", exact: true });
      assert.equal(await settingsButton.evaluate((element) => element.scrollWidth > element.clientWidth), false);
      const layout = await page.evaluate(() => {
        const video = document.querySelector(".video-frame").getBoundingClientRect();
        const sidebar = document.querySelector(".review-sidebar").getBoundingClientRect();
        const header = document.querySelector(".topbar-inner").getBoundingClientRect();
        return { videoLeft: video.left, videoWidth: video.width, videoRight: video.right,
          sidebarLeft: sidebar.left, sidebarRight: sidebar.right, sidebarWidth: sidebar.width,
          headerWidth: header.width, viewport: document.documentElement.clientWidth,
          fit: getComputedStyle(document.querySelector("video")).objectFit };
      });
      assert.ok(layout.videoLeft <= 32 && layout.viewport - layout.sidebarRight <= 33, `${width}: excessive outer margins`);
      assert.ok(layout.videoRight < layout.sidebarLeft, `${width}: video overlaps sidebar`);
      assert.ok(layout.sidebarWidth <= 441, `${width}: sidebar grows instead of video`);
      assert.equal(layout.headerWidth, layout.viewport);
      assert.equal(layout.fit, "contain");
      if (width >= 1920) assert.ok(layout.videoWidth > width - 540, `${width}: video does not use available width`);
      await page.screenshot({ path: `${output}/review-${width}.png`, animations: "disabled" });
      const jumpCard = page.locator(".marker-item:not(.is-selected)").first();
      await page.mouse.move(0, 0);
      await page.waitForTimeout(180);
      const restBorder = await jumpCard.evaluate(node => getComputedStyle(node).borderColor);
      await jumpCard.locator(".marker-quote").hover({ position: { x: 8, y: 8 } });
      await page.waitForTimeout(180);
      assert.notEqual(await jumpCard.evaluate(node => getComputedStyle(node).borderColor), restBorder,
        "marker jumps must visibly highlight their card border");
      const jumpIcon = jumpCard.locator(".marker-jump-indicator");
      assert.equal(await jumpIcon.isVisible(), true);
      assert.ok(await jumpIcon.evaluate(node => node.getBoundingClientRect().right <= node.closest(".marker-item").getBoundingClientRect().right));
      await page.screenshot({ path: `${output}/review-hover-${width}.png`, animations: "disabled" });
      const secondMarker = page.locator(".marker-item").nth(1);
      const secondTitle = await secondMarker.locator(".marker-main strong").innerText();
      for (const selector of [".marker-note", ".marker-quote"]) {
        await page.locator(".marker-main").first().click();
        await secondMarker.locator(selector).click({ position: { x: 8, y: 8 } });
        assert.equal(await page.locator(".marker-main[aria-current=true] strong").innerText(), secondTitle,
          `${width}: ${selector} must jump to its marker`);
      }
      await page.locator(".marker-main").first().click();
      await secondMarker.click({ position: { x: 5, y: 5 } });
      assert.equal(await page.locator(".marker-main[aria-current=true] strong").innerText(), secondTitle,
        `${width}: blank card padding must jump to its marker`);
    }
    await page.setViewportSize({ width: 1316, height: 949 });
    await page.locator('.quote-toggle').first().click();
    assert.equal(await page.locator('.quote-toggle').first().getAttribute('aria-expanded'), 'true');
    await page.locator('.quote-toggle').first().click();
    await page.getByRole('button', { name: '播放', exact: true }).click();
    const playingFrom = await page.locator('video').evaluate((video) => video.currentTime);
    for (const [mode, label] of [['left', '左声道 → 双声道'], ['right', '右声道 → 双声道'], ['original', '原始声道']]) {
      await page.getByRole('button', { name: '声道映射', exact: true }).click();
      await page.getByRole('radio', { name: label, exact: true }).click();
      await page.waitForTimeout(300);
      const levels = await page.evaluate(() => window.__audioMeters.map((meter) => {
        const samples = new Float32Array(meter.fftSize);
        meter.getFloatTimeDomainData(samples);
        return Math.sqrt(samples.reduce((sum, value) => sum + value * value, 0) / samples.length);
      }));
      if (mode === 'left') assert.ok(levels[0] > .03 && Math.abs(levels[0] - levels[1]) < .005, JSON.stringify(levels));
      if (mode === 'right') assert.ok(levels.every((value) => value < .005), JSON.stringify(levels));
      if (mode === 'original') assert.ok(levels[0] > .03 && levels[1] < .005, JSON.stringify(levels));
      assert.equal(await page.locator('video').evaluate((video) => video.paused), false);
    }
    assert.ok(await page.locator('video').evaluate((video) => video.currentTime) > playingFrom);
    await page.getByRole('button', { name: '暂停', exact: true }).click();
    const caption = page.getByRole('button', { name: '字幕', exact: true });
    const captionEnabled = await caption.getAttribute('aria-pressed');
    await caption.click();
    assert.notEqual(await caption.getAttribute('aria-pressed'), captionEnabled);
    await caption.click();
    await page.locator(".marker-main").first().click();
    await page.keyboard.press("d");
    assert.equal(await page.locator(".marker-main[aria-current=true] strong").innerText(), "递归式中的参数");
    await page.keyboard.press("a");
    assert.equal(await page.locator(".marker-main[aria-current=true] strong").innerText(), "分治算法的基本思路");
    await page.locator("video").evaluate((video) => { video.currentTime = 2.5; });
    await page.keyboard.press("s");
    await page.locator(".marker-item").first().locator(".marker-time-adjusted").waitFor();
    await page.locator(".marker-item").first().getByRole("button", { name: "很重要（红色）", exact: true }).click();
    await page.locator(".marker-item").first().locator(".marker-importance-label").filter({ hasText: "很重要" }).waitFor();
    await stopServer();
    await startServer();
    await page.reload();
    await page.locator(".marker-item").first().locator(".marker-time-adjusted").waitFor();
    assert.equal(await page.locator(".marker-time-jump").first().innerText(), "0:02");
    assert.equal(await page.locator(".marker-item").first().locator(".marker-importance-label").innerText(), "很重要");

    // Returning from review restores library scroll, sorting and focus.
    await page.goto(origin);
    await page.getByLabel("视频库排序").selectOption("name");
    await page.evaluate(() => window.scrollTo(0, 200));
    await page.waitForTimeout(100);
    const rootScroll = await page.evaluate(() => window.scrollY);
    await card(1).locator(".library-thumbnail-link").click();
    await page.waitForURL(`${origin}/review/1`);
    await page.locator(".topbar").getByRole("link", { name: "返回视频库", exact: false }).click();
    await card(1).waitFor();
    assert.equal(await page.getByLabel("视频库排序").inputValue(), "name");
    assert.ok(Math.abs(await page.evaluate(() => window.scrollY) - rootScroll) < 5);
    await page.emulateMedia({ reducedMotion: "reduce" });
    await openImport("视频");
    assert.equal(await visible().evaluate((element) => getComputedStyle(element).animationName), "none");
    assert.equal(await page.locator('[aria-modal="true"]:visible').count(), 0);
    await page.keyboard.press("Escape");
    assert.equal(new URL(page.url()).pathname, '/process');
    await closePanel();
    await page.getByRole('button', { name: '查看任务详情', exact: true }).click();
    assert.equal(await page.locator(".workbench-panel").evaluate((element) => getComputedStyle(element).animationName), "none");
    await page.keyboard.press("Tab");
    assert.equal(await page.evaluate(() => !!document.activeElement.closest(".workbench-panel")), true);
    await page.keyboard.press("Escape");
    await page.waitForURL(`${origin}/`);
    assert.equal(await page.evaluate(() => document.activeElement.getAttribute('aria-label')), '查看任务详情');
    // External OS-file drags use real uploads against the disposable API.
    async function fileTransfer(name, suffix = "") {
      const bytes = fs.readFileSync(`${workspace}/lesson.mp4`);
      return page.evaluateHandle(({ name, bytes, suffix }) => {
        const transfer = new DataTransfer();
        transfer.items.add(new File([Uint8Array.from(bytes), suffix], name, { type: "video/mp4" }));
        return transfer;
      }, { name, bytes: [...bytes], suffix });
    }
    const existingVideo = await fileTransfer("existing-lesson.mp4");
    const dropTarget = page.locator(".library-inner .video-card").first();
    await dropTarget.dispatchEvent("dragover", { dataTransfer: existingVideo });
    await page.getByText("松开即可导入视频", { exact: true }).waitFor();
    await dropTarget.dispatchEvent("drop", { dataTransfer: existingVideo });
    await page.locator(".library-import-status").filter({ hasText: "1 个视频已在库中" }).waitFor();
    assert.equal(await page.locator(".library-file-drop-overlay").count(), 0);
    await page.getByLabel("关闭导入结果", { exact: true }).click();

    await page.evaluate(async () => {
      const videos = await (await fetch("/api/videos")).json();
      const library = await (await fetch("/api/library")).json();
      const folder = library.items.find((item) => item.type === "folder").folder;
      for (const video of videos) {
        const response = await fetch(`/api/videos/${video.id}/folder`, {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ folder_id: folder.id }),
        });
        if (!response.ok) throw new Error("Fixture move failed");
      }
      return folder.id;
    });
    await page.reload();
    await page.locator(".empty-library").waitFor();
    const dropped = await fileTransfer("拖拽课堂 #1.mp4", "external-drop-unique");
    const empty = page.locator(".empty-library");
    for (const width of [856, 1316, 1540, 1920]) {
      await page.setViewportSize({ width, height: 949 });
      await empty.dispatchEvent("dragenter", { dataTransfer: dropped });
      await page.locator(".library-file-drop-overlay").waitFor();
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      await page.screenshot({ path: `${output}/file-drop-hover-${width}.png`, fullPage: true });
      await empty.dispatchEvent("dragleave", { dataTransfer: dropped });
      assert.equal(await page.locator(".library-file-drop-overlay").count(), 0);
    }
    await page.setViewportSize({ width: 1316, height: 949 });
    await empty.dispatchEvent("drop", { dataTransfer: dropped });
    await page.locator(".library-import-status").filter({ hasText: "已导入 1 个视频" }).waitFor();
    await page.locator(".library-inner .video-card").filter({ hasText: "拖拽课堂 #1" }).waitFor();
    assert.equal(fs.existsSync(`${workspace}/lesson.mp4`), true);
    assert.equal(await page.locator(".empty-library").count(), 0);
    await page.screenshot({ path: `${output}/file-drop-success.png`, fullPage: true });

    let failUpload = true;
    await page.route("**/api/videos/upload?**", async (route) => {
      if (!failUpload) return route.fallback();
      failUpload = false;
      await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "fixture upload unavailable" }) });
    });
    const mixed = await fileTransfer("retry-lesson.mp4", "retry-unique");
    await mixed.evaluate((transfer) => transfer.items.add(new File(["notes"], "notes.txt")));
    await page.locator(".library-inner").dispatchEvent("drop", { dataTransfer: mixed });
    await page.locator(".library-import-issues").filter({ hasText: "fixture upload unavailable" }).waitFor();
    assert.match(await page.locator(".library-import-issues").innerText(), /notes.txt：不支持的格式/);
    await page.screenshot({ path: `${output}/file-drop-failure.png`, fullPage: true });
    await page.getByRole("button", { name: "重试失败项", exact: true }).click();
    await page.locator(".library-import-status").filter({ hasText: "已导入 1 个视频" }).waitFor();
    assert.equal(await page.locator(".library-import-issues").count(), 0);
    await page.locator(".library-inner .video-card").filter({ hasText: "retry-lesson" }).waitFor();
    assert.equal(await page.locator(".library-inner .video-card").count(), 2);
    await existingVideo.dispose(); await dropped.dispose(); await mixed.dispose();

    // Library processing stays on the current list and uses a batch only for multiple IDs.
    const libraryIds = await page.locator(".library-inner .video-card").evaluateAll((cards) =>
      cards.map((card) => Number(card.dataset.libraryKey.slice("video-".length))));
    assert.equal(await page.locator(".library-inner .library-selection-process").isDisabled(), true);
    assert.equal(await page.locator(".library-selection-dock").count(), 0);
    await page.locator(".library-inner .library-video-select input").first().check();
    const selectionDock = page.locator(".library-inner > .library-selection-dock");
    await selectionDock.waitFor();
    assert.match(await selectionDock.innerText(), /已选 1 个视频/);
    assert.equal(await page.locator(".library-inner .library-selection-process").isEnabled(), true);
    for (const width of [856, 1316, 1540, 1920]) {
      await page.setViewportSize({ width, height: 949 });
      await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
      const bounds = await selectionDock.boundingBox();
      assert.ok(bounds && bounds.x >= 0 && bounds.x + bounds.width <= width);
      assert.ok(Math.abs(bounds.y + bounds.height - (949 - 20)) < 2);
      const lastCard = await page.locator(".library-inner .video-card").last().boundingBox();
      assert.ok(lastCard.y + lastCard.height <= bounds.y, "Bottom spacing keeps the last card clear of the dock");
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      await page.screenshot({ path: `${output}/selection-dock-${width}.png` });
    }
    await selectionDock.getByRole("button", { name: "取消选择", exact: true }).click();
    await selectionDock.waitFor({ state: "hidden" });
    assert.equal(await page.locator(".library-inner .video-card.is-selected").count(), 0);
    const startCount = starts;
    await page.locator(".library-inner .library-card-process").first().click();
    const options = page.locator(".library-inner .library-processing-options");
    await options.getByRole("button", { name: "开始处理", exact: true }).waitFor();
    await chooseRecall(options, ["low_recall"]);
    for (const width of [856, 1316, 1540, 1920]) {
      await page.setViewportSize({ width, height: 949 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      await page.screenshot({ path: `${output}/library-process-options-${width}.png`, fullPage: true });
    }
    await page.setViewportSize({ width: 1316, height: 949 });
    await options.getByRole("button", { name: "开始处理", exact: true }).click();
    await page.getByText("已提交视频处理任务", { exact: true }).waitFor();
    assert.equal(starts, startCount + 1);
    assert.equal(new URL(page.url()).pathname, "/");
    assert.equal(await page.locator(".library-inner .video-card.is-selected").count(), 0);
    async function stopLibraryTask() {
      await page.getByRole("button", { name: "查看任务详情", exact: true }).click();
      await page.getByRole("button", { name: "停止任务", exact: true }).click();
      await page.locator(".task-details-title h3").filter({ hasText: "已停止" }).waitFor();
      await closePanel();
    }
    await stopLibraryTask();
    // This case covers explicit videos; select-all now includes nonempty folders.
    for (const id of libraryIds) await card(id).locator(".library-video-select input").check();
    await page.locator(".library-inner .library-selection-count").filter({ hasText: "已选 2 个视频" }).waitFor();
    await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
    await selectionDock.getByRole("button", { name: "处理所选（2）", exact: true }).click();
    await options.waitFor();
    await page.waitForFunction(() => {
      const bounds = document.querySelector(".library-inner .library-processing-options")?.getBoundingClientRect();
      return bounds && bounds.top >= 64 && bounds.top < innerHeight;
    });
    assert.equal(starts, startCount + 1, "Opening options must not start processing");
    await chooseRecall(options, ["high_recall"]);
    failSelectedBatch = true;
    await options.getByRole("button", { name: "开始批处理（2）", exact: true }).click();
    await options.getByRole("alert").filter({ hasText: "fixture selected queue unavailable" }).waitFor();
    assert.equal(await page.locator(".library-inner .video-card.is-selected").count(), 2);
    await page.screenshot({ path: `${output}/library-selected-batch-failure.png`, fullPage: true });
    await options.getByRole("button", { name: "开始批处理（2）", exact: true }).click();
    await page.getByText("已提交批处理，共 2 个视频", { exact: true }).waitFor();
    assert.deepEqual(selectedBatchRequests.at(-1).video_ids, libraryIds);
    assert.equal(selectedBatchRequests.at(-1).analysis_mode, "high_recall");
    assert.equal(selectedBatchRequests.at(-1).folders, undefined);
    assert.equal(new URL(page.url()).pathname, "/");
    const stopsBeforeClose = stops;
    await page.getByRole("button", { name: "查看任务详情", exact: true }).click();
    await page.locator(".task-details-title").filter({ hasText: "批量处理" }).waitFor();
    await page.locator(".task-entry-list li").nth(1).waitFor();
    assert.equal(await page.locator(".task-entry-list li").count(), 2);
    await page.screenshot({ path: `${output}/library-selected-batch-task.png`, fullPage: true });
    await closePanel();
    assert.equal(stops, stopsBeforeClose);
    await stopLibraryTask();
    await page.locator(".library-inner .folder-card-link").click();
    const folderWindow = page.locator(".folder-window");
    const folderChecks = folderWindow.locator(".library-video-select input:not(:disabled)");
    await folderChecks.first().check();
    await folderChecks.nth(1).check();
    assert.equal(await selectionDock.count(), 0);
    const folderDock = folderWindow.locator(":scope > .library-selection-dock");
    for (const width of [856, 1316, 1540, 1920]) {
      await page.setViewportSize({ width, height: 949 });
      await folderWindow.locator(".folder-window-content").evaluate(node => { node.scrollTop = node.scrollHeight; });
      const bounds = await folderDock.boundingBox(), folderBounds = await folderWindow.boundingBox();
      assert.ok(bounds && folderBounds && bounds.x >= folderBounds.x && bounds.x + bounds.width <= folderBounds.x + folderBounds.width);
      assert.ok(Math.abs(bounds.y + bounds.height - (folderBounds.y + folderBounds.height - 17)) < 2);
      await page.screenshot({ path: `${output}/folder-selection-dock-${width}.png` });
    }
    await page.setViewportSize({ width: 1316, height: 949 });
    const selectedFolderIds = await folderWindow.locator(".video-card.is-selected").evaluateAll((cards) =>
      cards.map((card) => Number(card.dataset.libraryKey.slice("video-".length))));
    await folderDock.getByRole("button", { name: "处理所选（2）", exact: true }).click();
    await chooseRecall(folderWindow, ["low_recall"]);
    await folderWindow.locator('a[href="/settings?section=asr"]').click();
    await page.waitForURL(`${origin}/settings?section=asr`);
    await closePanel();
    await folderWindow.waitFor();
    assert.deepEqual(await checkedRecall(folderWindow), ["low_recall"]);
    assert.equal(await folderWindow.locator(".video-card.is-selected").count(), 2);
    await page.screenshot({ path: `${output}/folder-selected-batch-options.png`, fullPage: true });
    await folderWindow.getByRole("button", { name: "开始批处理（2）", exact: true }).click();
    await folderWindow.locator(".library-folder-notice").filter({ hasText: "已提交批处理，共 2 个视频" }).waitFor();
    assert.deepEqual(selectedBatchRequests.at(-1).video_ids, selectedFolderIds);
    assert.equal(selectedBatchRequests.at(-1).analysis_mode, "low_recall");
    assert.ok(selectedFolderIds.every((id) => !libraryIds.includes(id)));
    await page.getByLabel("关闭文件夹", { exact: true }).click();
    await folderWindow.waitFor({ state: "hidden" });
    await stopLibraryTask();
    // Folder destinations form a lazy tree, not a flat list of previously visited descendants.
    const createMenuFolder = async (name, parent_id = null) => {
      const response = await fetch(`${base}/api/folders`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, parent_id }),
      });
      assert.equal(response.status, 201);
      return (await response.json()).id;
    };
    const menuParentA = await createMenuFolder("目录测试·算法");
    const menuParentB = await createMenuFolder("目录测试·数据库");
    const menuChildA = await createMenuFolder("第1周", menuParentA);
    const menuChildB = await createMenuFolder("第1周", menuParentB);
    const menuLeaf = await createMenuFolder("课堂笔记", menuChildB);
    await page.reload();
    const menuVideoId = Number((await page.locator(".library-inner .video-card").first().getAttribute("data-library-key")).slice(6));
    const moveMenu = card(menuVideoId).getByRole("menu");
    for (const width of [856, 1316, 1540, 1920]) {
      await page.setViewportSize({ width, height: 949 });
      await card(menuVideoId).getByRole("button", { name: /^管理视频/ }).click();
      await moveMenu.locator(`[data-move-folder="${menuParentA}"]`).waitFor();
      assert.equal(await moveMenu.locator(`[data-move-folder="${menuChildA}"]`).count(), 0);
      assert.equal(await moveMenu.locator(`[data-move-folder="${menuChildB}"]`).count(), 0);
      for (const [parent, child] of [[menuParentA, menuChildA], [menuParentB, menuChildB]]) {
        await moveMenu.locator(`[data-folder-id="${parent}"] > .move-folder-row .move-folder-toggle`).click();
        await moveMenu.locator(`[data-move-folder="${child}"]`).waitFor();
      }
      assert.equal(await moveMenu.locator(`[data-move-folder="${menuChildA}"]`).getAttribute("title"), "视频库 / 目录测试·算法 / 第1周");
      assert.equal(await moveMenu.locator(`[data-move-folder="${menuChildB}"]`).getAttribute("title"), "视频库 / 目录测试·数据库 / 第1周");
      await page.screenshot({ path: `${output}/move-folder-tree-${width}.png`, animations: "disabled" });
      assert.ok(await moveMenu.evaluate(node => {
        const rect = node.getBoundingClientRect();
        return rect.left >= 0 && rect.right <= innerWidth && rect.top >= 0 && rect.bottom <= innerHeight
          && node.scrollWidth <= node.clientWidth;
      }), `${width}: move tree overflow`);
      await page.keyboard.press("Escape");
      await moveMenu.waitFor({ state: "detached" });
    }
    // Reach a nested target without opening it in the library, then verify exact membership.
    await card(menuVideoId).getByRole("button", { name: /^管理视频/ }).click();
    await moveMenu.locator(`[data-folder-id="${menuParentB}"] > .move-folder-row .move-folder-toggle`).click();
    await moveMenu.locator(`[data-folder-id="${menuChildB}"] > .move-folder-row .move-folder-toggle`).click();
    await moveMenu.locator(`[data-move-folder="${menuLeaf}"]`).click();
    await card(menuVideoId).waitFor({ state: "detached" });
    const movedRows = await (await fetch(`${base}/api/videos`)).json();
    assert.equal(movedRows.find(row => row.id === menuVideoId).folder_id, menuLeaf);
    assert.equal(new URL(page.url()).pathname, "/");
    if (process.env.THEME_QA) await require("./frontend_theme.cjs")({ page, origin, output });
    assert.deepEqual(errors, []);
    protectedFiles.forEach((file, index) => {
      if (originals[index]) assert.ok(fs.readFileSync(`${root}/${file}`).equals(originals[index]), `Protected file changed during QA: ${file}`);
      else assert.equal(fs.existsSync(`${root}/${file}`), false);
    });
    fs.writeFileSync(`${output}/requests.json`, JSON.stringify(requests, null, 2));
    console.log(JSON.stringify({ passed: true, output, tests: ["PC 856/1316/1540/1920", "menu keyboard",
      "move failure/retry", "folder drag-in/out", "folder rename/context/scroll", "video rename", "configuration save/return",
      "history/reload", "temporary import", "task close/stop", "batch preview/draft", "in-memory API draft",
      "unsaved section/page leave cancellation and discard without secret persistence or automatic saves",
      "awaiting API/retry failure/recovery", "resource pause", "A/D/S", "marker color/timestamp persistence",
      "review return scroll/sort", "open video tabs, active close, keyboard, history and missing video",
      "tab switch saves position and pauses old player", "long-title tab overflow at four PC widths",
      "reduced motion/focus/Escape", "quote expansion/subtitles", "live stereo channel samples",
      "external video file drop on card/empty library", "file drop hover PC screenshots", "upload duplicate/invalid/failure/retry",
      "library single/selected batch processing without navigation", "selected batch failure/retry and task drawer",
      "folder selection/configuration draft after settings", "lazy move folder tree",
      "same-name destination paths", "move to previously unvisited descendants"], pcOnly: true }, null, 2));
  } catch (error) {
    if (page) {
      await page.screenshot({ path: `${output}/failure.png`, fullPage: true });
      console.error((await page.locator("body").innerText()).slice(-6000));
    }
    fs.writeFileSync(`${output}/requests.json`, JSON.stringify(requests, null, 2));
    throw error;
  } finally {
    try {
      if ((process.env.THEME_QA || process.env.SEARCH_QA) && page?.video()) {
        const recording = page.video();
        await page.context().close();
        await recording.saveAs(`${output}/${process.env.SEARCH_QA ? "search" : "theme"}-motion.webm`);
      }
    } finally {
      if (browser) await browser.close();
      await stopServer();
    }
  }
}
main().catch((error) => { console.error(error, logs); process.exitCode = 1; });
