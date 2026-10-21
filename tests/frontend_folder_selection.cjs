// Mock-only PC checks: never import files, run ASR/AI, or alter a real library.
const { chromium } = require("./playwright.cjs");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const origin = process.env.FRONTEND_URL || "http://127.0.0.1:5175";
const output = path.resolve(__dirname, "../.runtime", `folder-selection-qa-${Date.now()}`);

async function main() {
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ executablePath: "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", headless: true });
  let page;
  try {
    page = await browser.newPage({ viewport: { width: 1316, height: 949 } });
    page.setDefaultTimeout(15000);
    const errors = [], submissions = [];
    page.on("pageerror", error => errors.push(error.message));
    const folders = [
      { id: 7, name: "数据库课程", parent_id: null, video_count: 3 },
      { id: 8, name: "子文件夹 · 索引与查询", parent_id: 7, video_count: 2 },
      { id: 9, name: "空文件夹", parent_id: 7, video_count: 0 },
      { id: 10, name: "算法课程", parent_id: null, video_count: 1 },
      { id: 11, name: "尚未导入视频", parent_id: null, video_count: 0 },
    ];
    const videos = [null, 7, 8, 8, 10].map((folder_id, index) => ({
      id: index + 1, display_name: `第 ${index + 1} 讲`, filename: `lesson-${index + 1}.mp4`,
      video_path: `fixture-${index + 1}.mp4`, duration_ms: 60000, folder_id, status: "review_ready",
    }));
    let fail = true;
    await page.route("**/api/**", async route => {
      const request = route.request(), url = new URL(request.url()), p = url.pathname;
      if (!p.startsWith("/api/")) return route.continue();
      const json = (value, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(value) });
      if (request.method() !== "GET") {
        assert.equal(p, "/api/batch/process");
        assert.equal(request.method(), "POST");
        const body = request.postDataJSON();
        submissions.push(body);
        assert.equal(body.folders, undefined, "must not import or rescan source folders");
        await new Promise(resolve => setTimeout(resolve, 500));
        if (fail) { fail = false; return json({ detail: "测试：队列暂时不可用" }, 409); }
        const ids = new Set(body.video_ids);
        const selected = new Set(body.folder_ids);
        for (const folder of folders) if (selected.has(folder.parent_id)) selected.add(folder.id);
        for (const video of videos) if (selected.has(video.folder_id)) ids.add(video.id);
        return json({ job_id: `mock-${submissions.length}`, status: "queued", total: ids.size });
      }
      if (p === "/api/library/status") return json({ library_id: "folder-selection-fixture", data_root: "folder-selection-fixture", video_count: videos.length, active_job_id: null });
      if (p === "/api/videos") return json(videos);
      if (p === "/api/library") {
        const id = url.searchParams.has("folder_id") ? Number(url.searchParams.get("folder_id")) : null;
        return json({ folder: folders.find(folder => folder.id === id) ?? null, parent_id: id, sort: "manual", order: "asc",
          ancestors: id ? folders.filter(folder => folder.id === 7 || folder.id === id) : [],
          items: [...folders.filter(folder => folder.parent_id === id).map(folder => ({ type: "folder", folder })),
            ...videos.filter(video => video.folder_id === id).map(video => ({ type: "video", video }))] });
      }
      if (p === "/api/asr/selection") return json({ id: "whisper-turbo", name: "Whisper turbo", downloaded: true, engine: "whisper" });
      if (p === "/api/settings/ai") return json({ model: "模拟服务", has_api_key: true });
      if (p === "/api/jobs/paused") return json([]);
      if (p.startsWith("/api/jobs/")) return json({ id: p.split("/").at(-1), status: "interrupted", progress: 0 });
      if (p === "/api/batch/latest" || p === "/api/recycle-bin/latest") return json(null);
      if (p.endsWith("/thumbnail")) return route.fulfill({ status: 404, body: "" });
      return json({});
    });
    await page.goto(origin);
    const folderCheck = id => page.locator(`[data-library-key="folder-${id}"] .library-folder-select input`);
    const rootDock = page.locator(".library-inner > .library-selection-dock");
    await folderCheck(7).check();
    await rootDock.getByText("已选 3 个视频", { exact: true }).waitFor();
    assert.equal(await page.locator(".folder-window").count(), 0, "checkbox must not open the folder");
    assert.equal(await folderCheck(11).isDisabled(), true);
    await page.evaluate(() => document.fonts.ready);
    for (const width of [856, 1316, 1540, 1920]) {
      await page.setViewportSize({ width, height: 949 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      const checkbox = await folderCheck(7).boundingBox();
      const icon = await page.locator('[data-library-key="folder-7"] .folder-icon').boundingBox();
      assert.ok(checkbox.y + checkbox.height <= icon.y || checkbox.x + checkbox.width <= icon.x, "folder checkbox must not overlap its icon");
      await page.screenshot({ path: path.join(output, `selected-${width}.png`), fullPage: true });
    }
    await page.setViewportSize({ width: 1316, height: 949 });
    await page.getByRole("button", { name: "打开文件夹“数据库课程”", exact: true }).click();
    await folderCheck(8).waitFor();
    assert.equal(await folderCheck(9).isDisabled(), true);
    assert.equal(await page.locator(".folder-window > .library-selection-dock").count(), 0);
    await folderCheck(8).check();
    await page.locator(".folder-window > .library-selection-dock").getByText("已选 2 个视频", { exact: true }).waitFor();
    await page.keyboard.press("Escape");
    await page.locator(".folder-window").waitFor({ state: "detached" });
    assert.equal(await folderCheck(7).isChecked(), true);
    await rootDock.getByRole("button", { name: "取消选择", exact: true }).click();
    assert.equal(await folderCheck(7).isChecked(), false);

    await page.locator(".library-inner .library-select-all input").check();
    await rootDock.getByText("已选 5 个视频", { exact: true }).waitFor();
    assert.equal(await folderCheck(7).isChecked(), true);
    assert.equal(await folderCheck(10).isChecked(), true);
    assert.equal(await folderCheck(11).isChecked(), false);
    await rootDock.getByRole("button", { name: "处理所选（5）", exact: true }).click();
    const options = page.locator(".library-inner .library-processing-options");
    const start = options.getByRole("button", { name: "开始批处理（5）", exact: true });
    await start.waitFor();
    await options.locator("summary").click();
    assert.ok((await options.innerText()).includes("数据库课程 · 3 个视频（含子文件夹）"));
    await start.click();
    assert.equal(await folderCheck(7).isDisabled(), true);
    await options.getByRole("alert").filter({ hasText: "队列暂时不可用" }).waitFor();
    assert.equal(await folderCheck(7).isChecked(), true);
    assert.equal(submissions.length, 1);
    await start.click();
    await rootDock.waitFor({ state: "detached" });
    assert.deepEqual(submissions.map(body => body.video_ids), [[1], [1]]);
    assert.deepEqual(submissions.map(body => body.folder_ids), [[7, 10], [7, 10]]);
    assert.ok(submissions.every(body => body.asr_model_id === "whisper-turbo"));
    assert.equal(await folderCheck(7).isChecked(), false);

    // A library with no direct root videos must still expose the folder batch controls.
    videos[0].folder_id = 7;
    folders[0].video_count++;
    await page.reload();
    await folderCheck(7).check();
    await rootDock.getByText("已选 4 个视频", { exact: true }).waitFor();
    assert.equal(await page.locator(".library-inner .video-card").count(), 0);
    await page.emulateMedia({ reducedMotion: "reduce" });
    await rootDock.getByRole("button", { name: "处理所选（4）", exact: true }).click();
    await page.locator(".library-inner .library-processing-options").getByRole("button", { name: "开始批处理（4）", exact: true }).click();
    await rootDock.waitFor({ state: "detached" });
    assert.deepEqual(submissions.at(-1).video_ids, []);
    assert.deepEqual(submissions.at(-1).folder_ids, [7]);
    assert.deepEqual(errors, []);
    console.log(`Folder selection passed at four PC widths. Screenshots: ${output}`);
  } catch (error) {
    if (page) {
      await page.screenshot({ path: path.join(output, "failure.png"), fullPage: true }).catch(() => {});
      console.error((await page.locator("body").innerText()).slice(-6000));
    }
    throw error;
  } finally { await browser.close(); }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
