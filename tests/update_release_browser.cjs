// Real isolated WebView2 acceptance. No mocked native bridge or install API.
const { chromium } = require("./playwright.cjs");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(process.argv[2] || "");
const action = process.argv[3] || "upgrade";
const recordPath = path.join(root, ".runtime/release-qa.json");
assert(fs.existsSync(recordPath), "Only a prepared release acceptance install may be controlled");
const record = JSON.parse(fs.readFileSync(recordPath, "utf8"));
assert.equal(path.resolve(record.root), root);
const origin = `http://127.0.0.1:${record.api_port}`;
const endpoint = `http://127.0.0.1:${record.debug_port}`;
const evidence = path.join(root, ".runtime/acceptance");
fs.mkdirSync(evidence, { recursive: true });
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(fn, message, timeout = 120000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    const value = await fn();
    if (value) return value;
    await pause(200);
  }
  throw new Error(message);
}
async function connect() {
  return until(async () => {
    try {
      const browser = await chromium.connectOverCDP(endpoint, { timeout: 2000 });
      const page = browser.contexts().flatMap(context => context.pages())
        .find(page => page.url().startsWith(origin));
      return page ? { browser, page } : null;
    } catch { return null; }
  }, "Isolated desktop did not become ready");
}
async function main() {
  let { page } = await connect();
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  const status = () => page.evaluate(() => fetch("/api/updates").then(response => response.json()));
  const screenshot = name => page.screenshot({ path: path.join(evidence, name + ".png"), fullPage: true });
  if (action === "exit") {
    await page.evaluate(() => window.pywebview.api.exit_application());
    return { action, requested: true };
  }
  if (action === "fresh") {
    await page.getByRole("region", { name: "确认存储", exact: true }).waitFor();
    await screenshot("first-run");
    const images = await page.locator(".onboarding-mascot img").evaluateAll(images =>
      images.map(image => ({ loaded: image.complete && image.naturalWidth > 0, src: image.getAttribute("src") })));
    assert(images.length && images.every(image => image.loaded), "Onboarding mascot must load");
    await page.locator('[data-onboarding="settings"]').click();
    await page.locator(".storage-directories-section").waitFor();
    await screenshot("first-run-storage");
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    return { action, passed: true, images, errors };
  }
  await page.goto(origin + "/settings?section=updates");
  await page.getByRole("heading", { name: "关于与更新", exact: true }).waitFor();
  await page.waitForFunction(() => Boolean(window.pywebview?.api?.install_update));
  assert.equal((await status()).portable, true, "Must use an unmodified formal distribution");
  if (action === "inspect") {
    await screenshot("status");
    return { action, state: await status(), errors };
  }
  if (action === "auto") {
    if (!(await status()).preferences.auto_download) {
      await page.getByRole("checkbox", { name: "自动下载兼容更新", exact: true }).click();
    }
    await until(async () => (await status()).preferences.auto_download, "Preference not saved");
  }
  await page.getByRole("button", { name: "检查更新", exact: true }).click();
  const release = await until(async () => {
    const state = await status();
    if (!state.checking && state.error) throw new Error(state.error);
    return state.release;
  }, "New signed release not found");
  await screenshot("new-release");
  if (action !== "auto") {
    await page.getByRole("button", { name: /^下载更新/ }).click();
    if (record.transport) {
      await until(async () => (await status()).downloaded_bytes > 0, "Download did not start");
      await page.getByRole("button", { name: "暂停下载", exact: true }).click();
      await until(async () => !(await status()).downloading, "Download did not pause");
      const partials = fs.readdirSync(path.join(root, ".runtime/updates")).filter(name => name.endsWith(".part"));
      assert.equal(partials.length, 1);
      const partialSize = fs.statSync(path.join(root, ".runtime/updates", partials[0])).size;
      assert(partialSize > 0 && partialSize < release.size, "Pause must leave a resumable partial package");
      await screenshot("paused");
      await page.getByRole("button", { name: /^下载更新/ }).click();
    }
  }
  await until(async () => {
    const state = await status();
    if (state.download_error) throw new Error(state.download_error);
    return state.ready;
  }, "Download was not verified");
  await screenshot("ready");
  const transaction = path.join(root, ".runtime/updates/transaction.json");
  const previousTransaction = fs.existsSync(transaction) ? fs.readFileSync(transaction, "utf8") : null;
  if (previousTransaction) {
    assert.equal(JSON.parse(previousTransaction).status, "rolled_back", "Only a safely rolled-back install may be retried");
  }
  const unsaved = await page.evaluate(async () => {
    await window.pywebview.api.set_unsaved_changes(true);
    try { await window.pywebview.api.install_update(); return null; }
    catch (error) { return String(error); }
    finally { await window.pywebview.api.set_unsaved_changes(false); }
  });
  assert(unsaved, "Native installation must reject unsaved settings");
  const installButton = page.getByRole("button", { name: "安装并重启", exact: true });
  await installButton.waitFor({ timeout: 90000 });
  await installButton.click();
  await page.getByRole("dialog").getByRole("button", { name: "取消", exact: true }).click();
  assert.equal(fs.existsSync(transaction) ? fs.readFileSync(transaction, "utf8") : null,
    previousTransaction, "Cancel must leave the current installation untouched");
  await installButton.click();
  await screenshot("confirmation");
  await page.getByRole("dialog").getByRole("button", { name: "安装并重启", exact: true }).click();
  await until(async () => {
    // Do not open the journal while the old Windows helper is replacing it.
    try {
      const response = await fetch(origin + "/api/health", { signal: AbortSignal.timeout(1500) });
      const health = await response.json();
      return health.version === release.version;
    } catch { return false; }
  }, "Real update helper and restarted API did not acknowledge completion", 180000);
  ({ page } = await connect());
  await page.goto(origin + "/settings?section=updates");
  const state = await page.evaluate(() => fetch("/api/updates").then(response => response.json()));
  assert.equal(state.version, release.version);
  assert.equal(JSON.parse(fs.readFileSync(transaction, "utf8")).status, "completed");
  await page.screenshot({ path: path.join(evidence, "updated.png"), fullPage: true });
  await page.goto(origin + "/review/1");
  // The library's actual link is used if the route convention changes.
  if (!(await page.locator("video").count())) {
    await page.goto(origin + "/");
    const video = page.getByText("fixture", { exact: true }).first();
    await video.dblclick();
  }
  await page.locator("video").waitFor();
  const playback = await page.locator("video").evaluate(async video => {
    video.muted = true;
    await video.play();
    await new Promise(resolve => setTimeout(resolve, 700));
    video.pause();
    return { time: video.currentTime, ready: video.readyState, error: video.error?.message };
  });
  assert(playback.time > 0 && playback.ready >= 2 && !playback.error);
  await page.screenshot({ path: path.join(evidence, "playback.png"), fullPage: true });
  assert.deepEqual(errors, []);
  return { action, passed: true, version: state.version, playback, unsavedRejected: true, errors };
}
main().then(result => {
  fs.writeFileSync(path.join(evidence, action + "-result.json"), JSON.stringify(result, null, 2));
  console.log(JSON.stringify(result, null, 2));
  process.exit(0);
}).catch(error => { console.error(error); process.exit(1); });
