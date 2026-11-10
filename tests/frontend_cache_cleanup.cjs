// All APIs use synthetic data; this regression never deletes real cache files.
const { chromium } = require("./playwright.cjs");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

async function main() {
  const output = path.resolve(__dirname, "../.runtime/review/cache-cleanup-ui");
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({
    executablePath: "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", headless: true,
  });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
  const errors = [], writes = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.addInitScript(() => {
    localStorage.setItem("classroom-onboarding:v1:fixture", JSON.stringify({ dismissed: true }));
  });
  let busy = true, statusReads = 0, failed = false, jobReads = 0, cleaned = false, failInspect = false;
  let cleanStarts = 0, cancelled = false;
  let preferences = { enabled: true, interval_days: 7, retention_days: 30, preview_limit_mb: 512,
    categories: ["transcription", "previews", "logs", "updates"] };
  const categories = [
    ["transcription", "已完成转写缓存", 6 * 1024 ** 2, true],
    ["previews", "视频封面与搜索预览", 240 * 1024 ** 2, true],
    ["logs", "历史运行日志", 21 * 1024 ** 2, true],
    ["updates", "旧更新下载与解压文件", 180 * 1024 ** 2, true],
    ["downloads", "依赖下载缓存", 2 * 1024 ** 3, false],
    ["ai", "AI 分块结果缓存", 12 * 1024 ** 2, false],
    ["backups", "旧更新回退备份", 1200 * 1024 ** 2, false],
  ].map(([id, label, bytes, automatic]) => ({ id, label, bytes, automatic, eligible_bytes: bytes / 2 }));
  await page.route("**/api/**", async route => {
    const request = route.request(), url = new URL(request.url()), name = url.pathname;
    if (!name.startsWith("/api/")) return route.continue();
    const send = (value, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(value) });
    if (request.method() !== "GET") writes.push([name, request.postData() ? request.postDataJSON() : null]);
    if (name === "/api/updates") return send({
      version: "0.0.0", repository: "example/synthetic", configured: false, portable: false,
      checking: false, downloading: false, downloaded_bytes: 0, last_checked: null,
      error: null, download_error: null, compatible: false, ready: false,
      preferences: { auto_check: false, auto_download: false, show_announcements: false },
      announcements: [], release: null, install: null,
    });
    if (name === "/api/settings/recall-selection") return send({
      library_id: "synthetic", initialized: true, revision: 1, analysis_modes: ["medium_recall"],
    });
    if (name === "/api/cache") {
      if (++statusReads > 1) busy = false;
      return send({ preferences, busy, last_run: cleaned || cancelled ? {
      finished_at: Date.now() / 1000, released_bytes: (cancelled ? 32 : 80) * 1024 ** 2,
      files_deleted: cancelled ? 168 : 420, skipped: cancelled ? 0 : 2,
      automatic: false, cancelled, warnings: [], ai_cache_bytes_removed: 0,
      } : null });
    }
    if (name === "/api/cache/preferences") { preferences = request.postDataJSON(); return send({ preferences, last_run: null }); }
    if (name === "/api/cache/preview") {
      if (failInspect) { failInspect = false; return send({ detail: "Synthetic inspection failure" }, 500); }
      return send({ categories: categories.map(item => ({ ...item, eligible_bytes: cleaned ? 0 : item.eligible_bytes })) });
    }
    if (name === "/api/cache/clean") {
      if (!failed) { failed = true; return send({ detail: "合成测试：文件暂时被占用，请稍后重试" }, 409); }
      return send({ job_id: ++cleanStarts > 1 ? "synthetic-cancel" : "synthetic-cleanup" }, 202);
    }
    if (name === "/api/jobs/synthetic-cancel/cancel") {
      cancelled = true;
      return send({ id: "synthetic-cancel", job_type: "cache_cleanup", status: "running" });
    }
    if (name === "/api/jobs/synthetic-cancel") {
      return send({ id: "synthetic-cancel", job_type: "cache_cleanup", status: cancelled ? "interrupted" : "running",
        cleanup: { scope: "应用缓存", phase: "deleting", phase_progress: .4,
          files_deleted: 168, files_total: 420, files_scanned: 420,
          directories_deleted: 0, directories_total: 0, directories_scanned: 4,
          bytes_deleted: 32 * 1024 ** 2, bytes_total: 80 * 1024 ** 2,
          elapsed_seconds: 2, updated_at: Date.now() / 1000, message: cancelled ? "缓存清理已停止" : "清理合成缓存" } });
    }
    if (name === "/api/jobs/synthetic-cleanup") {
      jobReads++;
      cleaned = jobReads > 2;
      return send({ id: "synthetic-cleanup", job_type: "cache_cleanup", status: cleaned ? "completed" : "running",
        cleanup: { scope: "应用缓存", phase: cleaned ? "completed" : "deleting", phase_progress: cleaned ? 1 : .4,
          files_deleted: cleaned ? 420 : 168, files_total: 420, files_scanned: 420,
          directories_deleted: 0, directories_total: 0, directories_scanned: 4,
          bytes_deleted: cleaned ? 80 * 1024 ** 2 : 32 * 1024 ** 2, bytes_total: 80 * 1024 ** 2,
          elapsed_seconds: 2, updated_at: Date.now() / 1000, message: "清理合成缓存" } });
    }
    if (name === "/api/settings/directories") return send({
      library_id: "synthetic", data_root: "fixture/library", asr_storage_root: "fixture/assets",
      audio_cache_dir: "fixture/audio", audio_cache_default_dir: "fixture/audio", log_dir: "fixture/logs",
      video_library_dir: "fixture/videos", video_library_default_dir: "fixture/videos",
      model_dir: "fixture/models", runtime_dir: "fixture/runtime", package_cache_dir: "fixture/cache",
      config_dir: "fixture/config", active_asr_storage_root: "fixture/assets", active_log_dir: "fixture/logs",
      restart_required: false, busy: false, has_paused_jobs: false, delete_source_after_import: false,
    });
    if (name === "/api/library/status") return send({ library_id: "synthetic", video_count: 0, data_root: "fixture" });
    if (name === "/api/videos" || name === "/api/jobs/recent" || name === "/api/jobs/paused") return send([]);
    if (name.endsWith("/latest")) return send(null);
    return send({});
  });
  try {
    const capture = async name => {
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.screenshot({ path: path.join(output, name), fullPage: true });
    };
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto(`${process.env.FRONTEND_URL || "http://127.0.0.1:5191"}/settings?section=storage`);
    const section = page.locator(".cache-cleanup-section");
    await section.getByLabel("自动清理频率").waitFor();
    await page.evaluate(() => document.fonts.ready);
    await capture("incumbent-and-cleanup.png");
    assert.equal(await section.getByRole("button", { name: "检查占用", exact: true }).isDisabled(), true);
    await section.getByText("有运行或暂停中的任务，暂不可清理。", { exact: true }).waitFor();
    await page.waitForFunction(() => {
      const button = [...document.querySelectorAll(".cache-cleanup-section button")]
        .find(node => node.textContent.includes("检查占用"));
      return button && !button.disabled;
    });
    await section.getByRole("button", { name: "检查占用", exact: true }).click();
    await section.getByLabel("清理视频封面与搜索预览").waitFor();
    assert.equal(await section.getByLabel("清理AI 分块结果缓存").isChecked(), false);
    assert.equal(await section.getByLabel("清理旧更新回退备份").isChecked(), false);
    for (const width of [1440, 856, 390]) {
      await page.setViewportSize({ width, height: 1100 });
      await section.scrollIntoViewIfNeeded();
      assert.ok(await section.evaluate(node => node.scrollWidth <= node.clientWidth + 1), `${width}: cleanup overflow`);
      const box = await section.boundingBox();
      assert.ok(box.x >= 0 && box.x + box.width <= width + 1, `${width}: section outside viewport`);
      const action = await section.getByRole("button", { name: /^清理所选/ }).evaluate(button => {
        const bounds = button.getBoundingClientRect(), icon = button.querySelector("svg").getBoundingClientRect();
        return { buttonLeft: bounds.left, iconLeft: icon.left, iconRight: icon.right, buttonRight: bounds.right };
      });
      assert.ok(action.iconLeft >= action.buttonLeft && action.iconRight <= action.buttonRight,
        `${width}: cleanup icon outside button: ${JSON.stringify(action)}`);
      await capture(`${width}.png`);
      await page.setViewportSize({ width, height: Math.ceil(box.height) + 180 });
      await section.evaluate(node => window.scrollTo(0, node.getBoundingClientRect().top + window.scrollY - 90));
      await section.screenshot({ path: path.join(output, `${width}-section.png`) });
    }
    await page.setViewportSize({ width: 1440, height: 1100 });
    await section.getByLabel("自动清理频率").selectOption("1");
    await section.getByRole("button", { name: "保存清理设置" }).click();
    await section.getByText("自动清理设置已保存。", { exact: true }).waitFor();
    assert.equal(preferences.interval_days, 1);
    await section.getByRole("button", { name: /^清理所选/ }).click();
    await section.getByText("合成测试：文件暂时被占用，请稍后重试", { exact: true }).waitFor();
    await capture("error.png");
    await section.getByRole("button", { name: /^清理所选/ }).click();
    await section.getByLabel("缓存清理进度").waitFor();
    await capture("progress.png");
    await section.getByText(/上次清理：/).waitFor();
    assert.ok((await section.innerText()).includes("跳过 2 个文件"));
    await capture("completed.png");
    cleaned = false;
    await section.getByRole("button", { name: "检查占用", exact: true }).click();
    await section.getByRole("button", { name: /^清理所选/ }).click();
    await section.getByRole("button", { name: "停止清理", exact: true }).click();
    await section.getByText("清理已停止，已完成的删除不会回滚，可重新发起清理。", { exact: true }).waitFor();
    assert.equal(await section.getByRole("progressbar").getAttribute("aria-valuenow"), "40");
    assert.ok((await section.getByLabel("缓存清理进度").innerText()).includes("168 / 420"));
    await capture("cancelled.png");
    assert.equal(writes.filter(([name]) => name === "/api/cache/clean").length, 3);
    assert.equal(writes.filter(([name]) => name === "/api/jobs/synthetic-cancel/cancel").length, 1);
    assert.deepEqual(errors, []);
    console.log(`Cache cleanup browser regression passed: ${output}`);
  } catch (error) {
    await page.screenshot({ path: path.join(output, "failure.png"), fullPage: true });
    console.error(errors);
    console.error((await page.locator("body").innerText()).slice(0, 5000));
    throw error;
  } finally { await browser.close(); }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
