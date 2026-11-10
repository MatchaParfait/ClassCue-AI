const { chromium } = require("./playwright.cjs");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const { html, transition } = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));

(async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const errors = [];
  const context = await browser.newContext();
  context.on("page", page => page.on("pageerror", error => errors.push(error.message)));
  const dispatch = async (page, detail) => page.evaluate(value =>
    window.dispatchEvent(new CustomEvent("classroom-desktop-dialog", { detail: value })), detail);
  const checkDialog = async (page, modal) => {
    await dispatch(page, { kind: "unsaved-close", message: "Synthetic unsaved edits" });
    await modal.waitFor({ state: "visible" });
    assert.ok((await modal.textContent()).includes("Synthetic unsaved edits"));
    await dispatch(page, null);
    await modal.waitFor({ state: "hidden" });
    await dispatch(page, { kind: "starting", message: "Synthetic recovery" });
    await modal.waitFor({ state: "visible" });
    await dispatch(page, null);
    await modal.waitFor({ state: "hidden" });
  };
  try {
    const startup = await context.newPage();
    await startup.setContent(html);
    await checkDialog(startup, startup.locator("#dialog"));
    await startup.close();

    const application = await context.newPage();
    await application.route("http://127.0.0.1:8765/**", route => route.fulfill({
      contentType: "text/html", body: '<!doctype html><html><body><div id="root"></div></body></html>',
    }));
    await application.addInitScript({ content: transition });
    await application.goto("http://127.0.0.1:8765/");
    const frame = application.frameLocator("#classroom-startup-cover iframe");
    await frame.locator("#dialog").waitFor({ state: "attached" });
    await checkDialog(application, frame.locator("#dialog"));
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ passed: true, surfaces: 2 }));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
