import { MemoryRouter, useLocation } from "react-router-dom";
import { DesktopSettings } from "./DesktopSettings";
import type { CloseBehavior } from "../hooks/useDesktopAPI";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";
import { DesktopControls } from "./DesktopControls";

function LocationProbe() { const location = useLocation(); return <span data-location={location.pathname + location.search} />; }

let root: Root | undefined, host: HTMLDivElement | undefined;
const bridge = () => ({
  describe: vi.fn(async () => ({ name: "课堂重点复习", tray: true, dialog: null, hotkey: "" })),
  set_close_behavior: vi.fn(async (value: CloseBehavior) => ({ close_behavior: value })),
  respond: vi.fn(async () => ({})), retry: vi.fn(async () => ({})),
  set_hotkey: vi.fn(async (value: string) => ({ hotkey: value })),
  hide_to_tray: vi.fn(async () => ({})), exit_application: vi.fn(async () => ({})),
  open_logs: vi.fn(async () => ({})),
});
async function mount(api?: ReturnType<typeof bridge>, settings = false) {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  if (api) window.pywebview = { api }; else delete window.pywebview;
  host = document.createElement("div"); document.body.append(host);
  root = createRoot(host);
  await act(async () => { root!.render(<MemoryRouter><DesktopControls />{settings && <DesktopSettings />}<LocationProbe /></MemoryRouter>); });
}
afterEach(() => {
  act(() => root?.unmount()); host?.remove(); root = undefined;
  delete window.pywebview; vi.restoreAllMocks();
});
it("does not show desktop controls in the normal browser", async () => {
  await mount();
  expect(host!.textContent).toBe("");
});
it("connects when the native bridge becomes available after React mounts", async () => {
  await mount();
  const api = bridge();
  await act(async () => {
    window.pywebview = { api }; window.dispatchEvent(new Event("pywebviewready"));
  });
  expect(host!.querySelector('[aria-label="桌面应用设置"]')).toBeNull();
  expect(api.describe).toHaveBeenCalledTimes(1);
});
it("waits for a partially constructed bridge without crashing the app", async () => {
  await mount({} as ReturnType<typeof bridge>);
  expect(host!.textContent).toBe("");
  const api = bridge();
  await act(async () => {
    window.pywebview = { api }; window.dispatchEvent(new Event("pywebviewready"));
  });
  expect(api.describe).toHaveBeenCalledTimes(1);
  expect(host!.querySelector('[aria-label="桌面应用设置"]')).toBeNull();
});
it("defaults task close focus to cancel and Escape preserves the task", async () => {
  const api = bridge(); await mount(api);
  await act(async () => window.dispatchEvent(new CustomEvent("classroom-desktop-dialog", {
    detail: { kind: "close-task", message: "当前仍有处理任务", tray: true },
  })));
  expect(document.activeElement?.textContent).toBe("取消关闭");
  expect(document.querySelector('[role="dialog"]')?.textContent).toContain("后台继续");
  await act(async () => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
  expect(api.respond).toHaveBeenCalledWith("cancel");
  expect(document.querySelector('[role="dialog"]')).toBeNull();
});
it("background close sends only the background action", async () => {
  const api = bridge(); await mount(api);
  await act(async () => window.dispatchEvent(new CustomEvent("classroom-desktop-dialog", {
    detail: { kind: "close-task", message: "处理中", tray: true },
  })));
  const button = [...document.querySelectorAll("button")].find(item => item.textContent === "后台继续")!;
  await act(async () => button.click());
  expect(api.respond).toHaveBeenCalledWith("background");
  expect(api.exit_application).not.toHaveBeenCalled();
});

it("offers an explicit window-only escape when backend status is unavailable", async () => {
  const api = bridge(); await mount(api);
  await act(async () => window.dispatchEvent(new CustomEvent("classroom-desktop-dialog", {
    detail: { kind: "service-error", message: "暂时无法确认任务状态", tray: true },
  })));
  expect(document.activeElement?.textContent).toBe("取消");
  const dialog = document.querySelector('[role="dialog"]')!;
  expect(dialog.textContent).toContain("任务不会被停止");
  const button = [...dialog.querySelectorAll("button")].find(item => item.textContent === "仅关闭窗口")!;
  await act(async () => button.click());
  expect(api.respond).toHaveBeenCalledWith("close-window");
  expect(api.exit_application).not.toHaveBeenCalled();
});

async function showChoice(tray = true) {
  await act(async () => window.dispatchEvent(new CustomEvent("classroom-desktop-dialog", {
    detail: { kind: "close-choice", message: "关闭窗口后", tray },
  })));
}
function button(name: string) { return [...document.querySelectorAll("button")].find(item => item.textContent === name)!; }

it("groups equal close choices separately from the cancel action", async () => {
  const api = bridge(); await mount(api); await showChoice();
  const choices = document.querySelector('[role="group"][aria-label="关闭方式"]')!;
  const minimize = button("最小化到托盘"), exit = button("退出应用"), cancel = button("取消关闭");
  expect(choices.contains(minimize)).toBe(true);
  expect(choices.contains(exit)).toBe(true);
  expect(choices.contains(cancel)).toBe(false);
  expect(minimize.className).toBe(exit.className);
  expect(choices.querySelector(".primary-button")).toBeNull();
  expect(cancel.classList.contains("desktop-close-cancel")).toBe(true);
  expect(document.activeElement).toBe(cancel);
  await act(async () => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
  expect(api.respond).toHaveBeenCalledWith("cancel");
});

it("disables both close choices and cancel while a native response is pending", async () => {
  const api = bridge(); await mount(api); await showChoice();
  let resolve: (value: object) => void = () => undefined;
  api.respond.mockImplementationOnce(() => new Promise(done => { resolve = done; }));
  await act(async () => button("最小化到托盘").click());
  expect(button("最小化到托盘").disabled).toBe(true);
  expect(button("退出应用").disabled).toBe(true);
  expect(button("取消关闭").disabled).toBe(true);
  await act(async () => { button("退出应用").click(); resolve({}); });
  expect(api.respond).toHaveBeenCalledTimes(1);
});

it("remembers a chosen action only when the checkbox is checked", async () => {
  const api = bridge(); await mount(api); await showChoice();
  expect(document.activeElement?.textContent).toBe("取消关闭");
  await act(async () => (document.querySelector('input[type="checkbox"]') as HTMLInputElement).click());
  await act(async () => button("最小化到托盘").click());
  expect(api.respond).toHaveBeenCalledWith("background", true);
  await showChoice();
  expect((document.querySelector('input[type="checkbox"]') as HTMLInputElement).checked).toBe(false);
  await act(async () => button("退出应用").click());
  expect(api.respond).toHaveBeenLastCalledWith("exit", false);
});
it("cancel does not save the checked remember option", async () => {
  const api = bridge(); await mount(api); await showChoice();
  await act(async () => (document.querySelector('input[type="checkbox"]') as HTMLInputElement).click());
  await act(async () => button("取消关闭").click());
  expect(api.respond).toHaveBeenCalledWith("cancel");
  expect(api.set_close_behavior).not.toHaveBeenCalled();
});
it("disables hiding when tray is unavailable and retains dialog on native failure", async () => {
  const api = bridge(); await mount(api); await showChoice(false);
  expect(button("最小化到托盘").disabled).toBe(true);
  api.respond.mockRejectedValueOnce(new Error("无法保存设置"));
  await act(async () => button("退出应用").click());
  expect(document.querySelector('[role="dialog"]')).not.toBeNull();
  expect(document.querySelector('[role="alert"]')?.textContent).toContain("无法保存设置");
});
it("does not dismiss the task confirmation opened during the exit response", async () => {
  const api = bridge(); await mount(api); await showChoice();
  api.respond.mockImplementationOnce(async () => {
    window.dispatchEvent(new CustomEvent("classroom-desktop-dialog", {
      detail: { kind: "exit-task", message: "仍有处理任务", tray: true },
    }));
    return {};
  });
  await act(async () => button("退出应用").click());
  expect(button("停止任务并退出")).toBeDefined();
});
it("routes the tray settings command to the settings page", async () => {
  const api = bridge(); await mount(api);
  await act(async () => window.dispatchEvent(new CustomEvent("classroom-desktop-dialog", {
    detail: { kind: "hotkey", message: "桌面设置" },
  })));
  expect(host!.querySelector('[data-location]')?.getAttribute("data-location")).toBe("/settings?section=shortcuts");
  expect(document.querySelector('[role="dialog"]')).toBeNull();
});
it("allows resetting the remembered choice in desktop settings", async () => {
  const api = bridge();
  api.describe.mockResolvedValue({ name: "课堂重点复习", tray: true, dialog: null, hotkey: "", close_behavior: "exit" } as Awaited<ReturnType<typeof api.describe>>);
  await mount(api, true);
  const select = document.querySelector('[aria-label="关闭窗口行为"]') as HTMLSelectElement;
  expect(select.value).toBe("exit");
  await act(async () => { select.value = "ask"; select.dispatchEvent(new Event("change", { bubbles: true })); });
  expect(api.set_close_behavior).toHaveBeenCalledWith("ask");
  expect(select.value).toBe("ask");
});
