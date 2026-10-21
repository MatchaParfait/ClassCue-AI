// Polling-clock tests run in their own module so fake RAF/time cannot leak
// into tests exercising real Motion enter/exit completion.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { focusManager } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { getLibrary, getVideos, moveVideoToFolder, reorderLibrary, processVideo, processSelectedVideos, getAsrSelection, getAiSettings, type LibraryResponse } from "./api/client";
import type { Video } from "./types";

vi.mock("./api/client", async (importOriginal) => ({
  ...await importOriginal<typeof import("./api/client")>(),
  getLibrary: vi.fn(),
  getVideos: vi.fn(),
  moveVideoToFolder: vi.fn(),
  reorderLibrary: vi.fn(),
  getAsrSelection: vi.fn(),
  getAiSettings: vi.fn(),
  processVideo: vi.fn(),
  processSelectedVideos: vi.fn(),
  getJob: vi.fn(async (id) => ({ id, status: "queued", progress: 0 })),
  getVideo: vi.fn(async (id: number) => ({ value: videos.find((video) => video.id === id), source: "api" })),
  getSubtitles: vi.fn(async () => ({ value: [], source: "api" })),
  getMarkers: vi.fn(async () => ({ value: [], source: "api" })),
  getLibraryStatus: vi.fn(async () => ({ data_root: "", database_path: "", temp_audio_dir: "", video_count: 2 })),
  getLatestBatch: vi.fn(async () => null),
  getPausedJobs: vi.fn(async () => []),
}));

vi.mock("./components/VideoPanel", () => ({ VideoPanel: () => <div /> }));

const folder = { id: 7, name: "Algebra", created_at: "2026-10-14T12:00:00" };
let videos: Video[];
let container: HTMLDivElement;
let root: Root;

function libraryResponse(parentId: number | null = null): LibraryResponse {
  return {
    folder: parentId === null ? null : folder, parent_id: parentId, sort: "manual", order: "asc",
    items: [
      ...(parentId === null ? [{ type: "folder" as const, folder }] : []),
      ...videos.filter((video) => video.folder_id === parentId).map((video) => ({ type: "video" as const, video: { ...video } })),
    ],
  };
}

async function flushUpdates() {
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
}

beforeEach(async () => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.clearAllMocks();
  window.sessionStorage.clear();
  vi.spyOn(window, "scrollTo").mockImplementation(() => {});
  const storage = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => storage.set(key, value),
  });
  videos = [1, 2].map((id) => ({
    id, filename: `lesson-${id}.mp4`, display_name: `Lesson ${id}`, video_path: `lesson-${id}.mp4`,
    duration_ms: 10000, status: "review_ready", folder_id: null,
  }));
  vi.mocked(getVideos).mockImplementation(async () => videos.map((video) => ({ ...video })));
  vi.mocked(getLibrary).mockImplementation(async (id) => libraryResponse(id ?? null));
  vi.mocked(moveVideoToFolder).mockReset().mockImplementation(async (id, folderId) => {
    videos = videos.map((video) => video.id === id ? { ...video, folder_id: folderId } : video);
    return { ...videos.find((video) => video.id === id)! };
  });
  vi.mocked(reorderLibrary).mockImplementation(async () => libraryResponse());
  vi.mocked(getAsrSelection).mockResolvedValue({ id: "whisper-turbo", name: "Whisper", engine: "whisper", selected: true, downloaded: true, cache_size_bytes: 0 });
  vi.mocked(getAiSettings).mockResolvedValue({ provider: "deepseek", base_url: "", model: "test-model", has_api_key: false,
    output_tokens: 2048, token_parameter: "max_tokens", json_mode: false, timeout_seconds: 60, max_retries: 2 });
  vi.mocked(processVideo).mockReset().mockResolvedValue({ id: "single", job_id: "single", status: "queued" });
  vi.mocked(processSelectedVideos).mockReset().mockResolvedValue({ job_id: "selected", status: "queued", total: 2 });
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: false })));
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
    x: 0, y: 0, left: 0, top: 0, right: 400, bottom: 100, width: 400, height: 100, toJSON: () => ({}),
  });
  window.history.replaceState({}, "", "/");
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => root.render(<App />));
  await vi.waitFor(async () => {
    await flushUpdates();
    expect(container.querySelectorAll(".library-card")).toHaveLength(3);
  });
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  focusManager.setFocused(undefined);
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function card(key: string) {
  const element = container.querySelector<HTMLElement>(`[data-library-key="${key}"]`);
  expect(element).not.toBeNull();
  return element!;
}

