// UI-only cleanup regression. API writes are fixtures, never real asset deletion.
const { chromium } = require("./playwright.cjs");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

async function main() {
  const output = path.resolve(__dirname, `../.runtime/cleanup-progress-qa-${Date.now()}`);
  fs.mkdirSync(output, { recursive: true });
  const models = [
    ["qwen3-asr-1.7b", "Qwen3-ASR 1.7B", "qwen"],
    ["fireredasr2-aed", "FireRedASR2S", "firered"],
    ["paraformer-large-cpu", "Paraformer-large", "paraformer"],
    ["dolphin-small-cn", "Dolphin 中文 small", "dolphin"],
    ["whisper-turbo", "Whisper turbo", "whisper"],
    ["whisper-large-v3", "Whisper large-v3", "whisper"],
  ].map(([id, name, engine]) => ({
    id, name, engine, downloaded: true, selected: false, device: "gpu",
    gpu_available: true, gpu_prepare_available: true, gpu_runtime_ready: true,
    cache_size_bytes: 1024 ** 3, runtime_size_bytes: 4 * 1024 ** 3,
    active_job_id: null, active_job_kind: null, download_state: null, inspection_status: "ready",
  }));
  const browser = await chromium.launch({ executablePath: "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", headless: true });
  const page = await browser.newPage({ viewport: { width: 1316, height: 949 } });
  let job = null, current = null;
  const writes = [], errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const detail = (phase, extra = {}) => ({
    phase, scope: "Dolphin GPU 运行环境", message: phase === "scanning" ? "检查目录并统计文件" : "正在删除文件",
    current_target: "GPU 环境", current_path: "Lib/site-packages/torch/lib/torch_cuda.dll",
    files_scanned: 12345, directories_scanned: 2000, files_deleted: 0, directories_deleted: 0,
    files_total: null, directories_total: null, bytes_deleted: 0, bytes_total: null,
    phase_progress: null, eta_seconds: null, elapsed_seconds: 5, updated_at: Date.now() / 1000, ...extra,
  });
  await page.route("**/api/**", async (route) => {
    const req = route.request(), url = new URL(req.url());
    if (!url.pathname.startsWith("/api/")) return route.continue();
    const json = (value, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(value) });
    if (url.pathname === "/api/asr/models") return json(models);
    if (/\/api\/asr\/models\/[^/]+\/status$/.test(url.pathname)) return json(models.find((model) => model.id === url.pathname.split("/")[4]));
    if (req.method() === "DELETE") {
      assert.match(url.pathname, /^\/api\/asr\/models\/[^/]+(?:\/runtime)?$/);
      writes.push({ path: url.pathname, device: url.searchParams.get("device"), removeRuntime: url.searchParams.get("remove_runtime") });
      current = models.find((model) => model.id === url.pathname.split("/")[4]);
      const runtime = url.pathname.endsWith("/runtime");
      job = { id: `clean-${writes.length}`, model_id: current.id, job_type: runtime ? "asr_runtime_delete" : "asr_delete",
        status: "running", stage: "cleaning", progress: 0, message: "释放占用", cleanup: detail("checking") };
      current.active_job_id = job.id; current.active_job_kind = runtime ? "cleaning_runtime" : "deleting";
      return json({ job_id: job.id }, 202);
    }
    if (url.pathname === "/api/jobs/paused") return json([]);
    if (url.pathname.startsWith("/api/jobs/")) return json(job);
    if (url.pathname === "/api/library/status") return json({
      data_root: "E:/isolated/cleanup-progress", library_id: "cleanup-progress", database_path: "E:/isolated/cleanup-progress/project.db",
      video_count: 0, processing: job?.status === "running", active_job_id: job?.status === "running" ? job.id : null,
    });
    if (req.method() !== "GET") throw new Error(`Unexpected API write ${req.method()} ${url.pathname}`);
    if (url.pathname === "/api/videos") return json([]);
    if (url.pathname === "/api/batch/latest") return json(null);
    return json({});
  });
  const finish = (runtime) => {
    job.status = "completed"; job.stage = "completed"; job.progress = 1;
    job.cleanup = detail("completed", {
      files_total: 200, files_deleted: 200, directories_total: 20, directories_deleted: 20,
      bytes_total: 1024 ** 3, bytes_deleted: 1024 ** 3, phase_progress: 1, current_path: null,
    });
    current.active_job_id = null; current.active_job_kind = null;
    if (runtime) { current.gpu_runtime_ready = false; current.runtime_size_bytes = 0; }
    else { current.downloaded = false; current.cache_size_bytes = 0; }
  };
  try {
    await page.goto("http://127.0.0.1:5175/settings?section=asr");
    await page.locator(".asr-model-option").first().waitFor();
    for (const model of models) {
      if (model.engine === "whisper") await page.locator(".asr-more-models").evaluate((node) => { node.open = true; });
      const card = page.locator(`[data-model-id="${model.id}"]`);
      if (model.engine !== "whisper") {
        await card.getByRole("button", { name: /清理 .* GPU 运行环境（保留模型权重）/ }).click();
        await card.getByRole("progressbar", { name: "释放占用并检查清理范围" }).waitFor();
        job.cleanup = detail("scanning");
        await card.getByText("已检查 12,345 个文件 · 2,000 个目录，总数量尚在统计").waitFor();
        assert.equal(await card.getByRole("progressbar").getAttribute("aria-valuenow"), null);
        job.cleanup = detail("deleting", { files_total: 200, files_deleted: 80, directories_total: 20,
          directories_deleted: 8, bytes_deleted: 400 * 1024 ** 2, bytes_total: 1000 * 1024 ** 2,
          phase_progress: .4, eta_seconds: 30 });
        await card.getByText("已删除 80 / 200 个文件 · 8 / 20 个目录").waitFor();
        assert.equal(await card.getByRole("progressbar").getAttribute("aria-valuenow"), "40");
        await card.getByText("按近期删除速度估计，还需约 30 秒。").waitFor();
        if (model.engine === "dolphin") {
          const before = writes.length;
          await page.reload();
          await card.getByText("已删除 80 / 200 个文件 · 8 / 20 个目录").waitFor();
          assert.equal(writes.length, before, "refresh must reconnect without repeating deletion");
          assert.ok((await page.getByRole("button", { name: "查看任务详情", exact: true }).innerText()).includes("80/200"));
          for (const width of [856, 1316, 1540, 1920]) {
            await page.setViewportSize({ width, height: 949 });
            await card.scrollIntoViewIfNeeded();
            assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
            assert.equal(await card.evaluate((node) => node.scrollWidth > node.clientWidth + 1), false);
            await card.screenshot({ path: `${output}/cleanup-${width}.png` });
          }
          await page.getByRole("button", { name: "查看任务详情", exact: true }).click();
          const drawer = page.getByRole("dialog", { name: "任务详情", exact: true });
          await drawer.getByText("已删除 80 / 200 个文件 · 8 / 20 个目录").waitFor();
          await drawer.screenshot({ path: `${output}/cleanup-drawer.png` });
          await drawer.getByRole("button", { name: "关闭面板", exact: true }).click();
          await drawer.waitFor({ state: "detached" });
          job.cleanup.updated_at = Date.now() / 1000 - 12;
          await card.getByText("等待系统文件操作返回，暂时无法估算剩余时间。").waitFor();
          assert.ok(!(await card.innerText()).includes("还需约"));
        }
        finish(true);
        await card.getByRole("progressbar").waitFor({ state: "detached" });
        assert.equal(model.downloaded, true);
        assert.ok((await card.innerText()).includes("1.0 GB"), "runtime cleanup must retain the weight cache");
        const write = writes.at(-1);
        assert.equal(write.device, ["paraformer", "dolphin"].includes(model.engine) ? "gpu" : null);
      }
      await card.getByRole("button", { name: `清理${model.name}缓存`, exact: true }).click();
      await card.getByRole("progressbar", { name: "释放占用并检查清理范围" }).waitFor();
      finish(false);
      await card.getByRole("progressbar").waitFor({ state: "detached" });
    }
    assert.equal(writes.length, 10);
    assert.ok(writes.every((write) => write.removeRuntime !== "true"));
    assert.deepEqual(errors, []);
    fs.writeFileSync(`${output}/checks.json`, JSON.stringify({ passed: true, writes, checks: [
      "six models weight cleanup", "four separate runtime endpoints", "selected CPU/GPU scope",
      "indeterminate scan", "real deletion counts and bytes", "numeric ETA and stale withdrawal",
      "reload reconnects without repeating deletion", "topbar and task drawer", "PC 856/1316/1540/1920",
    ] }, null, 2));
    console.log(JSON.stringify({ passed: true, output }));
  } catch (error) {
    await page.screenshot({ path: `${output}/failure.png`, fullPage: true });
    console.error({ output, writes, errors }); throw error;
  } finally { await browser.close(); }
}
main().catch((error) => { console.error(error); process.exit(1); });
