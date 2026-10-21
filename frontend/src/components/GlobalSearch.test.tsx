import { defaultShortcuts, saveShortcuts } from "../shortcuts";
import { act, forwardRef, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { GlobalSearchButton, GlobalSearchProvider } from "./GlobalSearch";
import { getLibraryStatus, searchSubtitles } from "../api/client";
import type { SubtitleSearchHit, SubtitleSearchResult } from "../types";

vi.mock("../api/client", () => ({
  getLibraryStatus: vi.fn(), searchSubtitles: vi.fn(),
  getSearchPreviewUrl: (videoId: number, timeMs: number, libraryId: string) =>
    `/api/videos/${videoId}/preview-frame?time_ms=${timeMs}&library_id=${libraryId}`,
}));
vi.mock("./Motion", () => ({
  AnimatePresence: ({ children }: { children: ReactNode }) => children,
  useIsPresent: () => true,
  useEntryMotion: () => {},
  AnimatedOverlay: ({ children, className, onClick }: React.HTMLAttributes<HTMLDivElement>) => <div className={className} onClick={onClick}>{children}</div>,
  AnimatedPopup: forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement> & { duration?: number; distance?: number; exitDuration?: number }>(
    ({ duration, distance, exitDuration, ...props }, ref) => <div {...props} ref={ref} />),
}));

let container: HTMLDivElement, root: Root, client: QueryClient;
const hit = (videoId: number, segmentId: number, start = 1000): SubtitleSearchHit => ({
  video_id: videoId, segment_id: segmentId, start_ms: start, end_ms: start + 1000, video_name: `课程 ${videoId}`,
  filename: "lesson.mp4", video_status: "failed", duration_ms: 60000, folder_id: 1, folder_name: "算法课程",
  video_match_count: 2, text: "分治算法", snippet: "分治算法", snippet_parts: [{ text: "分治", match: true }, { text: "算法", match: false }],
});
function result(query = "分治", items = [hit(1, 1), hit(1, 2, 8000)], extra = {}): SubtitleSearchResult {
  return { query, items, library_id: "one", total: items.length, total_videos: 1, offset: 0, limit: 20, has_more: false, ...extra };
}
function Page() {
  const location = useLocation();
  const navigate = useNavigate();
  return <><GlobalSearchButton /><input aria-label="原有草稿" defaultValue="保留表单" /><output>{location.pathname}{location.search}</output>
    <button aria-label="查看视频2" onClick={() => navigate("/review/2")} />
    <button aria-label="查看视频3" onClick={() => navigate("/review/3")} />
    <button aria-label="返回视频库" onClick={() => navigate("/")} />
  </>;
}
const input = () => container.querySelector<HTMLInputElement>('[role="combobox"]')!;
const button = (label: string) => container.querySelector<HTMLButtonElement>(`[aria-label="${label}"]`)!;
function key(value: string, options: KeyboardEventInit = {}) { act(() => (document.activeElement ?? document).dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: value, ...options }))); }
function type(value: string) {
  act(() => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input(), value);
    input().dispatchEvent(new Event("input", { bubbles: true }));
  });
}
async function tick(ms = 260) { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); }
function open() { act(() => button("全局搜索字幕").click()); }

beforeEach(() => {
  const values = new Map<string, string>();
  vi.stubGlobal("localStorage", { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => values.set(key, value) });
  vi.useFakeTimers();
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  client.setQueryData(["library-status"], { data_root: "library-one", library_id: "one" });
  vi.mocked(getLibraryStatus).mockImplementation(async () => client.getQueryData(["library-status"])!);
  vi.mocked(searchSubtitles).mockResolvedValue(result());
  container = document.createElement("div"); document.body.append(container); root = createRoot(container);
  act(() => root.render(<QueryClientProvider client={client}><MemoryRouter><GlobalSearchProvider><Page /></GlobalSearchProvider></MemoryRouter></QueryClientProvider>));
});
afterEach(() => { act(() => root.unmount()); container.remove(); client.clear(); vi.useRealTimers(); vi.restoreAllMocks(); vi.clearAllMocks(); vi.unstubAllGlobals(); });