function dragEvent(type: string, target: Element, clientX = 200, relatedTarget: EventTarget | null = null, clientY = 50) {
  const event = new MouseEvent(type, { clientX, clientY, relatedTarget, bubbles: true, cancelable: true });
  Object.defineProperty(event, "dataTransfer", { value: { effectAllowed: "uninitialized", dropEffect: "none" } });
  act(() => target.dispatchEvent(event));
  return event;
}

function beginVideoDrag() {
  dragEvent("dragstart", card("video-1").querySelector(".library-card-body")!);
}

async function advanceSyncClock(milliseconds = 0) {
  await act(async () => { await vi.advanceTimersByTimeAsync(milliseconds); });
  for (let index = 0; index < 3; index++) {
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  }
}

async function mountWithSyncClock() {
  act(() => root.unmount());
  vi.useFakeTimers();
  focusManager.setFocused(true);
  root = createRoot(container);
  await act(async () => root.render(<App />));
  await advanceSyncClock();
  vi.mocked(getVideos).mockClear();
  vi.mocked(getLibrary).mockClear();
}

describe("library status synchronization", () => {
  it("shows zero counts initially and updates the three color counts on the 15 second sync", async () => {
    await mountWithSyncClock();
    expect(card("video-1").querySelector(".video-marker-counts")?.textContent).toBe("000");
    videos[0].marker_importance_counts = { standard: 12, important: 3, critical: 2 };
    await advanceSyncClock(15_000);
    await advanceSyncClock(1);
    const counts = card("video-1").querySelector(".video-marker-counts")!;
    expect([...counts.querySelectorAll(".video-marker-count")].map((element) => element.textContent)).toEqual(["12", "3", "2"]);
    expect([...counts.querySelectorAll(".video-marker-count")].map((element) => element.getAttribute("aria-label"))).toEqual(["白色标准：12 个", "蓝色重要：3 个", "红色很重要：2 个"]);
    expect(card("video-2").querySelector(".video-marker-counts")?.textContent).toBe("000");
    expect(getVideos).toHaveBeenCalledTimes(1);
  });

  it("shows the same color counts on videos inside the folder window", async () => {
    videos[0].folder_id = 7;
    videos[0].marker_importance_counts = { standard: 4, important: 2, critical: 1 };
    await mountWithSyncClock();
    click(container.querySelector(".folder-card-link"));
    await advanceSyncClock();
    const counts = container.querySelector(".folder-window .video-marker-counts")!;
    expect([...counts.querySelectorAll(".video-marker-count")].map((element) => element.textContent)).toEqual(["4", "2", "1"]);
  });

  it("updates a completed video after 15 seconds without waiting for other videos", async () => {
    videos[0].status = "processing";
    videos[1].status = "uploaded";
    await mountWithSyncClock();
    const initial = card("video-1");
    expect(initial.textContent).toContain("处理中");
    videos[0].status = "review_ready";
    await advanceSyncClock(14_999);
    expect(getVideos).not.toHaveBeenCalled();
    expect(getLibrary).not.toHaveBeenCalled();
    expect(initial.textContent).toContain("处理中");
    await advanceSyncClock(1);
    expect(getVideos).toHaveBeenCalledTimes(1);
    expect(getLibrary).toHaveBeenCalledTimes(1);
    await advanceSyncClock(1);
    expect(card("video-1")).toBe(initial);
    expect(initial.textContent).toContain("可复习");
    expect(card("video-2").classList.contains("is-disabled")).toBe(false);
    expect(card("video-2").textContent).toContain("待处理");
    click(initial.querySelector(".library-card-body"));
    expect(window.location.pathname).toBe("/review/1");
    await advanceSyncClock();
  });

  it("updates the open folder and root without closing the folder or changing the order", async () => {
    videos[0].folder_id = 7;
    videos[0].status = "processing";
    await mountWithSyncClock();
    click(container.querySelector(".folder-card-link"));
    await advanceSyncClock();
    const dialog = container.querySelector(".folder-window");
    const order = [...container.querySelectorAll(".library-inner .library-card")]
      .map((element) => element.getAttribute("data-library-key"));
    expect(card("video-1").textContent).toContain("处理中");
    vi.mocked(getVideos).mockClear();
    vi.mocked(getLibrary).mockClear();
    videos[0].status = "review_ready";
    await advanceSyncClock(15_000);
    expect(getVideos).toHaveBeenCalledTimes(1);
    expect(getLibrary).toHaveBeenCalledWith(7);
    expect(getLibrary).toHaveBeenCalledWith();
    await advanceSyncClock(1);
    expect(card("video-1").textContent).toContain("可复习");
    expect(container.querySelector(".folder-window")).toBe(dialog);
    expect([...container.querySelectorAll(".library-inner .library-card")]
      .map((element) => element.getAttribute("data-library-key"))).toEqual(order);
  });

  it("pauses polling in the background and immediately refreshes on returning to the foreground", async () => {
    videos[0].status = "processing";
    await mountWithSyncClock();
    act(() => focusManager.setFocused(false));
    videos[0].status = "review_ready";
    await advanceSyncClock(30_000);
    expect(getVideos).not.toHaveBeenCalled();
    expect(getLibrary).not.toHaveBeenCalled();
    expect(card("video-1").textContent).toContain("处理中");
    act(() => focusManager.setFocused(true));
    await advanceSyncClock();
    expect(getVideos).toHaveBeenCalledTimes(1);
    expect(getLibrary).toHaveBeenCalledTimes(1);
    expect(card("video-1").textContent).toContain("可复习");
  });

  it("pauses the timer while dragging and resumes after drag end", async () => {
    videos[0].status = "processing";
    await mountWithSyncClock();
    beginVideoDrag();
    videos[0].status = "review_ready";
    await advanceSyncClock(15_000);
    expect(getVideos).not.toHaveBeenCalled();
    expect(getLibrary).not.toHaveBeenCalled();
    expect(card("video-1").textContent).toContain("处理中");
    dragEvent("dragend", card("video-1"));
    await advanceSyncClock(15_000);
    expect(getVideos).toHaveBeenCalledTimes(1);
    await advanceSyncClock(1);
    expect(card("video-1").textContent).toContain("可复习");
  });

  it("refreshes immediately when returning to the library with a fresh cached list", async () => {
    videos[0].status = "processing";
    await mountWithSyncClock();
    act(() => {
      window.history.pushState({}, "", "/review/1");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    await advanceSyncClock();
    expect(container.querySelector(".workbench")?.hasAttribute("hidden")).toBe(true);
    await advanceSyncClock(1000);
    expect(getVideos).not.toHaveBeenCalled();
    videos[0].status = "review_ready";
    act(() => {
      window.history.pushState({}, "", "/");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    await advanceSyncClock();
    expect(getVideos).toHaveBeenCalledTimes(1);
    expect(card("video-1").textContent).toContain("可复习");
  });

  it("retains the last visible list on errors and recovers on the next interval", async () => {
    videos[0].status = "processing";
    await mountWithSyncClock();
    vi.mocked(getVideos).mockRejectedValueOnce(new Error("Temporary failure"));
    vi.mocked(getLibrary).mockRejectedValueOnce(new Error("Temporary failure"));
    await advanceSyncClock(15_000);
    expect(container.querySelectorAll(".library-card")).toHaveLength(3);
    expect(card("video-1").textContent).toContain("处理中");
    videos[0].status = "review_ready";
    await advanceSyncClock(15_000);
    expect(card("video-1").textContent).toContain("可复习");
  });
});

function click(element: Element | null) {
  expect(element).not.toBeNull();
  act(() => (element as HTMLButtonElement).click());
}
