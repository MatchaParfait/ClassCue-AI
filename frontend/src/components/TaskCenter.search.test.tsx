import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, useRef } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { defaultShortcuts, saveShortcuts } from "../shortcuts";
import { GlobalSearchProvider } from "./GlobalSearch";
import { TopBar } from "./TopBar";
import * as api from "../api/client";

vi.mock("../api/client", async original => ({
  ...await original<typeof import("../api/client")>(),
  getLibraryStatus: vi.fn(), getRecentJobs: vi.fn(), getJob: vi.fn(), searchSubtitles: vi.fn(),
}));
vi.mock("./Announcements", () => ({ Announcements: () => null }));
vi.mock("./ReviewTabs", () => ({ ReviewTabs: () => null }));

let container: HTMLDivElement, root: Root, client: QueryClient;
beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.resetAllMocks();
  const storage = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => storage.set(key, value),
  });
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: true })));
  const shortcuts = defaultShortcuts();
  shortcuts.search = ["Ctrl+F"];
  saveShortcuts(shortcuts);
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const library = { library_id: "synthetic-library", data_root: "", database_path: "", temp_audio_dir: "", video_count: 0 };
  client.setQueryData(["library-status"], library);
  vi.mocked(api.getLibraryStatus).mockResolvedValue(library);
  vi.mocked(api.searchSubtitles).mockImplementation(async query => ({
    query, library_id: library.library_id, total: 0, total_videos: 0, offset: 0, limit: 20, has_more: false, items: [],
  }));
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount());
  client.clear();
  container.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function Review() {
  const background = useRef<HTMLDivElement>(null);
  return <div ref={background}><TopBar demo backgroundRef={background} /><video /></div>;
}
function key(value: string, options: KeyboardEventInit = {}) {
  act(() => (document.activeElement ?? document).dispatchEvent(
    new KeyboardEvent("keydown", { key: value, bubbles: true, cancelable: true, ...options })));
}
async function settle(check: () => void) {
  await vi.waitFor(async () => {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 5)); });
    check();
  });
}

it("keeps search focus, Tab and Escape above the portaled task drawer and restores the drawer afterward", async () => {
  await act(async () => root.render(<QueryClientProvider client={client}>
    <MemoryRouter initialEntries={["/review/1"]}><GlobalSearchProvider><Review /></GlobalSearchProvider></MemoryRouter>
  </QueryClientProvider>));
  const taskTrigger = container.querySelector<HTMLButtonElement>('[aria-label="查看任务详情"]')!;
  act(() => { taskTrigger.focus(); taskTrigger.click(); });
  const taskClose = document.querySelector<HTMLButtonElement>('.workbench-panel [aria-label="关闭面板"]')!;
  expect(document.activeElement).toBe(taskClose);
  const player = container.querySelector("video")!;
  player.currentTime = 35;

  key("f", { ctrlKey: true });
  const search = container.querySelector<HTMLElement>(".global-search-dialog")!;
  const input = search.querySelector<HTMLInputElement>('[role="combobox"]')!;
  expect(search).not.toBeNull();
  expect(document.activeElement).toBe(input);
  // The task portal still follows search in DOM order, unlike their opening order.
  expect([...document.querySelectorAll('[role="dialog"]')].at(-1)?.contains(taskClose)).toBe(true);
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "合成关键词");
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await settle(() => expect(api.searchSubtitles).toHaveBeenCalledWith("合成关键词", 0, expect.any(AbortSignal), 1));
  expect(document.activeElement).toBe(input);
  const first = search.querySelector<HTMLButtonElement>('[aria-label="关闭全局搜索"]')!;
  const last = search.querySelector<HTMLButtonElement>(".global-search-refresh")!;
  act(() => last.focus());
  key("Tab");
  expect(document.activeElement).toBe(first);
  key("Tab", { shiftKey: true });
  expect(document.activeElement).toBe(last);

  key("Escape");
  await settle(() => {
    expect(container.querySelector(".global-search-dialog")).toBeNull();
    expect(document.activeElement).toBe(taskClose);
  });
  expect(taskTrigger.getAttribute("aria-expanded")).toBe("true");
  expect(document.body.style.overflow).toBe("hidden");
  expect(player.currentTime).toBe(35);
  key("Escape");
  await settle(() => expect(taskTrigger.getAttribute("aria-expanded")).toBe("false"));
  expect(document.activeElement).toBe(taskTrigger);
  expect(document.body.style.overflow).not.toBe("hidden");
  expect(api.getRecentJobs).not.toHaveBeenCalled();
  expect(api.getJob).not.toHaveBeenCalled();
});
