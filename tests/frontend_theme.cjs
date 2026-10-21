// Runs against the disposable workbench fixture, never the user's workspace.
// FRONTEND_URL=http://127.0.0.1:5186 THEME_QA=1 node tests/frontend_workbench.cjs
const assert = require("node:assert/strict");
const fs = require("node:fs");

module.exports = async function themeQA({ page, origin, output }) {
  const results = [];
  await page.emulateMedia({ reducedMotion: "no-preference" });
  async function capture(name, width) {
    await page.evaluate(() => document.fonts.ready);
    await page.waitForTimeout(350);
    const overflow = await page.evaluate(() => ({ viewport: innerWidth, width: document.documentElement.scrollWidth,
      offenders: [...document.querySelectorAll("body *")].filter((node) => {
        if (node.closest('[hidden], [aria-hidden="true"]')) return false;
        const r = node.getBoundingClientRect(); return r.width > 0 && r.right > innerWidth + 1;
      }).slice(0, 8).map((node) => node.className) }));
    assert.ok(overflow.width <= overflow.viewport + 1, `${name}/${width}: ${JSON.stringify(overflow)}`);
    await page.screenshot({ path: `${output}/theme-${name}-${width}.png`, fullPage: true });
    results.push({ name, width, overflow: false });
  }
  for (const width of [1440, 1024, 390]) {
    await page.setViewportSize({ width, height: 960 });
    await page.goto(origin);
    await page.locator('.library-inner .video-card').first().waitFor();
    await capture("library", width);
    const card = page.locator('.library-inner .video-card').first();
    await card.hover();
    if (width === 1440) {
      await page.waitForTimeout(170);
      assert.ok(await card.evaluate((node) => node.dataset.spotlight === "true"));
    }
    await card.locator('.library-card-process').click();
    await page.locator('.library-inner .library-processing-options').waitFor();
    await capture("processing", width);
    await page.getByRole('button', { name: '收起处理选项', exact: true }).click();
    await page.locator('.library-inner .library-processing-options').waitFor({ state: 'detached' });
    await page.locator('.library-inner .folder-card-link').first().click();
    await page.locator('.folder-window').waitFor();
    await capture("folder", width);
    await page.getByRole('button', { name: '关闭文件夹', exact: true }).click();
    await page.locator('.folder-window').waitFor({ state: 'detached' });
    assert.ok(await page.locator('.library-inner .folder-card-link').first().evaluate((node) => document.activeElement === node));
    await page.getByRole('button', { name: '查看任务详情', exact: true }).click();
    await page.getByRole('dialog', { name: '任务详情' }).waitFor();
    await capture("task", width);
    await page.getByRole('button', { name: '关闭面板', exact: true }).click();
    await page.locator('.workbench-panel-backdrop').waitFor({ state: 'hidden' });
    // Reopen while an exit is still in flight: no stale completion may hide it.
    await page.getByRole('button', { name: '查看任务详情', exact: true }).click();
    await page.getByRole('button', { name: '关闭面板', exact: true }).click();
    await page.getByRole('button', { name: '查看任务详情', exact: true }).evaluate((node) => node.click());
    await page.waitForTimeout(350);
    assert.equal(await page.locator('.workbench-panel-backdrop').evaluate((node) => node.hidden || node.inert), false);
    await page.getByRole('button', { name: '关闭面板', exact: true }).click();
    await page.locator('.workbench-panel-backdrop').waitFor({ state: 'hidden' });
    await page.goto(`${origin}/settings?section=ai`);
    await page.getByLabel('模型名称', { exact: true }).waitFor();
    await capture("settings", width);
    await page.goto(`${origin}/process?videoId=1`);
    await page.locator('.process-video-section').waitFor();
    await capture("process-page", width);
    await page.goto(`${origin}/batch-process`);
    await page.locator('.process-page').waitFor();
    await capture("batch-page", width);
    await page.goto(`${origin}/review/1`);
    await page.locator('.marker-item').first().waitFor();
    await page.locator('.marker-main').first().click();
    await capture("review", width);
    await page.getByRole('button', { name: '打开复习设置', exact: true }).click();
    await page.getByRole('dialog').waitFor();
    await page.keyboard.press('Escape');
    await page.getByRole('dialog').waitFor({ state: 'hidden' });
    assert.ok(await page.getByRole('button', { name: '打开复习设置', exact: true }).evaluate((node) => document.activeElement === node));
  }
  const contrast = await page.evaluate(() => {
    const css = getComputedStyle(document.documentElement);
    const luminance = (hex) => {
      const c = hex.trim().replace('#', '');
      const rgb = [0, 2, 4].map((i) => parseInt(c.slice(i, i + 2), 16) / 255).map((v) => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4);
      return rgb[0] * .2126 + rgb[1] * .7152 + rgb[2] * .0722;
    };
    return [['--ui-text', '--ui-surface'], ['--ui-muted', '--ui-surface'], ['--ui-on-primary', '--ui-primary'], ['--ui-accent-strong', '--ui-accent-soft']].map(([fg, bg]) => {
      const a = luminance(css.getPropertyValue(fg)), b = luminance(css.getPropertyValue(bg));
      return { fg, bg, ratio: (Math.max(a, b) + .05) / (Math.min(a, b) + .05) };
    });
  });
  contrast.forEach((item) => assert.ok(item.ratio >= 4.5, JSON.stringify(item)));
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto(origin);
  await page.locator('.folder-card-link').first().click();
  await page.getByRole('button', { name: '关闭文件夹', exact: true }).click();
  assert.equal(await page.locator('.folder-window').count(), 0);
  await page.locator('.library-inner .video-card').first().hover();
  assert.equal(await page.locator('.library-inner .video-card').first().evaluate((node) => getComputedStyle(node).translate), 'none');
  fs.writeFileSync(`${output}/theme-acceptance.json`, JSON.stringify({ passed: true, screens: results, contrast,
    checks: ['rapid drawer reopen', 'focus restoration', 'reduced motion', 'no horizontal overflow'] }, null, 2));
};
