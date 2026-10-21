import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { beforeEach, afterEach, expect, it, vi } from "vitest";
import { ShortcutSettings } from "./ShortcutSettings";
import { defaultShortcuts, readShortcuts, saveShortcuts, SHORTCUTS_KEY } from "../shortcuts";
import type { DesktopAPI } from "../hooks/useDesktopAPI";
let root: Root, host: HTMLDivElement;
beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const values = new Map<string, string>();
  vi.stubGlobal("localStorage", { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => values.set(key, value) });
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});
afterEach(() => { act(() => root.unmount()); host.remove(); delete window.pywebview; vi.unstubAllGlobals(); vi.restoreAllMocks(); });
async function mount(api?: DesktopAPI) { if (api) window.pywebview = { api }; await act(async () => root.render(<ShortcutSettings />)); }
function recorder(label: string) { return host.querySelector<HTMLInputElement>(`[aria-label="${label}"]`)!; }
function button(label: string) { return [...host.querySelectorAll("button")].find(button => button.textContent === label)!; }
function record(label: string, key: string, options: KeyboardEventInit = {}) {
  act(() => { recorder(label).focus(); recorder(label).dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...options })); });
}
async function save() { await act(async () => button("保存快捷键").click()); }
function bridge() {
  let hotkey = "CTRL+ALT+K";
  return { describe: vi.fn(async () => ({ name: "test", tray: true, dialog: null, hotkey })),
    respond: vi.fn(async () => ({})), retry: vi.fn(async () => ({})),
    set_hotkey: vi.fn(async (value: string) => { hotkey = value; return { hotkey }; }),
    set_close_behavior: vi.fn(), hide_to_tray: vi.fn(), exit_application: vi.fn(), open_logs: vi.fn(),
  };
}
it("records, saves, disables and restores shortcuts in the browser", async () => {
  await mount();
  expect([...host.querySelectorAll("h3")].map(node => node.textContent)).not.toContain("字幕位置");
  expect(host.textContent).not.toContain("字幕向");
  expect(host.querySelector('[aria-label="字幕向上移动快捷键"]')).toBeNull();
  expect(recorder("启动或唤起快捷键").disabled).toBe(true);
  record("下一个考点快捷键", "j", { ctrlKey: true });
  expect(readShortcuts().nextMarker).toEqual(["D"]);
  await save(); expect(readShortcuts().nextMarker).toEqual(["Ctrl+J"]);
  record("下一个考点快捷键", "Backspace"); await save();
  expect(readShortcuts().nextMarker).toEqual([]);
  act(() => button("恢复默认").click());
  expect(readShortcuts().nextMarker).toEqual([]);
  await save(); expect(readShortcuts()).toEqual(defaultShortcuts());
});
it("blocks duplicates and wake conflicts, and never triggers application shortcuts while recording", async () => {
  const api = bridge(); await mount(api);
  const listener = vi.fn(); document.addEventListener("keydown", listener);
  record("下一个考点快捷键", "k");
  expect(listener).not.toHaveBeenCalled(); document.removeEventListener("keydown", listener);
  expect(host.querySelector('[role="alert"]')?.textContent).toContain("同时用于");
  expect(button("保存快捷键").disabled).toBe(true);
  record("下一个考点快捷键", "k", { ctrlKey: true, altKey: true });
  expect(host.querySelector('[role="alert"]')?.textContent).toContain("启动 / 唤起");
  expect(api.set_hotkey).not.toHaveBeenCalled();
});
it("moves the native wake shortcut and saves both sets together", async () => {
  const api = bridge(); await mount(api);
  expect(recorder("启动或唤起快捷键").value).toBe("Ctrl + Alt + K");
  record("启动或唤起快捷键", "u", { ctrlKey: true, altKey: true });
  record("播放 / 暂停快捷键", "p");
  await save();
  expect(api.set_hotkey).toHaveBeenCalledWith("Ctrl+Alt+U");
  expect(readShortcuts().playPause).toEqual(["P", "K"]);
  expect(host.querySelector('[role="status"]')?.textContent).toContain("已保存");
});
it("retains edits and old bindings when native save fails", async () => {
  const api = bridge(); api.set_hotkey.mockRejectedValue(new Error("快捷键不可用")); await mount(api);
  record("启动或唤起快捷键", "u", { ctrlKey: true, altKey: true });
  record("下一个考点快捷键", "j"); await save();
  expect(host.querySelector('[role="alert"]')?.textContent).toContain("快捷键不可用");
  expect(recorder("下一个考点快捷键").value).toBe("J");
  expect(readShortcuts().nextMarker).toEqual(["D"]);
});
it("rolls back native wake when application storage cannot be saved", async () => {
  const api = bridge(); await mount(api);
  record("启动或唤起快捷键", "u", { ctrlKey: true, altKey: true });
  vi.spyOn(window.localStorage, "setItem").mockImplementation(() => { throw new Error("disk full"); });
  await save();
  expect(api.set_hotkey.mock.calls.map(call => call[0])).toEqual(["Ctrl+Alt+U", "Ctrl+Alt+K"]);
  expect(host.querySelector('[role="alert"]')?.textContent).toContain("disk full");
  expect(host.querySelector('[role="status"]')).toBeNull();
});
it("loads saved settings again and keeps Enter/Escape as standard interaction", async () => {
  const map = defaultShortcuts(); map.search = ["Ctrl+J"]; saveShortcuts(map); await mount();
  expect(recorder("全局搜索快捷键").value).toBe("Ctrl + J");
  record("全局搜索快捷键", "Enter");
  expect(host.querySelector('[role="alert"]')?.textContent).toContain("保留基础操作");
  record("全局搜索快捷键", "Escape");
  expect(document.activeElement).not.toBe(recorder("全局搜索快捷键"));
  expect(JSON.parse(window.localStorage.getItem(SHORTCUTS_KEY)!).search).toEqual(["Ctrl+J"]);
});
