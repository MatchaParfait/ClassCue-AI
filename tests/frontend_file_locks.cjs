// All API traffic is mocked. No real media, processing jobs or settings are changed.
const { chromium } = require("./playwright.cjs");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

(async () => {
  const output = path.resolve(__dirname, "..", ".runtime", `file-lock-qa-${Date.now()}`);
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({
    executablePath: "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", headless: true,
  });
  const page = await browser.newPage();
  const errors = [], renames = [];
  let scenario = "running", failRename = false;
  const videos = [1, 2, 3, 4].map(id => ({
    id, filename: `lesson-${id}.mp4`, display_name: `课程 ${id}`, duration_ms: 60000,
    video_path: `fixture/lesson-${id}.mp4`, status: "review_ready", folder_id: null, has_transcript: true,
    file_change_block_reason: null, marker_importance_counts: { standard: 0, important: 0, critical: 0 },
  }));
  const batch = () => ({
    id: "batch", job_type: scenario === "migration" ? "storage_migration" : "batch_video_process",
    status: scenario === "stopped" ? "interrupted" : scenario === "paused" ? "resource_paused" : "running",
    progress: .3, message: "模拟处理",
    batch_entries: [{ video_id: 1, status: "pending_ai" }, { video_id: 2, status: "pending_asr" }],
  });
  page.on("pageerror", error => errors.push(error.message));
  try {
    await page.route("**/api/**", async route => {
      const request = route.request(), pathname = new URL(request.url()).pathname;
      if (!pathname.startsWith("/api/")) return route.continue();
      const json = (body, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
      if (/\/api\/videos\/\d+\/thumbnail$/.test(pathname)) return route.fulfill({
        contentType: "image/png",
        body: Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=", "base64"),
      });
      if (request.method() !== "GET") {
        assert.match(pathname, /^\/api\/videos\/4\/filename$/);
        assert.equal(request.method(), "PATCH");
        renames.push(request.postDataJSON());
        if (failRename) { failRename = false; return json({ detail: "模拟文件占用，请重试" }, 409); }
        Object.assign(videos[3], { filename: request.postDataJSON().filename, display_name: "已重命名课程",
          video_path: "fixture/renamed.mp4" });
        return json(videos[3]);
      }
      if (pathname === "/api/library/status") return json({ library_id: "file-lock-fixture",
        data_root: "file-lock-fixture", database_path: "fixture/project.db", temp_audio_dir: "fixture/audio",
        video_count: 4, active_job_id: ["running", "migration"].includes(scenario) ? "batch" : null });
      if (pathname === "/api/batch/latest" || pathname === "/api/jobs/batch") return json(batch());
      if (pathname === "/api/jobs/paused") return json(scenario === "stopped" || scenario === "migration" ? [] : [
        ...(scenario === "paused" ? [batch()] : []),
        { id: "other-paused", job_type: "video_process", video_id: 3, status: "resource_paused" },
      ]);
      if (pathname === "/api/jobs/other-paused") return json({ id: "other-paused", video_id: 3,
        job_type: "video_process", status: "resource_paused" });
      if (pathname === "/api/videos") return json(videos);
      if (pathname === "/api/library") return json({ folder: null, parent_id: null, ancestors: [],
        sort: "manual", order: "asc", items: videos.map(video => ({ type: "video", video })) });
      return json([]);
    });
    const origin = process.env.FRONTEND_URL || "http://127.0.0.1:5175";
    const card = id => page.locator(`.library-inner [data-library-key="video-${id}"]`);
    const rename = id => card(id).locator(".library-card-rename");
    await page.goto(origin);
    await card(4).waitFor();
    for (const width of [856, 1316, 1540, 1920]) {
      await page.setViewportSize({ width, height: 949 });
      for (const id of [1, 2, 3]) await rename(id).waitFor();
      await page.waitForFunction(() => [...document.querySelectorAll(".library-card-rename")]
        .filter(button => button.disabled).length === 3);
      assert.equal(await rename(4).isEnabled(), true);
      assert.ok((await rename(2).getAttribute("title")).includes("队列"));
      assert.ok((await rename(3).getAttribute("title")).includes("暂停"));
      assert.equal(await card(4).locator(".library-card-process").isDisabled(), true);
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await page.screenshot({ path: path.join(output, `running-${width}.png`), fullPage: true });
    }
    await rename(4).click();
    await page.getByRole("textbox", { name: "文件名", exact: true }).fill("renamed.mp4");
    failRename = true;
    await page.getByRole("button", { name: "保存文件名", exact: true }).click();
    await page.locator(".library-video-rename [role=alert]").waitFor();
    assert.equal(await page.getByRole("textbox", { name: "文件名", exact: true }).inputValue(), "renamed.mp4");
    await page.getByRole("button", { name: "保存文件名", exact: true }).click();
    await page.locator(".library-video-rename").waitFor({ state: "detached" });
    assert.equal(await card(4).locator("strong").textContent(), "已重命名课程");
    assert.equal(new URL(page.url()).pathname, "/");
    assert.equal(await rename(1).isDisabled(), true);
    assert.deepEqual(renames, [{ filename: "renamed.mp4" }, { filename: "renamed.mp4" }]);
    for (scenario of ["paused", "migration", "stopped"]) {
      await page.reload();
      await card(4).waitFor();
      const disabledCount = scenario === "migration" ? 4 : scenario === "paused" ? 3 : 0;
      await page.waitForFunction(count => [...document.querySelectorAll(".library-card-rename")]
        .filter(button => button.disabled).length === count, disabledCount);
      await page.screenshot({ path: path.join(output, `${scenario}.png`), fullPage: true });
    }
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, "report.json"), JSON.stringify({ passed: true, errors, renames,
      checks: ["PC 856/1316/1540/1920", "unrelated rename and failure retry", "active batch entire queue locked",
        "other paused queue locked", "storage migration locks all", "stop releases locks", "processing remains serial"] }, null, 2));
    console.log(output);
  } catch (error) {
    await page.screenshot({ path: path.join(output, "failure.png"), fullPage: true });
    fs.writeFileSync(path.join(output, "errors.json"), JSON.stringify(errors));
    throw error;
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
