const assert = require("node:assert/strict");

// Invoked with the disposable workbench server; synthetic videos alias only its
// generated clip. No real library, ASR, or AI requests are involved.
module.exports = async function checkReviewTabs({ page, output, base, origin }) {
  const template = await (await fetch(`${base}/api/videos/1`)).json();
  const pattern = /\/api\/videos\/(10[1-8]|999)(?:\/|\?|$)/;
  const handler = async route => {
    const url = new URL(route.request().url());
    const id = Number(url.pathname.split("/")[3]);
    assert.equal(route.request().method(), "GET", "tabs never mutate or delete library records");
    if (id === 999) return route.fulfill({ status: 404, contentType: "application/json", body: '{"detail":"测试视频已移除"}' });
    if (/\/api\/videos\/\d+$/.test(url.pathname)) {
      return route.fulfill({ contentType: "application/json", body: JSON.stringify({
        ...template, id, display_name: `课程 ${id} · 分治算法与递归复杂度分析_很长的课堂录制名称_2026`,
      }) });
    }
    const response = await route.fetch({ url: `${base}${url.pathname.replace(`/videos/${id}`, "/videos/1")}${url.search}` });
    await route.fulfill({ response });
  };
  await page.route(pattern, handler);
  const navigation = () => page.getByRole("navigation", { name: "已打开的视频", exact: true });
  const tab = id => navigation().locator(`.review-tab-link[href="/review/${id}"]`);
  const close = id => tab(id).locator("..").locator(".review-tab-close");
  const ready = async () => {
    await page.locator(".video-player").waitFor();
    await page.waitForFunction(() => document.querySelector(".video-player")?.readyState >= 2);
    assert.equal(await page.locator("video").count(), 1, "only the active tab owns a player");
  };
  const checkBorders = async () => {
    const borders = await navigation().locator(".review-tab, .review-tabs-library").evaluateAll(elements => elements.map(element => {
      const style = getComputedStyle(element);
      return { width: style.borderTopWidth, style: style.borderTopStyle, color: style.borderTopColor };
    }));
    for (const border of borders) {
      assert.equal(border.width, "1px", "all tabs keep a stable border, including inactive videos");
      assert.equal(border.style, "solid");
      assert.notEqual(border.color, "rgba(0, 0, 0, 0)", "tab borders must remain visible on the library page");
    }
  };
  const openFromLibrary = async id => {
    await navigation().locator(".review-tabs-library").click();
    await page.locator(".library-inner").waitFor();
    await checkBorders();
    if (id === 101) await page.screenshot({ path: `${output}/review-tabs-library.png`, animations: "disabled" });
    // Seed synthetic already-open videos through the same POP handling used by
    // browser history, without adding fake cards to the actual library.
    await page.evaluate(id => {
      history.pushState({}, "", `/review/${id}`);
      dispatchEvent(new PopStateEvent("popstate"));
    }, id);
    if (id !== 999) await ready();
  };
  try {
    await ready();
    await page.locator("video").evaluate(async element => { element.currentTime = 2.5; await element.play(); });
    await page.evaluate(() => { window.__qaOldPlayer = document.querySelector("video"); });
    await openFromLibrary(101);
    const saved = await page.evaluate(libraryId => ({
      time: Number(localStorage.getItem(`classroom-study-progress:${libraryId}:1`)),
      paused: window.__qaOldPlayer.paused,
    }), template.library_id);
    assert.ok(saved.time >= 2.5 && saved.time < 5, JSON.stringify(saved));
    assert.ok(saved.paused, "leaving the tab explicitly pauses its old player");
    await tab(1).click(); await ready();
    assert.ok(Math.abs(await page.locator("video").evaluate(element => element.currentTime) - saved.time) < .1);
    assert.ok(await page.locator("video").evaluate(element => element.paused), "switching tabs does not autoplay");
    await tab(1).click();
    assert.equal(await navigation().locator(".review-tab-link").count(), 2, "opening the same video is deduplicated");

    for (let id = 102; id <= 108; id++) await openFromLibrary(id);
    for (const width of [856, 1316, 1540, 1920]) {
      await page.setViewportSize({ width, height: 949 });
      await tab(101).click(); await ready();
      await tab(108).click(); await ready();
      await checkBorders();
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      const geometry = await navigation().evaluate(nav => {
        const selected = nav.querySelector('[aria-current="page"]');
        const list = nav.querySelector(".review-tabs-list");
        const active = selected.getBoundingClientRect(), viewport = list.getBoundingClientRect();
        const title = selected.querySelector("span");
        const close = selected.nextElementSibling.getBoundingClientRect();
        const header = nav.parentElement.getBoundingClientRect();
        const player = document.querySelector(".video-section").getBoundingClientRect();
        const sidebar = document.querySelector(".review-sidebar").getBoundingClientRect();
        return { active: { left: active.left, right: active.right }, viewport: { left: viewport.left, right: viewport.right },
          closeRight: close.right, overflowing: list.scrollWidth > list.clientWidth, ellipsis: getComputedStyle(title).textOverflow,
          titleFits: title.clientWidth < title.scrollWidth, headerBottom: header.bottom, playerTop: player.top, sidebarTop: sidebar.top };
      });
      assert.ok(geometry.overflowing, `${width}: many tabs stay in a horizontally scrolling row`);
      assert.ok(geometry.active.left >= geometry.viewport.left - 1 && geometry.closeRight <= geometry.viewport.right + 1,
        `${width}: active tab and close button are visible`);
      assert.equal(geometry.ellipsis, "ellipsis");
      assert.ok(geometry.titleFits, "long titles are truncated and retain a full hover title");
      assert.ok(geometry.playerTop >= geometry.headerBottom && geometry.sidebarTop >= geometry.headerBottom);
      await page.screenshot({ path: `${output}/review-tabs-${width}.png`, animations: "disabled" });
    }
    await navigation().getByRole("button", { name: "向左滚动视频标签" }).click();
    assert.ok(await navigation().locator(".review-tabs-list").evaluate(element => element.scrollLeft >= 0));
    await tab(1).focus();
    await page.keyboard.press("ArrowRight");
    assert.equal(await page.evaluate(() => document.activeElement.getAttribute("href")), "/review/101");
    assert.equal(new URL(page.url()).pathname, "/review/108", "arrow keys focus without switching videos");
    await page.keyboard.press("Enter"); await ready();
    assert.equal(new URL(page.url()).pathname, "/review/101");
    await close(103).click();
    assert.equal(new URL(page.url()).pathname, "/review/101", "closing an inactive tab does not navigate");
    await close(101).click(); await ready();
    assert.equal(new URL(page.url()).pathname, "/review/102", "active close chooses the next tab");
    await page.goBack(); await ready();
    assert.equal(new URL(page.url()).pathname, "/review/108", "closing replaces its history entry");
    await page.goForward(); await ready();
    assert.equal(new URL(page.url()).pathname, "/review/102");

    await openFromLibrary(999);
    await page.getByRole("alert").filter({ hasText: "复习页面无法加载" }).waitFor();
    assert.equal(await tab(999).count(), 0);
    await page.screenshot({ path: `${output}/review-tabs-missing.png`, animations: "disabled" });
    await tab(1).click(); await ready();
    for (const id of [102, 104, 105, 106, 107, 108]) await close(id).click();
    assert.equal(await navigation().locator(".review-tab-link").count(), 1);
    await close(1).click();
    await page.waitForURL(`${origin}/`);
    assert.equal(await navigation().count(), 0, "closing the last tab returns to the unchanged library");
    await page.locator('.library-inner [data-library-key="video-1"] .library-thumbnail-link').click();
    await ready();
    await page.emulateMedia({ reducedMotion: "reduce" });
    assert.equal(await navigation().locator(".review-tab").evaluate(element => getComputedStyle(element).transitionDuration), "0s");
    await page.emulateMedia({ reducedMotion: "no-preference" });
  } finally {
    await page.unroute(pattern, handler);
  }
};
