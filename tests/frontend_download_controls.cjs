// Isolated UI lifecycle: all API traffic is mocked; real user model caches are never touched.
const { chromium } = require("./playwright.cjs");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

async function main() {
  const output = path.resolve(__dirname, `../.runtime/download-controls-qa-${Date.now()}`);
  fs.mkdirSync(output, { recursive: true });
  const models = [
    ["qwen3-asr-1.7b", "Qwen3-ASR 1.7B", "qwen"],
    ["fireredasr2-aed", "FireRedASR2S", "firered"],
    ["paraformer-large-cpu", "Paraformer-large", "paraformer"],
    ["dolphin-small-cn", "Dolphin 中文 small", "dolphin"],
    ["whisper-turbo", "Whisper turbo", "whisper"],
    ["whisper-large-v3", "Whisper large-v3", "whisper"],
  ].map(([id, name, engine]) => ({
    id, name, engine, downloaded: false, selected: false, cache_size_bytes: 120 * 1024 ** 2,
    gpu_available: true, gpu_prepare_available: true, gpu_runtime_ready: true,
    inspection_status: "ready", active_job_id: null, active_job_kind: null,
    download_state: { status: "interrupted", retained_bytes: 120 * 1024 ** 2,
      completed_bytes: 100 * 1024 ** 2, partial_bytes: 20 * 1024 ** 2,
      temporary_files: 2, can_cleanup: true, current_file: "model.safetensors", device: "cpu" },
  }));
  const browser = await chromium.launch({ executablePath: "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", headless: true });
  const context = await browser.newContext({ viewport: { width: 1316, height: 949 } });
  const page = await context.newPage();
  let job = null, current = null, counter = 0, failCleanup = false;
  const writes = [], errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const start = (model, type = "asr_prepare") => {
    current = model;
    job = { id: `download-${++counter}`, model_id: model.id, status: "running", stage: "preparing",
      job_type: type, progress: .5, message: "下载权重", preparation: {
        phase: "downloading", label: "下载模型权重", message: "ModelScope · recognizer/model.safetensors",
        component: "recognizer", current_file: "model.safetensors", updated_at: Date.now() / 1000,
        bytes_completed: 120 * 1024 ** 2, bytes_total: 300 * 1024 ** 2, phase_progress: .4,
        file_bytes_completed: 20 * 1024 ** 2, speed_bytes_per_second: 1024 ** 2, eta_seconds: 180,
      } };
    model.active_job_id = job.id;
    model.active_job_kind = type === "asr_prepare" ? "preparing" : "cleaning_partial";
    return job.id;
  };
  const finish = (status, stage) => {
    job.status = status; job.stage = stage;
    current.active_job_id = null; current.active_job_kind = null;
  };
  await page.route("**/api/**", async (route) => {
    const req = route.request(), url = new URL(req.url());
    if (!url.pathname.startsWith("/api/")) return route.continue();
    const json = (value, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(value) });
    if (req.method() !== "GET") writes.push({ method: req.method(), path: url.pathname });
    if (url.pathname === "/api/asr/models") return json(models);
    if (/\/api\/asr\/models\/[^/]+\/status$/.test(url.pathname)) return json(models.find((model) => model.id === url.pathname.split("/")[4]));
    if (/\/download\/resume$/.test(url.pathname)) {
      const model = models.find((model) => model.id === url.pathname.split("/")[4]);
      model.download_state.status = "preparing";
      return json({ job_id: start(model) }, 202);
    }
    if (/\/download\/pause$/.test(url.pathname)) {
      job.stage = "pausing";
      setTimeout(() => { current.download_state.status = "paused"; finish("interrupted", "download_paused"); }, 300);
      return json(job);
    }
    if (/\/download\/incomplete$/.test(url.pathname)) {
      if (failCleanup) { failCleanup = false; return json({ detail: "暂时无法清理，请重试" }, 503); }
      const model = models.find((model) => model.id === url.pathname.split("/")[4]);
      const id = start(model, "asr_download_cleanup");
      setTimeout(() => {
        model.download_state = { ...model.download_state, status: "interrupted", retained_bytes: 100 * 1024 ** 2,
          partial_bytes: 0, temporary_files: 0, can_cleanup: false, current_file: null };
        finish("completed", "completed");
      }, 300);
      return json({ job_id: id }, 202);
    }
    if (url.pathname === "/api/jobs/paused") return json([]);
    if (url.pathname.startsWith("/api/jobs/")) return json(job);
    if (url.pathname === "/api/settings/workspace" || url.pathname === "/api/settings/library") return json({
      path: "E:/isolated/download-controls", library_id: "download-controls", workspace_id: "download-controls",
      database_path: "E:/isolated/download-controls/videos.db", processing: job?.status === "running",
      active_job_id: job?.status === "running" ? job.id : null, video_count: 0,
    });
    if (req.method() !== "GET") throw new Error(`Unexpected write ${req.method()} ${url.pathname}`);
    if (url.pathname === "/api/videos") return json([]);
    if (url.pathname === "/api/batch/latest") return json(null);
    return json({});
  });
  try {
    await page.goto("http://127.0.0.1:5175/settings?section=asr");
    await page.locator(".asr-model-option").first().waitFor();
    assert.equal(writes.length, 0, "initial recovery must not auto-resume");
    for (const model of models) {
      if (model.engine === "whisper") await page.locator(".asr-more-models").evaluate((node) => { node.open = true; });
      const card = page.locator(`[data-model-id="${model.id}"]`);
      await card.getByRole("button", { name: "继续下载", exact: true }).click();
      await card.getByRole("progressbar", { name: "下载模型权重", exact: true }).waitFor();
      await card.getByRole("button", { name: "暂停下载", exact: true }).click();
      await card.getByRole("button", { name: "继续下载", exact: true }).waitFor();
      assert.ok((await card.innerText()).includes("下载已暂停"));
      if (model.id === "paraformer-large-cpu") {
        const count = writes.length;
        await page.reload();
        await card.getByRole("button", { name: "继续下载", exact: true }).waitFor();
        assert.equal(writes.length, count, "refresh must leave paused task paused");
        for (const width of [856, 1316, 1540, 1920]) {
          await page.setViewportSize({ width, height: 949 });
          await card.scrollIntoViewIfNeeded();
          assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
          assert.equal(await card.evaluate((node) => node.scrollWidth > node.clientWidth + 1), false);
          await page.screenshot({ path: `${output}/paused-${width}.png`, fullPage: true });
        }
        // Drawer presents the same recovery choices.
        await card.getByRole("button", { name: "继续下载", exact: true }).click();
        await card.getByRole("button", { name: "暂停下载", exact: true }).waitFor();
        await page.getByRole("button", { name: "查看任务详情", exact: true }).click();
        const drawer = page.getByRole("dialog", { name: "任务详情", exact: true });
        await drawer.getByRole("button", { name: "暂停下载", exact: true }).click();
        await drawer.getByRole("button", { name: "继续下载", exact: true }).waitFor();
        await drawer.screenshot({ path: `${output}/paused-drawer.png` });
        await drawer.getByRole("button", { name: "关闭面板", exact: true }).click();
        await drawer.waitFor({ state: "detached" });
        failCleanup = true;
        await card.getByRole("button", { name: "清理未完成下载", exact: true }).click();
        await card.getByRole("alert").getByText("暂时无法清理，请重试").waitFor();
        assert.equal(await card.getByRole("button", { name: "继续下载", exact: true }).isEnabled(), true);
      }
      await card.getByRole("button", { name: "清理未完成下载", exact: true }).click();
      await page.waitForFunction((id) => {
        const card = document.querySelector(`[data-model-id="${id}"]`);
        return [...card.querySelectorAll("button")].some((button) => button.textContent.includes("清理未完成下载") && button.disabled);
      }, model.id);
      await card.getByRole("button", { name: "继续下载", exact: true }).waitFor();
      assert.ok((await card.innerText()).includes("100.0 MB"), "completed weights remain");
      await card.getByRole("button", { name: "继续下载", exact: true }).click();
      await card.getByRole("button", { name: "暂停下载", exact: true }).waitFor();
      model.download_state = null; model.downloaded = true;
      finish("completed", "completed");
      await card.getByRole("button", { name: "准备模型", exact: true }).waitFor({ state: "visible" });
      await card.getByRole("button", { name: "继续下载", exact: true }).waitFor({ state: "detached" });
    }
    assert.deepEqual(errors, []);
    assert.ok(!writes.some((write) => /^\/api\/asr\/models\/[^/]+$/.test(write.path)), "never delete complete model caches");
    fs.writeFileSync(`${output}/checks.json`, JSON.stringify({ passed: true, writes, checks: [
      "six-model recovery without automatic resume", "pause and continue", "reload retains paused state",
      "partial cleanup preserves completed weights", "cleanup failure and retry", "task drawer controls",
      "PC 856/1316/1540/1920", "completion removes recovery panel",
    ] }, null, 2));
    console.log(JSON.stringify({ passed: true, output }));
  } catch (error) {
    await page.screenshot({ path: `${output}/failure.png`, fullPage: true });
    console.error({ output, errors, writes }); throw error;
  } finally { await browser.close(); }
}
main().catch((error) => { console.error(error); process.exit(1); });
