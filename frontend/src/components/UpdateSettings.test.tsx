import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import { UpdateSettings } from "./UpdateSettings";
import { Announcements } from "./Announcements";
import { safeExternal, type UpdateStatus } from "../api/updates";

let root: Root | undefined, host: HTMLDivElement | undefined, client: QueryClient;
const status = (): UpdateStatus => ({
  version: "0.2.0", repository: "owner/project", configured: true, portable: true, busy: false,
  checking: false, downloading: false, downloaded_bytes: 0, last_checked: "2026-10-20T00:00:00Z",
  error: null, download_error: null, compatible: true, ready: true,
  preferences: { auto_check: true, auto_download: false, show_announcements: true },
  announcements: [{ id: "news", title: "维护公告", body: '<img src=x onerror="alert(1)">', published_at: "2026-10-20T00:00:00Z", level: "important", read: false }],
  release: { version: "0.2.1", size: 2048, notes: "修复与改进", url: "https://github.com/owner/project/releases/download/v0.2.1/update.zip" },
  install: null,
});
function bridge() {
  return {
    describe: vi.fn(async () => ({ name: "app", tray: true, dialog: null, hotkey: "" })),
    respond: vi.fn(), retry: vi.fn(), set_hotkey: vi.fn(), set_close_behavior: vi.fn(),
    hide_to_tray: vi.fn(), exit_application: vi.fn(), open_logs: vi.fn(),
    set_unsaved_changes: vi.fn(async () => ({})), install_update: vi.fn(async () => ({})),
  };
}
async function mount(value = status(), newsOnly = false, native = true) {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const api = bridge();
  if (native) window.pywebview = { api };
  vi.stubGlobal("fetch", vi.fn(async (_url: string, options?: RequestInit) => {
    if (_url.endsWith("/read")) {
      const ids = JSON.parse(String(options?.body)).ids;
      value.announcements.forEach(item => { if (ids.includes(item.id)) item.read = true; });
    }
    return new Response(JSON.stringify(value), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
  host = document.createElement("div"); document.body.append(host);
  root = createRoot(host);
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  await act(async () => {
    root!.render(<QueryClientProvider client={client}>{newsOnly ? <Announcements /> : <UpdateSettings active />}</QueryClientProvider>);
    await new Promise(resolve => setTimeout(resolve, 20));
  });
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)); });
  return api;
}
function button(label: string) {
  return [...document.querySelectorAll<HTMLButtonElement>("button")].find(item => item.textContent?.trim() === label)!;
}
afterEach(() => {
  act(() => root?.unmount()); root = undefined;
  host?.remove(); client?.clear(); delete window.pywebview;
  vi.unstubAllGlobals(); vi.restoreAllMocks();
});

it("shows release notes and safe defaults without automatically installing", async () => {
  const api = await mount();
  expect(host!.textContent).toContain("v0.2.0");
  expect(host!.textContent).toContain("修复与改进");
  const checks = host!.querySelectorAll<HTMLInputElement>('input[type="checkbox"]');
  expect([...checks].map(item => item.checked)).toEqual([true, false, true]);
  expect(api.install_update).not.toHaveBeenCalled();
});

it("defaults install confirmation to Cancel and Escape restores focus", async () => {
  const api = await mount();
  const opener = button("安装并重启"); opener.focus();
  await act(async () => opener.click());
  expect(document.activeElement?.textContent).toBe("取消");
  expect(document.querySelector('[role="dialog"]')?.textContent).toContain("数据库备份");
  await act(async () => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
  expect(document.querySelector('[role="dialog"]')).toBeNull();
  expect(document.activeElement).toBe(opener);
  expect(api.install_update).not.toHaveBeenCalled();
});

it("uses the native bridge only after explicit installation confirmation", async () => {
  const api = await mount();
  await act(async () => button("安装并重启").click());
  const confirm = [...document.querySelectorAll<HTMLButtonElement>('[role="dialog"] button')].find(item => item.textContent?.includes("安装并重启"))!;
  await act(async () => confirm.click());
  expect(api.set_unsaved_changes).toHaveBeenCalledWith(false);
  expect(api.install_update).toHaveBeenCalledTimes(1);
});

it("keeps install errors visible and allows a retry", async () => {
  const api = await mount();
  api.install_update.mockRejectedValueOnce(new Error("任务仍在运行"));
  await act(async () => button("安装并重启").click());
  const confirm = [...document.querySelectorAll<HTMLButtonElement>('[role="dialog"] button')].find(item => item.textContent?.includes("安装并重启"))!;
  await act(async () => confirm.click());
  expect(document.querySelector('[role="dialog"]')?.textContent).toContain("任务仍在运行");
  expect(confirm.disabled).toBe(false);
});

it("blocks busy or browser-only installation", async () => {
  const value = status(); value.busy = true;
  await mount(value, false, false);
  expect(button("安装并重启").disabled).toBe(true);
  expect(host!.textContent).toContain("独立桌面窗口");
});

it("never offers in-place updates for source installs", async () => {
  const value = status(); value.portable = false; value.ready = false;
  await mount(value);
  expect(host!.textContent).toContain("源码运行");
  expect(button("安装并重启")).toBeUndefined();
  expect(host!.querySelector('a[href$="update.zip"]')).not.toBeNull();
});

it("renders signed announcements as text, marks read and closes with Escape", async () => {
  await mount(status(), true);
  expect(host!.querySelector(".announcement-count")?.textContent).toBe("1");
  await act(async () => button("公告1").click());
  const heading = document.querySelector<HTMLButtonElement>(".announcement-heading")!;
  await act(async () => { heading.click(); await new Promise(resolve => setTimeout(resolve, 10)); });
  expect(document.querySelector(".announcement-body")?.textContent).toContain("<img");
  expect(document.querySelector(".announcement-body img")).toBeNull();
  expect(fetch).toHaveBeenCalledWith("/api/updates/read", expect.objectContaining({ body: '{"ids":["news"]}' }));
  await act(async () => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
  expect(document.querySelector('[role="dialog"]')).toBeNull();
});

it("hides unread reminders without hiding the announcements entry", async () => {
  const value = status(); value.preferences.show_announcements = false;
  await mount(value, true);
  expect(button("公告")).toBeDefined();
  expect(host!.querySelector(".announcement-count")).toBeNull();
});

it("rejects executable links even before rendering", () => {
  expect(safeExternal("javascript:alert(1)")).toBeUndefined();
  expect(safeExternal("http://example.org")).toBeUndefined();
  expect(safeExternal("https://user:secret@example.org")).toBeUndefined();
  expect(safeExternal("https://github.com/owner/project")).toBe("https://github.com/owner/project");
});
