// SEARCH_QA=1 node tests/frontend_workbench.cjs — disposable real search API and media.
const assert = require("node:assert/strict");
const fs = require("node:fs");

module.exports = async ({ page, origin, output, requests, base }) => {
  // Search has no default binding; opt in only for this nested-dialog keyboard QA.
  await page.evaluate(() => {
    localStorage.setItem("classroom.shortcuts.v2", JSON.stringify({ search: ["Ctrl+K"] }));
    window.dispatchEvent(new Event("classroom-shortcuts-change"));
  });
  const input = () => page.getByRole("combobox", { name: "搜索所有视频的字幕" });
  const dialog = () => page.getByRole("dialog", { name: "全局字幕搜索" });
  const rows = () => dialog().locator('#global-search-results [role="option"]');
  const close = async () => { await page.keyboard.press("Escape"); await page.locator(".global-search-dialog").waitFor({ state: "detached" }); };
  const search = async () => { await page.keyboard.press("Control+k"); await input().fill("分治"); await rows().first().waitFor(); };
  const capture = async (name, dialogOnly = false) => {
    await page.evaluate(() => document.fonts.ready);
    await page.waitForTimeout(230);
    if (dialogOnly) {
      const bounds = await page.locator(".global-search-dialog").boundingBox();
      assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= page.viewportSize().width, `dialog overflow: ${name}`);
      assert.equal(await page.locator(".global-search-dialog").evaluate(node => node.scrollWidth > node.clientWidth + 1), false);
    } else {
      await page.waitForFunction(() => document.documentElement.scrollWidth <= innerWidth + 1, null, { timeout: 5000 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false, `overflow: ${name}`);
    }
    await page.screenshot({ path: `${output}/${name}.png`, fullPage: false });
  };
  const workspace = await (await fetch(`${base}/api/library/status`)).json();
  await page.locator(".library-inner .video-card").first().waitFor();
  await page.setViewportSize({ width: 1440, height: 960 });
  await capture("search-entry-1440");
  await search();
  assert.equal(await rows().count(), 20);
  assert.equal(await page.locator(".global-search-group").count(), 1);
  assert.ok((await page.locator(".global-search-summary").innerText()).includes("75 处字幕 · 3 个视频"));
  await page.getByRole("button", { name: "加载更多字幕" }).click();
  await page.waitForFunction(() => document.querySelectorAll('[role="option"]').length === 40);
  assert.equal(await page.locator(".global-search-group").count(), 2);
  for (const width of [856, 1316, 1540, 1920]) {
    await page.setViewportSize({ width, height: 960 });
    await capture(`search-results-${width}`);
  }
  await page.setViewportSize({ width: 1440, height: 960 });
  await input().focus();
  await page.keyboard.press("Shift+Tab");
  assert.equal(await page.evaluate(() => document.activeElement.getAttribute("aria-label")), "搜索文件夹范围");
  await page.keyboard.press("Shift+Tab");
  assert.equal(await page.evaluate(() => document.activeElement.getAttribute("aria-label")), "关闭全局搜索");
  await page.keyboard.press("Tab");
  await page.keyboard.press("Tab");
  assert.equal(await input().evaluate((node) => document.activeElement === node), true);
  // Put an existing synthetic video two levels below the course for recursive search.
  const mutate = async (pathname, body) => {
    const response = await fetch(`${base}${pathname}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    assert.ok(response.ok, `${pathname}: ${response.status}`);
    return response.json();
  };
  const child = await mutate("/api/folders", { name: "复习单元", parent_id: 1 });
  const nested = await mutate("/api/folders", { name: "例题讲解", parent_id: child.id });
  await mutate("/api/videos/1/folder", { folder_id: nested.id });
  const prefix = page.getByRole("combobox", { name: "搜索文件夹范围" });
  assert.deepEqual(await prefix.locator("option").allTextContents(), ["全部视频", "算法课程"]);
  await prefix.selectOption("1");
  await page.getByRole("dialog", { name: "文件夹字幕搜索" }).locator('#global-search-results [role="option"]').first().waitFor();
  await page.waitForFunction(() => document.querySelector(".global-search-summary").textContent.includes("50 处字幕 · 2 个视频"));
  assert.equal(await page.getByRole("combobox", { name: "搜索当前文件夹及子文件夹的字幕" }).inputValue(), "分治");
  await page.getByRole("button", { name: "加载更多字幕" }).click();
  await page.waitForFunction(() => document.querySelectorAll('[role="option"]').length === 40);
  assert.deepEqual(await page.locator(".global-search-group").evaluateAll(nodes =>
    nodes.map(node => node.getAttribute("aria-labelledby"))), ["search-video-13", "search-video-1"]);
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 960 });
    // The existing video-library grid is desktop-only; check the new dialog at phone width.
    await capture(`search-folder-prefix-${width}`, width < 856);
    const field = await page.locator(".global-search-field input").boundingBox();
    const selector = await prefix.boundingBox();
    assert.ok(field.width >= 100);
    if (width >= 856) assert.ok(selector.x + selector.width <= field.x + 1);
    else assert.ok(selector.y + selector.height <= field.y + 1);
  }
  await page.setViewportSize({ width: 1440, height: 960 });
  await mutate("/api/videos/1/folder", { folder_id: null });
  await prefix.selectOption("");
  await rows().first().waitFor();
  await page.getByRole("button", { name: "加载更多字幕" }).click();
  await page.waitForFunction(() => document.querySelectorAll('[role="option"]').length === 40);
  await page.evaluate((id) => localStorage.setItem(`classroom-study-progress:${id}:13`, "5"), workspace.library_id);
  await input().focus();
  await page.keyboard.press("ArrowDown");
  const targetId = await page.locator('[role="option"][aria-selected="true"]').getAttribute("id");
  assert.ok(targetId.startsWith("search-hit-13:"));
  await page.keyboard.press("Enter");
  await page.waitForURL(/\/review\/13\?start_ms=300/);
  await page.waitForFunction(() => { const video = document.querySelector("video"); return video && video.duration > 0 && video.currentTime > 0 && video.currentTime < 3; });
  await page.locator("video").evaluate((node) => { node.pause(); window.__searchVideo = node; });
  assert.equal(await page.locator(".marker-item.is-selected").count(), 0);
  await page.keyboard.press("Control+k");
  assert.equal(await input().inputValue(), "分治");
  await rows().first().waitFor();
  assert.equal(await rows().count(), 20);
  assert.equal(await page.locator(".global-search-section-label").first().textContent(), "本视频25 处匹配");
  await page.getByRole("button", { name: "加载更多字幕" }).click();
  await page.waitForFunction(() => document.querySelectorAll('[role="option"]').length === 40);
  assert.equal(await rows().count(), 40);
  assert.equal(await page.getByRole("separator", { name: "其他视频" }).count(), 1);
  await page.locator("video").evaluate((node) => { node.currentTime = 6; });
  await input().press("Enter");
  await page.locator(".global-search-dialog").waitFor({ state: "detached" });
  await page.waitForFunction(() => document.querySelector("video").currentTime < 3);
  assert.equal(await page.locator("video").evaluate((node) => node === window.__searchVideo), true);
  await page.locator("video").evaluate((node) => node.pause());
  await page.keyboard.press("Control+k");
  const playback = await page.locator("video").evaluate((node) => node.currentTime);
  await close();
  assert.equal(await page.locator("video").evaluate((node) => node.currentTime), playback);
  assert.equal(requests.filter((item) => /^PATCH .*markers/.test(item)).length, 0);
  await capture("search-review-1440");
  await page.setViewportSize({ width: 856, height: 960 });
  await capture("search-review-856");

  // Video 1 would ordinarily be on the final page; priority must precede SQL pagination.
  await page.goto(`${origin}/review/1`);
  await page.locator("video").waitFor();
  assert.ok((await page.getByRole("button", { name: "全局搜索字幕", exact: true }).innerText()).includes("本视频优先"));
  await search();
  assert.ok((await rows().first().getAttribute("id")).startsWith("search-hit-1:"));
  await page.getByRole("button", { name: "加载更多字幕" }).click();
  await page.waitForFunction(() => document.querySelectorAll('[role="option"]').length === 40);
  assert.deepEqual(await page.locator(".global-search-group").evaluateAll(nodes =>
    nodes.map(node => node.getAttribute("aria-labelledby"))), ["search-video-1", "search-video-13"]);
  for (const width of [856, 1316, 1540, 1920]) {
    await page.setViewportSize({ width, height: 960 });
    await page.locator(".global-search-scroll").evaluate(node => { node.scrollTop = 0; });
    await capture(`search-current-video-first-${width}`);
    await page.getByRole("separator", { name: "其他视频" }).evaluate(node => node.scrollIntoView({ block: "center" }));
    await capture(`search-other-videos-divider-${width}`);
    await page.locator(".global-search-scroll").evaluate(node => { node.scrollTop = 0; });
  }
  await close();

  // Nested search must not close the folder or fight its focus trap.
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.getByRole("link", { name: "返回视频库" }).click();
  await page.locator(".folder-card-link").first().click();
  await page.locator(".folder-window").waitFor();
  await page.getByRole("button", { name: "搜索文件夹字幕", exact: true }).click();
  const scopedInput = page.getByRole("combobox", { name: "搜索当前文件夹及子文件夹的字幕" });
  await scopedInput.fill("分治");
  await page.waitForFunction(() => document.querySelector(".global-search-summary").textContent.includes("25 处字幕 · 1 个视频"));
  assert.equal(await page.locator(".global-search-folder-prefix").count(), 0);
  await capture("search-folder-entry-1440");
  await scopedInput.fill("合成资料不存在的短语");
  await page.waitForFunction(() => document.querySelector(".global-search-summary").textContent.includes("0 处字幕 · 0 个视频"));
  assert.equal(await page.getByRole("button", { name: "当前文件夹（含子文件夹）" }).getAttribute("aria-pressed"), "true");
  await close();
  assert.equal(await page.evaluate(() => document.activeElement.getAttribute("aria-label")), "搜索文件夹字幕");
  await page.keyboard.press("Control+k");
  assert.equal(await input().evaluate((node) => document.activeElement === node), true);
  await close();
  assert.equal(await page.locator(".folder-window").isVisible(), true);
  assert.equal(await page.evaluate(() => document.body.style.overflow), "hidden");
  assert.equal(await page.evaluate(() => document.querySelector(".search-page-background").inert), false);
  assert.equal(await page.locator(".folder-window").evaluate((node) => Boolean(node.closest("[inert]"))), false);
  assert.equal(await page.locator(".folder-window").evaluate((node) => node.contains(document.activeElement)), true);
  await page.keyboard.press("Escape");
  await page.locator(".folder-window").waitFor({ state: "detached" });

  // Existing settings drafts and scroll survive opening/closing search.
  await page.getByRole("link", { name: "设置", exact: true }).click();
  await page.getByRole("button", { name: /AI/ }).first().click();
  const model = page.getByLabel("模型名称", { exact: true });
  await model.fill("unsaved-search-draft");
  await search(); await close();
  assert.equal(await model.inputValue(), "unsaved-search-draft");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await search(); await close(); await search(); await close();
  assert.equal(await page.evaluate(() => Boolean(document.querySelector(".search-page-background").inert)), false);

  // Reopening during an exit must cancel that exit and keep one active dialog.
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await search();
  await page.keyboard.press("Escape"); await page.keyboard.press("Control+k");
  await page.waitForTimeout(250);
  assert.equal(await dialog().count(), 1);
  await close();
  await page.getByRole("button", { name: "放弃修改", exact: true }).click();
  await page.getByRole("button", { name: "查看任务详情", exact: true }).click();
  await page.getByRole("dialog", { name: "任务详情" }).waitFor();
  await search(); await close();
  await page.getByRole("button", { name: "关闭面板", exact: true }).click();
  await page.locator(".workbench-panel-backdrop").waitFor({ state: "hidden" });

  // A different server identity clears search without destroying the folder page.
  await page.goto(origin);
  await page.locator(".folder-card-link").first().click();
  await page.locator(".folder-window").waitFor();
  await search();
  await page.route("**/api/library/status", (route) => route.fulfill({ contentType: "application/json",
    body: JSON.stringify({ ...workspace, library_id: "different-server" }) }));
  await page.locator(".global-search-dialog").waitFor({ state: "detached", timeout: 20000 });
  assert.equal(await page.evaluate(() => document.body.style.overflow), "hidden");
  assert.equal(await page.locator(".folder-window").count(), 1);
  await page.keyboard.press("Control+k");
  assert.equal(await input().inputValue(), "");
  assert.equal(await rows().count(), 0);
  await close();
  fs.writeFileSync(`${output}/search-qa.json`, JSON.stringify({ passed: true, widths: [390, 856, 1316, 1440, 1540, 1920],
    checks: ["real backend search", "pagination across video groups", "keyboard and focus", "search over saved progress",
      "repeat same-video seek without remount", "query/results retained", "no marker writes", "nested folder focus", "settings draft", "reduced motion",
      "reopen during exit", "search over task drawer", "review video priority before pagination", "current/other video divider",
      "root prefix lists only first-level folders", "folder search includes grandchildren before pagination",
      "folder prefix desktop and narrow layout", "folder toolbar entry and empty scope",
      "server identity change clears search and preserves folder"] }, null, 2));
};
