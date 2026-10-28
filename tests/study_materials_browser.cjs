/* Manual study reader acceptance. Only run against study_material_browser_server.py. */
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium } = require("./playwright.cjs");

async function main() {
  const base = process.env.CLASSROOM_STUDY_QA_URL || "http://127.0.0.1:18880";
  const fixtures = path.resolve(process.env.CLASSROOM_STUDY_FIXTURES || ".runtime/study-qa/fixtures");
  const output = path.resolve(".runtime/study-materials-browser");
  await fs.mkdir(output, { recursive: true });
  const browser = await chromium.launch({ channel: process.env.CLASSROOM_BROWSER_CHANNEL || "msedge", headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: "reduce" });
  const page = await context.newPage();
  const errors = [], unexpected = [], modelRequests = [], externalRequests = [];
  try {
    const identity = await page.request.get(`${base}/api/study-qa`);
    assert.equal(identity.status(), 200, "Acceptance requires the isolated synthetic QA server");
    assert.equal((await identity.json()).synthetic, true);
    const status = await (await page.request.get(`${base}/api/library/status`)).json();
    const old = await (await page.request.get(`${base}/api/study-documents`)).json();
    for (const doc of old) await page.request.delete(`${base}/api/study-documents/${doc.id}`);
    await page.addInitScript(scope => localStorage.setItem(`classroom-onboarding:v1:${scope}`, JSON.stringify({ dismissed: true })), status.library_scope);
    page.on("pageerror", error => errors.push(error.message));
    page.on("response", response => { if (response.status() >= 400 && !response.url().includes("/api/study-documents?name=broken.pdf")) unexpected.push(`${response.status()} ${response.url()}`); });
    page.on("request", request => {
      if (/\/api\/(material-|study-.*process|.*analy[sz]|.*judge|.*dsh)/.test(request.url())) modelRequests.push(request.url());
      if (!request.url().startsWith(base) && !request.url().startsWith("data:") && !request.url().startsWith("blob:")) externalRequests.push(request.url());
    });
    await page.goto(base);
    assert.equal(await page.locator('a[href="/materials"]').count(), 0);
    await page.getByRole("link", { name: "学习资料", exact: true }).click();
    await page.getByRole("heading", { name: "学习资料", exact: true }).waitFor();
    await page.getByLabel("选择学习资料", { exact: true }).setInputFiles([
      "broken.pdf", "synthetic-paper.pdf", "synthetic-notes.md", "synthetic-photo.png",
    ].map(name => path.join(fixtures, name)));
    await page.locator(".study-file-row").filter({ hasText: "synthetic-photo.png" }).waitFor();
    assert.match(await page.locator('.study-error[role="alert"]').innerText(), /broken.pdf/);
    const docs = await (await page.request.get(`${base}/api/study-documents`)).json();
    assert.equal(docs.length, 3);
    const pdf = docs.find(doc => doc.format === "pdf"), md = docs.find(doc => doc.format === "markdown");
    assert.equal(pdf.page_count, 14);
    assert.match(await page.locator(".study-current-name").innerText(), /synthetic-paper.pdf/);
    await page.getByRole("button", { name: "关闭导入错误" }).click();
    const canvas = page.locator(".study-page-canvas").first();
    await canvas.locator("img").evaluate(image => image.decode());
    await page.getByRole("button", { name: "选题", exact: true }).click();
    await page.getByRole("checkbox", { name: "吸附文字边界", exact: true }).uncheck();
    const box = await canvas.boundingBox();
    await page.mouse.move(box.x + box.width * .07, box.y + box.height * .08);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width * .93, box.y + box.height * .34, { steps: 15 });
    await page.mouse.up();
    await page.locator(".study-region-preview img").first().evaluate(image => image.decode());
    assert.equal(await page.locator(".study-region-preview").count(), 1);
    const selected = page.locator(".study-selection-box").first();
    const originalBounds = await selected.getAttribute("style");
    assert.notEqual(await selected.evaluate(node => getComputedStyle(node).pointerEvents), "none");
    assert.equal(await selected.locator("[data-handle]").count(), 4);
    // The first stays editable while drawing; the next successful box locks it.
    await page.mouse.move(box.x + box.width * .15, box.y + box.height * .40);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width * .60, box.y + box.height * .50, { steps: 5 });
    assert.equal(await selected.locator("[data-handle]").count(), 4);
    await page.mouse.up();
    assert.equal(await page.locator(".study-region-preview").count(), 2);
    assert.equal(await selected.getAttribute("style"), originalBounds);
    assert.equal(await selected.evaluate(node => getComputedStyle(node).pointerEvents), "none");
    await page.getByRole("button", { name: "撤销选区操作", exact: true }).click();
    await selected.focus();
    await page.keyboard.press("ArrowRight");
    assert.ok(Number(await page.getByLabel("选区左边界", { exact: true }).inputValue()) > 7);
    assert.equal(await selected.locator("[data-handle]").count(), 4);
    await page.getByRole("button", { name: "撤销选区操作", exact: true }).click();
    await selected.locator('[data-handle="se"]').hover();
    await page.mouse.down();
    await page.mouse.move(box.x + box.width * .95, box.y + box.height * .38, { steps: 5 });
    await page.mouse.up();
    assert.equal(await selected.locator("[data-handle]").count(), 4);
    await page.getByRole("button", { name: "撤销选区操作", exact: true }).click();
    await page.screenshot({ path: path.join(output, "desktop-draft.png"), fullPage: true });
    await page.getByRole("button", { name: "下一页", exact: true }).click();
    await page.getByRole("button", { name: "添加本页选框", exact: true }).click();
    assert.equal(await selected.locator("[data-handle]").count(), 0);
    assert.equal(await page.locator(".study-region-preview").count(), 2);
    await page.getByLabel("题目标题", { exact: true }).fill("第三题：完整情景题（含两个小问）");
    await page.getByLabel("原题号", { exact: true }).fill("三");
    await page.getByRole("button", { name: "保存题目", exact: true }).click();
    await page.getByText("题目已保存", { exact: true }).waitFor();
    const pdfQuestions = await (await page.request.get(`${base}/api/study-questions?document_id=${pdf.id}`)).json();
    assert.equal(pdfQuestions.length, 1);
    assert.equal(pdfQuestions[0].regions.length, 2);
    assert.equal(pdfQuestions[0].regions[0].block.page, 1);
    assert.equal(pdfQuestions[0].regions[1].block.page, 2);
    assert.equal(await page.getByLabel("题目标题", { exact: true }).count(), 0);
    await page.getByRole("button", { name: "完整预览", exact: true }).click();
    const fullPreview = page.getByRole("dialog", { name: "第三题：完整情景题（含两个小问）", exact: true });
    await fullPreview.waitFor();
    assert.equal(await fullPreview.locator("img").count(), 2);
    await page.getByRole("button", { name: "放大预览", exact: true }).click();
    assert.match(await fullPreview.locator(".study-preview-tools").innerText(), /125%/);
    await page.keyboard.press("Escape");
    await fullPreview.waitFor({ state: "detached" });
    const before = pdfQuestions[0].regions[0].bbox;
    assert.ok(Math.abs(before[0] - .07) < .02 && Math.abs(before[2] - .93) < .02);
    await page.getByRole("button", { name: "放大", exact: true }).click();
    await page.waitForResponse(response => response.url().endsWith(`/api/study-documents/${pdf.id}/reading`) && response.status() === 200);
    await page.reload();
    await page.locator(".study-question-row").first().waitFor();
    await page.waitForFunction(() => document.querySelector(".study-zoom")?.textContent === "110%");
    await page.waitForFunction(() => document.querySelectorAll(".study-selection-box").length === 2);
    assert.equal(await page.locator(".study-region-preview").count(), 2);
    await page.getByRole("button", { name: "编辑题目", exact: true }).click();
    await page.getByRole("button", { name: "调整选框 1", exact: true }).click();
    await page.getByLabel("选区左边界", { exact: true }).fill("10");
    await page.getByRole("button", { name: "撤销选区操作", exact: true }).click();
    await page.getByRole("button", { name: "取消", exact: true }).click();
    // Drag continuously from page 1 into page 2 using edge scrolling.
    await page.getByRole("button", { name: "新建题目", exact: true }).click();
    await page.getByRole("checkbox", { name: "吸附文字边界", exact: true }).check();
    await page.getByRole("button", { name: "适合宽度", exact: true }).click();
    const crossCanvases = page.locator(".study-page-canvas");
    await page.locator(".study-reader-scroll").evaluate(panel => {
      const first = panel.querySelector(".study-page-canvas");
      panel.scrollTop += first.getBoundingClientRect().top - panel.getBoundingClientRect().top
        + first.getBoundingClientRect().height * .65;
    });
    const crossStart = await crossCanvases.nth(0).boundingBox();
    const crossPanel = await page.locator(".study-reader-scroll").boundingBox();
    await page.mouse.move(crossStart.x + crossStart.width * .3, crossPanel.y + 50);
    await page.mouse.down();
    await page.mouse.move(crossPanel.x + crossPanel.width * .65, crossPanel.y + crossPanel.height - 4, { steps: 10 });
    await page.waitForFunction(() => {
      const pages = document.querySelectorAll(".study-page-canvas"), panel = document.querySelector(".study-reader-scroll").getBoundingClientRect();
      return pages[1].getBoundingClientRect().top < panel.bottom - 120;
    });
    await page.screenshot({ path: path.join(output, "pdf-cross-page-drag.png"), fullPage: true });
    await page.mouse.up();
    await page.waitForFunction(() => document.querySelectorAll(".study-region-preview").length === 2);
    assert.equal(await page.locator(".study-selection-box:not(.locked)").count(), 2);
    assert.equal(await page.locator(".study-selection-handle").count(), 8);
    assert.equal(await page.locator(".study-draft > .study-question-content img").count(), 2);
    const guides = await crossCanvases.nth(0).getAttribute("data-text-left");
    assert.ok(Number(guides) < .1);
    const leftOfCrop = await page.locator(".study-selection-box").first().evaluate(node => Number.parseFloat(node.style.left) / 100);
    assert.ok(Math.abs(leftOfCrop - Number(guides)) < .002);
    await page.getByRole("button", { name: "完整预览", exact: true }).click();
    assert.equal(await page.getByRole("dialog").locator("img").count(), 2);
    await page.screenshot({ path: path.join(output, "pdf-cross-page-preview.png"), fullPage: true });
    await page.keyboard.press("Escape");
    await page.getByRole("button", { name: "保存题目", exact: true }).click();
    await page.getByText("题目已保存", { exact: true }).waitFor();
    const crossQuestions = await (await page.request.get(`${base}/api/study-questions?document_id=${pdf.id}`)).json();
    assert.equal(crossQuestions.length, 2);
    const cross = crossQuestions[1];
    assert.equal(cross.regions.length, 2);
    assert.equal(cross.regions[0].selection_group, cross.regions[1].selection_group);
    assert.ok(cross.regions[0].selection_group);
    assert.deepEqual(cross.regions.map(region => region.block.page), [1, 2]);
    await page.reload();
    await page.locator(".study-draft > .study-question-content img").first().waitFor();
    await page.getByRole("button", { name: "编辑题目", exact: true }).click();
    await page.locator(".study-source-management summary").click();
    await page.getByRole("button", { name: "调整选框 1", exact: true }).click();
    assert.equal(await page.locator(".study-selection-box:not(.locked)").count(), 2);
    await page.getByRole("button", { name: "取消", exact: true }).click();
    // Reverse drag must also cross real page boundaries while scrolling upward.
    await page.getByRole("button", { name: "新建题目", exact: true }).click();
    await page.locator(".study-reader-scroll").evaluate(panel => {
      const third = panel.querySelectorAll(".study-page-canvas")[2];
      panel.scrollTop += third.getBoundingClientRect().top - panel.getBoundingClientRect().top
        + third.getBoundingClientRect().height * .2;
    });
    await crossCanvases.nth(2).locator("img").evaluate(image => image.decode());
    const reverseStart = await crossCanvases.nth(2).boundingBox();
    const reverseReader = await page.locator(".study-reader-scroll").boundingBox();
    await page.mouse.move(reverseStart.x + reverseStart.width * .8, reverseReader.y + 80);
    await page.mouse.down();
    await page.mouse.move(reverseReader.x + reverseReader.width * .2, reverseReader.y + 4, { steps: 10 });
    await page.waitForFunction(() => {
      const panel = document.querySelector(".study-reader-scroll").getBoundingClientRect();
      return document.querySelector(".study-page-canvas").getBoundingClientRect().bottom > panel.top + 140;
    });
    await page.screenshot({ path: path.join(output, "pdf-reverse-cross-page-drag.png"), fullPage: true });
    await page.mouse.up();
    await page.waitForFunction(() => document.querySelectorAll(".study-region-preview").length === 3);
    assert.equal(await page.locator(".study-selection-box:not(.locked)").count(), 3);
    await page.getByRole("button", { name: "保存题目", exact: true }).click();
    await page.getByText("题目已保存", { exact: true }).waitFor();
    const reverseQuestions = await (await page.request.get(`${base}/api/study-questions?document_id=${pdf.id}`)).json();
    assert.deepEqual(reverseQuestions[2].regions.map(region => region.block.page), [1, 2, 3]);
    assert.equal(new Set(reverseQuestions[2].regions.map(region => region.selection_group)).size, 1);
    // Extend a completed crop upward: the corner must transfer out of its original page.
    await page.getByRole("button", { name: "新建题目", exact: true }).click();
    await page.locator(".study-reader-scroll").evaluate(panel => {
      const second = panel.querySelectorAll(".study-page-canvas")[1];
      panel.scrollTop += second.getBoundingClientRect().top - panel.getBoundingClientRect().top;
    });
    const resizeSource = await crossCanvases.nth(1).boundingBox();
    await page.mouse.move(resizeSource.x + resizeSource.width * .25, resizeSource.y + resizeSource.height * .12);
    await page.mouse.down();
    await page.mouse.move(resizeSource.x + resizeSource.width * .8, resizeSource.y + resizeSource.height * .3, { steps: 6 });
    await page.mouse.up();
    await page.locator('.study-selection-handle.nw').hover();
    await page.mouse.down();
    const resizeReader = await page.locator(".study-reader-scroll").boundingBox();
    await page.mouse.move(resizeSource.x + resizeSource.width * .15, resizeReader.y - 8, { steps: 10 });
    await page.waitForFunction(() => {
      const panel = document.querySelector(".study-reader-scroll").getBoundingClientRect();
      return document.querySelector(".study-page-canvas").getBoundingClientRect().bottom > panel.top + 120;
    });
    await page.screenshot({ path: path.join(output, "pdf-upward-cross-page-resize.png"), fullPage: true });
    await page.mouse.up();
    await page.waitForFunction(() => document.querySelectorAll(".study-region-preview").length === 2);
    assert.equal(await page.locator(".study-selection-box:not(.locked)").count(), 2);
    await page.getByRole("button", { name: "撤销选区操作", exact: true }).click();
    assert.equal(await page.locator(".study-region-preview").count(), 1);
    await page.getByRole("button", { name: "取消", exact: true }).click();
    await page.locator(".study-file-row").filter({ hasText: "synthetic-notes.md" }).locator(".study-file-open").click();
    await page.locator(".study-markdown-block table").waitFor();
    await page.locator(".study-markdown-block .katex").first().waitFor();
    await page.getByRole("button", { name: "选题", exact: true }).click();
    const choices = page.locator(".study-block-check input");
    const markdownBlocks = page.locator(".study-markdown-block");
    const markdownStart = await markdownBlocks.nth(2).boundingBox(), markdownEnd = await markdownBlocks.nth(7).boundingBox();
    await page.mouse.move(markdownStart.x + 40, markdownStart.y + 3);
    await page.mouse.down();
    await page.mouse.move(markdownEnd.x + markdownEnd.width - 8, markdownEnd.y + markdownEnd.height - 3, { steps: 12 });
    assert.equal(await page.locator(".study-block-check input:checked").count(), 6);
    assert.equal(await page.locator(".study-markdown-selection-box").count(), 1);
    await page.screenshot({ path: path.join(output, "markdown-box-selection.png"), fullPage: true });
    await page.mouse.up();
    assert.equal(await page.locator(".study-region-preview").count(), 6);
    assert.equal(await page.locator(".study-draft > .study-question-content > .study-markdown").count(), 1);
    assert.equal(await page.locator(".study-source-management").getAttribute("open"), null);
    assert.doesNotMatch(await page.locator(".study-draft > .study-question-content").innerText(), /第.*行/);
    await page.screenshot({ path: path.join(output, "markdown-continuous-draft.png"), fullPage: true });
    await page.getByRole("button", { name: "保存题目", exact: true }).click();
    await page.getByText("题目已保存", { exact: true }).waitFor();
    const mdQuestions = await (await page.request.get(`${base}/api/study-questions?document_id=${md.id}`)).json();
    assert.equal(mdQuestions.length, 1);
    assert.match(mdQuestions[0].regions.map(region => region.block.text).join("\n"), /（1）.*（2）/s);
    await page.screenshot({ path: path.join(output, "desktop-markdown.png"), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByRole("button", { name: "题目侧栏", exact: true }).click();
    await page.locator(".study-source-management summary").click();
    await page.locator(".study-region-preview header button").last().click();
    await page.waitForFunction(() => {
      const panel = document.querySelector(".study-reader-scroll").getBoundingClientRect();
      const located = document.querySelector(".study-located")?.getBoundingClientRect();
      return panel.height > 0 && located && located.top >= panel.top && located.top < panel.bottom;
    });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.getByRole("button", { name: "新建题目", exact: true }).click();
    await choices.nth(0).click();
    const fixedSave = await page.getByRole("button", { name: "保存题目", exact: true }).boundingBox();
    const questionPane = await page.locator(".study-question-pane").boundingBox();
    assert.ok(fixedSave.y >= questionPane.y && fixedSave.y + fixedSave.height <= questionPane.y + questionPane.height);
    await page.getByRole("button", { name: "返回视频库", exact: true }).click();
    await page.getByRole("dialog", { name: "有未保存的修改", exact: true }).waitFor();
    await page.getByRole("button", { name: "继续编辑", exact: true }).click();
    await page.getByRole("button", { name: "取消", exact: true }).click();
    await page.getByRole("button", { name: "移除题目 1", exact: true }).click();
    await page.waitForFunction(() => !document.querySelector(".study-question-row"));
    await page.getByRole("checkbox", { name: "已移除内容", exact: true }).check();
    await page.getByRole("button", { name: "恢复题目 1", exact: true }).click();
    await page.getByRole("checkbox", { name: "已移除内容", exact: true }).uncheck();
    await page.locator(".study-question-row").first().waitFor();
    await page.getByRole("button", { name: "修改名称 synthetic-notes.md", exact: true }).click();
    await page.getByLabel("资料名称", { exact: true }).fill("手动选题测试.md");
    await page.getByRole("button", { name: "保存资料名称", exact: true }).click();
    await page.getByRole("button", { name: "移除 手动选题测试.md", exact: true }).click();
    await page.locator(".study-file-row").filter({ hasText: "手动选题测试.md" }).waitFor({ state: "detached" });
    await page.getByRole("checkbox", { name: "已移除内容", exact: true }).check();
    await page.getByRole("button", { name: "恢复 手动选题测试.md", exact: true }).last().click();
    await page.getByRole("checkbox", { name: "已移除内容", exact: true }).uncheck();
    await page.locator(".study-file-row").filter({ hasText: "手动选题测试.md" }).locator(".study-file-open").click();
    assert.equal((await (await page.request.get(`${base}/api/study-questions?document_id=${md.id}`)).json())[0].id, mdQuestions[0].id);
    for (const viewport of [{ width: 1280, height: 720 }, { width: 768, height: 1024 }, { width: 390, height: 844 }]) {
      await page.setViewportSize(viewport);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `Overflow at ${viewport.width}`);
      if (viewport.width <= 950) {
        await page.getByRole("button", { name: "返回资料", exact: true }).click();
        await page.locator(".study-file-row").filter({ hasText: "synthetic-photo.png" }).locator(".study-file-open").click();
        await page.getByRole("button", { name: "选题", exact: true }).click();
        await page.getByRole("button", { name: "题目侧栏", exact: true }).click();
        await page.getByRole("button", { name: "新建题目", exact: true }).click();
        await page.getByRole("button", { name: "题目侧栏", exact: true }).click();
        await page.getByRole("button", { name: "添加本页选框", exact: true }).click();
        await page.getByRole("button", { name: "保存题目", exact: true }).click();
        await page.getByText("题目已保存", { exact: true }).waitFor();
        await page.screenshot({ path: path.join(output, `${viewport.width}-questions.png`), fullPage: true });
        await page.getByRole("button", { name: "返回原文", exact: true }).click();
      }
      await page.screenshot({ path: path.join(output, `${viewport.width}-reader.png`), fullPage: true });
    }
    // Repeat the continuous gesture in a narrow reader, then undo the whole group.
    await page.getByRole("button", { name: "返回资料", exact: true }).click();
    await page.locator(".study-file-row").filter({ hasText: "synthetic-paper.pdf" }).locator(".study-file-open").click();
    await page.getByRole("button", { name: "题目侧栏", exact: true }).click();
    await page.getByRole("button", { name: "新建题目", exact: true }).click();
    await page.getByRole("button", { name: "适合宽度", exact: true }).click();
    await page.locator(".study-reader-scroll").evaluate(panel => {
      const first = panel.querySelector(".study-page-canvas");
      panel.scrollTop += first.getBoundingClientRect().top - panel.getBoundingClientRect().top
        + first.getBoundingClientRect().height * .6;
    });
    const narrowFirst = await crossCanvases.nth(0).boundingBox();
    const narrowReader = await page.locator(".study-reader-scroll").boundingBox();
    await page.mouse.move(narrowFirst.x + narrowFirst.width * .2, narrowReader.y + 24);
    await page.mouse.down();
    await page.mouse.move(narrowReader.x + narrowReader.width * .8, narrowReader.y + narrowReader.height - 4, { steps: 8 });
    await page.waitForFunction(() => {
      const panel = document.querySelector(".study-reader-scroll").getBoundingClientRect();
      return document.querySelectorAll(".study-page-canvas")[1].getBoundingClientRect().top < panel.bottom - 120;
    });
    const narrowSecond = await crossCanvases.nth(1).boundingBox();
    await page.mouse.move(narrowSecond.x + narrowSecond.width * .8, narrowSecond.y + narrowSecond.height * .25, { steps: 3 });
    await page.mouse.up();
    await page.getByRole("button", { name: "预览并保存", exact: true }).click();
    assert.equal(await page.locator(".study-region-preview").count(), 2);
    assert.equal(await page.locator(".study-draft > .study-question-content img").count(), 2);
    await page.screenshot({ path: path.join(output, "390-cross-page-question.png"), fullPage: true });
    await page.getByRole("button", { name: "撤销选区操作", exact: true }).click();
    assert.equal(await page.locator(".study-region-preview").count(), 0);
    await page.getByRole("button", { name: "取消", exact: true }).click();
    // Exercise a single box spanning content outside the initial viewport.
    const longMarkdown = Array.from({ length: 30 }, (_, index) => `Question ${index + 1}: retain the original condition and all subquestions.\n\n`).join("");
    const longUpload = await page.request.post(`${base}/api/study-documents?name=long-box-selection.md`, {
      data: Buffer.from(longMarkdown), headers: { "Content-Type": "application/octet-stream" },
    });
    assert.equal(longUpload.status(), 201);
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.reload();
    await page.locator(".study-file-row").filter({ hasText: "long-box-selection.md" }).locator(".study-file-open").click();
    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByRole("button", { name: "选题", exact: true }).click();
    const longStart = await page.locator(".study-markdown-block").first().boundingBox();
    const longReader = await page.locator(".study-reader-scroll").boundingBox();
    await page.mouse.move(longStart.x + 40, longStart.y + 3);
    await page.mouse.down();
    await page.mouse.move(longReader.x + longReader.width - 12, longReader.y + longReader.height - 5, { steps: 10 });
    await page.waitForFunction(() => document.querySelector(".study-reader-scroll").scrollTop > 350);
    await page.mouse.up();
    const selectedByBox = await page.locator(".study-block-check input:checked").count();
    assert.ok(selectedByBox > 4);
    await page.getByRole("button", { name: "预览并保存", exact: true }).click();
    assert.equal(await page.locator(".study-region-preview").count(), selectedByBox);
    await page.getByRole("button", { name: "撤销选区操作", exact: true }).click();
    assert.equal(await page.locator(".study-region-preview").count(), 0);
    await page.getByRole("button", { name: "取消", exact: true }).click();
    assert.deepEqual(errors, []);
    assert.deepEqual(unexpected, []);
    assert.deepEqual(modelRequests, []);
    assert.deepEqual(externalRequests, []);
    await fs.writeFile(path.join(output, "result.json"), JSON.stringify({ passed: true, errors, unexpected,
      modelRequests, externalRequests, viewports: [1440, 1280, 768, 390] }, null, 2));
    console.log("Study materials acceptance passed: forward and reverse PDF cross-page drag, upward corner resize across pages, auto-scroll, maximum text width, group persistence and undo, Markdown selection, responsive reading, no model requests.");
  } catch (error) {
    await page.screenshot({ path: path.join(output, "failure.png"), fullPage: true }).catch(() => {});
    console.error(await page.locator(".study-materials-page").innerText().catch(() => "Page unavailable"));
    throw error;
  } finally {
    await browser.close();
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
