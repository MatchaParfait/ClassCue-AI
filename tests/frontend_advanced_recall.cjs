// All application requests are intercepted; prompts use the local builder, never an AI service.
const { chromium } = require("./playwright.cjs");
const { execFileSync } = require("node:child_process");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const output = path.join(root, ".runtime", `advanced-recall-qa-${Date.now()}`);
const origin = process.env.FRONTEND_URL || "http://127.0.0.1:5175";
const builder = [
  "import json,sys",
  "from recall_schemes import RecallPromptInput,preview_recall_prompt",
  "print(json.dumps(preview_recall_prompt(RecallPromptInput.model_validate_json(sys.stdin.buffer.read())),ensure_ascii=True))",
].join("\n");
function preview(payload) {
  return JSON.parse(execFileSync(path.join(root, ".venv/Scripts/python.exe"), ["-c", builder], {
    cwd: root, input: JSON.stringify(payload), encoding: "utf8", windowsHide: true,
  }));
}

async function main() {
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({
    executablePath: "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", headless: true,
  });
  let page;
  const errors = [], consoleErrors = [], failedRequests = [];
  try {
    page = await browser.newPage({ viewport: { width: 1316, height: 949 } });
    const schemes = [], writes = [], previews = [];
    page.on("pageerror", error => errors.push(error.message));
    page.on("console", message => { if (message.type() === "error") consoleErrors.push(message.text()); });
    page.on("requestfailed", request => failedRequests.push(`${request.url()} ${request.failure()?.errorText}`));
    page.setDefaultTimeout(15000);
    await page.route("**/api/**", async route => {
      const request = route.request(), p = new URL(request.url()).pathname;
      if (!p.startsWith("/api/")) return route.continue();
      const body = request.postData() ? request.postDataJSON() : null;
      const json = (value, status = 200) => route.fulfill({ status, json: value });
      if (p === "/api/settings/recall-schemes/prompt-preview") {
        previews.push(body);
        return json(preview(body));
      }
      if (request.method() !== "GET") {
        writes.push(p);
        if (p === "/api/settings/recall-schemes" && request.method() === "POST") {
          const scheme = { ...body, id: `custom_${String(schemes.length + 1).padStart(32, "0")}`,
            revision: 1, archived: false };
          schemes.push(scheme); return json(scheme, 201);
        }
        if (/^\/api\/settings\/recall-schemes\/custom_/.test(p) && request.method() === "PUT") {
          const current = schemes.find(scheme => p.endsWith(scheme.id));
          const same = current.advanced_prompt != null && body.advanced_prompt != null
            ? current.advanced_prompt === body.advanced_prompt
            : ["base_mode", "rules", "notes", "advanced_prompt"].every(key => JSON.stringify(current[key] ?? null) === JSON.stringify(body[key] ?? null));
          Object.assign(current, body, { revision: current.revision + (same ? 0 : 1) });
          return json(current);
        }
        throw new Error(`Unexpected mutation ${request.method()} ${p}`);
      }
      if (p === "/api/settings/recall-schemes") return json(schemes);
      if (p === "/api/updates") return json({
        version: "0.2.0", repository: "fixture/project", configured: false, portable: false,
        checking: false, downloading: false, preferences: {
          auto_check: false, auto_download: false, show_announcements: false,
        }, announcements: [], release: null, install: null,
      });
      if (p === "/api/library/status") return json({ library_id: "advanced-fixture", data_root: "mock", video_count: 0 });
      if (p === "/api/library") return json({ folder: null, parent_id: null, sort: "manual", order: "asc", items: [] });
      if (p === "/api/videos" || p === "/api/jobs/paused") return json([]);
      if (p === "/api/batch/latest" || p === "/api/recycle-bin/latest") return json(null);
      return json({});
    });
    await page.goto(`${origin}/settings?section=recall`);
    await page.getByRole("button", { name: "新建方案", exact: true }).click();
    const form = page.getByRole("form", { name: "编辑召回方案" });
    const toggle = form.locator(".recall-advanced-toggle");
    assert.equal(await toggle.getAttribute("aria-expanded"), "false");
    assert.equal(previews.length, 0);
    await form.getByLabel("方案名称", { exact: true }).fill("完整提示词试验");
    await form.getByLabel(/提示词基座/).selectOption("low_recall");
    await form.getByRole("button", { name: "添加正向表达", exact: true }).click();
    await form.getByLabel("教师表达 1", { exact: true }).fill("记到本子上");
    await form.getByLabel(/补充偏好/).fill("原有补充偏好");
    await toggle.click();
    const editor = form.locator(".recall-prompt-text");
    await page.waitForFunction(() => ![...document.querySelectorAll("button")]
      .find(button => button.textContent.includes("启用完整编辑"))?.disabled);
    const initial = await editor.inputValue();
    assert.ok(initial.includes("低召回") && initial.includes("记到本子上") && initial.includes("原有补充偏好"));
    assert.ok(initial.includes("<synthetic_examples>") && initial.includes("# 执行指令"));
    assert.equal(await editor.getAttribute("readonly"), "");
    await form.getByRole("button", { name: "启用完整编辑", exact: true }).click();
    assert.equal(await form.getByLabel(/提示词基座/).isDisabled(), true);
    const prompt = "只提取实际字幕中讲解的概念定义，不要求出现考试提示。\n\n# 判断示例\n字幕：递归调用自身。\n结果：提取“递归定义”。\n\n# 执行指令\n按以上规则输出固定 JSON。";
    await editor.fill(prompt);
    await page.waitForFunction(() => !document.querySelector(".recall-advanced-body [role=status]"));
    await form.getByText("固定输出与字幕引用协议 · 只读", { exact: true }).click();
    const protocol = form.locator(".recall-prompt-readonly").first();
    assert.ok((await protocol.innerText()).includes("evidence_sentence_ids"));
    assert.equal(await protocol.locator("textarea, input, [contenteditable=true]").count(), 0);
    await form.getByText("当前完整请求预览", { exact: true }).click();
    const requestPreview = form.locator(".recall-prompt-readonly").nth(1);
    assert.ok((await requestPreview.innerText()).includes(prompt));
    assert.ok(!(await requestPreview.innerText()).includes("原有补充偏好"));
    assert.ok(!(await requestPreview.innerText()).includes("仅有的两条纳入路径"));
    await page.evaluate(() => document.fonts.ready);
    for (const width of [856, 1316, 1540, 1920]) {
      await page.setViewportSize({ width, height: 949 });
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await page.screenshot({ path: path.join(output, `advanced-${width}.png`), fullPage: true });
    }
    assert.deepEqual(writes, []);
    await toggle.click();
    assert.equal(await editor.count(), 0);
    await form.getByRole("button", { name: "保存方案", exact: true }).click();
    await page.getByText("方案已保存", { exact: true }).waitFor();
    assert.equal(schemes[0].advanced_prompt, prompt);
    assert.equal(schemes[0].revision, 1);
    await page.reload();
    await page.locator(".recall-scheme-entry").first().click();
    assert.equal(await toggle.getAttribute("aria-expanded"), "false");
    assert.ok((await toggle.innerText()).includes("已启用"));
    await toggle.click();
    assert.equal(await editor.inputValue(), prompt);
    await editor.fill("未保存的完整提示词");
    await page.getByRole("button", { name: /返回视频库/ }).click();
    const dialog = page.getByRole("dialog", { name: "有未保存的修改" });
    await dialog.waitFor();
    await dialog.getByRole("button", { name: "继续编辑", exact: true }).click();
    assert.equal(await editor.inputValue(), "未保存的完整提示词");
    await form.getByRole("button", { name: "撤销修改", exact: true }).click();
    assert.equal(await editor.inputValue(), prompt);
    await form.getByRole("button", { name: "恢复基础编辑", exact: true }).click();
    await form.getByRole("button", { name: "取消", exact: true }).click();
    assert.equal(await form.getByLabel(/提示词基座/).isDisabled(), true);
    await form.getByRole("button", { name: "恢复基础编辑", exact: true }).click();
    await form.getByRole("button", { name: "确认恢复", exact: true }).click();
    assert.equal(await form.getByLabel(/提示词基座/).isEnabled(), true);
    assert.equal(await form.getByLabel("教师表达 1", { exact: true }).inputValue(), "记到本子上");
    await form.getByRole("button", { name: "保存方案", exact: true }).click();
    await page.getByText("方案已保存", { exact: true }).waitFor();
    assert.equal(schemes[0].advanced_prompt, null);
    assert.equal(schemes[0].revision, 2);
    assert.ok(writes.every(p => p.startsWith("/api/settings/recall-schemes")));
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, "result.json"), JSON.stringify({
      widths: [856, 1316, 1540, 1920], previews: previews.length, writes, errors,
      checks: ["derive", "full-edit", "protocol-readonly", "exact-preview", "collapse-save",
        "reload", "unsaved-guard", "discard", "restore-basic"],
    }, null, 2));
    console.log(output);
  } catch (error) {
    if (page) {
      await page.screenshot({ path: path.join(output, "failure.png"), fullPage: true }).catch(() => {});
      fs.writeFileSync(path.join(output, "failure.txt"), `${error}\n${await page.locator("body").innerText()}\n${JSON.stringify({ errors, consoleErrors, failedRequests }, null, 2)}`);
    }
    throw error;
  } finally {
    await browser.close();
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
