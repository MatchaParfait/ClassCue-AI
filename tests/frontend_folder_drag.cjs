// Real pointer drags against a disposable database; never writes to the user's library.
const { chromium } = require("./playwright.cjs");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const net = require("node:net");
const assert = require("node:assert/strict");

(async () => {
  const root = path.resolve(__dirname, "..");
  const output = path.join(root, ".runtime", `folder-drag-${Date.now()}`);
  const fixture = path.join(output, "application");
  fs.mkdirSync(fixture, { recursive: true });
  for (const file of fs.readdirSync(root)) {
    if (file.endsWith(".py")) fs.copyFileSync(path.join(root, file), path.join(fixture, file));
  }
  for (const file of ["pyproject.toml", "release_config.json", "uv.lock"]) fs.copyFileSync(path.join(root, file), path.join(fixture, file));
  for (const dir of ["api", "scripts"]) fs.cpSync(path.join(root, dir), path.join(fixture, dir), {
    recursive: true, filter: file => fs.statSync(file).isDirectory() ? !file.includes("__pycache__") : file.endsWith(".py"),
  });
  const listener = net.createServer();
  await new Promise(resolve => listener.listen(0, "127.0.0.1", resolve));
  const port = listener.address().port;
  await new Promise(resolve => listener.close(resolve));
  const base = `http://127.0.0.1:${port}`;
  const origin = process.env.FRONTEND_URL || "http://127.0.0.1:5175";
  const server = spawn(path.join(root, ".venv/Scripts/python.exe"), [
    path.join(root, "tests/fixtures/workbench_server.py"), output, String(port),
  ], { cwd: fixture, windowsHide: true, env: { ...process.env, PYTHONUTF8: "1" } });
  let logs = "", browser, page;
  for (const stream of [server.stdout, server.stderr]) stream.on("data", data => { logs = (logs + data).slice(-6000); });
  const request = async (endpoint, data) => {
    const response = await fetch(`${base}${endpoint}`, data === undefined ? {} : {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data),
    });
    const value = await response.json();
    assert.ok(response.ok, `${endpoint}: ${JSON.stringify(value)}`);
    return value;
  };
  const parentOf = async id => (await request(`/api/folders/${id}`)).parent_id;
  try {
    for (let attempt = 0; ; attempt++) {
      assert.ok(attempt < 200 && server.exitCode === null, logs || "Fixture startup failed");
      try { if ((await fetch(`${base}/api/health`)).ok) break; } catch {}
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    const a = (await request("/api/folders", { name: "Drag source" })).id;
    const b = (await request("/api/folders", { name: "Drag destination" })).id;
    const c = (await request("/api/folders", { name: "Child source", parent_id: a })).id;
    const d = (await request("/api/folders", { name: "Child destination", parent_id: a })).id;
    const leaf = (await request("/api/folders", { name: "Preserved subtree", parent_id: c })).id;
    await request("/api/videos/1/folder", { folder_id: leaf });
    browser = await chromium.launch({
      executablePath: "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", headless: true,
    });
    page = await browser.newPage({ viewport: { width: 1316, height: 949 } });
    const errors = [];
    page.on("pageerror", error => { errors.push(error.message); console.error("Browser:", error.message); });
    page.on("requestfailed", request => console.error("Request failed:", request.url(), request.failure()?.errorText));
    await page.route("**/api/**", async route => {
      const url = new URL(route.request().url());
      if (!url.pathname.startsWith("/api/")) return route.continue();
      await route.fulfill({ response: await route.fetch({ url: `${base}${url.pathname}${url.search}` }) });
    });
    const card = id => page.locator(`[data-library-key="folder-${id}"]`);
    const rootCard = id => page.locator(`.library-inner [data-library-key="folder-${id}"]`);
    const childCard = id => page.locator(`.folder-window [data-library-key="folder-${id}"]`);
    const settled = () => page.waitForTimeout(550);
    const dragWithPreview = async (source, target, screenshot, verify) => {
      await source.scrollIntoViewIfNeeded();
      const from = await source.boundingBox();
      await page.mouse.move(from.x + from.width / 2, from.y + from.height / 2);
      await page.mouse.down();
      await page.mouse.move(from.x + from.width / 2 + 14, from.y + from.height / 2 + 3, { steps: 5 });
      await target.waitFor();
      const to = await target.boundingBox();
      const x = to.x + (screenshot.startsWith("order-") ? to.width - 8 : to.width / 2);
      const y = to.y + to.height / 2;
      await page.mouse.move(x, y, { steps: 14 });
      await page.mouse.move(x + 1, y);
      await page.waitForTimeout(160);
      await verify();
      await page.screenshot({ path: path.join(output, `${screenshot}.png`) });
      await page.mouse.up();
    };
    const open = async id => {
      await settled();
      await card(id).locator(".folder-card-link").click();
      await settled();
    };
    const close = async () => {
      await page.keyboard.press("Escape");
      await page.locator(".folder-window").waitFor({ state: "detached" });
      await settled();
    };
    for (const width of [856, 1316, 1540, 1920]) {
      await page.setViewportSize({ width, height: 1038 });
      await request(`/api/folders/${a}/parent`, { parent_id: null });
      await request(`/api/folders/${c}/parent`, { parent_id: a });
      await page.goto(origin);
      await rootCard(a).waitFor();
      await settled();
      // Center drops nest; left/right edge drops remain sorting operations.
      await dragWithPreview(rootCard(a), rootCard(b), `move-${width}`, async () => {
        assert.equal(await rootCard(b).locator(".library-drop-feedback.is-move").textContent(), "移入「Drag destination」");
        assert.ok(await rootCard(b).evaluate(node => getComputedStyle(node).outlineStyle === "dashed"));
      });
      await rootCard(a).waitFor({ state: "detached" });
      assert.equal(await parentOf(a), b);
      await open(b);
      await childCard(a).waitFor();
      await page.screenshot({ path: path.join(output, `nested-${width}.png`) });
      await childCard(a).dragTo(page.locator(".folder-window-backdrop"), { targetPosition: { x: 8, y: 120 } });
      await childCard(a).waitFor({ state: "detached" });
      await rootCard(a).waitFor();
      assert.equal(await parentOf(a), null);
      assert.equal(await page.locator("#folder-window-title").textContent(), "Drag destination");
      await close();
      await open(a);
      await childCard(c).waitFor();
      await childCard(c).dragTo(childCard(d));
      await childCard(c).waitFor({ state: "detached" });
      assert.equal(await parentOf(c), d);
      await open(d);
      await childCard(c).waitFor();
      // A deep drag-out returns to the immediate parent, not the root.
      await dragWithPreview(childCard(c), page.locator(`.folder-drop-destination[data-drop-destination="folder-${a}"]`),
        `parent-target-${width}`, async () => {
          assert.equal(await page.locator(`.folder-drop-destination[data-drop-destination="folder-${a}"]`).getAttribute("data-drop-active"), "true");
          assert.equal(await page.locator(".folder-drop-destination").count(), 2);
        });
      await childCard(c).waitFor({ state: "detached" });
      assert.equal(await parentOf(c), a);
      await page.getByRole("button", { name: "返回上一级", exact: true }).click();
      await childCard(c).waitFor();
      await settled();
      await dragWithPreview(childCard(c), childCard(d), `order-${width}`, async () => {
        assert.equal(await childCard(d).locator(".library-drop-feedback.is-order").textContent(), "放在后面");
        const line = await childCard(d).evaluate(node => {
          const style = getComputedStyle(node.querySelector(".library-drop-line"));
          return { color: style.backgroundColor, opacity: style.opacity, right: style.right };
        });
        assert.deepEqual(line, { color: "rgb(50, 105, 197)", opacity: "1", right: "-12px" });
      });
      await settled();
      assert.equal(await parentOf(c), a);
      const children = (await request(`/api/library?folder_id=${a}`)).items.filter(item => item.type === "folder");
      assert.deepEqual(children.map(item => item.folder.id), [d, c]);
      assert.equal(await parentOf(leaf), c);
      assert.equal((await request("/api/videos")).find(video => video.id === 1).folder_id, leaf);
      await page.screenshot({ path: path.join(output, `siblings-${width}.png`) });
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${width}: page overflow`);
      await close();
      await page.reload();
      await rootCard(a).waitFor();
      await open(a);
      await childCard(c).waitFor();
      await open(c);
      await childCard(leaf).waitFor();
      await dragWithPreview(childCard(leaf), page.locator('.folder-drop-destination[data-drop-destination="root"]'),
        `root-target-${width}`, async () => {
          assert.equal(await page.locator('.folder-drop-destination[data-drop-destination="root"]').getAttribute("data-drop-active"), "true");
        });
      await rootCard(leaf).waitFor();
      assert.equal(await parentOf(leaf), null);
      assert.equal(await page.locator("#folder-window-title").textContent(), "Child source");
      await request(`/api/folders/${leaf}/parent`, { parent_id: c });
      await close();
    }
    await page.goto(`${origin}/settings?section=desktop`);
    const preference = page.getByRole("switch", { name: "拖拽文字提示", exact: true });
    await preference.waitFor();
    assert.equal(await preference.isChecked(), true);
    for (const width of [856, 1316, 1540, 1920]) {
      await page.setViewportSize({ width, height: 1038 });
      await preference.scrollIntoViewIfNeeded();
      await page.screenshot({ path: path.join(output, `hint-settings-${width}.png`) });
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    }
    await preference.uncheck();
    await page.reload();
    await preference.waitFor();
    assert.equal(await preference.isChecked(), false);
    await page.goto(origin);
    await rootCard(a).waitFor();
    await settled();
    await dragWithPreview(rootCard(a), rootCard(b), "hints-disabled", async () => {
      assert.equal(await page.locator(".library-drop-feedback").count(), 0);
      assert.ok(await rootCard(b).evaluate(node => node.classList.contains("drop-folder")));
    });
    await rootCard(a).waitFor({ state: "detached" });
    assert.equal(await parentOf(a), b);
    await open(b);
    await dragWithPreview(childCard(a), page.locator('.folder-drop-destination[data-drop-destination="root"]'),
      "destinations-without-hints", async () => {
        assert.equal(await page.locator(".folder-outside-drop-label").count(), 0);
        assert.ok((await page.locator(".folder-drop-destination").textContent()).includes("视频库"));
      });
    await rootCard(a).waitFor();
    assert.equal(await parentOf(a), null);
    await close();
    await page.goto(`${origin}/settings?section=desktop`);
    await preference.waitFor();
    assert.equal(await preference.isChecked(), false);
    await preference.check();
    await page.goto(origin);
    await rootCard(a).waitFor();
    await settled();
    await dragWithPreview(rootCard(a), rootCard(b), "hints-enabled-again", async () => {
      assert.equal(await rootCard(b).locator(".library-drop-feedback.is-move").textContent(), "移入「Drag destination」");
    });
    await rootCard(a).waitFor({ state: "detached" });
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, "checks.json"), JSON.stringify({
      passed: true, widths: [856, 1316, 1540, 1920],
      checks: ["Root folder nesting", "Sibling nesting", "Root and deep drag-out", "Edge sorting",
        "Distinct nest and insertion previews", "In-dialog parent/root targets", "Subtree and video preservation",
        "Reload persistence", "Drag hint setting on/off and persistence", "Four PC settings layouts",
        "Moves and exit targets without text hints", "No page overflow"], errors,
    }, null, 2));
    console.log(JSON.stringify({ passed: true, output }, null, 2));
  } catch (error) {
    if (page) await page.screenshot({ path: path.join(output, "failure.png") }).catch(() => {});
    console.error(logs);
    throw error;
  } finally {
    await browser?.close();
    if (server.exitCode === null) {
      const exited = new Promise(resolve => server.once("exit", resolve));
      server.kill();
      await exited;
    }
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