it("debounces, groups literal highlights, navigates by keyboard and retains the query and draft", async () => {
  open();
  expect(document.activeElement).toBe(input());
  type("分治"); await tick(249); expect(searchSubtitles).not.toHaveBeenCalled(); await tick(1);
  expect(container.querySelectorAll('[role="group"]')).toHaveLength(1);
  expect(container.querySelectorAll("mark")).toHaveLength(2);
  key("ArrowDown"); key("Enter"); await tick();
  expect(container.querySelector("output")?.textContent).toBe("/review/1?start_ms=8000&library_id=one");
  expect(container.querySelector<HTMLInputElement>('[aria-label="原有草稿"]')?.value).toBe("保留表单");
  open(); expect(input().value).toBe("分治"); await tick();
  expect(container.querySelectorAll('[role="option"]')).toHaveLength(2);
  expect(searchSubtitles).toHaveBeenCalledTimes(2);
  expect(vi.mocked(searchSubtitles).mock.calls[1][3]).toBe(1);
});

it("prioritizes the review video through all pages, separates other videos, and preserves keyboard ordering", async () => {
  act(() => button("查看视频2").click());
  expect(button("全局搜索字幕").textContent).toContain("本视频优先");
  expect(button("全局搜索字幕").title).toBe("搜索字幕 · 本视频优先");
  vi.mocked(searchSubtitles).mockResolvedValueOnce(result("分治", [hit(2, 1)], { total: 3, has_more: true }))
    .mockResolvedValueOnce(result("分治", [hit(2, 2, 8000), hit(1, 1)], { total: 3, offset: 20 }));
  open(); type("分治"); await tick();
  expect(input().placeholder).toContain("本视频");
  expect(vi.mocked(searchSubtitles).mock.calls[0][3]).toBe(2);
  expect(container.querySelector(".global-search-section-label")?.textContent).toBe("本视频2 处匹配");
  expect(container.querySelector('[role="separator"]')).toBeNull();
  act(() => container.querySelector<HTMLButtonElement>(".global-search-more")!.click()); await tick();
  expect(vi.mocked(searchSubtitles).mock.calls[1][3]).toBe(2);
  expect(container.querySelector('[role="separator"]')?.textContent).toBe("其他视频");
  expect([...container.querySelectorAll('[role="group"]')].map(node => node.getAttribute("aria-labelledby")))
    .toEqual(["search-video-2", "search-video-1"]);
  key("ArrowDown");
  expect(input().getAttribute("aria-activedescendant")).toBe("search-hit-2:2");
  key("Enter"); await tick();
  expect(container.querySelector("output")?.textContent).toBe("/review/2?start_ms=8000&library_id=one");
  const calls = vi.mocked(searchSubtitles).mock.calls.length;
  open(); await tick();
  expect(vi.mocked(searchSubtitles).mock.calls).toHaveLength(calls);
});

it("shows other-video matches when the review video has none and restores global search outside review", async () => {
  act(() => button("查看视频2").click());
  open(); type("分治"); await tick();
  expect(container.querySelector(".global-search-local-empty")?.textContent).toBe("本视频暂无匹配字幕");
  expect(container.querySelector('[role="separator"]')).not.toBeNull();
  expect(container.querySelectorAll('[role="option"]')).toHaveLength(2);
  key("Escape"); await tick();
  act(() => button("返回视频库").click());
  expect(button("全局搜索字幕").textContent).toBe("搜索所有视频的字幕");
  open(); expect(input().value).toBe("分治"); await tick();
  expect(vi.mocked(searchSubtitles).mock.calls.at(-1)?.[3]).toBeUndefined();
  expect(container.querySelector(".global-search-section-label")).toBeNull();
});

it("aborts stale video searches when navigating between review videos", async () => {
  let resolveOld!: (value: SubtitleSearchResult) => void;
  vi.mocked(searchSubtitles).mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve; }))
    .mockResolvedValueOnce(result("分治", [hit(3, 1)]));
  act(() => button("查看视频2").click());
  open(); type("分治"); await tick();
  const signal = vi.mocked(searchSubtitles).mock.calls[0][2]!;
  act(() => button("查看视频3").click()); await tick();
  expect(signal.aborted).toBe(true);
  expect(vi.mocked(searchSubtitles).mock.calls[1][3]).toBe(3);
  await act(async () => resolveOld(result("分治", [hit(2, 1)])));
  expect(container.querySelectorAll('[role="option"]')).toHaveLength(1);
  expect(container.querySelector('[role="option"]')?.id).toBe("search-hit-3:1");
});

