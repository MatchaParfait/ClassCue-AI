// Isolated browser checks: all API requests use fixtures; no real model changes.
const { chromium } = require("./playwright.cjs");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

async function main() {
  const origin = process.env.FRONTEND_URL || "http://127.0.0.1:5175";
  const output = path.resolve(__dirname, `../.runtime/asr-settings-qa-${Date.now()}`);
  fs.mkdirSync(output, { recursive: true });
  const common = { downloaded: true, cache_size_bytes: 1024 ** 3, runtime_size_bytes: 4 * 1024 ** 3,
    status_checked_at: 1792075800, inspection_status: "ready", gpu_available: true, gpu_runtime_ready: true,
    gpu_prepare_available: true, gpu_name: "Fixture GPU" };
  const models = [
    { ...common, id: "whisper-turbo", name: "Whisper turbo", engine: "whisper" },
    { ...common, id: "whisper-large-v3", name: "Whisper large-v3", engine: "whisper", downloaded: false, cache_size_bytes: 0 },
    { ...common, id: "qwen3-asr-1.7b", name: "Qwen3-ASR 1.7B", engine: "qwen", qwen_ready: true },
    { ...common, id: "fireredasr2-aed", name: "FireRedASR2S（VAD + AED）", engine: "firered", device: "gpu", selected: true, aed_ready: true },
    { ...common, id: "paraformer-large-cpu", name: "Paraformer-large（BiCIF）", engine: "paraformer", device: "cpu" },
    { ...common, id: "dolphin-small-cn", name: "Dolphin 中文 small（CTC）", engine: "dolphin", device: "cpu" },
  ];
  const writes = [], errors = [];
  const browser = await chromium.launch({ executablePath: "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", headless: true });
  const context = await browser.newContext({ viewport: { width: 1338, height: 1038 } });
  const page = await context.newPage();
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/api/**", async (route) => {
    const request = route.request(), url = new URL(request.url());
    if (!url.pathname.startsWith("/api/")) return route.continue();
    const json = (value, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(value) });
    if (url.pathname === "/api/library/status") return json({ data_root: "E:/fixture/library", library_id: "asr-fixture",
      database_path: "E:/fixture/project.db", video_count: 0 });
    if (url.pathname === "/api/asr/models") return json(models);
    if (/^\/api\/asr\/models\/[^/]+\/status$/.test(url.pathname)) return json(models.find((model) => model.id === url.pathname.split("/")[4]));
    if (url.pathname === "/api/asr/selection" && request.method() === "PUT") {
      const body = request.postDataJSON(); writes.push(body);
      models.forEach((model) => {
        model.selected = model.id === body.model_id;
        if (model.selected) model.device = body.paraformer_device || body.dolphin_device || body.firered_device || model.device;
      });
      return json(models.find((model) => model.selected));
    }
    if (request.method() !== "GET") throw new Error(`Unexpected mutation: ${request.method()} ${url.pathname}`);
    if (url.pathname === "/api/jobs/paused" || url.pathname === "/api/videos") return json([]);
    if (url.pathname === "/api/batch/latest") return json(null);
    return json({});
  });
  try {
    await page.goto(`${origin}/settings?section=asr`);
    const section = page.getByRole("region", { name: "选择转写引擎" });
    const more = section.locator(".asr-more-models");
    const card = (id) => section.locator(`[data-model-id="${id}"]`);
    await card("qwen3-asr-1.7b").waitFor();
    await page.evaluate(() => document.fonts.ready);
    assert.equal(await section.locator(".asr-model-option:visible").count(), 4);
    assert.deepEqual(await section.locator(".asr-model-option:visible").evaluateAll((nodes) => nodes.map((node) => node.dataset.modelId)),
      ["qwen3-asr-1.7b", "fireredasr2-aed", "paraformer-large-cpu", "dolphin-small-cn"]);
    assert.equal(await more.evaluate((node) => node.open), false);
    assert.deepEqual(await section.locator(".asr-model-meta").evaluateAll((nodes) => nodes.map((node) => [...node.querySelectorAll("dt")].map((item) => item.textContent))),
      Array.from({ length: 6 }, () => ["状态", "缓存"]));
    const para = await card("paraformer-large-cpu").boundingBox(), dolphin = await card("dolphin-small-cn").boundingBox();
    assert.ok(Math.abs(para.y - dolphin.y) < 1, "CPU/GPU models must share the desktop row");
    assert.ok((await card("fireredasr2-aed").innerText()).includes("约 200 秒"));
    assert.ok((await card("paraformer-large-cpu").innerText()).includes("GPU 约 40 秒 · CPU 约 60 秒"));
    assert.ok((await card("dolphin-small-cn").innerText()).includes("GPU 约 50 秒 · CPU 约 75 秒"));
    assert.equal(await card("fireredasr2-aed").locator('input[value="cpu"]').count(), 0);
    assert.equal(await card("qwen3-asr-1.7b").locator(".asr-fixed-device").innerText(), "GPU");
    assert.equal(await section.locator(".firered-device-controls:visible").count(), 4);
    assert.deepEqual(await section.locator(".gpu-device-status:visible").allTextContents(), Array(4).fill("Fixture GPU"));
    assert.equal(await section.getByRole("button", { name: "保存模型选择", exact: true }).isDisabled(), true);
    for (const id of ["paraformer-large-cpu", "dolphin-small-cn"]) {
      assert.equal(await card(id).locator('input[value="gpu"]').isChecked(), true);
      assert.equal(await card(id).locator('input[value="gpu"]').isEnabled(), true);
    }
    async function capture(name, width) {
      await page.setViewportSize({ width, height: 1038 });
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.waitForTimeout(180);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false, name);
      const clipped = await section.locator(".asr-model-option:visible").evaluateAll((nodes) => nodes.filter((node) => node.scrollWidth > node.clientWidth + 1).map((node) => node.dataset.modelId));
      assert.deepEqual(clipped, [], `overflowing cards: ${name}`);
      await page.screenshot({ path: `${output}/${name}-${width}.png`, fullPage: true });
    }
    for (const width of [1338, 1024, 390]) await capture("asr-default", width);
    await page.setViewportSize({ width: 1338, height: 1038 });
    await more.locator("summary").focus();
    await page.keyboard.press("Enter");
    assert.equal(await section.locator(".asr-model-option:visible").count(), 6);
    assert.ok((await card("whisper-turbo").innerText()).includes("不推荐用于中文课堂"));
    await capture("asr-expanded", 1338);
    await card("whisper-large-v3").locator('input[name="asr-model"]').check();
    assert.equal(writes.length, 0);
    await more.locator("summary").click();
    assert.ok((await section.locator(".asr-current-choice").innerText()).includes("Whisper large-v3"));
    assert.equal(await card("whisper-large-v3").locator("input").isChecked(), true);
    await section.getByRole("button", { name: "保存模型选择", exact: true }).click();
    await page.waitForFunction(() => [...document.querySelectorAll(".settings-notice")].some((node) => node.textContent.includes("已保存")));
    assert.equal(writes.at(-1).model_id, "whisper-large-v3");

    // Device controls and explicit save still work for both CPU/GPU engines.
    for (const [id, field] of [["paraformer-large-cpu", "paraformer_device"], ["dolphin-small-cn", "dolphin_device"]]) {
      // Click a preselected device on an inactive card to select that model.
      await card(id).locator('input[value="gpu"]').click();
      assert.equal(await card(id).locator('input[name="asr-model"]').isChecked(), true);
      await section.getByRole("button", { name: "保存模型选择", exact: true }).click();
      await page.waitForFunction((modelId) => document.querySelector(`[data-model-id="${modelId}"] .asr-model-actions .danger-icon`).disabled, id);
      await new Promise((resolve) => setTimeout(resolve, 50));
      assert.equal(writes.at(-1).model_id, id);
      assert.equal(writes.at(-1)[field], "gpu");
    }

    // No CUDA: the GPU control remains visible but disabled, with CPU as fallback.
    models.forEach((model) => {
      model.selected = model.id === "fireredasr2-aed";
      if (model.engine === "qwen" || model.engine === "firered") {
        Object.assign(model, { gpu_available: false, gpu_unavailable_reason: "运行环境中 CUDA 不可用" });
      }
      if (model.engine === "paraformer" || model.engine === "dolphin") {
        Object.assign(model, { device: "cpu", gpu_available: false, gpu_runtime_ready: false,
          gpu_prepare_available: false, gpu_unavailable_reason: "未检测到可用的 CUDA 设备" });
      }
    });
    await page.reload();
    await card("paraformer-large-cpu").locator('input[value="cpu"]').waitFor();
    for (const id of ["qwen3-asr-1.7b", "fireredasr2-aed"]) {
      assert.equal(await card(id).locator(".gpu-device-status").innerText(), "运行环境中 CUDA 不可用");
    }
    for (const id of ["paraformer-large-cpu", "dolphin-small-cn"]) {
      const gpu = card(id).locator('input[value="gpu"]');
      assert.equal(await gpu.isDisabled(), true);
      assert.equal(await card(id).locator('input[value="cpu"]').isChecked(), true);
      assert.ok(Number(await gpu.evaluate((node) => getComputedStyle(node.closest("label")).opacity)) < 1);
      await gpu.evaluate((node) => node.click());
      assert.equal(await card(id).locator('input[name="asr-model"]').isChecked(), false);
    }
    for (const width of [1338, 390]) await capture("asr-no-cuda", width);
    await page.setViewportSize({ width: 1338, height: 1038 });
    await card("dolphin-small-cn").locator('input[value="cpu"]').click();
    assert.equal(await card("dolphin-small-cn").locator('input[name="asr-model"]').isChecked(), true);
    await section.getByRole("button", { name: "保存模型选择", exact: true }).click();
    await page.waitForFunction(() => [...document.querySelectorAll(".settings-notice")].some((node) => node.textContent.includes("已保存")));
    assert.equal(writes.at(-1).dolphin_device, "cpu");
    assert.deepEqual(errors, []);
    fs.writeFileSync(`${output}/result.json`, JSON.stringify({ passed: true, viewports: [1338, 1024, 390],
      checks: ["four visible models", "CPU/GPU adjacency", "status/cache only", "supplied model comparisons",
        "keyboard disclosure", "selection retained when collapsed", "Whisper save", "Paraformer/Dolphin GPU save",
        "four device sections with runtime GPU names", "GPU recommended without changing the saved model", "no-CUDA disabled controls",
        "CPU fallback save", "no layout overflow"] }, null, 2));
    console.log(JSON.stringify({ passed: true, output }));
  } catch (error) {
    await page.screenshot({ path: `${output}/failure.png`, fullPage: true });
    console.error(JSON.stringify({ errors, body: (await page.locator("body").innerText()).slice(-1800) }));
    throw error;
  } finally {
    await browser.close();
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
