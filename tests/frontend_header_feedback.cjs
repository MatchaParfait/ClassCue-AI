const assert = require("node:assert/strict");

module.exports = async function headerFeedback({ page, review = false }) {
  const brand = page.locator(review ? ".brand-mark" : ".workbench-brand");
  await page.mouse.move(0, 0);
  await page.waitForTimeout(180);
  const rest = await brand.evaluate(node => {
    const style = getComputedStyle(node);
    return { shadow: style.boxShadow, width: node.offsetWidth, height: node.offsetHeight };
  });
  await brand.hover();
  await page.waitForTimeout(180);
  const hover = await brand.evaluate(node => {
    const style = getComputedStyle(node);
    return { shadow: style.boxShadow, translate: style.translate, width: node.offsetWidth, height: node.offsetHeight };
  });
  assert.notEqual(hover.shadow, rest.shadow);
  assert.equal(hover.translate, "0px -1px");
  assert.equal(hover.width, rest.width);
  assert.equal(hover.height, rest.height);
  if (!review) {
    const task = page.getByRole("button", { name: "查看任务详情", exact: true });
    const settings = page.locator(".workbench-settings-link");
    assert.equal(await task.locator(".task-indicator-label").innerText(), "任务");
    assert.equal(await settings.innerText(), "设置");
    assert.equal(await task.getAttribute("aria-haspopup"), "dialog");
    const controls = [page.locator(".global-search-trigger"), task, settings];
    let previous;
    for (const control of controls) {
      const box = await control.boundingBox();
      assert.ok(box && box.y >= 0 && box.x + box.width <= page.viewportSize().width, "Header controls stay in the window");
      if (previous) assert.ok(box.x >= previous.x + previous.width, "Header controls do not overlap");
      previous = box;
    }
    for (const control of [task, settings]) {
      const restBox = await control.boundingBox();
      assert.ok(restBox.height >= 40, "Header entries have prominent hit targets");
      await module.exports.returnBorder(control);
      await control.hover();
      await page.waitForTimeout(180);
      assert.notEqual(await control.evaluate(node => getComputedStyle(node).boxShadow), "none");
      assert.deepEqual(await control.boundingBox(), restBox, "Hover does not shift header entries");
      await page.keyboard.press("Tab");
      await control.focus();
      assert.equal(await control.evaluate(node => getComputedStyle(node).outlineStyle), "solid");
    }
    await brand.focus();
    assert.equal(await brand.evaluate(node => getComputedStyle(node).outlineStyle), "solid");
    await brand.hover();
    await page.mouse.down();
    await page.waitForTimeout(180);
    assert.equal(await brand.evaluate(node => getComputedStyle(node).translate), "0px 1px");
    await page.mouse.move(0, 0);
    await page.mouse.up();
    await brand.evaluate(node => node.blur());
  }
  await page.emulateMedia({ reducedMotion: "reduce" });
  await brand.hover();
  assert.equal(await brand.evaluate(node => getComputedStyle(node).translate), "none");
  await page.mouse.move(0, 0);
  await page.emulateMedia({ reducedMotion: "no-preference" });
};

module.exports.returnBorder = async function returnBorder(control) {
  assert.deepEqual(await control.evaluate(node => {
    const style = getComputedStyle(node);
    return [style.borderTopWidth, style.borderTopStyle, style.borderTopColor === "rgba(0, 0, 0, 0)"];
  }), ["1px", "solid", false]);
};