it("does not capture Ctrl+K or Cmd+K by default and keeps click-to-search", () => {
  const trigger = button("全局搜索字幕");
  expect(trigger.querySelector("kbd")).toBeNull();
  expect(trigger.hasAttribute("aria-keyshortcuts")).toBe(false);
  expect(trigger.title).toBe("搜索所有视频的字幕");
  for (const modifiers of [{ ctrlKey: true }, { metaKey: true }]) {
    const event = new KeyboardEvent("keydown", { key: "k", bubbles: true, cancelable: true, ...modifiers });
    act(() => document.dispatchEvent(event));
    expect(event.defaultPrevented).toBe(false);
    expect(container.querySelector('[role="dialog"]')).toBeNull();
  }
  open();
  expect(container.querySelector('[role="dialog"]')).not.toBeNull();
  expect(document.activeElement).toBe(input());
});

it("does not search intermediate IME text, empty input or overlong queries", async () => {
  open();
  act(() => input().dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true })));
  type("fen"); await tick(300); expect(searchSubtitles).not.toHaveBeenCalled();
  type("分治");
  act(() => input().dispatchEvent(new CompositionEvent("compositionend", { bubbles: true })));
  await tick(); expect(searchSubtitles).toHaveBeenCalledTimes(1);
  type("   "); await tick(); expect(container.querySelectorAll('[role="option"]')).toHaveLength(0);
  type("字".repeat(121)); await tick(); expect(searchSubtitles).toHaveBeenCalledTimes(1);
  expect(container.textContent).toContain("关键词最多 120 个字符");
});

it("discards late responses even when a transport ignores abort", async () => {
  let resolveOld!: (value: SubtitleSearchResult) => void;
  vi.mocked(searchSubtitles).mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; }))
    .mockResolvedValueOnce(result("递归", [hit(2, 9)]));
  open(); type("分治"); await tick(); const signal = vi.mocked(searchSubtitles).mock.calls[0][2]!;
  type("递归"); await tick(); expect(signal.aborted).toBe(true);
  await act(async () => resolveOld(result()));
  expect(container.querySelectorAll('[role="option"]')).toHaveLength(1);
  expect(container.textContent).toContain("课程 2"); expect(container.textContent).not.toContain("课程 1");
});

it("merges split video pages, deduplicates segments, and resets pagination on refresh", async () => {
  vi.mocked(searchSubtitles).mockResolvedValueOnce(result("分治", [hit(1, 1)], { has_more: true, total: 3 }))
    .mockResolvedValueOnce(result("分治", [hit(1, 1), hit(1, 2), hit(2, 1)], { offset: 20, total: 3 }));
  open(); type("分治"); await tick();
  act(() => container.querySelector<HTMLButtonElement>(".global-search-more")!.click()); await tick();
  expect(vi.mocked(searchSubtitles).mock.calls[1][1]).toBe(20);
  expect(container.querySelectorAll('[role="option"]')).toHaveLength(3);
  expect(container.querySelectorAll('[role="group"]')).toHaveLength(2);
  act(() => container.querySelector<HTMLButtonElement>(".global-search-summary button")!.click()); await tick();
  expect(vi.mocked(searchSubtitles).mock.calls[2][1]).toBe(0);
});

it("clears on library identity change without remounting the page and rejects cross-library pages", async () => {
  const draft = container.querySelector('[aria-label="原有草稿"]');
  open(); type("分治"); await tick();
  act(() => client.setQueryData(["library-status"], { data_root: "library-two", library_id: "two" })); await tick(1);
  expect(container.querySelector('[role="dialog"]')).toBeNull();
  expect(container.querySelector('[aria-label="原有草稿"]')).toBe(draft);
  open(); expect(input().value).toBe(""); type("分治"); await tick();
  expect(container.querySelectorAll('[role="option"]')).toHaveLength(0);
  expect(container.textContent).toContain("视频库连接已变化");
});

it("closes with Escape, restores focus and retries failed searches", async () => {
  vi.mocked(searchSubtitles).mockRejectedValueOnce(new Error("连接中断"));
  button("全局搜索字幕").focus(); open(); type("分治"); await tick();
  expect(container.querySelector('[role="alert"]')?.textContent).toContain("连接中断");
  act(() => container.querySelector<HTMLButtonElement>(".global-search-error button")!.click()); await tick();
  expect(container.querySelectorAll('[role="option"]')).toHaveLength(2);
  key("Escape"); await tick(); expect(document.activeElement).toBe(button("全局搜索字幕"));
  expect(document.body.style.overflow).not.toBe("hidden");
});

