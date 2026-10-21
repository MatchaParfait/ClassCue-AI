// Native Windows / WebView2 end-to-end validation in a disposable installation.
const { chromium } = require("./playwright.cjs");
const { spawn, spawnSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const root = path.resolve(__dirname, "..");
const environment = [".venv", path.join(".runtime", "venv")]
  .map(directory => path.join(root, directory, "Scripts"))
  .find(directory => fs.existsSync(path.join(directory, "python.exe")));
assert.ok(environment, "Prepare the desktop Python environment first");
const python = path.join(environment, "python.exe");
const pythonw = path.join(environment, "pythonw.exe");
const prepared = spawnSync(python, ["tests/fixtures/desktop_application.py"], { cwd: root, encoding: "utf8" });
assert.equal(prepared.status, 0, prepared.stderr);
const fixture = JSON.parse(prepared.stdout.trim().split("\n").at(-1));
const env = { ...process.env,
  CLASSROOM_DATA_ROOT: fixture.data, CLASSROOM_API_PORT: "18876", CLASSROOM_FRONTEND_PORT: "15176",
  CLASSROOM_DESKTOP_DEBUG_PORT: "9238",
};
for (const key of Object.keys(env)) if (/_API_KEY$/.test(key)) delete env[key];
let processHandle, browser, unavailableServer;
const api = "http://127.0.0.1:18876";
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(read, predicate, seconds = 40) {
  const deadline = Date.now() + seconds * 1000;
  let last;
  while (Date.now() < deadline) {
    try { last = await read(); if (predicate(last)) return last; } catch {}
    await wait(150);
  }
  throw new Error(`Timed out: ${JSON.stringify(last)}`);
}
async function ipc(action) {
  const state = JSON.parse(fs.readFileSync(path.join(fixture.project, ".runtime", "desktop_instance.json")));
  const response = await fetch(`http://127.0.0.1:${state.port}/command`, {
    method: "POST", headers: { Authorization: `Bearer ${state.token}`, "Content-Type": "application/json" },
    body: JSON.stringify({ action }),
  });
  assert.ok(response.ok); return response.json();
}
async function post(url, body = {}) {
  const response = await fetch(`${api}${url}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  assert.ok(response.ok, `${response.status} ${await response.clone().text()}`);
  return response.json();
}
function nativeClose(pid) {
  const code = `
import ctypes
from ctypes import wintypes
u=ctypes.windll.user32
u.PostMessageW.argtypes=[wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM]
u.GetWindowThreadProcessId.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.DWORD)]
u.GetWindowTextW.argtypes=[wintypes.HWND,wintypes.LPWSTR,ctypes.c_int]
pid=${Number(pid)}
matched=[]
@ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
def visit(hwnd,_):
    found=wintypes.DWORD()
    u.GetWindowThreadProcessId(hwnd,ctypes.byref(found))
    if found.value==pid and u.IsWindowVisible(hwnd):
        title=ctypes.create_unicode_buffer(256)
        u.GetWindowTextW(hwnd,title,256)
        if title.value != "\\u8bfe\\u5802\\u91cd\\u70b9\\u590d\\u4e60":
            return True
        matched.append(int(hwnd))
        u.PostMessageW(hwnd,0x10,0,0)
        return False
    return True
u.EnumWindows(visit,0)
assert len(matched)==1,matched
`;
  assert.equal(spawnSync(python, ["-c", code], { encoding: "utf8" }).status, 0);
}
async function main() {
  processHandle = spawn(pythonw, [path.join(fixture.project, "scripts", "desktop_app.py")], {
    cwd: fixture.project, env, windowsHide: true, stdio: "ignore",
  });
  processHandle.unref();
  const initial = await until(() => ipc("status"), state => state.ready && state.tray);
  console.log("ready", initial.pid);
  browser = await chromium.connectOverCDP("http://127.0.0.1:9238");
  const page = browser.contexts()[0].pages()[0];
  const screenshot = options => process.env.CLASSROOM_DESKTOP_QA_SKIP_SCREENSHOTS ? Promise.resolve() : page.screenshot(options);
  await page.waitForFunction(() => typeof window.pywebview?.api?.set_close_behavior === "function");
  await screenshot({ path: path.join(fixture.project, "desktop-library.png") });
  const failureMode = process.env.CLASSROOM_DESKTOP_QA_FAILURE;
  if (failureMode) {
    assert.ok(["offline", "unavailable"].includes(failureMode));
    // Stop only this disposable installation, leaving its loaded native UI.
    const stopped = spawnSync(python, [path.join(fixture.project, "scripts", "start_frontend.py"), "stop"], {
      cwd: fixture.project, env, windowsHide: true, encoding: "utf8", timeout: 30000,
    });
    assert.equal(stopped.status, 0, stopped.stderr);
    if (failureMode === "unavailable") {
      unavailableServer = require("node:http").createServer((_request, response) => {
        response.writeHead(503, { "Content-Type": "application/json" });
        response.end(JSON.stringify({ detail: "QA service unavailable" }));
      });
      await new Promise(resolve => unavailableServer.listen(18876, "127.0.0.1", resolve));
    }
    await ipc("exit");
    if (failureMode === "unavailable") {
      await page.getByRole("button", { name: "仅关闭窗口", exact: true }).waitFor();
      assert.ok((await ipc("status")).dialog.kind === "service-error");
      await screenshot({ path: path.join(fixture.project, "desktop-service-error.png") });
      await page.getByRole("button", { name: "仅关闭窗口", exact: true }).click();
    }
    await until(async () => !fs.existsSync(path.join(fixture.project, ".runtime", "desktop_instance.json")), Boolean);
    if (failureMode === "unavailable") {
      assert.equal((await fetch(`${api}/api/health`)).status, 503, "Window-only exit must not stop the server");
    }
    console.log(JSON.stringify({ ok: true, mode: failureMode, project: fixture.project }));
    return;
  }
  nativeClose(initial.pid);
  await page.getByRole("button", { name: "最小化到托盘", exact: true }).waitFor();
  assert.equal(await page.evaluate(() => document.activeElement.textContent), "取消关闭");
  await screenshot({ path: path.join(fixture.project, "desktop-close-choice.png") });
  await page.getByRole("button", { name: "取消关闭", exact: true }).click();
  await page.goto(`${api}/settings?section=desktop`);
  await page.getByRole("combobox", { name: "关闭窗口行为" }).waitFor();
  await page.getByRole("combobox", { name: "关闭窗口行为" }).selectOption("background");
  await page.getByText("关闭窗口行为已保存。", { exact: true }).waitFor();
  assert.equal((await page.evaluate(() => window.pywebview.api.describe())).close_behavior, "background");
  await screenshot({ path: path.join(fixture.project, "desktop-settings.png") });
  nativeClose(initial.pid);
  await until(() => ipc("status"), state => !state.visible);
  await ipc("activate");
  await until(() => ipc("status"), state => state.visible);
  await page.getByRole("combobox", { name: "关闭窗口行为" }).selectOption("ask");
  await until(() => page.evaluate(() => window.pywebview.api.describe()), state => state.close_behavior === "ask");
  await page.goto(`${api}/settings?section=shortcuts`);
  const wakeInput = page.getByRole("textbox", { name: "启动或唤起快捷键", exact: true });
  await until(() => wakeInput.isEnabled(), Boolean);
  await wakeInput.press("Control+Alt+U");
  const searchInput = page.getByRole("textbox", { name: "全局搜索快捷键", exact: true });
  await searchInput.press("Control+Shift+Y");
  await page.getByRole("button", { name: "保存快捷键", exact: true }).click();
  await page.getByText("快捷键已保存并生效。", { exact: true }).waitFor();
  assert.equal((await page.evaluate(() => window.pywebview.api.describe())).hotkey, "CTRL+ALT+U");
  const lnk = spawnSync("powershell.exe", ["-NoProfile", "-Command",
    '$shell = New-Object -ComObject WScript.Shell; $file = Get-ChildItem -LiteralPath (Join-Path $env:SHORTCUT_TEST_ROOT "qa-shortcuts\\Desktop") -Filter *.lnk | Select-Object -First 1; $shell.CreateShortcut($file.FullName).Hotkey'],
    { encoding: "utf8", windowsHide: true, env: { ...env, SHORTCUT_TEST_ROOT: fixture.project } });
  assert.equal(lnk.status, 0, lnk.stderr);
  assert.ok(/CTRL/i.test(lnk.stdout) && /ALT/i.test(lnk.stdout) && /U/i.test(lnk.stdout), lnk.stdout);
  await page.keyboard.press("Control+k");
  assert.equal(await page.getByRole("dialog").count(), 0);
  await page.keyboard.press("Control+Shift+Y");
  await page.getByRole("dialog").waitFor();
  await page.keyboard.press("Escape");
  await page.getByRole("dialog").waitFor({ state: "detached" });
  await page.reload();
  await until(() => wakeInput.inputValue(), value => value === "Ctrl + Alt + U");
  assert.equal(await searchInput.inputValue(), "Ctrl + Shift + Y");
  const viewport = await page.evaluate(() => ({ width: innerWidth, height: innerHeight }));
  for (const width of (process.env.CLASSROOM_DESKTOP_QA_SKIP_SCREENSHOTS ? [] : [1440, 1024, 390])) {
    await page.setViewportSize({ width, height: 950 });
    await page.evaluate(() => window.scrollTo(0, 0));
    await wait(250);
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `Shortcut settings overflow at ${width}`);
    await screenshot({ path: path.join(fixture.project, `shortcuts-${width}.png`), fullPage: true });
  }
  await page.setViewportSize(viewport);
  await page.getByRole("button", { name: "恢复默认", exact: true }).click();
  await page.getByRole("button", { name: "保存快捷键", exact: true }).click();
  await page.getByText("快捷键已保存并生效。", { exact: true }).waitFor();
  assert.equal((await page.evaluate(() => window.pywebview.api.describe())).hotkey, "");
  console.log("shortcut persistence, native .lnk and search rebinding passed");
  // Dirty settings intercept the native X even with a remembered close action.
  const nextInput = page.getByRole("textbox", { name: "下一个考点快捷键", exact: true });
  await nextInput.press("J");
  await until(() => ipc("status"), state => state.unsaved_changes);
  await page.evaluate(() => window.pywebview.api.set_close_behavior("exit"));
  nativeClose(initial.pid);
  await page.getByRole("dialog", { name: "有未保存的修改" }).waitFor();
  await page.getByRole("button", { name: "继续编辑", exact: true }).click();
  assert.equal(await nextInput.inputValue(), "J");
  assert.ok((await ipc("status")).unsaved_changes);
  await page.evaluate(() => window.pywebview.api.set_close_behavior("ask"));
  nativeClose(initial.pid);
  await page.getByRole("button", { name: "保存并离开", exact: true }).click();
  await page.getByRole("button", { name: "取消关闭", exact: true }).click();
  await until(() => ipc("status"), state => !state.unsaved_changes);
  await nextInput.press("D");
  await until(() => ipc("status"), state => state.unsaved_changes);
  nativeClose(initial.pid);
  await page.getByRole("button", { name: "放弃修改", exact: true }).click();
  await page.getByRole("button", { name: "取消关闭", exact: true }).click();
  assert.equal(await nextInput.inputValue(), "J");
  await until(() => ipc("status"), state => !state.unsaved_changes);
  console.log("native unsaved settings save, discard and cancel passed");
  await page.goto(`${api}/process?videoId=1`);
  const video = page.locator("video").first();
  await video.waitFor();
  const playback = await video.evaluate(async element => {
    element.muted = true;
    await element.play();
    await new Promise(resolve => setTimeout(resolve, 600));
    const result = { time: element.currentTime, ready: element.readyState, error: element.error?.message };
    element.pause(); return result;
  });
  assert.ok(playback.time > 0 && playback.ready >= 2 && !playback.error, JSON.stringify(playback));
  console.log("video playback passed");
  await screenshot({ path: path.join(fixture.project, "desktop-video.png") });
  await page.goto(`${api}/settings?section=storage`);
  const before = await fetch(`${api}/api/library/status`).then(r => r.json());
  const destination = path.join(fixture.project, "migrated");
  const plan = await post("/api/settings/application-data/preview", { destination, library_id: before.library_id });
  await post("/api/settings/application-data/migrate", { destination, library_id: before.library_id, plan_id: plan.plan_id });
  const after = await until(
    () => fetch(`${api}/api/library/status`).then(r => r.json()),
    state => state.data_root === path.join(destination, "library"),
  );
  assert.equal(after.library_id, before.library_id);
  assert.equal((await ipc("status")).pid, initial.pid);
  console.log("migration and native host preservation passed");
  await page.reload();
  await page.waitForFunction(() => typeof window.pywebview?.api?.set_close_behavior === "function");
  const task = await post("/api/desktop-qa-task");
  await until(() => fetch(`${api}/api/library/status`).then(r => r.json()), state => state.processing);
  nativeClose(initial.pid);
  await wait(500);
  console.log("after native close", await ipc("status"));
  await page.getByRole("button", { name: "最小化到托盘", exact: true }).waitFor();
  assert.equal(await page.evaluate(() => document.activeElement.textContent), "取消关闭");
  await screenshot({ path: path.join(fixture.project, "desktop-close-task.png") });
  await page.getByRole("checkbox", { name: "下次不再询问，记住本次选择" }).check();
  await page.getByRole("button", { name: "最小化到托盘", exact: true }).click();
  await until(() => ipc("status"), state => !state.visible);
  assert.ok((await fetch(`${api}/api/library/status`).then(r => r.json())).processing);
  const secondary = spawn(pythonw, [path.join(fixture.project, "scripts", "desktop_app.py")], {
    cwd: fixture.project, env, windowsHide: true, stdio: "ignore",
  });
  await new Promise(resolve => secondary.once("exit", resolve));
  const activated = await until(() => ipc("status"), state => state.visible);
  assert.equal(activated.pid, initial.pid);
  assert.equal((await page.evaluate(() => window.pywebview.api.describe())).close_behavior, "background");
  nativeClose(initial.pid);
  await until(() => ipc("status"), state => !state.visible);
  await ipc("activate");
  await until(() => ipc("status"), state => state.visible);
  await page.evaluate(() => window.pywebview.api.set_close_behavior("exit"));
  nativeClose(initial.pid);
  await page.getByRole("button", { name: "停止任务并退出", exact: true }).waitFor();
  await page.getByRole("button", { name: "取消", exact: true }).click();
  assert.ok((await fetch(`${api}/api/library/status`).then(r => r.json())).processing);
  await page.goto(`${api}/settings?section=shortcuts`);
  await nextInput.press("D");
  await until(() => ipc("status"), state => state.unsaved_changes);
  await ipc("exit");
  await page.getByRole("button", { name: "放弃修改", exact: true }).click();
  await page.getByRole("button", { name: "停止任务并退出", exact: true }).waitFor();
  await screenshot({ path: path.join(fixture.project, "desktop-exit-task.png") });
  await page.getByRole("button", { name: "停止任务并退出", exact: true }).click();
  await until(async () => {
    try { await fetch(`${api}/api/health`); return false; } catch { return true; }
  }, value => value);
  await until(async () => !fs.existsSync(path.join(fixture.project, ".runtime", "desktop_instance.json")), Boolean);
  assert.ok(fs.existsSync(path.join(destination, "library", "project.db")));
  assert.ok(fs.existsSync(path.join(destination, "config", "desktop_settings.json")));
  console.log(JSON.stringify({ ok: true, project: fixture.project, playback, migrated: after.data_root, task: task.job_id }));
}
main().catch(error => {
  console.error(error);
  console.error(`Disposable installation retained: ${fixture.project}`);
  process.exitCode = 1;
}).finally(async () => {
  if (unavailableServer) await new Promise(resolve => unavailableServer.close(resolve));
  if (browser) await browser.close().catch(() => {});
  // On success all processes have already exited through the real application.
  // Keep a failed fixture for diagnosis instead of terminating unknown trees.
});
