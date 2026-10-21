// Visual and interaction checks with in-memory API responses; never calls a paid model.
const { chromium } = require("./playwright.cjs");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

(async () => {
  const root = path.resolve(__dirname, "..");
  const output = path.join(root, ".runtime", `recall-qa-${Date.now()}`);
  const origin = process.env.FRONTEND_URL || "http://127.0.0.1:5175";
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ executablePath: "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1338, height: 1000 } });
    // Reuse the user's video only for read-only playback checks.
    const sourceVideo = await (await page.request.get(`${origin}/api/videos/1`)).json();
    const video = { ...sourceVideo, id: 1, folder_id: null, status: "review_ready", has_transcript: true };
    const marker = {
      id: 10000, video_id: 1, start_ms: 10000, end_ms: 20000,
      title: "已完成的中召回考点", note: "复用字幕识别，保留现有复习结果。",
      source_quote: "考试会考这个知识点。", analysis_mode: "medium_recall", review_status: "active",
    };
    let markers = [marker];
    const runs = [{ analysis_mode: "medium_recall", status: "completed", candidate_count: 1 }];
    let job = null, failNext = false;
    const submitted = [], errors = [], writes = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/api/**", async route => {
      const request = route.request();
      const url = new URL(request.url());
      const pathname = url.pathname;
      const json = (data, status = 200) => route.fulfill({ status, json: data });
      if (request.method() !== "GET") {
        writes.push(`${request.method()} ${pathname}`);
        if (pathname !== "/api/videos/1/analysis") throw new Error(`Unexpected mutation: ${pathname}`);
        const mode = request.postDataJSON().analysis_mode;
        submitted.push(mode);
        if (failNext) { failNext = false; return json({ detail: "测试连接暂不可用，请重试" }, 503); }
        job = { id: `recall-${submitted.length}`, video_id: 1, job_type: "video_analysis", status: "running",
          progress: .25, analysis_modes: [mode], message: "使用已保存字幕识别考点" };
        runs.push({ analysis_mode: mode, status: "processing", candidate_count: 0 });
        return json({ job_id: job.id, video_id: 1, status: "queued", analysis_modes: [mode] }, 202);
      }
      if (pathname === "/api/videos") return json([video]);
      if (pathname === "/api/library") return json({
        folder: null, parent_id: null, sort: "manual", order: "asc", items: [{ type: "video", video }],
      });
      if (pathname === "/api/videos/1") return json(video);
      if (pathname === "/api/videos/1/subtitles") return json([{ id: 1, start_ms: 0, end_ms: 30000, text: "考试会考这个知识点。" }]);
      if (pathname === "/api/videos/1/markers") return json(markers);
      if (pathname === "/api/videos/1/analysis-runs") return json(runs);
      if (pathname === "/api/videos/1/processing-options") return json({ active_job: job?.status === "running" ? job : null });
      if (pathname.startsWith("/api/jobs/recall-")) return json(job);
      if (pathname === "/api/asr/selection") return json({ id: "whisper-turbo", name: "Whisper turbo", engine: "whisper", downloaded: true, selected: true, cache_size_bytes: 0 });
      if (pathname === "/api/settings/ai") return json({
        provider: "openai_compatible", service_type: "openai_compatible", model: "测试连接",
        base_url: "https://unused.example/v1", configured: true, has_api_key: true, output_tokens: 16384,
        timeout_seconds: 120, max_retries: 5, token_parameter: "max_tokens", json_mode: false,
      });
      return route.continue();
    });
    async function capture(name) {
      await page.evaluate(() => document.fonts.ready);
      await page.screenshot({ path: path.join(output, `${name}.png`), fullPage: true });
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), name);
    }
    await page.goto(origin);
    await page.locator(".library-card-process").first().click();
    await page.locator(".recall-mode-control").waitFor();
    const choices = page.locator(".recall-mode-control");
    assert.deepEqual(await choices.locator("input:checked").evaluateAll(nodes => nodes.map(node => node.value)), ["medium_recall"]);
    await choices.getByRole("checkbox", { name: "高召回", exact: true }).check();
    await choices.getByRole("checkbox", { name: "低召回", exact: true }).check();
    assert.equal(await choices.locator("input:checked").count(), 3);
    assert.ok(await choices.getByText(/更多 token/).isVisible());
    await capture("library-multiple-recall-1338");
    await page.setViewportSize({ width: 390, height: 844 });
    await capture("library-multiple-recall-390");

    await page.setViewportSize({ width: 1183, height: 728 });
    await page.goto(`${origin}/review/1`);
    await page.getByText("已完成的中召回考点", { exact: true }).waitFor();
    const player = page.locator("video");
    await player.evaluate(node => new Promise(resolve => {
      if (node.readyState >= 1) return resolve();
      node.addEventListener("loadedmetadata", resolve, { once: true });
    }));
    await player.evaluate(node => { node.currentTime = 35; node.dataset.preservedPlayer = "true"; });
    await page.getByRole("tab", { name: /^高召回/ }).click();
    await page.getByRole("button", { name: "识别高召回", exact: true }).waitFor();
    await capture("review-unprocessed-1183");
    await page.getByRole("button", { name: "识别高召回", exact: true }).click();
    await page.getByText("高召回识别中", { exact: true }).waitFor();
    assert.deepEqual(submitted, ["high_recall"]);
    assert.equal(await page.getByRole("button", { name: "正在识别…", exact: true }).isDisabled(), true);
    await capture("review-running-1183");
    runs.find(run => run.analysis_mode === "high_recall").status = "completed";
    runs.find(run => run.analysis_mode === "high_recall").candidate_count = 1;
    markers = [...markers, { ...marker, id: 10001, title: "补充识别的高召回考点", analysis_mode: "high_recall" }];
    job = { ...job, status: "completed", progress: 1 };
    await page.getByText("补充识别的高召回考点", { exact: true }).waitFor();
    assert.equal(await player.getAttribute("data-preserved-player"), "true");
    assert.ok(Math.abs(await player.evaluate(node => node.currentTime) - 35) < .1);
    await capture("review-completed-1183");

    await page.getByRole("tab", { name: /^低召回/ }).click();
    await page.getByRole("button", { name: "识别低召回", exact: true }).waitFor();
    failNext = true;
    await page.getByRole("button", { name: "识别低召回", exact: true }).click();
    await page.getByText("测试连接暂不可用，请重试", { exact: true }).waitFor();
    await capture("review-request-failed");
    assert.equal(await page.getByRole("button", { name: "识别低召回", exact: true }).isEnabled(), true);
    await page.getByRole("button", { name: "识别低召回", exact: true }).click();
    await page.getByText("低召回识别中", { exact: true }).waitFor();
    runs.find(run => run.analysis_mode === "low_recall").status = "completed";
    job = { ...job, status: "completed", progress: 1 };
    await page.getByText("低召回已完成识别", { exact: true }).waitFor();
    assert.equal(await page.getByRole("button", { name: "识别低召回", exact: true }).count(), 0);
    await capture("review-completed-empty");
    assert.deepEqual(submitted, ["high_recall", "low_recall", "low_recall"]);
    for (const width of [1338, 1024, 390]) {
      await page.setViewportSize({ width, height: 900 });
      await page.getByRole("tab", { name: /^中召回/ }).click();
      await page.getByText("已完成的中召回考点", { exact: true }).waitFor();
      await capture(`review-existing-${width}`);
    }
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({ passed: true, output, submitted, writes,
      checks: ["Default medium and three simultaneous choices", "Token warning", "Missing-mode action",
        "Disabled during processing", "Results refresh", "Player node and time preserved", "Failed submission retry",
        "Completed zero results do not offer another run", "Existing medium results retained", "No horizontal overflow", "No paid API calls"] }, null, 2));
    console.log(JSON.stringify({ passed: true, output, submitted }, null, 2));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