function hover(row: Element, pointerType = "mouse") {
  const event = new Event("pointermove", { bubbles: true });
  Object.defineProperty(event, "pointerType", { value: pointerType });
  act(() => row.dispatchEvent(event));
}

function previewRects() {
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    const top = this.classList.contains("global-search-scroll") ? 250 : this.classList.contains("global-search-hit") ? 420 : 80;
    const bottom = this.classList.contains("global-search-hit") ? 470 : 700;
    return { left: 180, right: 900, top, bottom, x: 180, y: top, width: 720, height: bottom - top, toJSON: () => ({}) };
  });
}

it("only requests the hovered timestamp after a delay, switches cleanly, and clears on query changes", async () => {
  previewRects();
  open(); type("分治"); await tick();
  const rows = container.querySelectorAll('[role="option"]');
  hover(rows[0]); await tick(199);
  expect(container.querySelector(".global-search-preview img")).toBeNull();
  hover(rows[1]); await tick(200);
  const image = container.querySelector<HTMLImageElement>(".global-search-preview img")!;
  expect(image.getAttribute("src")).toContain("time_ms=8000&library_id=one");
  act(() => image.dispatchEvent(new Event("load")));
  expect(image.classList.contains("is-ready")).toBe(true);
  hover(rows[0]);
  expect(container.querySelector(".global-search-preview")).toBeNull();
  await tick(200);
  expect(container.querySelector(".global-search-preview img")?.getAttribute("src")).toContain("time_ms=1000");
  type("递归");
  expect(container.querySelector(".global-search-preview")).toBeNull();
});

it("previews keyboard selections without moving focus or navigating and falls back on frame errors", async () => {
  previewRects();
  open(); type("分治"); await tick();
  key("ArrowDown"); await tick(200);
  const image = container.querySelector<HTMLImageElement>(".global-search-preview img")!;
  expect(image.getAttribute("src")).toContain("time_ms=8000");
  expect(document.activeElement).toBe(input());
  expect(container.querySelector("output")?.textContent).toBe("/");
  act(() => image.dispatchEvent(new Event("error")));
  expect(container.textContent).toContain("画面暂不可用，仍可点击字幕跳转");
  key("Enter"); await tick();
  expect(container.querySelector("output")?.textContent).toContain("/review/1?start_ms=8000");
  expect(container.querySelector(".global-search-preview")).toBeNull();
});

it("does not request preview images on touch movement", async () => {
  open(); type("分治"); await tick();
  hover(container.querySelectorAll('[role="option"]')[1], "touch");
  await tick(300);
  expect(container.querySelector(".global-search-preview")).toBeNull();
  expect(container.querySelector('[role="option"][aria-selected=true]')?.id).toBe("search-hit-1:1");
});

it("removes the hover preview on scroll and closes it on a library identity change", async () => {
  previewRects();
  open(); type("分治"); await tick();
  hover(container.querySelector('[role="option"]')!); await tick(200);
  expect(container.querySelector(".global-search-preview img")).not.toBeNull();
  act(() => window.dispatchEvent(new Event("scroll")));
  expect(container.querySelector(".global-search-preview")).toBeNull();
  hover(container.querySelector('[role="option"]')!); await tick(200);
  expect(container.querySelector(".global-search-preview img")).not.toBeNull();
  act(() => client.setQueryData(["library-status"], { data_root: "library-two", library_id: "two" })); await tick(1);
  expect(container.querySelector(".global-search-preview")).toBeNull();
  expect(container.querySelector('[role="dialog"]')).toBeNull();
});


it("switches search bindings and its hint immediately while single keys never interrupt text entry", async () => {
  const map = defaultShortcuts(); map.search = ["Q"];
  act(() => saveShortcuts(map));
  expect(button("全局搜索字幕").title).toContain("Q");
  key("k", { ctrlKey: true }); expect(container.querySelector('[role="dialog"]')).toBeNull();
  const draft = container.querySelector<HTMLInputElement>('[aria-label="原有草稿"]')!;
  act(() => draft.focus()); key("q"); expect(container.querySelector('[role="dialog"]')).toBeNull();
  act(() => button("全局搜索字幕").focus()); key("q"); expect(container.querySelector('[role="dialog"]')).not.toBeNull();
});
