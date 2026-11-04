import { act, StrictMode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { playbackProgressKey, savePlaybackProgress } from "../playbackProgress";
import { ReviewTabsProvider, useReviewTabs } from "./ReviewTabs";
import { VideoWatchProgress } from "./VideoWatchProgress";

let container: HTMLDivElement;
let root: Root;
let session: Map<string, number> | undefined;
const key = playbackProgressKey("library-a", 1);

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const storage = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (name: string) => storage.get(name) ?? null,
    setItem: (name: string, value: string) => storage.set(name, value),
    clear: () => storage.clear(),
  });
  session = undefined;
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function Progress({ libraryId = "library-a", durationMs = 10_000 }: { libraryId?: string; durationMs?: number }) {
  session = useReviewTabs()?.positions;
  return <VideoWatchProgress libraryId={libraryId} videoId={1} durationMs={durationMs} />;
}

function render(libraryId = "library-a", durationMs = 10_000) {
  act(() => root.render(<StrictMode><Progress libraryId={libraryId} durationMs={durationMs} /></StrictMode>));
}

function bar() {
  return container.querySelector<HTMLElement>('[role="progressbar"]')!;
}

it("reads existing saved positions and shows only a track and position symbol", () => {
  localStorage.setItem(key, "4.5");
  render();
  expect(bar().getAttribute("aria-valuenow")).toBe("45");
  expect(bar().style.getPropertyValue("--watch-progress")).toBe("45%");
  expect(bar().title).toBe("上次看到 0:04 / 0:10");
  expect(container.querySelector(".video-watch-position")).not.toBeNull();
  expect(container.textContent).toBe("");
});

it("updates immediately on save, including when playback moves backward", () => {
  render();
  expect(bar().title).toBe("未开始");
  expect(container.querySelector(".video-watch-position")).toBeNull();
  act(() => savePlaybackProgress(key, 8));
  expect(bar().getAttribute("aria-valuenow")).toBe("80");
  act(() => savePlaybackProgress(key, 2));
  expect(bar().getAttribute("aria-valuenow")).toBe("20");
  act(() => savePlaybackProgress(key, 0));
  expect(bar().title).toBe("未开始");
  expect(container.querySelector(".video-watch-position")).toBeNull();
});

it.each(["-5", "NaN", "Infinity", "invalid"])("treats invalid saved position %s as empty", value => {
  localStorage.setItem(key, value);
  render();
  expect(bar().getAttribute("aria-valuenow")).toBe("0");
  expect(container.querySelector(".video-watch-position")).toBeNull();
});

it("clamps positions past the end without overflowing the track", () => {
  localStorage.setItem(key, "15");
  render();
  expect(bar().getAttribute("aria-valuenow")).toBe("100");
  expect(bar().style.getPropertyValue("--watch-progress")).toBe("100%");
  expect(bar().title).toBe("上次看到 0:10 / 0:10");
});

it("isolates videos with the same id in different libraries", () => {
  localStorage.setItem(key, "4");
  localStorage.setItem(playbackProgressKey("library-b", 1), "7");
  render();
  expect(bar().getAttribute("aria-valuenow")).toBe("40");
  render("library-b");
  expect(bar().getAttribute("aria-valuenow")).toBe("70");
  act(() => savePlaybackProgress(key, 2));
  expect(bar().getAttribute("aria-valuenow")).toBe("70");
});

it.each([0, -1, NaN, Infinity])("does not invent a percentage for unknown duration %s", duration => {
  localStorage.setItem(key, "4");
  render("library-a", duration);
  expect(bar().hasAttribute("aria-valuenow")).toBe(false);
  expect(bar().title).toBe("上次看到 0:04（视频时长未知）");
  expect(container.querySelector(".video-watch-position")).toBeNull();
});

it("uses session positions when browser storage is unavailable", () => {
  vi.spyOn(localStorage, "getItem").mockImplementation(() => { throw new Error("Storage unavailable"); });
  vi.spyOn(localStorage, "setItem").mockImplementation(() => { throw new Error("Storage unavailable"); });
  act(() => root.render(<StrictMode><ReviewTabsProvider><Progress /></ReviewTabsProvider></StrictMode>));
  act(() => savePlaybackProgress(key, 6, session));
  expect(bar().getAttribute("aria-valuenow")).toBe("60");
  expect(session?.get(key)).toBe(6);
});

it("refreshes session snapshots for other-window storage writes and clears", () => {
  act(() => root.render(<ReviewTabsProvider><Progress /></ReviewTabsProvider>));
  act(() => savePlaybackProgress(key, 6, session));
  localStorage.setItem(key, "3");
  act(() => window.dispatchEvent(new StorageEvent("storage", { key })));
  expect(bar().getAttribute("aria-valuenow")).toBe("30");
  localStorage.clear();
  act(() => window.dispatchEvent(new StorageEvent("storage", { key: null })));
  expect(bar().getAttribute("aria-valuenow")).toBe("0");
});
