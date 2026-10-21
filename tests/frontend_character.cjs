// Read-only browser checks for the decorative eye; never changes library data.
const { chromium } = require("./playwright.cjs");
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");

(async () => {
  const output = path.resolve(__dirname, `../.runtime/character-eye-${Date.now()}`);
  fs.mkdirSync(output, { recursive: true });
  const origin = process.env.FRONTEND_URL || "http://127.0.0.1:5175";
  const browser = await chromium.launch({ executablePath: "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", headless: true });
  let context;
  try {
    context = await browser.newContext({ viewport: { width: 1338, height: 1038 }, recordVideo: { dir: output, size: { width: 1338, height: 1038 } } });
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.goto(origin);
    await page.locator(".character-profile-eye").waitFor();
    await page.evaluate(() => document.fonts.ready);
    await page.waitForTimeout(300);
    assert.equal(await page.locator(".workbench-brand .character-eye i").count(), 1);
    assert.equal(await page.locator(".character-profile-eye").count(), 1);
    assert.equal(await page.locator(".character-profile-far-eye").count(), 1);
    const farEye = () => page.locator(".character-profile-far-pupil").evaluate(node => {
      const r = node.getBoundingClientRect();
      return { x: r.x, y: r.y, transform: getComputedStyle(node).transform };
    });
    const offset = () => page.locator(".character-profile-pupil").evaluate(node => {
      const m = new DOMMatrixReadOnly(getComputedStyle(node).transform);
      return { x: m.m41, y: m.m42 };
    });
    const centered = async () => {
      await page.waitForTimeout(180);
      const p = await offset();
      assert.ok(Math.hypot(p.x, p.y) < .05, JSON.stringify(p));
      assert.ok(await page.locator(".character-profile-far-pupil").evaluate(node => {
        const m = new DOMMatrixReadOnly(getComputedStyle(node).transform); return Math.hypot(m.m41, m.m42) < .05;
      }));
    };
    await page.mouse.move(100, 100);
    await page.waitForTimeout(180);
    const left = await offset();
    const farLeft = await farEye();
    assert.ok(left.x < -1, JSON.stringify(left));
    await page.screenshot({ path: `${output}/look-left.png` });
    await page.mouse.move(1330, 80);
    await page.waitForTimeout(180);
    const right = await offset();
    assert.ok(right.x > 1, JSON.stringify(right));
    const farRight = await farEye();
    assert.ok(farRight.x > farLeft.x, JSON.stringify({ farLeft, farRight }));
    assert.ok(farRight.y < farLeft.y, JSON.stringify({ farLeft, farRight }));
    assert.ok(Math.hypot(farRight.x - farLeft.x, farRight.y - farLeft.y) < 4);
    assert.ok(Math.hypot(left.x, left.y) <= 10.01 && Math.hypot(right.x, right.y) <= 10.01);
    assert.ok(await page.evaluate(() => {
      const eye = document.querySelector(".character-profile-eye").getBoundingClientRect();
      const pupil = document.querySelector(".character-profile-pupil").getBoundingClientRect();
      const distance = Math.hypot(pupil.x + pupil.width / 2 - eye.x - eye.width / 2, pupil.y + pupil.height / 2 - eye.y - eye.height / 2);
      return distance + pupil.width / 2 < eye.width / 2;
    }));
    await page.screenshot({ path: `${output}/look-right.png` });
    await page.evaluate(() => window.dispatchEvent(new PointerEvent("pointermove", { pointerType: "touch", clientX: 20, clientY: 20 })));
    await page.waitForTimeout(180);
    assert.deepEqual(await offset(), right);
    assert.deepEqual(await farEye(), farRight);
    await page.emulateMedia({ reducedMotion: "reduce" });
    await centered();
    await page.mouse.move(100, 200);
    await centered();
    await page.emulateMedia({ reducedMotion: "no-preference" });
    await page.waitForTimeout(100);
    await page.mouse.move(200, 100);
    await page.waitForTimeout(180);
    assert.ok((await offset()).x < -1);
    await page.evaluate(() => document.documentElement.dispatchEvent(new PointerEvent("pointerleave")));
    await centered();
    await page.mouse.move(300, 110);
    await page.waitForTimeout(180);
    await page.evaluate(() => document.documentElement.dataset.uiTheme = "square");
    await centered();
    assert.equal(await page.locator(".character-art").isVisible(), false);
    await page.evaluate(() => document.documentElement.dataset.uiTheme = "character");
    await page.waitForTimeout(150);
    await page.mouse.move(300, 120);
    await page.waitForTimeout(180);
    assert.ok((await offset()).x < -1);
    await page.setViewportSize({ width: 1338, height: 500 });
    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
    await centered();
    await page.mouse.move(200, 400);
    await centered();
    await page.setViewportSize({ width: 1338, height: 1038 });
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.waitForTimeout(180);
    await page.evaluate(() => window.dispatchEvent(new Event("blur")));
    await centered();
    await page.screenshot({ path: `${output}/library-1338.png`, fullPage: true });
    await page.locator(".library-heading").screenshot({ path: `${output}/profile-detail.png` });
    const smallEyeChecks = [];
    for (const [name, route] of [["process", "/process"], ["batch", "/batch-process"], ["settings", "/settings?section=ai"], ["storage", "/settings?section=storage"], ["asr", "/settings?section=asr"], ["workspace", "/settings?section=workspace"]]) {
      await page.goto(`${origin}${route}`);
      await page.locator(".character-eye-heading:visible").first().waitFor();
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.waitForTimeout(200);
      const pupils = page.locator(".character-eye:visible .character-eye-pupil");
      const positions = () => pupils.evaluateAll(nodes => nodes.map(node => {
        const m = new DOMMatrixReadOnly(getComputedStyle(node).transform);
        return { x: m.m41, y: m.m42 };
      }));
      await page.mouse.move(2, 2);
      await page.waitForTimeout(180);
      const before = await positions();
      await page.mouse.move(1330, 200);
      await page.waitForTimeout(180);
      const after = await positions();
      assert.ok(after.length >= 2, `${name}: expected brand and section eyes`);
      // Only visible-in-viewport eyes animate; tall pages also have offscreen titles.
      const inViewport = await pupils.evaluateAll(nodes => nodes.map(node => {
        const r = node.getBoundingClientRect(); return r.bottom > 0 && r.top < innerHeight;
      }));
      after.forEach((position, i) => {
        if (!inViewport[i]) return;
        assert.ok(before[i].x < 0 && position.x > 0, `${name}: ${JSON.stringify({ before: before[i], after: position })}`);
        assert.ok(Math.hypot(position.x, position.y) <= 3.5, `${name}: excessive travel`);
      });
      assert.ok(await pupils.evaluateAll(nodes => nodes.every(node => {
        const iris = node.parentElement.getBoundingClientRect(), pupil = node.getBoundingClientRect();
        return Math.hypot(pupil.x + pupil.width / 2 - iris.x - iris.width / 2, pupil.y + pupil.height / 2 - iris.y - iris.height / 2) + pupil.width / 2 < iris.width / 2;
      })));
      await page.screenshot({ path: `${output}/${name}-eyes.png`, fullPage: true });
      await page.emulateMedia({ reducedMotion: "reduce" });
      await page.waitForTimeout(100);
      await page.mouse.move(5, 100);
      assert.ok((await positions()).every(p => Math.hypot(p.x, p.y) < .05));
      await page.emulateMedia({ reducedMotion: "no-preference" });
      smallEyeChecks.push(name);
    }
    // Route changes must remove stale registrations and retain the existing hero gaze.
    await page.goto(origin);
    await page.locator(".character-profile-eye").waitFor();
    await page.waitForTimeout(150);
    await page.mouse.move(50, 100);
    await page.waitForTimeout(180);
    assert.ok((await offset()).x < -1);
    assert.deepEqual(errors, []);
    const recording = page.video();
    await context.close(); context = null;
    await recording.saveAs(`${output}/eye-tracking.webm`);

    const touch = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
    const mobile = await touch.newPage();
    await mobile.goto(origin);
    await mobile.locator(".character-profile-eye").waitFor();
    await mobile.mouse.move(20, 20);
    await mobile.waitForTimeout(200);
    assert.equal(await mobile.evaluate(() => matchMedia("(hover: hover) and (pointer: fine)").matches), false);
    assert.ok(await mobile.locator(".character-profile-pupil, .character-profile-far-pupil").evaluateAll(nodes => nodes.every(node => {
      const m = new DOMMatrixReadOnly(getComputedStyle(node).transform); return m.m41 === 0 && m.m42 === 0;
    })));
    assert.ok(await mobile.locator(".character-eye-pupil").evaluateAll(nodes => nodes.every(node => {
      const m = new DOMMatrixReadOnly(getComputedStyle(node).transform); return m.m41 === 0 && m.m42 === 0;
    })));
    assert.ok(await mobile.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await mobile.evaluate(() => document.fonts.ready);
    await mobile.waitForTimeout(400);
    await mobile.screenshot({ path: `${output}/library-390.png`, fullPage: true });
    await touch.close();
    const result = { passed: true, output, left, right, farLeft, farRight, smallEyeChecks, checks: ["brand and section eyes follow pointer", "far eye follows in sync with subtle travel", "near eye tracks mouse within iris", "ignores touch", "all pupils reset for reduced motion", "route changes", "square theme preserved", "offscreen stops", "touch layout"] };
    fs.writeFileSync(`${output}/checks.json`, JSON.stringify(result, null, 2));
    console.log(JSON.stringify(result, null, 2));
  } finally {
    if (context) await context.close();
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
