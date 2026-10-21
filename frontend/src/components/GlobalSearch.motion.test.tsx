import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { GlobalSearchButton, GlobalSearchProvider } from "./GlobalSearch";
import { waitForExit } from "../test/waitForExit";

vi.mock("../api/client", () => ({
  getLibraryStatus: vi.fn(async () => ({ data_root: "test", library_id: "test" })),
  searchSubtitles: vi.fn(async () => ({
    query: "课程", library_id: "test", total: 1, total_videos: 1,
    offset: 0, limit: 20, has_more: false,
    items: [{ video_id: 7, segment_id: 1, start_ms: 12000, end_ms: 15000,
      video_name: "课程", text: "课程内容", snippet_parts: [{ text: "课程内容", match: true }], video_match_count: 1 }],
  })),
}));

let container: HTMLDivElement, root: Root, client: QueryClient;
beforeEach(async () => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  function Page() {
    const location = useLocation();
    return <><GlobalSearchButton /><output>{location.pathname}{location.search}</output><input aria-label="原有草稿" defaultValue="preserved" /></>;
  }
  await act(async () => root.render(<QueryClientProvider client={client}>
    <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <GlobalSearchProvider><Page /></GlobalSearchProvider>
    </MemoryRouter>
  </QueryClientProvider>));
});
afterEach(() => {
  act(() => { root.unmount(); client.clear(); });
  container.remove();
  vi.unstubAllGlobals();
});

const open = () => act(() => container.querySelector<HTMLButtonElement>(".global-search-trigger")!.click());

it("navigates and releases the focus trap before the search exit finishes", async () => {
  const draft = container.querySelector<HTMLInputElement>('[aria-label="原有草稿"]')!;
  draft.value = "unsaved edit";
  open();
  act(() => {
    const input = container.querySelector<HTMLInputElement>('[role="combobox"]')!;
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "课程");
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await vi.waitFor(async () => {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)); });
    expect(container.querySelector('[role="option"]')).not.toBeNull();
  });
  act(() => container.querySelector<HTMLElement>('[role="option"]')!.click());
  expect(container.querySelector("output")!.textContent).toBe("/review/7?start_ms=12000&library_id=test");
  expect(container.querySelector(".global-search-dialog[data-exiting]")).not.toBeNull();
  expect(container.querySelector<HTMLElement>(".search-page-background")!.inert).toBe(false);
  expect(document.body.style.overflow).not.toBe("hidden");
  expect(container.querySelector('[aria-label="原有草稿"]')).toBe(draft);
  expect(draft.value).toBe("unsaved edit");
  await waitForExit(container, ".global-search-dialog");
});

it("can reopen during exit without inheriting an inert dialog or a stale close", async () => {
  open();
  act(() => container.querySelector<HTMLButtonElement>('[aria-label="关闭全局搜索"]')!.click());
  expect(container.querySelector(".global-search-dialog[data-exiting]")).not.toBeNull();
  open();
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 250)); });
  const dialog = container.querySelector<HTMLElement>(".global-search-dialog")!;
  expect(dialog).not.toBeNull();
  expect(dialog.inert).toBe(false);
  expect(dialog.closest("[inert]")).toBeNull();
  expect(container.querySelector<HTMLElement>(".search-page-background")!.inert).toBe(true);
  expect(document.activeElement).toBe(dialog.querySelector("input"));
});
