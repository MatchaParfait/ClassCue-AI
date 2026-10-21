// Mock-only scheme editing and review checks. No real ASR, API charges or files.
const { chromium } = require("./playwright.cjs");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const origin = process.env.FRONTEND_URL || "http://127.0.0.1:5175";
const output = path.resolve(__dirname, "../.runtime", `recall-schemes-qa-${Date.now()}`);

async function main() {
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ executablePath: "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", headless: true });
  let page;
  try {
    page = await browser.newPage({ viewport: { width: 1316, height: 949 } });
    page.setDefaultTimeout(15000);
    const errors = [], writes = [], nativeDialogs = [], schemes = [];
    page.on("pageerror", error => errors.push(error.message));
    page.on("dialog", async dialog => { nativeDialogs.push(dialog.type()); await dialog.dismiss(); });
    const id = `custom_${"a".repeat(32)}`;
    let publishedRevision = 0;
    let job = null;
    const video = { id: 1, library_id: "recall-fixture", filename: "mock.mp4", display_name: "课堂测试",
      video_path: "mock.mp4", duration_ms: 60000, status: "review_ready", folder_id: null, has_transcript: true };
    const markers = [{ id: 1, video_id: 1, start_ms: 1000, end_ms: 3000, analysis_mode: "medium_recall",
      title: "内置方案考点", note: "独立结果", source_quote: "排序会考", review_status: "active", importance: "standard" }];
    await page.route("**/api/**", async route => {
      const request = route.request(), url = new URL(request.url()), p = url.pathname;
      if (!p.startsWith("/api/")) return route.continue();
      const json = (value, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(value) });
      if (request.method() !== "GET") {
        const body = request.postData() ? request.postDataJSON() : null;
        writes.push({ method: request.method(), path: p, body });
        if (p === "/api/settings/recall-schemes" && request.method() === "POST") {
          const scheme = { ...body, id, revision: 1, archived: false };
          schemes.push(scheme); return json(scheme, 201);
        }
        if (p === `/api/settings/recall-schemes/${id}` && request.method() === "PUT") {
          const current = schemes[0];
          const same = ["base_mode", "rules", "notes"].every(key => JSON.stringify(current[key]) === JSON.stringify(body[key]));
          Object.assign(current, body, { revision: current.revision + (same ? 0 : 1) }); return json(current);
        }
        if (p === `/api/settings/recall-schemes/${id}` && request.method() === "DELETE") {
          schemes[0].archived = true; return json({ archived: true });
        }
        if (["/api/videos/1/process", "/api/videos/1/analysis"].includes(p)) {
          assert.deepEqual(body.analysis_modes ?? [body.analysis_mode], [id], "only the selected custom scheme may run");
          assert.ok(!body.force_retranscribe, "must not rerun ASR");
          publishedRevision = schemes[0].revision;
          if (markers.length === 1) markers.push({ ...markers[0], id: 2, analysis_mode: id, title: "自定义考点",
            importance: "critical", user_start_ms: 500 });
          job = { id: `mock-${writes.length}`, job_id: `mock-${writes.length}`, video_id: 1, status: "completed",
            progress: 100, analysis_modes: [id], analysis_schemes: { [id]: schemes[0] } };
          return json({ ...job, status: "queued" }, 202);
        }
        throw new Error(`Unexpected mutation: ${request.method()} ${p}`);
      }
      if (p === "/api/settings/recall-schemes") return json(schemes);
      if (p === "/api/library/status") return json({ library_id: "recall-fixture", data_root: "recall-fixture", video_count: 1 });
      if (p === "/api/library") return json({ folder: null, parent_id: null, sort: "manual", order: "asc", items: [{ type: "video", video }] });
      if (p === "/api/videos") return json([video]);
      if (p === "/api/videos/1") return json(video);
      if (p === "/api/videos/1/subtitles") return json([{ id: 1, start_ms: 0, end_ms: 60000, text: "排序会考，拿笔画下来。" }]);
      if (p === "/api/videos/1/markers") return json(markers);
      if (p === "/api/videos/1/analysis-runs") return json([
        { analysis_mode: "medium_recall", status: "completed", candidate_count: 1 },
        ...(publishedRevision ? [{ analysis_mode: id, status: "completed", candidate_count: 1, scheme_revision: publishedRevision,
          latest_scheme_revision: schemes[0].revision, scheme_name: schemes[0].name, archived: schemes[0].archived }] : []),
      ]);
      if (p === "/api/videos/1/processing-options") return json({ asr_model: "Whisper", ai_service: { model: "mock-model" }, active_job: null });
      if (p === "/api/asr/selection") return json({ id: "whisper-turbo", name: "Whisper", engine: "whisper", selected: true, downloaded: true });
      if (p === "/api/settings/ai") return json({ model: "mock-model", provider: "deepseek", has_api_key: true, output_tokens: 2048 });
      if (p === "/api/jobs/paused") return json([]);
      if (p.startsWith("/api/jobs/")) return json(job ?? { id: "idle", status: "interrupted" });
      if (p === "/api/batch/latest" || p === "/api/recycle-bin/latest") return json(null);
      if (p.endsWith("/thumbnail") || p.endsWith("/stream") || p.endsWith("/media")) return route.fulfill({ status: 404, body: "" });
      return json({});
    });
    await page.goto(`${origin}/settings?section=recall`);
    await page.getByRole("button", { name: "新建方案", exact: true }).click();
    const form = page.getByRole("form", { name: "编辑召回方案", exact: true });
    const name = form.getByLabel("方案名称", { exact: true });
    const longName = "张老师的算法课程及期末复习重点提取方案";
    await name.fill(longName);
    await form.getByLabel(/提示词基座/).selectOption("medium_recall");
    await page.screenshot({ path: path.join(output, "empty-expressions.png"), fullPage: true });
    await form.getByRole("button", { name: "添加正向表达", exact: true }).click();
    await page.waitForFunction(() => document.activeElement?.getAttribute("aria-label") === "教师表达 1");
    await form.getByLabel("教师表达 1", { exact: true }).fill("拿笔画下来");
    await form.getByRole("button", { name: "添加负向表达", exact: true }).click();
    await form.getByLabel("教师表达 2", { exact: true }).fill("随便了解一下");
    assert.equal(await form.getByRole("group", { name: /正向提示词/ }).getByRole("textbox").count(), 1);
    assert.equal(await form.getByRole("group", { name: /负向提示词/ }).getByRole("textbox").count(), 1);
    await form.getByLabel(/补充偏好/).fill("重点定位教师明确要求掌握的知识对象，不把局部操作扩大成整章重点。");
    await form.getByRole("button", { name: "插入模板", exact: true }).click();
    assert.match(await form.getByLabel(/补充偏好/).inputValue(), /^重点定位.*\n\n优先关注：/);
    assert.equal(await form.getByRole("button", { name: "插入模板", exact: true }).isDisabled(), true);
    await page.evaluate(() => document.fonts.ready);
    for (const width of [856, 1316, 1540, 1920]) {
      await page.setViewportSize({ width, height: 949 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      await page.screenshot({ path: path.join(output, `edit-${width}.png`), fullPage: true });
    }
    assert.equal(writes.length, 0, "editing and previews must not send model requests");
    await form.getByRole("button", { name: "保存方案", exact: true }).click();
    await page.getByText("方案已保存", { exact: true }).waitFor();
    assert.equal(writes.length, 1);
    assert.equal(schemes[0].base_mode, "medium_recall");
    assert.deepEqual(schemes[0].rules, [
      { phrase: "拿笔画下来", meaning: "emphasis" },
      { phrase: "随便了解一下", meaning: "deemphasize" },
    ]);
    await name.fill("张老师");
    await form.getByRole("button", { name: "保存方案", exact: true }).click();
    await page.waitForFunction(() => document.querySelector(".recall-scheme-entry strong")?.textContent === "张老师");
    assert.equal(schemes[0].revision, 1);
    await page.getByRole("button", { name: /返回视频库/ }).click();
    await page.locator('.library-inner [data-library-key="video-1"] .library-video-select input').check();
    await page.getByRole("button", { name: "处理所选（1）", exact: true }).click();
    const options = page.locator(".library-inner .library-processing-options");
    await options.locator('input[value="medium_recall"]').uncheck();
    await options.locator(`input[value="${id}"]`).check();
    assert.deepEqual(await options.locator(".recall-mode-control input:checked").evaluateAll(nodes => nodes.map(node => node.value)), [id]);
    await page.screenshot({ path: path.join(output, "selected-custom.png"), fullPage: true });
    // A full navigation recreates the forms; selection must come from the saved library preference.
    await page.goto(`${origin}/process?videoId=1`);
    await page.locator(`.recall-mode-control input[value="${id}"]:checked`).waitFor();
    assert.deepEqual(await page.locator(".recall-mode-control input:checked").evaluateAll(nodes => nodes.map(node => node.value)), [id]);
    for (const width of [856, 1316, 1540, 1920]) {
      await page.setViewportSize({ width, height: 949 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      await page.screenshot({ path: path.join(output, `remembered-selection-${width}.png`), fullPage: true });
    }
    await page.goto(`${origin}/batch-process`);
    await page.locator(`.recall-mode-control input[value="${id}"]:checked`).waitFor();
    assert.deepEqual(await page.locator(".recall-mode-control input:checked").evaluateAll(nodes => nodes.map(node => node.value)), [id]);
    await page.goto(origin);
    await page.locator('.library-inner [data-library-key="video-1"] .library-video-select input').check();
    await page.getByRole("button", { name: "处理所选（1）", exact: true }).click();
    await options.locator(`input[value="${id}"]:checked`).waitFor();
    await options.getByRole("button", { name: "开始处理", exact: true }).click();
    await page.getByText("已提交视频处理任务", { exact: true }).waitFor();
    assert.equal(publishedRevision, 1);
    await page.goto(`${origin}/review/1`);
    const customTab = page.getByRole("tab", { name: /^张老师/ });
    await customTab.click();
    await page.getByText("自定义考点", { exact: true }).waitFor();
    assert.equal(await page.locator(".marker-main strong").filter({ hasText: "内置方案考点" }).count(), 0);
    await page.screenshot({ path: path.join(output, "custom-results.png"), fullPage: true });
    await page.goto(`${origin}/settings?section=recall`);
    await page.locator(".recall-scheme-entry").filter({ hasText: "张老师" }).click();
    await form.getByLabel(/补充偏好/).fill("更重视教师明确要求记住的概念区别。");
    await form.getByRole("button", { name: "保存方案", exact: true }).click();
    await page.getByText("方案已保存", { exact: true }).waitFor();
    assert.equal(schemes[0].revision, 2);
    await page.goto(`${origin}/process?videoId=1`);
    await page.locator(`.recall-mode-control input[value="${id}"]:checked`).waitFor();
    assert.deepEqual(await page.locator(".recall-mode-control input:checked").evaluateAll(nodes => nodes.map(node => node.value)), [id],
      "editing a scheme must not reset the remembered choice to medium");
    await page.goto(`${origin}/review/1`);
    await customTab.click();
    await page.getByRole("button", { name: "重新识别张老师", exact: true }).click();
    await page.getByText("自定义考点", { exact: true }).waitFor();
    assert.equal(publishedRevision, 2);
    await page.goto(`${origin}/settings?section=recall`);
    await page.getByRole("button", { name: "停用方案“张老师”", exact: true }).click();
    assert.equal(schemes[0].archived, false);
    await page.getByRole("button", { name: "确认停用", exact: true }).click();
    await page.getByText("方案已停用，历史结果保留", { exact: true }).waitFor();
    await page.goto(`${origin}/review/1`);
    await customTab.click();
    await page.getByText("自定义考点", { exact: true }).waitFor();
    assert.equal(markers[1].importance, "critical");
    assert.equal(markers[1].user_start_ms, 500);
    assert.deepEqual(errors, []); assert.deepEqual(nativeDialogs, []);
    fs.writeFileSync(path.join(output, "requests.json"), JSON.stringify(writes, null, 2));
    console.log(`Recall schemes PC flow passed. Screenshots: ${output}`);
  } catch (error) {
    if (page) {
      await page.screenshot({ path: path.join(output, "failure.png"), fullPage: true }).catch(() => {});
      console.error((await page.locator("body").innerText()).slice(-5000));
    }
    throw error;
  } finally { await browser.close(); }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
