// Model preparation UI checks; every API request is isolated from real model assets.
const { chromium } = require("./playwright.cjs");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

async function main() {
  const output = path.resolve(__dirname, `../.runtime/preparation-progress-qa-${Date.now()}`);
  fs.mkdirSync(output, { recursive: true });
  const models = [
    ["qwen3-asr-1.7b", "Qwen3-ASR 1.7B", "qwen"],
    ["fireredasr2-aed", "FireRedASR2S", "firered"],
    ["paraformer-large-cpu", "Paraformer-large", "paraformer"],
    ["dolphin-small-cn", "Dolphin 中文 small", "dolphin"],
    ["whisper-turbo", "Whisper turbo", "whisper"],
    ["whisper-large-v3", "Whisper large-v3", "whisper"],
  ].map(([id, name, engine]) => ({ id, name, engine, selected: false, device: "cpu",
    downloaded: false, cache_size_bytes: 0, gpu_available: true, gpu_runtime_ready: true,
    gpu_prepare_available: true, inspection_status: "ready", active_job_id: null, active_job_kind: null }));
  const browser = await chromium.launch({ executablePath: "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", headless: true });
  const context = await browser.newContext({ viewport: { width: 1316, height: 949 } });
  const page = await context.newPage();
  const errors = [], writes = [];
  let job = null, current = null, pollingFailure = false;
  page.on("pageerror", (error) => errors.push(error.message));
  const telemetry = (phase, extra = {}) => ({ phase, label: {
    installing: "安装运行环境", metadata: "获取权重文件清单", downloading: "下载模型权重",
    verifying: "校验模型文件", finalizing: "完成模型准备",
  }[phase], message: phase === "installing" ? "下载并安装 PyTorch 与计算依赖" : "ModelScope · recognizer/model.pt",
    updated_at: Date.now() / 1000, phase_progress: null, eta_seconds: null, ...extra });
  function download(extra = {}) {
    return telemetry("downloading", { component: "recognizer", source: "ModelScope",
      current_file: "model-00001-of-00002.safetensors", phase_progress: .4,
      bytes_completed: 400 * 1024 ** 2, bytes_total: 1000 * 1024 ** 2,
      speed_bytes_per_second: 5 * 1024 ** 2, eta_seconds: 120, ...extra });
  }
  await page.route("**/api/**", async (route) => {
    const req = route.request(), url = new URL(req.url());
    if (!url.pathname.startsWith("/api/")) return route.continue();
    const json = (value, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(value) });
    if (url.pathname === "/api/library/status") return json({ data_root: "E:/isolated/model-progress", library_id: "model-progress",
      database_path: "E:/isolated/model-progress/project.db", video_count: 0, processing: Boolean(job && job.status === "running"),
      active_job_id: job?.status === "running" ? job.id : null });
    if (url.pathname === "/api/asr/models") return json(models);
    if (/\/api\/asr\/models\/[^/]+\/status$/.test(url.pathname)) return json(models.find((model) => model.id === url.pathname.split("/")[4]));
    if (/\/api\/asr\/models\/[^/]+\/prepare$/.test(url.pathname) && req.method() === "POST") {
      writes.push({ url: url.pathname, method: req.method() });
      current = models.find((model) => model.id === url.pathname.split("/")[4]);
      job = { id: `prepare-${writes.length}`, status: "running", job_type: "asr_prepare", progress: .1,
        stage: "preparing", message: "安装依赖", preparation: telemetry("installing", {
          current_package: "torch", step: 2, steps: 4, phase_progress: .25, elapsed_seconds: 25,
        }) };
      current.active_job_id = job.id; current.active_job_kind = "preparing";
      return json({ job_id: job.id }, 202);
    }
    if (url.pathname === "/api/jobs/paused") return json([]);
    if (url.pathname.startsWith("/api/jobs/")) return json(pollingFailure ? { detail: "temporary connection failure" } : job, pollingFailure ? 503 : 200);
    if (req.method() !== "GET") throw new Error(`Unexpected write ${req.method()} ${url.pathname}`);
    if (url.pathname === "/api/videos") return json([]);
    if (url.pathname === "/api/batch/latest") return json(null);
    return json({});
  });
  try {
    await page.goto("http://127.0.0.1:5175/settings?section=asr");
    await page.locator(".asr-model-option").first().waitFor();
    for (const model of models) {
      const card = page.locator(`[data-model-id="${model.id}"]`);
      if (model.engine === "whisper") await page.locator(".asr-more-models").evaluate((node) => { node.open = true; });
      await card.getByRole("button", { name: "准备模型", exact: true }).click();
      await card.getByText("安装运行环境 · 步骤 2/4").waitFor();
      assert.ok((await card.innerText()).includes("当前依赖：torch"));
      job.preparation = telemetry("metadata");
      await card.getByRole("progressbar", { name: "获取权重文件清单" }).waitFor();
      assert.equal(await card.getByRole("progressbar").getAttribute("aria-valuenow"), null);
      job.preparation = download();
      await card.getByText("model-00001-of-00002.safetensors", { exact: true }).waitFor();
      assert.equal(await card.getByRole("progressbar").getAttribute("aria-valuenow"), "40");
      assert.ok((await card.innerText()).includes("预计还需约 2 分 0 秒"));
      job.preparation = download({ phase_progress: .65, bytes_completed: 650 * 1024 ** 2, eta_seconds: 70 });
      await page.waitForFunction((id) => document.querySelector(`[data-model-id="${id}"] [role="progressbar"]`)?.getAttribute("aria-valuenow") === "65", model.id);
      if (model.id === "paraformer-large-cpu") {
        assert.ok((await page.getByRole("button", { name: "查看任务详情", exact: true }).innerText()).includes("下载模型权重"));
        await page.getByRole("button", { name: "查看任务详情", exact: true }).click();
        const drawer = page.getByRole("dialog", { name: "任务详情", exact: true });
        await drawer.getByRole("progressbar", { name: "下载模型权重", exact: true }).waitFor();
        assert.ok((await drawer.innerText()).includes("model-00001-of-00002.safetensors"));
        await drawer.screenshot({ path: `${output}/task-drawer.png` });
        await drawer.getByRole("button", { name: "关闭面板", exact: true }).click();
        await page.waitForURL("**/settings?section=asr");
        for (const width of [856, 1316, 1540, 1920]) {
          await page.setViewportSize({ width, height: 949 });
          await page.evaluate(() => document.fonts.ready);
          await card.scrollIntoViewIfNeeded();
          assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
          assert.equal(await card.evaluate((node) => node.scrollWidth > node.clientWidth + 1), false);
          await page.screenshot({ path: `${output}/downloading-${width}.png`, fullPage: true });
        }
        const submitted = writes.length;
        await page.reload();
        await card.getByText("model-00001-of-00002.safetensors", { exact: true }).waitFor();
        assert.equal(writes.length, submitted, "reload must reconnect, not submit");
        pollingFailure = true;
        await card.getByText("进度连接暂时中断，正在重连；后台任务继续运行。").waitFor({ timeout: 15000 });
        assert.ok(!(await card.innerText()).includes("预计还需"));
        pollingFailure = false;
        job.preparation = download();
        await card.getByText(/权重下载预计还需/).waitFor({ timeout: 15000 });
        job.preparation = download({ waiting_for_network: true, eta_seconds: null, speed_bytes_per_second: null });
        await card.getByText("正在等待网络传输，剩余时间暂时无法估算。").waitFor();
      }
      job.preparation = telemetry("verifying", { current_file: "model.pt", phase_progress: .6, checked_bytes: 600, check_total_bytes: 1000 });
      await card.getByRole("progressbar", { name: "校验模型文件" }).waitFor();
      assert.ok(!(await card.innerText()).includes("预计还需"));
      await card.screenshot({ path: `${output}/${model.id}-verification.png` });
      job.preparation = telemetry("finalizing", { phase_progress: 1 });
      await card.getByRole("progressbar", { name: "完成模型准备" }).waitFor();
      job.status = "completed"; job.progress = 1;
      current.downloaded = true; current.cache_size_bytes = 1000;
      current.active_job_id = null; current.active_job_kind = null;
      await card.getByRole("progressbar").waitFor({ state: "detached" });
      assert.equal(await card.getByRole("button", { name: "准备模型", exact: true }).isEnabled(), true);
    }
    assert.equal(writes.length, 6);
    assert.deepEqual(errors, []);
    fs.writeFileSync(`${output}/checks.json`, JSON.stringify({ passed: true, models: models.map((m) => m.id),
      checks: ["installation steps", "indeterminate metadata", "byte progress", "numeric ETA", "live updates", "reload recovery",
        "task drawer", "topbar phase", "polling failure/reconnection", "network stall", "verification", "completion", "PC 856/1316/1540/1920"], writes }, null, 2));
    console.log(JSON.stringify({ passed: true, output }, null, 2));
  } catch (error) {
    await page.screenshot({ path: `${output}/failure.png`, fullPage: true });
    console.error(JSON.stringify({ errors, body: (await page.locator("body").innerText()).slice(-5000), output }));
    throw error;
  } finally {
    await browser.close();
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
