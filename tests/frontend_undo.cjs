// Run only against tests/fixtures/workbench_server.py in an ignored temporary directory.
const { chromium } = require("./playwright.cjs");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { expect: baseExpect } = require(path.join(process.env.CLASSROOM_PLAYWRIGHT_MODULE || process.env.PLAYWRIGHT_MODULE
  || path.join(__dirname, "../frontend/node_modules/playwright"), "test"));
const expect = baseExpect.configure({ timeout: 15000 });

const origin = process.env.FRONTEND_URL;
const fixtureRoot = process.env.CLASSROOM_UNDO_QA_ROOT;
assert.ok(origin && fixtureRoot, "Set FRONTEND_URL and CLASSROOM_UNDO_QA_ROOT to an isolated fixture");
const output = path.resolve(fixtureRoot, "screenshots");

async function main() {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const page = await browser.newPage({ viewport: { width: 1360, height: 960 } });
  page.setDefaultTimeout(15000);
  const errors = [];
  const writes = [];
  const videoReads = [];
  page.on("response", async response => {
    if (new URL(response.url()).pathname === "/api/videos") {
      const value = await response.json().catch(() => null);
      videoReads.push({ status: response.status(), ids: Array.isArray(value) ? value.map(video => video.id) : null });
    }
  });
  page.on("pageerror", error => errors.push(error.message));
  const get = async url => {
    const response = await page.request.get(`${origin}${url}`);
    assert.equal(response.status(), 200, `${url}: ${await response.text()}`);
    return response.json();
  };
  const post = async (url, data) => {
    const response = await page.request.post(`${origin}${url}`, { data });
    assert.ok(response.ok(), `${url}: ${await response.text()}`);
    return response.json();
  };
  const undo = async () => {
    await expect(page.locator(".library-undo")).toBeEnabled();
    const saved = page.waitForResponse(response => response.request().method() === "POST"
      && new URL(response.url()).pathname === "/api/library/undo");
    await page.locator("h1").click();
    await page.keyboard.press("Control+z");
    const response = await saved;
    assert.equal(response.status(), 200);
    const restored = await response.json();
    await expect(page.locator(".toast")).toContainText(
      restored.kind === "reorder" ? "已撤销排序" : restored.kind.startsWith("move_") ? "已撤销移动" : "已恢复"
    );
    await expect(page.locator(".library-undo .spin")).toHaveCount(0);
  };
  const drag = async (source, target, edge = false) => {
    await source.evaluate(element => {
      element.dispatchEvent(new DragEvent("dragstart", { bubbles: true, cancelable: true, dataTransfer: new DataTransfer() }));
    });
    await expect(source).toHaveAttribute("data-drag-source", "true");
    await target.evaluate((element, edge) => {
      const rect = element.getBoundingClientRect();
      const options = { bubbles: true, cancelable: true, dataTransfer: new DataTransfer(),
        clientX: edge ? rect.right - 8 : rect.left + rect.width / 2, clientY: rect.top + rect.height / 2 };
      element.dispatchEvent(new DragEvent("dragover", options));
      element.dispatchEvent(new DragEvent("drop", options));
    }, edge);
  };
  try {
    const status = await get("/api/library/status");
    assert.equal(path.resolve(status.data_root).toLowerCase(), path.resolve(fixtureRoot, "workspace").toLowerCase(),
      "Refusing to modify a library outside the disposable fixture");
    fs.mkdirSync(output, { recursive: true });
    await page.route("**/api/**", async route => {
      const request = route.request(), pathname = new URL(request.url()).pathname;
      if (request.method() !== "GET") writes.push(`${request.method()} ${pathname}`);
      if (request.method() !== "GET" && !(
        /^\/api\/videos\/\d+\/manual-questions$/.test(pathname)
        || /^\/api\/markers\/\d+(\/(jump-time|importance|title|question-emphasis))?$/.test(pathname)
        || pathname === "/api/library/undo" || pathname === "/api/library/reorder"
        || /^\/api\/videos\/\d+(\/folder)?$/.test(pathname)
        || /^\/api\/folders\/\d+\/parent$/.test(pathname)
      )) throw new Error(`Unexpected mutation: ${request.method()} ${pathname}`);
      await route.continue();
    });

    await page.goto(`${origin}/review/1`);
    const video = page.locator("video").first();
    await expect(video).toBeVisible();
    await page.waitForFunction(() => document.querySelector("video")?.readyState >= 1);
    await video.evaluate(element => { element.pause(); element.currentTime = 2; });
    await page.keyboard.press("w");
    const created = page.locator(".marker-item").filter({ hasText: "手动题目" });
    await expect(created).toHaveCount(1);
    const markerId = Number(await created.getAttribute("data-marker-id"));
    const manual = page.locator(`.marker-item[data-marker-id="${markerId}"]`);
    const marker = async () => (await get("/api/videos/1/markers?status=all")).find(item => item.id === markerId);
    const originalTitle = (await marker()).title;
    await manual.locator(".marker-main").click();
    const input = manual.getByRole("textbox", { name: "题目名称" });
    await input.fill("Synthetic question");
    await input.press("Control+z");
    assert.equal((await marker()).review_status, "active");
    await input.fill("Synthetic question");
    await manual.getByRole("button", { name: "保存题目名称", exact: true }).click();
    await expect(manual.locator(".marker-main strong")).toHaveText("Synthetic question");
    await manual.locator(".question-emphasis-toggle").click();
    await expect(manual.locator(".question-emphasis-toggle")).toHaveAttribute("aria-pressed", "false");
    await manual.locator(".importance-critical.marker-importance-swatch").click();
    await expect(manual.locator(".marker-importance-label")).toHaveText("很重要");
    await video.evaluate(element => { element.currentTime = 4.2; });
    await page.keyboard.press("s");
    await expect(manual.locator(".marker-time-jump")).toHaveText("0:04");
    await page.keyboard.press("Control+z");
    await expect.poll(async () => (await marker()).user_start_ms).toBe(null);
    await page.keyboard.press("Control+z");
    await expect(manual.locator(".marker-importance-label")).toHaveText("标准");
    await page.keyboard.press("Control+z");
    await expect.poll(async () => (await marker()).question_emphasis_override).toBe(null);
    await expect(manual.locator(".question-emphasis-toggle")).toHaveAttribute("aria-pressed", "true");
    await page.keyboard.press("Control+z");
    await expect(manual.locator(".marker-main strong")).toHaveText(originalTitle);
    await page.screenshot({ path: path.join(output, "review-undo.png") });
    await page.keyboard.press("Control+z");
    await expect(manual).toHaveCount(0);
    assert.equal((await marker()).review_status, "excluded");
    assert.equal(await video.evaluate(element => element.paused), true);

    await page.goto(origin);
    const card = key => page.locator(`.library-inner [data-library-key="${key}"]`);
    const order = () => page.locator(".library-inner .video-card").evaluateAll(elements =>
      elements.map(element => Number(element.dataset.libraryKey.slice(6))));
    await expect(card("video-1")).toBeVisible();
    const originalOrder = await order();
    await drag(card(`video-${originalOrder[0]}`), card(`video-${originalOrder.at(-1)}`), true);
    await expect.poll(async () => (await get("/api/library/undo/latest"))?.kind).toBe("reorder");
    const reordered = await order();
    assert.notDeepEqual(reordered, originalOrder);
    await expect(card("video-1")).toHaveAttribute("draggable", "true");
    await drag(card("video-1"), card("folder-1"));
    await expect(card("video-1")).toHaveCount(0);
    await card("video-2").locator(".library-card-delete").click();
    await page.getByRole("dialog", { name: "移入回收站？", exact: true })
      .getByRole("button", { name: "移入回收站", exact: true }).click();
    await expect(card("video-2")).toHaveCount(0);
    await page.reload();
    await expect.poll(async () => (await get("/api/library/undo/latest"))?.kind).toBe("video");
    await undo();
    await expect(card("video-2")).toBeVisible();
    await undo();
    await expect.poll(order).toEqual(reordered);
    await undo();
    await expect.poll(order).toEqual(originalOrder);
    await expect.poll(async () => get("/api/library/undo/latest")).toBe(null);
    await page.screenshot({ path: path.join(output, "library-undo.png") });

    const source = await post("/api/folders", { name: "Synthetic source" });
    const destination = await post("/api/folders", { name: "Synthetic destination" });
    const child = await post("/api/folders", { name: "Synthetic child", parent_id: source.id });
    await page.reload();
    await expect(card(`folder-${source.id}`)).toBeVisible();
    await drag(card(`folder-${source.id}`), card(`folder-${destination.id}`));
    await expect(card(`folder-${source.id}`)).toHaveCount(0);
    await undo();
    await expect(card(`folder-${source.id}`)).toBeVisible();
    assert.equal((await get(`/api/folders/${source.id}`)).parent_id, null);
    assert.equal((await get(`/api/folders/${child.id}`)).parent_id, source.id);
    await page.setViewportSize({ width: 980, height: 900 });
    await page.screenshot({ path: path.join(output, "library-980.png") });
    assert.deepEqual(errors, []);
    console.log(`Undo browser checks passed against synthetic media. Screenshots: ${output}`);
  } catch (error) {
    console.error(writes);
    console.error(videoReads);
    fs.mkdirSync(output, { recursive: true });
    await page.screenshot({ path: path.join(output, "failure.png") }).catch(() => {});
    throw error;
  } finally {
    await browser.close();
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
