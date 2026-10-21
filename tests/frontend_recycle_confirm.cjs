// All API requests are intercepted. No real files or records are deleted.
const { chromium } = require("./playwright.cjs");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const origin = process.env.FRONTEND_URL || "http://127.0.0.1:5175";
const output = path.resolve(__dirname, "../.runtime", `recycle-confirm-qa-${Date.now()}`);

async function main() {
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ executablePath: "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", headless: true });
  let page;
  try {
    page = await browser.newPage({ viewport: { width: 1316, height: 949 } });
    page.setDefaultTimeout(15000);
    const errors = [], nativeDialogs = [], writes = [];
    page.on("pageerror", error => errors.push(error.message));
    page.on("dialog", async dialog => { nativeDialogs.push(dialog.type()); await dialog.dismiss(); });
    const longName = "数据结构与算法课程录像 第01讲 分治算法及时间复杂度 ".repeat(4);
    const videos = [
      { id: 1, display_name: longName, filename: "lecture-01.mp4", folder_id: null },
      { id: 2, display_name: "文件夹内的课程", filename: "lecture-02.mp4", folder_id: 7 },
    ].map(video => ({ ...video, status: "review_ready", duration_ms: 10000, video_path: video.filename }));
    const folder = { id: 7, name: "课程归档", parent_id: null, video_count: 1 };
    let deleted = false, latest = null, fail = true;
    await page.route("**/api/**", async route => {
      const request = route.request(), url = new URL(request.url()), p = url.pathname;
      if (!p.startsWith("/api/")) return route.continue();
      const json = (value, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(value) });
      if (request.method() !== "GET") {
        writes.push(`${request.method()} ${p}`);
        if (request.method() === "DELETE" && p === "/api/videos/1") {
          await new Promise(resolve => setTimeout(resolve, 450));
          if (fail) { fail = false; return json({ detail: "测试：视频文件暂时被占用" }, 409); }
          deleted = true;
          latest = { operation_id: "test", kind: "video", videos: 1, folders: 0 };
          return json(latest);
        }
        if (request.method() === "POST" && p === "/api/recycle-bin/undo") {
          deleted = false;
          const restored = latest; latest = null;
          return json(restored);
        }
        throw new Error(`Unexpected write: ${request.method()} ${p}`);
      }
      if (p === "/api/library/status") return json({ library_id: "recycle-fixture", data_root: "recycle-fixture", video_count: 2, active_job_id: null });
      if (p === "/api/videos") return json(videos.filter(video => !deleted || video.id !== 1));
      if (p === "/api/library") {
        const inFolder = url.searchParams.get("folder_id") === "7";
        return json({ folder: inFolder ? folder : null, ancestors: inFolder ? [folder] : [],
          parent_id: inFolder ? 7 : null, sort: "manual", order: "asc",
          items: [...(inFolder ? [] : [{ type: "folder", folder }]),
            ...videos.filter(video => (!deleted || video.id !== 1) && video.folder_id === (inFolder ? 7 : null))
              .map(video => ({ type: "video", video }))] });
      }
      if (p === "/api/recycle-bin/latest") return json(latest);
      if (p === "/api/batch/latest") return json(null);
      if (p === "/api/jobs/paused") return json([]);
      if (p.endsWith("/thumbnail")) return route.fulfill({ status: 404, body: "" });
      return json({});
    });
    await page.goto(origin);
    const trigger = page.locator('[data-library-key="video-1"] .library-card-delete');
    await trigger.click();
    const dialog = page.getByRole("dialog", { name: "移入回收站？", exact: true });
    await dialog.waitFor();
    await page.waitForFunction(() => document.activeElement?.textContent === "取消");
    await page.waitForFunction(() => getComputedStyle(document.querySelector(".recycle-confirm-dialog")).opacity === "1");
    await page.evaluate(() => document.fonts.ready);
    assert.equal(writes.length, 0);
    for (const width of [856, 1316, 1540, 1920]) {
      await page.setViewportSize({ width, height: 949 });
      await page.screenshot({ path: path.join(output, `video-${width}.png`) });
      const box = await dialog.boundingBox();
      assert.ok(box.x >= 0 && box.x + box.width <= width && box.y >= 0 && box.y + box.height <= 949);
      assert.ok(await dialog.evaluate(element => element.scrollWidth <= element.clientWidth));
      assert.equal(await page.locator(".library-page").evaluate(element => element.inert), true);
    }
    await page.keyboard.press("Escape");
    await dialog.waitFor({ state: "detached" });
    await trigger.evaluate(element => { if (document.activeElement !== element) throw new Error("Delete trigger did not regain focus"); });
    await page.getByRole("button", { name: "删除文件夹“课程归档”", exact: true }).click();
    await dialog.waitFor();
    assert.ok((await dialog.innerText()).includes("子文件夹、视频"));
    assert.equal(await page.locator(".folder-window").count(), 0);
    await dialog.getByRole("button", { name: "取消", exact: true }).click();
    await page.getByRole("button", { name: "打开文件夹“课程归档”", exact: true }).click();
    const folderWindow = page.locator(".folder-window");
    await folderWindow.getByRole("button", { name: "删除视频“文件夹内的课程”", exact: true }).click();
    await dialog.waitFor();
    await page.keyboard.press("Escape");
    await dialog.waitFor({ state: "detached" });
    assert.equal(await folderWindow.count(), 1);
    await page.keyboard.press("Escape");
    await folderWindow.waitFor({ state: "detached" });

    await trigger.click();
    await dialog.getByRole("button", { name: "移入回收站", exact: true }).dblclick();
    assert.equal(await dialog.getByRole("button", { name: "取消", exact: true }).isDisabled(), true);
    await page.keyboard.press("Escape");
    await dialog.getByRole("alert").waitFor();
    assert.ok((await dialog.getByRole("alert").innerText()).includes("暂时被占用"));
    assert.equal(writes.length, 1);
    await page.screenshot({ path: path.join(output, "retry.png") });
    await dialog.getByRole("button", { name: "重试移入", exact: true }).click();
    await dialog.waitFor({ state: "detached" });
    await trigger.waitFor({ state: "detached" });
    await page.locator("h1").click();
    await page.keyboard.press("Control+z");
    await trigger.waitFor();
    assert.deepEqual(writes, ["DELETE /api/videos/1", "DELETE /api/videos/1", "POST /api/recycle-bin/undo"]);
    await page.emulateMedia({ reducedMotion: "reduce" });
    await trigger.click();
    await dialog.waitFor();
    await dialog.getByRole("button", { name: "取消", exact: true }).click();
    assert.deepEqual(nativeDialogs, []);
    assert.deepEqual(errors, []);
    console.log(`Recycle confirmation passed at four PC widths. Screenshots: ${output}`);
  } catch (error) {
    if (page) {
      await page.screenshot({ path: path.join(output, "failure.png") }).catch(() => {});
      console.error((await page.locator("body").innerText()).slice(0, 6000));
    }
    throw error;
  } finally {
    await browser.close();
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
