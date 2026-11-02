// All API requests are intercepted; only synthetic library records are changed.
const { chromium } = require("./playwright.cjs");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");

const output = path.resolve(__dirname, "../.runtime", `library-drag-scroll-${Date.now()}`);

async function main() {
  fs.mkdirSync(output, { recursive: true });
  const dist = path.resolve(__dirname, "../frontend/dist");
  const server = http.createServer((request, response) => {
    const pathname = decodeURIComponent(new URL(request.url, "http://127.0.0.1").pathname);
    const candidate = path.resolve(dist, `.${pathname}`);
    if (!candidate.startsWith(`${dist}${path.sep}`) && candidate !== dist) {
      response.writeHead(404).end(); return;
    }
    const file = fs.existsSync(candidate) && fs.statSync(candidate).isFile() ? candidate : path.join(dist, "index.html");
    const types = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".png": "image/png",
      ".webp": "image/webp", ".woff": "font/woff", ".woff2": "font/woff2" };
    response.writeHead(200, { "Content-Type": types[path.extname(file)] || "application/octet-stream" });
    fs.createReadStream(file).pipe(response);
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const origin = process.env.FRONTEND_URL || `http://127.0.0.1:${server.address().port}`;
  let browser, page;
  try {
    browser = await chromium.launch({
      executablePath: process.env.EDGE_EXECUTABLE || "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
      headless: true,
    });
    page = await browser.newPage({ viewport: { width: 1440, height: 960 } });
    page.setDefaultTimeout(12000);
    const errors = [], moves = [], folderMoves = [], orders = [];
    page.on("pageerror", error => errors.push(error.message));
    const folders = [{ id: 7, name: "Course", parent_id: null, video_count: 4 },
      ...Array.from({ length: 42 }, (_, index) => ({
        id: 100 + index, name: `Week ${String(index + 1).padStart(2, "0")}`, parent_id: 7, video_count: 0,
      }))];
    const videos = Array.from({ length: 4 }, (_, index) => ({
      id: index + 1, display_name: `00 Lesson ${index + 1}`, filename: `lesson-${index + 1}.mp4`,
      video_path: `fixture-${index + 1}.mp4`, duration_ms: 60000, folder_id: 7, status: "review_ready",
    }));
    const png = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6BwAAAABJRU5ErkJggg==", "base64");
    function library(id) {
      return { folder: folders.find(folder => folder.id === id) ?? null, parent_id: id, sort: "manual", order: "asc",
        ancestors: id ? folders.filter(folder => folder.id === 7 || folder.id === id) : [],
        items: [...videos.filter(video => video.folder_id === id).map(video => ({ type: "video", video })),
          ...folders.filter(folder => folder.parent_id === id).map(folder => ({ type: "folder", folder }))] };
    }
    await page.route("**/api/**", async route => {
      const request = route.request(), url = new URL(request.url()), endpoint = url.pathname;
      const json = value => route.fulfill({ contentType: "application/json", body: JSON.stringify(value) });
      if (request.method() !== "GET") {
        const body = request.postDataJSON();
        if (/^\/api\/videos\/\d+\/folder$/.test(endpoint)) {
          const video = videos.find(item => item.id === Number(endpoint.split("/")[3]));
          moves.push({ id: video.id, folder_id: body.folder_id });
          video.folder_id = body.folder_id;
          return json(video);
        }
        if (endpoint === "/api/library/reorder") {
          orders.push(body);
          return json(library(body.parent_id ?? null));
        }
        if (/^\/api\/folders\/\d+\/parent$/.test(endpoint)) {
          const folder = folders.find(item => item.id === Number(endpoint.split("/")[3]));
          folderMoves.push({ id: folder.id, parent_id: body.parent_id });
          folder.parent_id = body.parent_id;
          return json(folder);
        }
        throw new Error(`Unexpected mutation: ${request.method()} ${endpoint}`);
      }
      if (endpoint === "/api/library/status") return json({
        library_id: "drag-scroll-fixture", data_root: "drag-scroll-fixture", video_count: videos.length, active_job_id: null,
      });
      if (endpoint === "/api/videos") return json(videos);
      if (endpoint === "/api/library") return json(library(url.searchParams.has("folder_id") ? Number(url.searchParams.get("folder_id")) : null));
      if (endpoint.endsWith("/thumbnail")) return route.fulfill({ contentType: "image/png", body: png });
      if (endpoint === "/api/asr/selection") return json({ id: "whisper-turbo", name: "Fixture", downloaded: true, engine: "whisper" });
      if (endpoint === "/api/settings/ai") return json({ model: "fixture", has_api_key: false });
      if (endpoint === "/api/jobs/paused" || endpoint === "/api/jobs/recent") return json([]);
      if (endpoint === "/api/batch/latest" || endpoint === "/api/library/undo/latest") return json(null);
      return json({});
    });
    await page.goto(origin);
    await page.getByRole("button", { name: "打开文件夹“Course”", exact: true }).click();
    await page.locator(".folder-window [data-library-key='video-1']").waitFor();
    await page.waitForTimeout(600);
    const panel = page.locator(".folder-window-content");
    const geometry = () => page.locator(".folder-window-list").evaluate(node => ({
      columns: getComputedStyle(node).gridTemplateColumns.split(" ").length,
      cards: [...node.querySelectorAll(":scope > .library-card")].map(card => {
        const rect = card.getBoundingClientRect();
        return { key: card.dataset.libraryKey, folder: card.classList.contains("folder-card"), top: rect.top, bottom: rect.bottom, height: rect.height };
      }),
      dialog: document.querySelector(".folder-window").getBoundingClientRect().toJSON(),
      overflow: document.querySelector(".folder-window").scrollWidth > document.querySelector(".folder-window").clientWidth,
    }));
    for (const [width, height, columns] of [[1920, 1080, 3], [1440, 960, 3], [1024, 768, 2], [600, 800, 1]]) {
      await page.setViewportSize({ width, height });
      const layout = await geometry();
      assert.equal(layout.columns, columns, `${width}: column count`);
      assert.equal(layout.overflow, false, `${width}: no horizontal overflow`);
      assert.ok(layout.dialog.width <= width && layout.dialog.height <= height, "dialog fits window");
      const lastVideo = layout.cards.filter(card => !card.folder).at(-1);
      const firstFolder = layout.cards.find(card => card.folder);
      assert.ok(firstFolder.top >= lastVideo.bottom, `${width}: folder starts on a separate row`);
      assert.ok(layout.cards.filter(card => card.folder).every(card => card.height < 160), "folders stay compact");
      await page.screenshot({ path: path.join(output, `layout-${width}.png`) });
    }
    await page.setViewportSize({ width: 1440, height: 960 });
    await panel.evaluate(node => { node.scrollTop = 0; });
    const video = page.locator(".folder-window [data-library-key='video-1']");
    const start = async () => {
      const box = await video.boundingBox();
      await page.mouse.move(box.x + box.width / 2, box.y + 50);
      await page.mouse.down();
      await page.mouse.move(box.x + box.width / 2 + 12, box.y + 50, { steps: 3 });
      await page.waitForFunction(() => document.body.dataset.libraryPointerDrag === "true");
    };
    const top = () => panel.evaluate(node => node.scrollTop);
    const rootScroll = await page.evaluate(() => scrollY);
    await start();
    const viewport = await panel.boundingBox();
    await page.mouse.move(viewport.x + 100, viewport.y + viewport.height / 2, { steps: 4 });
    await page.mouse.wheel(0, 560);
    await page.waitForFunction(() => document.querySelector(".folder-window-content").scrollTop >= 500);
    const down = await top();
    await page.mouse.wheel(0, -240);
    await page.waitForTimeout(100);
    assert.ok(await top() < down, "wheel scrolls up while still holding the video");
    assert.equal(await page.evaluate(() => scrollY), rootScroll, "background does not scroll");
    const beforeEdge = await top();
    await page.mouse.move(viewport.x + 100, viewport.y + viewport.height - 90);
    await page.waitForTimeout(650);
    assert.ok(await top() > beforeEdge + 8, "automatic scroll starts 90px from the lower edge");
    await page.mouse.move(viewport.x + 100, viewport.y + 90);
    const beforeUp = await top();
    await page.waitForTimeout(650);
    assert.ok(await top() < beforeUp - 8, "automatic scroll starts 90px from the upper edge");
    await page.keyboard.press("Escape");
    await page.mouse.up();
    assert.equal(await page.locator(".folder-window").count(), 1, "Escape cancels drag without closing folder");
    assert.equal(moves.length, 0);
    assert.equal(orders.length, 0);
    assert.equal(await page.locator(".library-drag-preview").count(), 0);

    await panel.evaluate(node => { node.scrollTop = 0; });
    await start();
    await page.mouse.move(viewport.x + 100, viewport.y + viewport.height / 2);
    await page.mouse.wheel(0, 650);
    await page.waitForTimeout(100);
    const destination = await page.locator(".folder-window .folder-card").evaluateAll(nodes => {
      const panel = document.querySelector(".folder-window-content").getBoundingClientRect();
      const node = nodes.find(node => {
        const rect = node.getBoundingClientRect();
        return rect.top > panel.top + 115 && rect.bottom < panel.bottom - 115;
      });
      if (!node) return null;
      const rect = node.getBoundingClientRect();
      return { id: Number(node.dataset.libraryKey.slice(7)), x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
    });
    assert.ok(destination, "destination revealed by wheel");
    await page.mouse.move(destination.x, destination.y, { steps: 8 });
    await page.mouse.move(destination.x + 1, destination.y);
    await page.waitForTimeout(80);
    await page.screenshot({ path: path.join(output, "wheel-drop-preview.png") });
    await page.mouse.up();
    await page.waitForFunction(() => !document.querySelector(".folder-window [data-library-key='video-1']"));
    assert.deepEqual(moves, [{ id: 1, folder_id: destination.id }], "exactly one move to the revealed folder");
    assert.equal(await page.locator(".library-drag-preview").count(), 0);

    const pointerDrag = async (source, destination, edge = false) => {
      const from = await source.boundingBox(), to = await destination.boundingBox();
      await page.mouse.move(from.x + from.width / 2, from.y + from.height / 2);
      await page.mouse.down();
      await page.mouse.move(from.x + from.width / 2 + 12, from.y + from.height / 2, { steps: 3 });
      await page.mouse.move(to.x + (edge ? to.width - 8 : to.width / 2), to.y + to.height / 2, { steps: 8 });
      await page.waitForTimeout(80);
      await page.mouse.up();
      await page.waitForTimeout(300);
    };
    await panel.evaluate(node => { node.scrollTop = 0; });
    await pointerDrag(page.locator(".folder-window [data-library-key='video-2']"),
      page.locator(".folder-window [data-library-key='video-3']"), true);
    assert.equal(orders.length, 1, "edge sorting still issues one reorder");
    assert.deepEqual(orders[0].ordered_items.filter(item => item.type === "video").map(item => item.id), [3, 2, 4]);
    assert.equal(moves.length, 1, "sorting is not a folder move");
    await page.getByRole("combobox", { name: "文件夹内排序" }).selectOption("name");
    await page.waitForTimeout(500);
    const sortedLayout = await geometry();
    const lastVideo = sortedLayout.cards.filter(card => !card.folder).at(-1);
    assert.ok(sortedLayout.cards.find(card => card.folder).top >= lastVideo.bottom,
      "type boundaries also hold under name sorting");
    await panel.evaluate(node => { node.scrollTop = 300; });
    await page.screenshot({ path: path.join(output, "separate-folder-rows.png") });
    await page.getByRole("combobox", { name: "文件夹内排序" }).selectOption("manual");
    const firstFolder = page.locator(".folder-window [data-library-key='folder-100']");
    await firstFolder.scrollIntoViewIfNeeded();
    await pointerDrag(firstFolder, page.locator(".folder-window [data-library-key='folder-101']"));
    assert.deepEqual(folderMoves, [{ id: 100, parent_id: 101 }], "folder center drops still nest");
    await page.getByRole("button", { name: "打开文件夹“Week 02”", exact: true }).click();
    await page.locator("#folder-window-title").filter({ hasText: "Week 02" }).waitFor();
    const nested = page.locator(".folder-window [data-library-key='folder-100']");
    await nested.waitFor();
    const box = await nested.boundingBox();
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width / 2 + 12, box.y + box.height / 2, { steps: 3 });
    await page.waitForFunction(() => document.querySelector(".folder-drop-destination[data-drop-destination='root']"));
    const exit = await page.locator(".folder-drop-destination[data-drop-destination='root']").boundingBox();
    await page.mouse.move(exit.x + exit.width / 2, exit.y + exit.height / 2, { steps: 8 });
    await page.mouse.up();
    await nested.waitFor({ state: "detached" });
    assert.deepEqual(folderMoves, [{ id: 100, parent_id: 101 }, { id: 100, parent_id: null }],
      "root destination still moves a nested folder out");
    assert.equal(await page.locator(".folder-window").count(), 1, "moving out keeps the folder open");
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({
      passed: true, widths: [600, 1024, 1440, 1920], wheelDown: true, wheelUp: true,
      edgeBand90px: true, cancelWithoutClose: true, exactMove: moves, folderMoves,
      videoSorting: true, separateRows: true, backgroundUnchanged: true, errors,
    }, null, 2));
    console.log(JSON.stringify({ passed: true, output }, null, 2));
  } catch (error) {
    if (page) await page.screenshot({ path: path.join(output, "failure.png") }).catch(() => {});
    throw error;
  } finally {
    try {
      await browser?.close();
    } finally {
      await new Promise(resolve => server.close(resolve));
    }
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
