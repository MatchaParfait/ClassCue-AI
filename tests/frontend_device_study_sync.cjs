// Synthetic HTTP state only. The browser cannot reach a personal library.
const { chromium } = require("./playwright.cjs");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const origin = process.env.FRONTEND_URL || "http://127.0.0.1:5176";
const output = path.resolve(__dirname, "../.runtime/qa/study-sync-ui");

async function main() {
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, reducedMotion: "reduce" });
  await page.addInitScript(() => localStorage.setItem("classroom-onboarding:v1:synthetic-library", JSON.stringify({ dismissed: true })));
  const errors = [], writes = [];
  let fail = true;
  const conflict = { id: "synthetic-record", title: "同一考点的标题存在差异", fields: ["title", "note", "removed"],
    local: { title: "电脑保留的重点", note: "电脑端补充的备注", removed: false },
    remote: { title: "平板修正的重点", note: "在外学习时补充的备注", removed: true } };
  const task = { id: "synthetic-sync", direction: "incoming", video_id: 1, name: "合成课程 A", peer_name: "合成测试平板",
    state: "awaiting_confirmation", error: "", preview: { bootstrap: false, added: 2, removed: 1, edited: 1, conflicts: [conflict] } };
  const status = { devices: [{ id: "synthetic-device", name: "合成测试平板", paired: true, foreground: true, root_available: true,
      capabilities: ["study-sync-v1"] }], pairings: [], tasks: [], snapshots: [], error: "", sync_tasks: [task] };
  page.on("pageerror", error => errors.push(error.message));
  await page.route("**/api/**", async route => {
    const request = route.request(), p = new URL(request.url()).pathname;
    if (!p.startsWith("/api/")) return route.continue();
    const json = (value, code = 200) => route.fulfill({ status: code, contentType: "application/json", body: JSON.stringify(value) });
    if (request.method() !== "GET") {
      writes.push({ path: p, data: request.postDataJSON() });
      if (p === "/api/device-transfers/sync/synthetic-sync/accept") {
        if (fail) { fail = false; return json({ detail: "合成测试：保存失败，请重试" }, 409); }
        task.state = "awaiting_ack";
        return json(task);
      }
      throw new Error(`Unexpected mutation ${p}`);
    }
    if (p === "/api/library/status") return json({ library_id: "synthetic-library", data_root: "synthetic-library", video_count: 0, active_job_id: null });
    if (p === "/api/settings/recall-selection") return json({ library_id: "synthetic-library", initialized: true, revision: 1, analysis_modes: ["medium_recall"] });
    if (p === "/api/videos") return json([]);
    if (p === "/api/library") return json({ folder: null, parent_id: null, sort: "manual", order: "asc", items: [] });
    if (p === "/api/device-transfers") return json(status);
    if (p === "/api/jobs/paused" || p === "/api/jobs/recent") return json([]);
    if (p === "/api/batch/latest" || p.endsWith("/latest")) return json(null);
    return json({});
  });
  try {
    await page.goto(origin);
    await page.getByRole("button", { name: "连接平板", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "连接平板", exact: true });
    await dialog.getByText("同一考点的标题存在差异").waitFor();
    const accept = dialog.getByRole("button", { name: "确认同步", exact: true });
    assert.equal(await accept.isDisabled(), true);
    for (const viewport of [{ width: 1440, height: 900 }, { width: 1280, height: 800 }, { width: 390, height: 844 }]) {
      await page.setViewportSize(viewport);
      await dialog.getByRole("button", { name: "确认同步", exact: true }).scrollIntoViewIfNeeded();
      await page.screenshot({ path: path.join(output, `conflict-${viewport.width}.png`) });
      assert.equal(await dialog.evaluate(element => element.scrollWidth <= element.clientWidth), true);
      for (const control of await dialog.locator("button, select, input").all()) {
        const box = await control.boundingBox();
        if (box) assert.ok(box.x >= 0 && box.x + box.width <= viewport.width + 1);
      }
    }
    await dialog.getByRole("radio", { name: /采用平板修改/ }).check();
    assert.equal(await accept.isEnabled(), true);
    await accept.click();
    await dialog.getByRole("alert").getByText("合成测试：保存失败，请重试").waitFor();
    assert.equal(await accept.isEnabled(), true);
    assert.equal(await dialog.getByRole("radio", { name: /采用平板修改/ }).isChecked(), true);
    await accept.click();
    await dialog.getByText("已保存，等待平板确认").waitFor();
    assert.deepEqual(writes.map(write => write.data), [
      { library_id: "synthetic-library", choices: { "synthetic-record": "remote" } },
      { library_id: "synthetic-library", choices: { "synthetic-record": "remote" } },
    ]);
    await page.keyboard.press("Escape");
    await dialog.waitFor({ state: "detached" });
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ passed: true, viewports: [1440, 1280, 390], evidence: output }));
  } catch (error) {
    await page.screenshot({ path: path.join(output, "failure.png") });
    console.error((await page.locator("body").innerText()).slice(0, 4000), errors);
    throw error;
  } finally {
    await browser.close();
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
