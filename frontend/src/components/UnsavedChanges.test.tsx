import { act, StrictMode, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { createMemoryRouter, Link, RouterProvider, useLocation } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { UnsavedChangesProvider, useUnsavedDraft, useUnsavedLeave } from "./UnsavedChanges";
import { ShortcutSettings } from "./ShortcutSettings";
import { DesktopControls } from "./DesktopControls";
import { defaultShortcuts, readShortcuts } from "../shortcuts";
import type { DesktopAPI } from "../hooks/useDesktopAPI";
let host: HTMLDivElement, root: Root, router: ReturnType<typeof createMemoryRouter>;
const save = vi.fn<() => Promise<boolean | void>>(), discard = vi.fn(), exit = vi.fn();
function Draft() {
  const [dirty, setDirty] = useState(false);
  const location = useLocation();
  useUnsavedDraft({ label: "测试设置", dirty, save: async () => { const done = await save(); if (done !== false) setDirty(false); return done; },
    discard: () => { discard(); setDirty(false); } });
  const leave = useUnsavedLeave();
  return <><output>{location.pathname + location.search}</output><button onClick={() => setDirty(true)}>修改</button>
    <Link to="/settings?section=ai">切换分类</Link><Link to="/">返回</Link>
    <button onClick={() => leave(exit)}>退出测试</button></>;
}
beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  save.mockReset().mockResolvedValue(true); discard.mockReset(); exit.mockReset();
  const values = new Map<string, string>();
  vi.stubGlobal("localStorage", { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => values.set(key, value) });
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});
afterEach(() => { act(() => root.unmount()); router?.dispose(); host.remove(); delete window.pywebview; vi.unstubAllGlobals(); vi.restoreAllMocks(); });
async function mount(shortcuts = false) {
  router = createMemoryRouter([{ path: "*", element: <UnsavedChangesProvider>{shortcuts ? <><ShortcutSettings /><Link to="/">返回</Link><DesktopControls /></> : <Draft />}</UnsavedChangesProvider> }], {
    initialEntries: ["/", "/settings?section=shortcuts"], initialIndex: 1,
  });
  await act(async () => root.render(<StrictMode><RouterProvider router={router} /></StrictMode>));
}
function button(label: string) { return [...document.querySelectorAll<HTMLButtonElement>("button")].find(item => item.textContent === label)!; }
async function click(label: string) { await act(async () => button(label).click()); }
async function link(label: string) { await act(async () => [...host.querySelectorAll("a")].find(item => item.textContent === label)!.click()); }
function unload() { const event = new Event("beforeunload", { cancelable: true }); window.dispatchEvent(event); return event.defaultPrevented; }
it("blocks category changes, cancels without losing edits, and discards before navigation", async () => {
  await mount(); await click("修改"); expect(unload()).toBe(true);
  await link("切换分类"); expect(router.state.location.search).toBe("?section=shortcuts");
  expect(document.activeElement?.textContent).toBe("继续编辑");
  await click("继续编辑"); expect(discard).not.toHaveBeenCalled(); expect(unload()).toBe(true);
  await link("切换分类"); await click("放弃修改");
  expect(router.state.location.search).toBe("?section=ai"); expect(discard).toHaveBeenCalledTimes(1);
  expect(unload()).toBe(false);
});
it("keeps the page and draft on save failure and proceeds only after a successful retry", async () => {
  await mount(); await click("修改"); await link("返回");
  save.mockRejectedValueOnce(new Error("保存失败，请重试")); await click("保存并离开");
  expect(router.state.location.pathname).toBe("/settings");
  expect(document.querySelector('[role="alert"]')?.textContent).toContain("保存失败");
  expect(unload()).toBe(true);
  await click("保存并离开"); expect(router.state.location.pathname).toBe("/");
  expect(save).toHaveBeenCalledTimes(2); expect(unload()).toBe(false);
});
it("guards back navigation and preserves forward history", async () => {
  await mount(); await click("修改");
  await act(async () => { await router.navigate(-1); });
  expect(router.state.location.pathname).toBe("/settings");
  await click("放弃修改"); expect(router.state.location.pathname).toBe("/");
  await act(async () => { await router.navigate(1); });
  expect(router.state.location.search).toBe("?section=shortcuts");
});
it("uses the latest destination if another navigation arrives while the prompt is open", async () => {
  await mount(); await click("修改"); await link("切换分类");
  await act(async () => { await router.navigate("/"); });
  await click("放弃修改");
  expect(router.state.location.pathname).toBe("/");
  expect(document.querySelector('[role="dialog"]')).toBeNull();
  expect(discard).toHaveBeenCalledTimes(1);
});
it("retains migration drafts when save opens a required confirmation", async () => {
  await mount(); await click("修改"); save.mockResolvedValueOnce(false);
  await link("返回"); await click("保存并离开");
  expect(router.state.location.pathname).toBe("/settings");
  expect(document.querySelector('[role="dialog"]')).toBeNull(); expect(unload()).toBe(true);
  await link("返回"); expect(document.querySelector('[role="dialog"]')).not.toBeNull();
});
it("disables duplicate actions during save and guards explicit desktop exit requests", async () => {
  let done!: () => void; save.mockImplementationOnce(() => new Promise<void>(resolve => { done = resolve; }));
  await mount(); await click("修改"); await click("退出测试");
  await click("保存并离开"); expect(button("放弃修改").disabled).toBe(true);
  await act(async () => { done(); }); expect(exit).toHaveBeenCalledTimes(1); expect(save).toHaveBeenCalledTimes(1);
});
it("real shortcut drafts revert on discard and no longer warn when edited back to their saved value", async () => {
  await mount(true);
  const input = host.querySelector<HTMLInputElement>('[aria-label="下一个考点快捷键"]')!;
  const record = async (key: string) => act(async () => input.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true })));
  await record("J"); expect(unload()).toBe(true);
  await record("D"); expect(unload()).toBe(false);
  await record("J"); await link("返回"); await click("放弃修改");
  expect(input.value).toBe("D"); expect(readShortcuts()).toEqual(defaultShortcuts());
});
it("hands native unsaved-close back to native closing only after the user chooses save/discard", async () => {
  const api: DesktopAPI = {
    describe: vi.fn(async () => ({ name: "test", tray: true, hotkey: "", dialog: null })),
    set_unsaved_changes: vi.fn(async () => ({})), respond: vi.fn(async () => ({})), retry: vi.fn(),
    set_hotkey: vi.fn(), set_close_behavior: vi.fn(), hide_to_tray: vi.fn(), exit_application: vi.fn(), open_logs: vi.fn(),
  };
  window.pywebview = { api }; await mount(true);
  const input = host.querySelector<HTMLInputElement>('[aria-label="下一个考点快捷键"]')!;
  await act(async () => input.dispatchEvent(new KeyboardEvent("keydown", { key: "J", bubbles: true, cancelable: true })));
  expect(api.set_unsaved_changes).toHaveBeenLastCalledWith(true);
  const close = () => act(async () => window.dispatchEvent(new CustomEvent("classroom-desktop-dialog", { detail: { kind: "unsaved-close", message: "未保存" } })));
  await close(); await click("继续编辑"); expect(api.respond).toHaveBeenCalledWith("cancel");
  expect(api.respond).not.toHaveBeenCalledWith("continue-close");
  await close(); await click("保存并离开");
  expect(readShortcuts().nextMarker).toEqual(["J"]);
  expect(api.respond).toHaveBeenCalledWith("continue-close");
});
