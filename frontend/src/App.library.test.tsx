import { waitForExit } from "./test/waitForExit";
import { act, StrictMode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { focusManager } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { createLibraryFolder, deleteLibraryFolder, deleteVideo, getLatestRecycleBin, undoRecycleBin, getLibrary, getVideos, moveLibraryFolder, moveVideoToFolder, renameVideoFile, reorderLibrary, processVideo, processSelectedVideos, getAsrSelection, getAiSettings, getJob, getPausedJobs, getLatestBatch, getLibraryStatus, type LibraryResponse } from "./api/client";
import type { Job, Video } from "./types";
import { saveDragTextHints } from "./libraryPreferences";

vi.mock("./api/client", async (importOriginal) => ({
  ...await importOriginal<typeof import("./api/client")>(),
  getLibrary: vi.fn(),
  createLibraryFolder: vi.fn(),
  getVideos: vi.fn(),
  moveVideoToFolder: vi.fn(),
  moveLibraryFolder: vi.fn(),
  renameVideoFile: vi.fn(),
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
  deleteLibraryFolder: vi.fn(),
  deleteVideo: vi.fn(),
  getLatestRecycleBin: vi.fn(async () => null),
  undoRecycleBin: vi.fn(),
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
  vi.mocked(getJob).mockReset().mockImplementation(async id => ({ id, status: "queued", progress: 0 }));
  vi.mocked(getPausedJobs).mockReset().mockResolvedValue([]);
  vi.mocked(getLatestBatch).mockReset().mockResolvedValue(null);
  vi.mocked(getLibraryStatus).mockReset().mockResolvedValue({ data_root: "", database_path: "", temp_audio_dir: "", video_count: 2 });
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
  vi.mocked(moveLibraryFolder).mockReset();
  vi.mocked(deleteVideo).mockReset().mockResolvedValue({ operation_id: "trash-video", kind: "video", videos: 1, folders: 0 });
  vi.mocked(deleteLibraryFolder).mockReset().mockResolvedValue({ operation_id: "trash-folder", kind: "folder", videos: 0, folders: 1 });
  vi.mocked(getLatestRecycleBin).mockReset().mockResolvedValue(null);
  vi.mocked(undoRecycleBin).mockReset().mockResolvedValue({ operation_id: "undo", kind: "video", videos: 1, folders: 0 });
  vi.mocked(renameVideoFile).mockReset().mockImplementation(async (id, filename) => {
    videos = videos.map((video) => video.id === id
      ? { ...video, filename, display_name: filename.replace(/\.[^.]+$/, ""), video_path: `library/${filename}` } : video);
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
  delete window.__classroomStartupView;
  document.getElementById("classroom-startup-cover")?.remove();
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

async function remountWithCachedFolders() {
  window.localStorage.setItem("classroom-video-library", JSON.stringify({
    folders: [
      { id: "folder-old-local", name: "1", createdAt: 1 },
      { id: "folder-999", name: "Removed folder", createdAt: 2 },
      { id: "folder-7", name: "Old Algebra", createdAt: 3 },
    ],
    assignments: {}, rootOrder: ["folder-old-local", "folder-999", "folder-7"],
    folderOrders: { "folder-old-local": ["video-1"], "folder-999": [], "folder-7": [] },
  }));
  act(() => root.unmount());
  root = createRoot(container);
  await act(async () => root.render(<App />));
}

function setVideoFilename(value: string) {
  const input = container.querySelector<HTMLInputElement>(".library-video-rename input")!;
  act(() => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

function submitVideoFilename() {
  act(() => container.querySelector(".library-video-rename")!
    .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
}

describe("discoverable header entries", () => {
  it("keeps both entry names visible even without a task and opens the task dialog", async () => {
    const task = container.querySelector<HTMLButtonElement>(".task-indicator")!;
    expect(task.querySelector(".task-indicator-label")?.textContent).toBe("任务");
    expect(task.querySelector(".task-indicator-status")?.textContent).toBe("暂无任务");
    expect(task.getAttribute("aria-haspopup")).toBe("dialog");
    expect(task.getAttribute("aria-expanded")).toBe("false");
    expect(container.querySelector(".workbench-settings-link")?.textContent).toBe("设置");
    act(() => task.click());
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector('[role="dialog"]')).not.toBeNull();
      expect(task.getAttribute("aria-expanded")).toBe("true");
      expect(container.textContent).toContain("暂无处理任务");
    });
  });

  it.each([
    ["completed", "处理完成", ".lucide-circle-check"],
    ["running", "处理中 · 42%", ".lucide-loader-circle.spin"],
    ["failed", "处理失败", ".lucide-circle-alert"],
    ["resource_paused", "资源暂停", ".lucide-clock3"],
    ["awaiting_api", "等待 AI 恢复", ".lucide-clock3"],
  ])("preserves a named task entry for %s", async (status, label, icon) => {
    const job: Job = { id: "header-task", status, progress: 42, job_type: "batch_video_process" };
    vi.mocked(getLatestBatch).mockResolvedValue(job);
    vi.mocked(getJob).mockResolvedValue(job);
    act(() => root.unmount());
    root = createRoot(container);
    await act(async () => root.render(<App />));
    await vi.waitFor(async () => {
      await flushUpdates();
      const task = container.querySelector(".task-indicator")!;
      expect(task.querySelector(".task-indicator-label")?.textContent).toBe("任务");
      expect(task.querySelector(".task-indicator-status")?.textContent).toBe(label);
      expect(task.querySelector(icon)).not.toBeNull();
    });
  });

  it("opens settings from the named header link and exposes the current page", async () => {
    const settings = container.querySelector<HTMLAnchorElement>(".workbench-settings-link")!;
    expect(settings.getAttribute("aria-current")).toBeNull();
    act(() => settings.click());
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(window.location.pathname).toBe("/settings");
      expect(settings.getAttribute("aria-current")).toBe("page");
      expect(container.querySelector(".settings-page")).not.toBeNull();
    });
  });
});

describe("in-app recycle confirmation", () => {
  function dialog() { return document.querySelector<HTMLElement>(".recycle-confirm-dialog")!; }
  function button(label: string) {
    return [...dialog().querySelectorAll<HTMLButtonElement>("button")].find(item => item.textContent === label)!;
  }
  function openVideo() {
    const trigger = card("video-1").querySelector<HTMLButtonElement>(".library-card-delete")!;
    act(() => { trigger.focus(); trigger.click(); });
    return trigger;
  }

  it("shows the video name in an in-app dialog and cancels without a native prompt or delete request", () => {
    const nativeConfirm = vi.spyOn(window, "confirm");
    const trigger = openVideo();
    expect(dialog().getAttribute("role")).toBe("dialog");
    expect(dialog().textContent).toContain("Lesson 1");
    expect(dialog().textContent).toContain("不会永久删除");
    expect(document.activeElement).toBe(button("取消"));
    expect(container.querySelector<HTMLElement>(".library-page")!.inert).toBe(true);
    expect(deleteVideo).not.toHaveBeenCalled();
    act(() => button("取消").click());
    expect(dialog()).toBeNull();
    expect(document.activeElement).toBe(trigger);
    expect(container.querySelector<HTMLElement>(".library-page")!.inert).toBe(false);
    expect(nativeConfirm).not.toHaveBeenCalled();
    expect(deleteVideo).not.toHaveBeenCalled();
  });

  it("traps keyboard focus and supports Escape and backdrop cancellation", () => {
    openVideo();
    const confirm = button("移入回收站");
    act(() => confirm.focus());
    keyDown("Tab");
    expect(document.activeElement?.getAttribute("aria-label")).toBe("关闭删除确认");
    keyDown("Tab", true);
    expect(document.activeElement).toBe(confirm);
    keyDown("Escape");
    expect(dialog()).toBeNull();
    openVideo();
    act(() => document.querySelector<HTMLElement>(".recycle-confirm-backdrop")!.click());
    expect(dialog()).toBeNull();
    expect(deleteVideo).not.toHaveBeenCalled();
  });

  it("submits only once, keeps the dialog open while pending and preserves Ctrl+Z undo", async () => {
    let finish!: () => void;
    vi.mocked(deleteVideo).mockImplementation(() => new Promise(resolve => {
      finish = () => resolve({ operation_id: "trash", kind: "video", videos: 1, folders: 0 });
    }));
    openVideo();
    const confirm = button("移入回收站");
    act(() => { confirm.click(); confirm.click(); });
    expect(deleteVideo).toHaveBeenCalledTimes(1);
    expect(deleteVideo).toHaveBeenCalledWith(1);
    expect(button("取消").disabled).toBe(true);
    expect(dialog().textContent).toContain("正在移入…");
    keyDown("Escape");
    act(() => document.querySelector<HTMLElement>(".recycle-confirm-backdrop")!.click());
    expect(dialog()).not.toBeNull();
    await act(async () => finish());
    expect(dialog()).toBeNull();
    expect(container.querySelector(".toast")?.textContent).toContain("撤销");
    await act(async () => document.dispatchEvent(new KeyboardEvent("keydown", { key: "z", ctrlKey: true, bubbles: true, cancelable: true })));
    expect(undoRecycleBin).toHaveBeenCalledTimes(1);
  });

  it("keeps API errors beside the confirmation and retries without a second prompt", async () => {
    vi.mocked(deleteVideo).mockRejectedValueOnce(new Error("文件正在被占用"));
    openVideo();
    await act(async () => button("移入回收站").click());
    expect(dialog().querySelector('[role="alert"]')?.textContent).toContain("文件正在被占用");
    expect(button("取消").disabled).toBe(false);
    await act(async () => button("重试移入").click());
    expect(deleteVideo).toHaveBeenCalledTimes(2);
    expect(dialog()).toBeNull();
  });

  it("confirms folders without opening them and preserves an open folder when a nested delete is canceled", async () => {
    act(() => card("folder-7").querySelector<HTMLButtonElement>(".folder-delete-button")!.click());
    expect(dialog().textContent).toContain("Algebra");
    expect(dialog().textContent).toContain("子文件夹、视频");
    expect(container.querySelector(".folder-window")).toBeNull();
    await act(async () => button("移入回收站").click());
    expect(deleteLibraryFolder).toHaveBeenCalledTimes(1);
    expect(deleteLibraryFolder).toHaveBeenCalledWith(7);
    videos[0].folder_id = 7;
    act(() => { focusManager.setFocused(false); focusManager.setFocused(true); });
    await vi.waitFor(() => expect(container.querySelector('[data-library-key="video-1"]')).toBeNull());
    await act(async () => card("folder-7").querySelector<HTMLButtonElement>(".folder-card-link")!.click());
    await vi.waitFor(() => expect(container.querySelector(".folder-window-list .library-card-delete")).not.toBeNull());
    const trigger = container.querySelector<HTMLButtonElement>(".folder-window-list .library-card-delete")!;
    act(() => { trigger.focus(); trigger.click(); });
    expect(dialog()).not.toBeNull();
    keyDown("Escape");
    expect(dialog()).toBeNull();
    expect(container.querySelector(".folder-window")).not.toBeNull();
    expect(document.activeElement).toBe(trigger);
    expect(deleteVideo).not.toHaveBeenCalled();
  });
});

describe("native startup scroll handoff", () => {
  function installBridge(anchor: string | null, libraryId = "012345abcdef") {
    let view: { library_id: string; scroll_y: number; anchor: string | null; offset: number } | null =
      { library_id: libraryId, scroll_y: 900, anchor, offset: -70 };
    const api = {
      describe: vi.fn(async () => ({ name: "Test", tray: false, dialog: null, hotkey: "" })),
      respond: vi.fn(async () => ({})), retry: vi.fn(), set_hotkey: vi.fn(), set_close_behavior: vi.fn(),
      get_startup_view: vi.fn(async () => view),
      clear_startup_view: vi.fn(async () => { view = null; }),
    };
    vi.stubGlobal("pywebview", { api });
    return api;
  }

  async function remount() {
    act(() => root.unmount());
    root = createRoot(container);
    await act(async () => root.render(<StrictMode><App /></StrictMode>));
    await vi.waitFor(async () => { await flushUpdates(); expect(container.querySelectorAll(".library-card")).toHaveLength(3); });
  }

  it.each([["video-1", 70], ["folder-7", 70], ["video-999", 900], [null, 900]] as const)(
    "restores %s only after the actual cards render, without replaying on refresh", async (anchor, top) => {
      const api = installBridge(anchor);
      vi.mocked(getLibraryStatus).mockResolvedValue({ library_id: "012345abcdef", library_scope: "test-workspace",
        data_root: "test", database_path: "", temp_audio_dir: "", video_count: 2 });
      await remount();
      await vi.waitFor(() => expect(api.clear_startup_view).toHaveBeenCalledTimes(1));
      expect(window.scrollTo).toHaveBeenLastCalledWith({ top, behavior: "instant" });
      vi.mocked(window.scrollTo).mockClear();
      const reads = vi.mocked(getLibrary).mock.calls.length;
      act(() => focusManager.setFocused(false));
      act(() => focusManager.setFocused(true));
      await vi.waitFor(() => expect(vi.mocked(getLibrary).mock.calls.length).toBeGreaterThan(reads));
      await flushUpdates();
      expect(window.scrollTo).not.toHaveBeenCalled();
    },
  );

  it("ignores a preview from a different library", async () => {
    const api = installBridge("video-1", "different");
    vi.mocked(getLibraryStatus).mockResolvedValue({ library_id: "012345abcdef", data_root: "test",
      database_path: "", temp_audio_dir: "", video_count: 2 });
    await remount();
    await vi.waitFor(() => expect(api.clear_startup_view).toHaveBeenCalledTimes(1));
    expect(window.scrollTo).not.toHaveBeenCalledWith({ top: 70, behavior: "instant" });
  });

  it("does not override user scrolling while workspace discovery is delayed", async () => {
    const api = installBridge("video-1");
    let finish!: (value: Awaited<ReturnType<typeof getLibraryStatus>>) => void;
    vi.mocked(getLibraryStatus).mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    await remount();
    act(() => window.dispatchEvent(new WheelEvent("wheel")));
    await vi.waitFor(() => expect(api.clear_startup_view).toHaveBeenCalledTimes(1));
    await act(async () => finish({ library_id: "012345abcdef", data_root: "test",
      database_path: "", temp_audio_dir: "", video_count: 2 }));
    await flushUpdates();
    expect(window.scrollTo).not.toHaveBeenCalledWith({ top: 70, behavior: "instant" });
  });

  it("keeps the preview over incomplete queries and restores before releasing it, even with a late bridge", async () => {
    window.__classroomStartupView = { library_id: "012345abcdef", scroll_y: 900, anchor: "video-1", offset: -70 };
    const cover = document.createElement("div");
    cover.id = "classroom-startup-cover";
    document.documentElement.append(cover);
    vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: true })));
    vi.mocked(getLibraryStatus).mockResolvedValue({ library_id: "012345abcdef", data_root: "test",
      database_path: "", temp_audio_dir: "", video_count: 2 });
    let finish!: (value: Video[]) => void;
    vi.mocked(getVideos).mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    act(() => root.unmount());
    root = createRoot(container);
    await act(async () => root.render(<StrictMode><App /></StrictMode>));
    await vi.waitFor(() => expect(finish).toBeTypeOf("function"));
    expect(cover.isConnected).toBe(true);
    expect(window.scrollTo).not.toHaveBeenCalledWith({ top: 70, behavior: "instant" });
    await act(async () => { finish(videos.map(video => ({ ...video }))); });
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(cover.isConnected).toBe(false);
    });
    expect(window.scrollTo).toHaveBeenLastCalledWith({ top: 70, behavior: "instant" });
    expect(window.__classroomStartupView).toBeUndefined();
    const api = installBridge("video-1");
    act(() => window.dispatchEvent(new Event("pywebviewready")));
    await vi.waitFor(() => expect(api.clear_startup_view).toHaveBeenCalledTimes(1));
    expect(api.get_startup_view).not.toHaveBeenCalled();
  });

  it.each(["listing", "identity"])("reveals usable error content if startup %s fails instead of freezing the preview", async failure => {
    window.__classroomStartupView = { library_id: "012345abcdef", scroll_y: 900, anchor: "video-1", offset: -70 };
    const cover = document.createElement("div");
    cover.id = "classroom-startup-cover";
    document.documentElement.append(cover);
    if (failure === "listing") {
      vi.mocked(getVideos).mockRejectedValue(new Error("Unable to read library"));
      vi.mocked(getLibrary).mockRejectedValue(new Error("Unable to read folders"));
    } else {
      vi.mocked(getLibraryStatus).mockRejectedValue(new Error("Unable to identify library"));
    }
    act(() => root.unmount());
    root = createRoot(container);
    await act(async () => root.render(<App />));
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(cover.isConnected).toBe(false);
    });
    expect(window.__classroomStartupView).toBeUndefined();
    if (failure === "listing") expect(container.querySelector(".error-panel")?.textContent).toContain("Unable to read library");
    else expect(container.querySelectorAll(".video-card")).toHaveLength(2);
  });

  it("waits for both query results to be reconciled before measuring an anchor with a different server order", async () => {
    const api = installBridge("video-1");
    vi.mocked(getLibrary).mockImplementation(async () => ({
      ...libraryResponse(), items: libraryResponse().items.filter(item => item.type === "video"),
    }));
    vi.mocked(getLibraryStatus).mockResolvedValue({ library_id: "012345abcdef", data_root: "test",
      database_path: "", temp_audio_dir: "", video_count: 2 });
    let finish!: (value: Video[]) => void;
    vi.mocked(getVideos).mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    const rect = HTMLElement.prototype.getBoundingClientRect;
    vi.mocked(rect).mockImplementation(function (this: HTMLElement) {
      const index = [...container.querySelectorAll("[data-library-key]")].indexOf(this);
      const top = Math.max(0, index) * 100;
      return { x: 0, y: top, left: 0, top, right: 400, bottom: top + 100,
        width: 400, height: 100, toJSON: () => ({}) };
    });
    act(() => root.unmount());
    root = createRoot(container);
    await act(async () => root.render(<App />));
    await vi.waitFor(() => expect(finish).toBeTypeOf("function"));
    vi.mocked(window.scrollTo).mockClear();
    await act(async () => finish([...videos].reverse()));
    await vi.waitFor(() => expect(api.clear_startup_view).toHaveBeenCalledTimes(1));
    expect([...container.querySelectorAll(".video-card")].map(element => element.getAttribute("data-library-key")))
      .toEqual(["video-1", "video-2"]);
    expect(window.scrollTo).toHaveBeenLastCalledWith({ top: 70, behavior: "instant" });
    expect(window.scrollTo).not.toHaveBeenCalledWith({ top: 170, behavior: "instant" });
  });
});

describe("library video rename", () => {
  it.each([
    ["single", "queued"], ["single", "running"], ["single", "resource_paused"],
    ["batch", "queued"], ["batch", "running"], ["batch", "resource_paused"],
  ])("allows unrelated renames during a %s %s task but locks its members", async (kind, status) => {
    const job: Job = { id: "busy", status, progress: .3,
      job_type: kind === "batch" ? "batch_video_process" : "video_process",
      ...(kind === "single" ? { video_id: 1 } : { batch_entries: [{ video_id: 1, status: "pending_asr" }] }),
    };
    vi.mocked(getJob).mockResolvedValue(job);
    vi.mocked(getLibraryStatus).mockResolvedValue({ data_root: "", database_path: "", temp_audio_dir: "",
      video_count: 2, active_job_id: status === "resource_paused" ? null : job.id });
    if (kind === "batch") vi.mocked(getLatestBatch).mockResolvedValue(job);
    if (status === "resource_paused") vi.mocked(getPausedJobs).mockResolvedValue([job]);
    act(() => root.unmount());
    root = createRoot(container);
    await act(async () => root.render(<App />));
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(card("video-1").querySelector<HTMLButtonElement>(".library-card-rename")?.disabled).toBe(true);
      expect(card("video-2").querySelector<HTMLButtonElement>(".library-card-rename")?.disabled).toBe(false);
    });
    expect(card("video-1").querySelector(".library-card-rename")?.getAttribute("title")).toContain("任务");
    expect(card("video-2").querySelector<HTMLButtonElement>(".library-card-process")?.disabled).toBe(true);
    click(card("video-2").querySelector(".library-card-rename"));
    setVideoFilename("other-renamed.mp4");
    submitVideoFilename();
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(card("video-2").querySelector("strong")?.textContent).toBe("other-renamed");
    });
    expect(renameVideoFile).toHaveBeenCalledWith(2, "other-renamed.mp4");
    expect(processVideo).not.toHaveBeenCalled();
    expect(window.location.pathname).toBe("/");
  });

  it("shows backend file restrictions for another paused queue without disabling unrelated files", async () => {
    videos[0].file_change_block_reason = "该视频被资源暂停任务占用";
    act(() => root.unmount());
    root = createRoot(container);
    await act(async () => root.render(<App />));
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(card("video-1").querySelector<HTMLButtonElement>(".library-card-rename")?.disabled).toBe(true);
      expect(card("video-2").querySelector<HTMLButtonElement>(".library-card-rename")?.disabled).toBe(false);
    });
    expect(card("video-1").querySelector(".library-card-rename")?.getAttribute("title")).toContain("资源暂停");
  });

  it("keeps a rename draft when the video becomes occupied and allows saving after release", async () => {
    click(card("video-2").querySelector(".library-card-rename"));
    setVideoFilename("after-stop.mp4");
    videos[1].file_change_block_reason = "该视频在批次队列中，请停止任务后修改文件名";
    act(() => { focusManager.setFocused(false); focusManager.setFocused(true); });
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector<HTMLButtonElement>('[aria-label="保存文件名"]')?.disabled).toBe(true);
    });
    expect(container.querySelector<HTMLInputElement>(".library-video-rename input")?.value).toBe("after-stop.mp4");
    submitVideoFilename();
    expect(renameVideoFile).not.toHaveBeenCalled();
    expect(container.querySelector(".library-video-rename [role=alert]")?.textContent).toContain("批次队列");
    videos[1].file_change_block_reason = null;
    act(() => { focusManager.setFocused(false); focusManager.setFocused(true); });
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector<HTMLButtonElement>('[aria-label="保存文件名"]')?.disabled).toBe(false);
    });
    submitVideoFilename();
    await vi.waitFor(() => expect(renameVideoFile).toHaveBeenCalledWith(2, "after-stop.mp4"));
  });

  it("renames from the card without opening processing and keeps selection and review navigation", async () => {
    selectVideo(1);
    const trigger = card("video-1").querySelector<HTMLButtonElement>(".library-card-rename")!;
    click(trigger);
    const input = container.querySelector<HTMLInputElement>(".library-video-rename input")!;
    expect(document.activeElement).toBe(input);
    expect(input.value).toBe("lesson-1.mp4");
    expect(input.selectionEnd).toBe("lesson-1".length);
    expect(card("video-1").draggable).toBe(false);
    expect(window.location.pathname).toBe("/");
    expect(container.textContent).not.toContain("处理与文件名");
    expect(container.querySelector<HTMLButtonElement>('[aria-label="保存文件名"]')?.disabled).toBe(true);
    setVideoFilename("  重命名课程.mp4  ");
    submitVideoFilename();
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(card("video-1").querySelector("strong")?.textContent).toBe("重命名课程");
    });
    expect(renameVideoFile).toHaveBeenCalledWith(1, "重命名课程.mp4");
    expect(processVideo).not.toHaveBeenCalled();
    expect(card("video-1").querySelector<HTMLInputElement>('input[type="checkbox"]')?.checked).toBe(true);
    expect(container.querySelector(".library-video-rename")).toBeNull();
    expect(document.activeElement).toBe(trigger);
    click(card("video-1").querySelector(".library-card-body"));
    expect(window.location.pathname).toBe("/review/1");
  });

  it("prevents repeated submits and closing while the rename is pending", async () => {
    let finish!: (video: Video) => void;
    vi.mocked(renameVideoFile).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    click(card("video-1").querySelector(".library-card-rename"));
    setVideoFilename("saved.mp4");
    submitVideoFilename();
    submitVideoFilename();
    expect(renameVideoFile).toHaveBeenCalledTimes(1);
    expect(container.querySelector<HTMLInputElement>(".library-video-rename input")?.disabled).toBe(true);
    expect(container.querySelector<HTMLButtonElement>('[aria-label="取消重命名"]')?.disabled).toBe(true);
    act(() => container.querySelector(".library-video-rename input")!
      .dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true })));
    expect(container.querySelector(".library-video-rename")).not.toBeNull();
    videos[0] = { ...videos[0], filename: "saved.mp4", display_name: "saved" };
    await act(async () => { finish(videos[0]); });
    expect(container.querySelector(".library-video-rename")).toBeNull();
  });

  it("preserves the draft on failure and permits retry without changing the existing name", async () => {
    vi.mocked(renameVideoFile).mockRejectedValueOnce(new Error("视频源文件不存在"));
    click(card("video-1").querySelector(".library-card-rename"));
    setVideoFilename("新名称.mp4");
    submitVideoFilename();
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector(".library-video-rename [role=alert]")?.textContent).toContain("视频源文件不存在");
    });
    expect(container.querySelector<HTMLInputElement>(".library-video-rename input")?.value).toBe("新名称.mp4");
    expect(videos[0].filename).toBe("lesson-1.mp4");
    expect(container.querySelector<HTMLButtonElement>('[aria-label="保存文件名"]')?.disabled).toBe(false);
    submitVideoFilename();
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(card("video-1").querySelector("strong")?.textContent).toBe("新名称");
    });
    expect(renameVideoFile).toHaveBeenCalledTimes(2);
  });

  it("cancels on Escape inside a folder without closing the folder or submitting", async () => {
    await loadFolderVideos();
    const trigger = card("video-1").querySelector<HTMLButtonElement>(".library-card-rename")!;
    click(trigger);
    setVideoFilename("未保存.mp4");
    act(() => container.querySelector(".library-video-rename input")!
      .dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true })));
    expect(container.querySelector(".library-video-rename")).toBeNull();
    expect(container.querySelector(".folder-window")).not.toBeNull();
    expect(card("video-1").querySelector("strong")?.textContent).toBe("Lesson 1");
    expect(document.activeElement).toBe(trigger);
    expect(renameVideoFile).not.toHaveBeenCalled();
    click(trigger);
    setVideoFilename("文件夹内.mp4");
    submitVideoFilename();
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(card("video-1").querySelector("strong")?.textContent).toBe("文件夹内");
    });
    expect(container.querySelector(".folder-window")).not.toBeNull();
    expect(videos[0].folder_id).toBe(7);
  });
});

describe("library drag and drop", () => {
  it("does not retain a browser-only folder after receiving the authoritative root list", async () => {
    await remountWithCachedFolders();
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector(".folder-card strong")?.textContent).toBe("Algebra");
    });
    expect(container.querySelectorAll(".folder-card")).toHaveLength(1);
    expect(container.textContent).not.toContain("Old Algebra");
    expect(container.querySelector('[data-library-key="folder-old-local"]')).toBeNull();
    expect(container.querySelector("option[value=folder-old-local]")).toBeNull();
    const saved = JSON.parse(window.localStorage.getItem("classroom-video-library")!);
    expect(saved.folders.map((item: { id: string }) => item.id)).toEqual(["folder-7"]);
    expect(saved.rootOrder).not.toContain("folder-999");
    expect(saved.folderOrders["folder-old-local"]).toBeUndefined();
    expect(saved.folderOrders["folder-999"]).toBeUndefined();
  });

  it("removes cached folders when the backend successfully returns no folders", async () => {
    vi.mocked(getLibrary).mockImplementation(async () => ({
      ...libraryResponse(), items: libraryResponse().items.filter((item) => item.type === "video"),
    }));
    await remountWithCachedFolders();
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelectorAll(".video-card")).toHaveLength(2);
    });
    expect(container.querySelectorAll(".folder-card")).toHaveLength(0);
    click(card("video-1").querySelector('[aria-haspopup="menu"]'));
    expect([...card("video-1").querySelectorAll('[role="menuitem"]')].map((item) => item.textContent)).toEqual(["导出分享包", "一级目录"]);
    expect(JSON.parse(window.localStorage.getItem("classroom-video-library")!).folders).toEqual([]);
  });

  it("preserves cached metadata on a failed folder request but never exposes it as a working drop target", async () => {
    vi.mocked(getLibrary).mockRejectedValue(new Error("Library unavailable"));
    await remountWithCachedFolders();
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector(".local-error")?.textContent).toContain("Library unavailable");
    });
    expect(container.querySelectorAll(".library-card")).toHaveLength(0);
    expect(JSON.parse(window.localStorage.getItem("classroom-video-library")!).folders).toHaveLength(3);
  });

  it("loads real folders when opening a folder page directly, without trusting the cached names", async () => {
    window.history.replaceState({}, "", "/library/folder/folder-7");
    videos[0].folder_id = 7;
    await remountWithCachedFolders();
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector("h1")?.textContent).toBe("视频库");
      expect(container.querySelector("#folder-window-title")?.textContent).toBe("Algebra");
      expect(container.querySelectorAll(".folder-window .video-card")).toHaveLength(1);
    });
    click(container.querySelector('.folder-window [data-library-key="video-1"] [aria-haspopup="menu"]'));
    expect([...container.querySelectorAll('.folder-window [data-move-folder]')].map((item) => item.textContent)).toEqual(["一级目录", "Algebra"]);
    expect(JSON.parse(window.localStorage.getItem("classroom-video-library")!).folders).toHaveLength(1);
  });

  it("accepts numeric folder names when they correspond to a real backend folder", async () => {
    vi.mocked(getLibrary).mockImplementation(async (id) => {
      const response = libraryResponse(id ?? null);
      return {
        ...response, folder: response.folder ? { ...folder, name: "1" } : null,
        items: response.items.map((item) => item.type === "folder" ? { ...item, folder: { ...folder, name: "1" } } : item),
      };
    });
    await remountWithCachedFolders();
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelectorAll(".folder-card")).toHaveLength(1);
      expect(container.querySelector(".folder-card strong")?.textContent).toBe("1");
    });
    beginVideoDrag();
    await act(async () => { dragEvent("drop", card("folder-7"), 16); });
    expect(moveVideoToFolder).toHaveBeenCalledWith(1, 7);
    expect(container.querySelector(".toast")?.textContent).toContain("视频已移入文件夹");
  });

  it.each([0, 16, 40, 200, 360, 390, 400])("moves videos into the folder at x=%s, including the icon and edges", async (x) => {
    beginVideoDrag();
    const target = card("folder-7");
    const event = dragEvent("dragover", target.querySelector(".folder-card-link")!, x);
    expect(event.defaultPrevented).toBe(true);
    expect(target.classList.contains("drop-folder")).toBe(true);
    expect(target.classList.contains("drop-swap")).toBe(false);
    await act(async () => { dragEvent("drop", target, x); });
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector('[data-library-key="video-1"]')).toBeNull();
    });
    expect(moveVideoToFolder).toHaveBeenCalledTimes(1);
    expect(moveVideoToFolder).toHaveBeenCalledWith(1, 7);
    expect(reorderLibrary).not.toHaveBeenCalled();
    expect(container.querySelector(".toast")?.textContent).toContain("视频已移入文件夹");
    expect(JSON.parse(window.localStorage.getItem("classroom-video-library")!).assignments["1"]).toBe("folder-7");
    act(() => root.unmount());
    root = createRoot(container);
    await act(async () => root.render(<App />));
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector('[data-library-key="video-2"]')).not.toBeNull();
    });
    expect(container.querySelector('[data-library-key="video-1"]')).toBeNull();
  });

  it("waits for the move response before claiming success", async () => {
    let finish!: (video: Video) => void;
    vi.mocked(moveVideoToFolder).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    beginVideoDrag();
    dragEvent("drop", card("folder-7"));
    expect(moveVideoToFolder).toHaveBeenCalledTimes(1);
    expect(moveVideoToFolder).toHaveBeenCalledWith(1, 7);
    expect(container.querySelector(".toast")).toBeNull();
    expect(container.querySelector('[data-library-key="video-1"]')).not.toBeNull();
    videos[0] = { ...videos[0], folder_id: 7 };
    await act(async () => { finish(videos[0]); });
    expect(container.querySelector(".toast")?.textContent).toContain("视频已移入文件夹");
  });

  it("keeps the card at the root and reports the real failure without a premature success toast", async () => {
    vi.mocked(moveVideoToFolder).mockRejectedValueOnce(new Error("Move failed"));
    beginVideoDrag();
    await act(async () => { dragEvent("drop", card("folder-7"), 16); });
    expect(container.querySelector(".library-operation-error")?.textContent).toContain("视频移动失败：Move failed");
    expect(container.textContent).not.toContain("已将视频移入");
    expect(container.querySelector('[data-library-key="video-1"]')).not.toBeNull();
    expect(videos[0].folder_id).toBeNull();
    expect(reorderLibrary).not.toHaveBeenCalled();
  });

  it("rejects mixed folder and video ordering between the two sections", () => {
    dragEvent("dragstart", card("folder-7"));
    dragEvent("dragover", card("video-2"), 390);
    expect(card("video-2").classList.contains("drop-swap")).toBe(false);
    dragEvent("drop", card("video-2"), 390);
    expect(moveVideoToFolder).not.toHaveBeenCalled();
    expect(reorderLibrary).not.toHaveBeenCalled();
  });

  it("preserves ordering when dragging a video onto another video", () => {
    beginVideoDrag();
    dragEvent("dragover", card("video-2"), 390);
    dragEvent("drop", card("video-2"), 390);
    expect(moveVideoToFolder).not.toHaveBeenCalled();
    expect(reorderLibrary).toHaveBeenCalledWith(null, [
      { type: "folder", id: 7 }, { type: "video", id: 2 }, { type: "video", id: 1 },
    ]);
  });

  it("distinguishes video swapping from edge insertion in the visible feedback", () => {
    beginVideoDrag();
    dragEvent("dragover", card("video-2"));
    expect(card("video-2").querySelector(".library-drop-feedback.is-order")?.textContent).toBe("交换位置");
    dragEvent("dragover", card("video-2"), 390);
    expect(card("video-2").querySelector(".library-drop-feedback")?.textContent).toBe("放在后面");
    expect(card("video-2").classList.contains("drop-after")).toBe(true);
    dragEvent("dragend", card("video-1"));
    expect(container.querySelector(".library-drop-feedback")).toBeNull();
    expect(reorderLibrary).not.toHaveBeenCalled();
  });

  it("hides drag text immediately without hiding the insertion line or disabling ordering", async () => {
    beginVideoDrag();
    dragEvent("dragover", card("video-2"), 390);
    expect(card("video-2").querySelector(".library-drop-feedback")).not.toBeNull();
    act(() => { saveDragTextHints(false); });
    expect(container.querySelector(".library-drop-feedback")).toBeNull();
    expect(card("video-2").querySelector(".library-drop-line")?.getAttribute("data-edge")).toBe("after");
    await act(async () => { dragEvent("drop", card("video-2"), 390); });
    expect(reorderLibrary).toHaveBeenCalledWith(null, [
      { type: "folder", id: 7 }, { type: "video", id: 2 }, { type: "video", id: 1 },
    ]);
    act(() => root.unmount());
    root = createRoot(container);
    await act(async () => root.render(<App />));
    await vi.waitFor(async () => { await flushUpdates(); expect(container.querySelectorAll(".video-card")).toHaveLength(2); });
    beginVideoDrag();
    dragEvent("dragover", card("video-2"));
    expect(container.querySelector(".library-drop-feedback")).toBeNull();
    act(() => { saveDragTextHints(true); });
    expect(card("video-2").querySelector(".library-drop-feedback")?.textContent).toBe("交换位置");
  });

  it("retains hover feedback across folder children and clears it when leaving the whole card", () => {
    beginVideoDrag();
    const target = card("folder-7");
    dragEvent("dragover", target);
    dragEvent("dragleave", target.querySelector(".folder-icon")!, 20, target.querySelector(".library-card-body"));
    expect(target.classList.contains("drop-folder")).toBe(true);
    dragEvent("dragleave", target, 450, document.body);
    expect(target.classList.contains("drop-folder")).toBe(false);
    dragEvent("dragend", card("video-1"));
    expect(moveVideoToFolder).not.toHaveBeenCalled();
    expect(reorderLibrary).not.toHaveBeenCalled();
  });

  it("ignores external drops and self-drops", () => {
    dragEvent("drop", card("folder-7"));
    beginVideoDrag();
    dragEvent("drop", card("video-1"));
    expect(moveVideoToFolder).not.toHaveBeenCalled();
    expect(reorderLibrary).not.toHaveBeenCalled();
  });
});

function click(element: Element | null) {
  expect(element).not.toBeNull();
  act(() => (element as HTMLButtonElement).click());
}

function setSelectValue(selector: string, value: string) {
  const select = container.querySelector<HTMLSelectElement>(selector)!;
  act(() => {
    select.value = value;
    select.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

function moveThroughMenu(id: number, destination: string) {
  const item = container.querySelector(`.folder-window [data-library-key="video-${id}"]`)!;
  click(item.querySelector('[aria-haspopup="menu"]'));
  click([...item.querySelectorAll('[role="menuitem"]')].find((element) => element.textContent === destination)!);
}

function keyDown(key: string, shiftKey = false) {
  const event = new KeyboardEvent("keydown", { key, shiftKey, bubbles: true, cancelable: true });
  act(() => document.dispatchEvent(event));
  return event;
}

async function loadFolderVideos() {
  videos = videos.map((video) => ({ ...video, folder_id: 7 }));
  act(() => root.unmount());
  root = createRoot(container);
  await act(async () => root.render(<App />));
  await vi.waitFor(async () => {
    await flushUpdates();
    expect(container.querySelectorAll(".library-inner .video-card")).toHaveLength(0);
    expect(container.querySelector(".folder-card")).not.toBeNull();
  });
  click(container.querySelector(".folder-card-link"));
  await vi.waitFor(async () => {
    await flushUpdates();
    expect(container.querySelectorAll(".folder-window .video-card")).toHaveLength(2);
  });
}

function selectVideo(id: number, inFolder = false) {
  click(container.querySelector(`${inFolder ? ".folder-window" : ".library-inner"} [data-library-key="video-${id}"] input[type="checkbox"]`));
}

async function openLibraryOptions(inFolder = false) {
  click(container.querySelector(`${inFolder ? ".folder-window" : ".library-inner"} .library-selection-process`));
  await vi.waitFor(async () => {
    await flushUpdates();
    const start = [...container.querySelectorAll<HTMLButtonElement>(".library-processing-options button.primary-button")]
      .find((button) => !button.closest("[inert]"));
    expect(start).toBeDefined();
    expect(start?.disabled).toBe(false);
  });
}

function submitLibraryProcessing(inFolder = false) {
  click(container.querySelector(`${inFolder ? ".folder-window" : ".library-inner"} .library-processing-options button.primary-button`));
}

describe("folder selection for processing", () => {
  async function setupFolders(onlyFolders = false) {
    videos[0].folder_id = 7;
    if (onlyFolders) videos[1].folder_id = 8;
    videos.push(...[3, 4].map(id => ({ ...videos[0], id, folder_id: 8, display_name: `Nested ${id}`, filename: `nested-${id}.mp4` })));
    const folders = [
      { ...folder, parent_id: null, video_count: onlyFolders ? 4 : 3 },
      { id: 8, name: "子课程", parent_id: 7, video_count: onlyFolders ? 3 : 2 },
      { id: 9, name: "空文件夹", parent_id: 7, video_count: 0 },
    ];
    vi.mocked(getLibrary).mockImplementation(async id => ({
      folder: folders.find(folder => folder.id === id) ?? null, parent_id: id ?? null, sort: "manual", order: "asc",
      ancestors: id === 7 ? [folders[0]] : id === 8 ? folders.slice(0, 2) : [],
      items: [
        ...folders.filter(folder => folder.parent_id === (id ?? null)).map(folder => ({ type: "folder" as const, folder })),
        ...videos.filter(video => video.folder_id === (id ?? null)).map(video => ({ type: "video" as const, video })),
      ],
    }));
    vi.mocked(processSelectedVideos).mockResolvedValue({ job_id: "folder-selected", status: "queued", total: 4 });
    act(() => root.unmount());
    root = createRoot(container);
    await act(async () => root.render(<App />));
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(card("folder-7").querySelector<HTMLInputElement>(".library-folder-select input")?.disabled).toBe(false);
    });
  }
  function selectFolder(id: number) { click(card(`folder-${id}`).querySelector(".library-folder-select input")); }

  it("combines a selected folder and a video without navigating or fetching its children, then queues their IDs", async () => {
    await setupFolders();
    selectFolder(7);
    expect(container.querySelector(".folder-window")).toBeNull();
    expect(card("folder-7").classList.contains("is-selected")).toBe(true);
    expect(container.querySelector(".library-selection-dock")?.textContent).toContain("处理所选（3）");
    expect(vi.mocked(getLibrary).mock.calls.every(([id]) => id === undefined)).toBe(true);
    selectVideo(2);
    expect(container.querySelector(".library-selection-dock")?.textContent).toContain("处理所选（4）");
    expect(container.querySelector<HTMLInputElement>(".library-select-all input")?.checked).toBe(true);
    await openLibraryOptions();
    expect(container.querySelector(".library-processing-selection")?.textContent).toContain("Algebra · 3 个视频（含子文件夹）");
    submitLibraryProcessing();
    await vi.waitFor(() => expect(processSelectedVideos).toHaveBeenCalledWith([2], expect.objectContaining({
      folder_ids: [7], force_retranscribe: false, asr_model_id: "whisper-turbo",
    })));
    await vi.waitFor(() => expect(container.querySelector(".toast")?.textContent).toContain("共 4 个视频"));
    expect(card("folder-7").querySelector<HTMLInputElement>(".library-folder-select input")?.checked).toBe(false);
    expect(container.querySelector(".library-selection-dock")).toBeNull();
    expect(window.location.pathname).toBe("/");
    expect(processVideo).not.toHaveBeenCalled();
  });

  it("offers processing with no root videos and includes folders in select-all and clear", async () => {
    await setupFolders(true);
    expect(container.querySelectorAll(".library-inner .video-card")).toHaveLength(0);
    click(container.querySelector(".library-select-all input"));
    expect(card("folder-7").querySelector<HTMLInputElement>(".library-folder-select input")?.checked).toBe(true);
    expect(container.querySelector(".library-selection-dock")?.textContent).toContain("处理所选（4）");
    click(container.querySelector(".library-selection-dock .text-button"));
    expect(card("folder-7").querySelector<HTMLInputElement>(".library-folder-select input")?.checked).toBe(false);
    expect(container.querySelector(".library-selection-dock")).toBeNull();
    selectFolder(7);
    await openLibraryOptions();
    submitLibraryProcessing();
    await vi.waitFor(() => expect(processSelectedVideos).toHaveBeenCalledWith([], expect.objectContaining({ folder_ids: [7] })));
  });

  it("keeps parent and child selections separate and disables empty folders", async () => {
    await setupFolders();
    selectFolder(7);
    click(card("folder-7").querySelector(".folder-card-link"));
    await vi.waitFor(async () => { await flushUpdates(); expect(container.querySelector(".folder-window [data-library-key='folder-8']")).not.toBeNull(); });
    expect(container.querySelector(".folder-window .library-selection-count")?.textContent).toBe("已选 0 个视频");
    expect(card("folder-9").querySelector<HTMLInputElement>(".library-folder-select input")?.disabled).toBe(true);
    selectFolder(8);
    selectVideo(1, true);
    expect(container.querySelector(".folder-window > .library-selection-dock")?.textContent).toContain("处理所选（3）");
    await openLibraryOptions(true);
    submitLibraryProcessing(true);
    await vi.waitFor(() => expect(processSelectedVideos).toHaveBeenCalledWith([1], expect.objectContaining({ folder_ids: [8] })));
    expect(container.querySelector(".folder-window")).not.toBeNull();
    keyDown("Escape");
    await vi.waitFor(() => expect(container.querySelector(".folder-window")).toBeNull());
    expect(card("folder-7").querySelector<HTMLInputElement>(".library-folder-select input")?.checked).toBe(true);
  });

  it("keeps selection after failure and blocks changes and duplicate starts while submitting", async () => {
    await setupFolders(true);
    vi.mocked(processSelectedVideos).mockRejectedValueOnce(new Error("队列暂时不可用"));
    selectFolder(7);
    await openLibraryOptions();
    submitLibraryProcessing();
    await vi.waitFor(() => expect(container.querySelector(".library-processing-options [role=alert]")?.textContent).toContain("队列暂时不可用"));
    expect(card("folder-7").querySelector<HTMLInputElement>(".library-folder-select input")?.checked).toBe(true);
    let finish!: () => void;
    vi.mocked(processSelectedVideos).mockImplementationOnce(() => new Promise(resolve => {
      finish = () => resolve({ job_id: "retry-folder", status: "queued", total: 4 });
    }));
    submitLibraryProcessing();
    submitLibraryProcessing();
    await flushUpdates();
    expect(processSelectedVideos).toHaveBeenCalledTimes(2);
    expect(card("folder-7").querySelector<HTMLInputElement>(".library-folder-select input")?.disabled).toBe(true);
    await act(async () => finish());
  });

  it("removes stale selected folders when their actual parent changes", async () => {
    await setupFolders();
    selectFolder(7);
    vi.mocked(getLibrary).mockResolvedValue({ folder: null, parent_id: null, sort: "manual", order: "asc",
      items: [{ type: "video", video: videos[1] }] });
    act(() => { focusManager.setFocused(false); focusManager.setFocused(true); });
    await vi.waitFor(() => expect(container.querySelector('[data-library-key="folder-7"]')).toBeNull());
    expect(container.querySelector(".library-selection-dock")).toBeNull();
  });
});

describe("library selection and processing", () => {
  it("adds a contextual bottom bar without removing the original processing button", async () => {
    expect(container.querySelector(".library-selection-dock")).toBeNull();
    expect(container.querySelector(".library-selection-process")).not.toBeNull();
    selectVideo(1);
    const dock = container.querySelector(".library-selection-dock")!;
    expect(dock.textContent).toContain("已选 1 个视频");
    expect(dock.parentElement?.classList.contains("library-inner")).toBe(true);
    click(dock.querySelector(".library-selection-dock-process"));
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector(".library-processing-options")?.textContent).toContain("test-model");
    });
    expect(processVideo).not.toHaveBeenCalled();
    expect(processSelectedVideos).not.toHaveBeenCalled();
    expect(container.querySelector(".library-selection-process")?.getAttribute("aria-expanded")).toBe("true");
    // The persistent bottom entry reveals options; it must not toggle them closed.
    click(dock.querySelector(".library-selection-dock-process"));
    expect(container.querySelector(".library-selection-process")?.getAttribute("aria-expanded")).toBe("true");
    selectVideo(2);
    expect(dock.textContent).toContain("处理所选（2）");
    click(dock.querySelector(".text-button"));
    expect(container.querySelector(".library-selection-dock")).toBeNull();
    expect(container.querySelectorAll(".video-card.is-selected")).toHaveLength(0);
    expect(document.activeElement).toBe(container.querySelector(".library-select-all input"));
  });

  it.each([1, 2])("validates empty recall choices only on submitting %i video(s) and allows correction", async (count) => {
    selectVideo(1);
    if (count === 2) selectVideo(2);
    await openLibraryOptions();
    const options = container.querySelector(".library-processing-options")!;
    click(options.querySelector('input[value="medium_recall"]'));
    expect(options.querySelectorAll(".recall-mode-control input:checked")).toHaveLength(0);
    expect(options.querySelector('[role="alert"]')).toBeNull();
    submitLibraryProcessing();
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(options.querySelector('[role="alert"]')?.textContent).toContain("请至少选择一种考点召回类型");
      expect(options.querySelector<HTMLButtonElement>("button.primary-button")?.disabled).toBe(false);
    });
    expect(processVideo).not.toHaveBeenCalled();
    expect(processSelectedVideos).not.toHaveBeenCalled();
    click(options.querySelector('input[value="low_recall"]'));
    expect(options.querySelector('[role="alert"]')).toBeNull();
    submitLibraryProcessing();
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(count === 1 ? processVideo : processSelectedVideos).toHaveBeenCalledWith(
        count === 1 ? 1 : [1, 2], expect.objectContaining({ analysis_modes: ["low_recall"] }),
      );
    });
  });

  it("selects videos without opening them, supports partial/all selection and clears selection", () => {
    selectVideo(1);
    expect(window.location.pathname).toBe("/");
    expect(card("video-1").classList.contains("is-selected")).toBe(true);
    const selectAll = container.querySelector<HTMLInputElement>(".library-inner .library-select-all input")!;
    expect(selectAll.indeterminate).toBe(true);
    expect(selectAll.checked).toBe(false);
    click(selectAll);
    expect(selectAll.checked).toBe(true);
    expect(container.querySelector(".library-selection-count")?.textContent).toBe("已选 2 个视频");
    click(container.querySelector('[aria-label="清空选择"]'));
    expect(selectAll.checked).toBe(false);
    expect(selectAll.indeterminate).toBe(false);
    expect(container.querySelectorAll(".video-card.is-selected")).toHaveLength(0);
  });

  it("opens options on the library card and starts one video without navigation", async () => {
    click(card("video-1").querySelector(".library-card-process"));
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector(".library-processing-options")?.textContent).toContain("test-model");
    });
    act(() => container.querySelector<HTMLInputElement>('.library-processing-options input[value="low_recall"]')!.click());
    act(() => container.querySelector<HTMLInputElement>('.library-processing-options input[value="medium_recall"]')!.click());
    submitLibraryProcessing();
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(processVideo).toHaveBeenCalledWith(1, expect.objectContaining({ analysis_mode: "low_recall", asr_model_id: "whisper-turbo" }));
      expect(container.textContent).toContain("已提交视频处理任务");
    });
    expect(processSelectedVideos).not.toHaveBeenCalled();
    expect(window.location.pathname).toBe("/");
    await waitForExit(container, ".library-processing-options");
    expect(container.querySelectorAll(".video-card.is-selected")).toHaveLength(0);
  });

  it("submits multiple selected IDs using the durable batch flow and chosen options", async () => {
    selectVideo(2);
    selectVideo(1);
    await openLibraryOptions();
    act(() => container.querySelector<HTMLInputElement>('.library-processing-options input[value="high_recall"]')!.click());
    click(container.querySelector('.library-processing-fields .check-option input'));
    submitLibraryProcessing();
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(processSelectedVideos).toHaveBeenCalledWith([1, 2], expect.objectContaining({
        analysis_mode: "high_recall", force_retranscribe: true, filter_formula_noise: false,
      }));
      expect(container.textContent).toContain("已提交批处理，共 2 个视频");
    });
    expect(processVideo).not.toHaveBeenCalled();
    expect(window.location.pathname).toBe("/");
    expect(container.querySelectorAll(".video-card.is-selected")).toHaveLength(0);
  });

  it("retains selection and options after a submission failure and retries the same batch", async () => {
    vi.mocked(processSelectedVideos).mockRejectedValueOnce(new Error("fixture queue unavailable"));
    selectVideo(1); selectVideo(2);
    await openLibraryOptions();
    submitLibraryProcessing();
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector(".library-processing-options [role=alert]")?.textContent).toContain("fixture queue unavailable");
    });
    expect(container.querySelectorAll(".video-card.is-selected")).toHaveLength(2);
    expect(container.querySelector<HTMLButtonElement>(".library-processing-options button.primary-button")?.disabled).toBe(false);
    submitLibraryProcessing();
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(processSelectedVideos).toHaveBeenCalledTimes(2);
      expect(container.textContent).toContain("已提交批处理");
    });
  });

  it("prevents duplicate submissions and changing selection while a request is pending", async () => {
    let resolve!: (value: { job_id: string; status: string }) => void;
    vi.mocked(processSelectedVideos).mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
    selectVideo(1); selectVideo(2);
    await openLibraryOptions();
    submitLibraryProcessing();
    submitLibraryProcessing();
    await flushUpdates();
    expect(processSelectedVideos).toHaveBeenCalledTimes(1);
    expect(container.querySelector<HTMLButtonElement>(".library-selection-dock-process")?.disabled).toBe(true);
    expect(container.querySelector(".library-selection-dock-process")?.textContent).toContain("正在提交");
    expect(container.querySelector<HTMLButtonElement>(".library-selection-dock .text-button")?.disabled).toBe(true);
    expect(container.querySelector<HTMLInputElement>('.library-inner .library-select-all input')?.disabled).toBe(true);
    expect(card("video-1").querySelector<HTMLInputElement>('input[type="checkbox"]')?.disabled).toBe(true);
    await act(async () => resolve({ job_id: "selected", status: "queued" }));
    await flushUpdates();
  });

  it("keeps root and folder selection separate and submits only videos in the current folder", async () => {
    videos[1].folder_id = 7;
    await act(async () => window.dispatchEvent(new Event("focus")));
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 5)); });
    // Recreate observers to load the changed server folder assignment.
    act(() => root.unmount());
    root = createRoot(container);
    await act(async () => root.render(<App />));
    await vi.waitFor(async () => { await flushUpdates(); expect(container.querySelectorAll(".library-inner .video-card")).toHaveLength(1); });
    selectVideo(1);
    expect(container.querySelector(".library-inner > .library-selection-dock")).not.toBeNull();
    click(container.querySelector(".folder-card-link"));
    await vi.waitFor(async () => { await flushUpdates(); expect(container.querySelector(".folder-window .video-card")).not.toBeNull(); });
    expect(container.querySelector(".library-inner > .library-selection-dock")).toBeNull();
    expect(container.querySelector(".folder-window .library-selection-count")?.textContent).toBe("已选 0 个视频");
    selectVideo(2, true);
    expect(container.querySelector(".folder-window > .library-selection-dock")?.textContent).toContain("处理所选（1）");
    click(container.querySelector(".folder-window .library-selection-dock-process"));
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector(".folder-window .library-processing-options")?.textContent).toContain("test-model");
    });
    submitLibraryProcessing(true);
    await vi.waitFor(async () => { await flushUpdates(); expect(processVideo).toHaveBeenCalledWith(2, expect.any(Object)); });
    expect(processSelectedVideos).not.toHaveBeenCalled();
  });

  it("runs a selection inside a folder as a batch and keeps the folder open", async () => {
    await loadFolderVideos();
    click(container.querySelector(".folder-window .library-select-all input"));
    await openLibraryOptions(true);
    submitLibraryProcessing(true);
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(processSelectedVideos).toHaveBeenCalledWith([1, 2], expect.any(Object));
      expect(container.querySelector(".library-folder-notice")?.textContent).toContain("已提交批处理");
    });
    expect(container.querySelector(".folder-window")).not.toBeNull();
    expect(window.location.pathname).toBe("/");
  });

  it("excludes processing and resource-paused videos from selection", async () => {
    videos[0].status = "processing"; videos[1].status = "resource_paused";
    act(() => root.unmount());
    root = createRoot(container);
    await act(async () => root.render(<App />));
    await vi.waitFor(async () => { await flushUpdates(); expect(card("video-1").textContent).toContain("处理中"); });
    expect(container.querySelector<HTMLInputElement>(".library-select-all input")?.disabled).toBe(true);
    expect(card("video-1").querySelector<HTMLInputElement>('input[type="checkbox"]')?.disabled).toBe(true);
    expect(card("video-2").querySelector<HTMLButtonElement>('.library-card-process')?.disabled).toBe(true);
  });

  it("shows configuration errors and does not submit until configuration can be loaded", async () => {
    vi.mocked(getAsrSelection).mockRejectedValueOnce(new Error("fixture config unavailable"));
    selectVideo(1);
    click(container.querySelector(".library-selection-process"));
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector(".library-processing-options [role=alert]")?.textContent).toContain("fixture config unavailable");
    });
    expect(container.querySelector<HTMLButtonElement>(".library-processing-options button.primary-button")?.disabled).toBe(true);
    click(container.querySelector('[aria-label="重新读取处理配置"]'));
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector<HTMLButtonElement>(".library-processing-options button.primary-button")?.disabled).toBe(false);
    });
    expect(processVideo).not.toHaveBeenCalled();
  });
});

describe("folder window", () => {
  it("opens above the unchanged library without navigating and restores focus and scrolling on close", async () => {
    setSelectValue('.library-toolbar select', "name");
    const background = container.querySelector<HTMLDivElement>(".library-inner")!;
    const opener = container.querySelector<HTMLButtonElement>(".folder-card-link")!;
    const rootOrder = [...background.querySelectorAll(".library-card")].map((item) => item.getAttribute("data-library-key"));
    act(() => opener.focus());
    click(opener);
    await flushUpdates();
    const dialog = container.querySelector(".folder-window")!;
    expect(dialog.getAttribute("role")).toBe("dialog");
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(window.location.pathname).toBe("/");
    expect(container.querySelector("h1")?.textContent).toBe("视频库");
    expect(container.querySelector(".library-inner")).toBe(background);
    expect(background.inert).toBe(true);
    expect(document.body.style.overflow).toBe("hidden");
    expect(document.activeElement).toBe(dialog.querySelector(".folder-window-close"));
    await vi.waitFor(async () => { await flushUpdates(); expect(container.querySelector(".folder-window-empty")?.textContent).toContain("这个文件夹还没有视频"); });
    click(dialog.querySelector(".folder-window-close"));
    expect(container.querySelector(".folder-window-backdrop.is-leaving")).not.toBeNull();
    await waitForExit(container, ".folder-window");
    expect(container.querySelector(".folder-window")).toBeNull();
    expect(background.inert).toBe(false);
    expect(document.body.style.overflow).toBe("");
    expect(document.activeElement).toBe(opener);
    expect(container.querySelector<HTMLSelectElement>(".library-toolbar select")!.value).toBe("name");
    expect([...background.querySelectorAll(".library-card")].map((item) => item.getAttribute("data-library-key"))).toEqual(rootOrder);
  });

  it("closes on Escape without navigating", async () => {
    click(container.querySelector(".folder-card-link"));
    await flushUpdates();
    expect(keyDown("Escape").defaultPrevented).toBe(true);
    await waitForExit(container, ".folder-window");
    expect(container.querySelector(".folder-window")).toBeNull();
    expect(window.location.pathname).toBe("/");
  });

  it("closes on a backdrop click but not content clicks or a press that began inside the window", async () => {
    click(container.querySelector(".folder-card-link"));
    await flushUpdates();
    const backdrop = container.querySelector(".folder-window-backdrop")!;
    const heading = container.querySelector(".folder-window-heading")!;
    click(heading);
    expect(container.querySelector(".folder-window")).not.toBeNull();
    act(() => heading.dispatchEvent(new MouseEvent("mousedown", { bubbles: true })));
    click(backdrop);
    expect(container.querySelector(".folder-window")).not.toBeNull();
    act(() => backdrop.dispatchEvent(new MouseEvent("mousedown", { bubbles: true })));
    click(backdrop);
    await waitForExit(container, ".folder-window");
    expect(container.querySelector(".folder-window")).toBeNull();
  });

  it("closes immediately when reduced motion is requested", async () => {
    vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: true })));
    click(container.querySelector(".folder-card-link"));
    await flushUpdates();
    keyDown("Escape");
    expect(container.querySelector(".folder-window")).toBeNull();
  });

  it("traps Tab and Shift+Tab and prevents focus from reaching the background", async () => {
    click(container.querySelector(".folder-card-link"));
    await flushUpdates();
    const close = container.querySelector<HTMLButtonElement>(".folder-window-close")!;
    const last = container.querySelector<HTMLButtonElement>(".folder-window-toolbar > button:last-child")!;
    expect(keyDown("Tab", true).defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(last);
    expect(keyDown("Tab").defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(close);
    act(() => container.querySelector<HTMLSelectElement>(".library-toolbar select")!.focus());
    expect(document.activeElement).toBe(close);
  });

  it("keeps the root sort independent from the folder sort and folder drag order", async () => {
    await loadFolderVideos();
    setSelectValue('.folder-window select[aria-label="文件夹内排序"]', "name");
    expect(container.querySelector<HTMLSelectElement>(".library-toolbar select")!.value).toBe("manual");
    const first = container.querySelector<HTMLElement>('.folder-window [data-library-key="video-1"]')!;
    const second = container.querySelector<HTMLElement>('.folder-window [data-library-key="video-2"]')!;
    dragEvent("dragstart", first);
    dragEvent("dragover", second, 390);
    await act(async () => { dragEvent("drop", second, 390); });
    expect(reorderLibrary).toHaveBeenCalledWith(7, [{ type: "video", id: 2 }, { type: "video", id: 1 }]);
    expect(moveVideoToFolder).not.toHaveBeenCalled();
    expect(container.querySelector<HTMLSelectElement>('.folder-window select[aria-label="文件夹内排序"]')!.value).toBe("manual");
    expect(container.querySelector<HTMLSelectElement>(".library-toolbar select")!.value).toBe("manual");
  });

  it("moves videos back to the root while keeping the folder window open and updating both lists", async () => {
    await loadFolderVideos();
    moveThroughMenu(1, "一级目录");
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector('.folder-window [data-library-key="video-1"]')).toBeNull();
      expect(container.querySelector('.library-inner [data-library-key="video-1"]')).not.toBeNull();
    });
    expect(moveVideoToFolder).toHaveBeenCalledWith(1, null);
    expect(container.querySelector(".folder-window")).not.toBeNull();
    expect(container.querySelector(".folder-window-heading")?.textContent).toContain("1 个视频");
    expect(container.querySelector(".folder-card-link")?.textContent).toContain("1 个视频");
    expect(container.querySelector(".toast")?.textContent).toContain("视频已放回一级目录");
    moveThroughMenu(2, "一级目录");
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector(".folder-window-empty")?.textContent).toContain("这个文件夹还没有视频");
    });
  });

  it("moves a dragged folder video onto the backdrop to the root without closing the window", async () => {
    await loadFolderVideos();
    const backdrop = container.querySelector(".folder-window-backdrop")!;
    act(() => backdrop.dispatchEvent(new MouseEvent("mousedown", { bubbles: true })));
    beginVideoDrag();
    expect(container.querySelector(".folder-outside-drop-label")?.textContent).toBe("移至一级目录");
    expect(dragEvent("dragover", backdrop).defaultPrevented).toBe(true);
    expect(backdrop.classList.contains("is-outside-drop")).toBe(true);
    await act(async () => { dragEvent("drop", backdrop); });
    click(backdrop);
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector('.folder-window [data-library-key="video-1"]')).toBeNull();
      expect(container.querySelector('.library-inner [data-library-key="video-1"]')).not.toBeNull();
    });
    expect(moveVideoToFolder).toHaveBeenCalledTimes(1);
    expect(moveVideoToFolder).toHaveBeenCalledWith(1, null);
    expect(reorderLibrary).not.toHaveBeenCalled();
    expect(container.querySelector(".folder-window-heading")?.textContent).toContain("1 个视频");
    expect(container.querySelector(".folder-card-link")?.textContent).toContain("1 个视频");
    expect(container.querySelector(".toast")?.textContent).toContain("视频已放回一级目录");
    expect(container.querySelector(".folder-window")).not.toBeNull();
    expect(container.querySelector(".folder-outside-drop-label")).toBeNull();
    expect(backdrop.classList.contains("is-outside-drop")).toBe(false);
    expect(JSON.parse(window.localStorage.getItem("classroom-video-library")!).assignments["1"]).toBeNull();
    dragEvent("dragstart", card("video-2"));
    await act(async () => { dragEvent("drop", backdrop); });
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector(".folder-window-empty")?.textContent).toContain("这个文件夹还没有视频");
      expect(container.querySelector(".folder-card-link")?.textContent).toContain("0 个视频");
    });
  });

  it("leaves folder contents unchanged when a drag-out move fails", async () => {
    await loadFolderVideos();
    vi.mocked(moveVideoToFolder).mockRejectedValueOnce(new Error("Move failed"));
    beginVideoDrag();
    await act(async () => { dragEvent("drop", container.querySelector(".folder-window-backdrop")!); });
    expect(container.querySelector(".library-operation-error")?.textContent).toContain("视频移动失败：Move failed");
    expect(container.querySelectorAll(".folder-window .video-card")).toHaveLength(2);
    expect(container.querySelectorAll(".library-inner .video-card")).toHaveLength(0);
    expect(container.querySelector(".folder-window-heading")?.textContent).toContain("2 个视频");
    expect(container.querySelector(".folder-outside-drop-label")).toBeNull();
  });

  it("only submits one move for duplicate outside drops and waits for the server response", async () => {
    await loadFolderVideos();
    let finish!: (video: Video) => void;
    vi.mocked(moveVideoToFolder).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    const backdrop = container.querySelector(".folder-window-backdrop")!;
    beginVideoDrag();
    act(() => {
      dragEvent("drop", backdrop);
      dragEvent("drop", backdrop);
    });
    expect(moveVideoToFolder).toHaveBeenCalledTimes(1);
    expect(moveVideoToFolder).toHaveBeenCalledWith(1, null);
    expect(container.querySelectorAll(".folder-window .video-card")).toHaveLength(2);
    expect(container.querySelector(".toast")).toBeNull();
    videos[0] = { ...videos[0], folder_id: null };
    await act(async () => { finish(videos[0]); });
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector('.folder-window [data-library-key="video-1"]')).toBeNull();
    });
    await flushUpdates();
  });

  it("clears outside feedback on re-entry and cancellation without moving a video", async () => {
    await loadFolderVideos();
    const backdrop = container.querySelector(".folder-window-backdrop")!;
    beginVideoDrag();
    dragEvent("dragover", backdrop);
    expect(backdrop.classList.contains("is-outside-drop")).toBe(true);
    dragEvent("dragover", container.querySelector(".folder-window-heading")!);
    expect(backdrop.classList.contains("is-outside-drop")).toBe(false);
    dragEvent("dragover", backdrop);
    dragEvent("dragleave", backdrop, 0, document.body);
    expect(backdrop.classList.contains("is-outside-drop")).toBe(false);
    dragEvent("dragover", backdrop);
    dragEvent("dragend", card("video-1"));
    expect(container.querySelector(".folder-outside-drop-label")).toBeNull();
    expect(dragEvent("drop", backdrop).defaultPrevented).toBe(false);
    expect(moveVideoToFolder).not.toHaveBeenCalled();
  });

  it("does not move a video when dropped inside the folder on non-card content", async () => {
    await loadFolderVideos();
    beginVideoDrag();
    const heading = container.querySelector(".folder-window-heading")!;
    expect(dragEvent("dragover", heading).defaultPrevented).toBe(false);
    expect(dragEvent("drop", heading).defaultPrevented).toBe(false);
    expect(moveVideoToFolder).not.toHaveBeenCalled();
    expect(container.querySelectorAll(".folder-window .video-card")).toHaveLength(2);
  });

  it("rejects external drags and drags originating outside the open folder", async () => {
    await loadFolderVideos();
    const backdrop = container.querySelector(".folder-window-backdrop")!;
    expect(dragEvent("dragover", backdrop).defaultPrevented).toBe(false);
    expect(dragEvent("drop", backdrop).defaultPrevented).toBe(false);
    dragEvent("dragstart", card("folder-7"));
    expect(dragEvent("dragover", backdrop).defaultPrevented).toBe(false);
    expect(dragEvent("drop", backdrop).defaultPrevented).toBe(false);
    dragEvent("dragend", card("folder-7"));
    moveThroughMenu(1, "一级目录");
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector('.library-inner [data-library-key="video-1"]')).not.toBeNull();
    });
    vi.mocked(moveVideoToFolder).mockClear();
    beginVideoDrag();
    expect(dragEvent("dragover", backdrop).defaultPrevented).toBe(false);
    expect(dragEvent("drop", backdrop).defaultPrevented).toBe(false);
    expect(moveVideoToFolder).not.toHaveBeenCalled();
  });

  it("keeps the previous contents and counts when moving a video fails", async () => {
    await loadFolderVideos();
    vi.mocked(moveVideoToFolder).mockRejectedValueOnce(new Error("Move failed"));
    moveThroughMenu(1, "一级目录");
    await flushUpdates();
    expect(container.querySelectorAll(".folder-window .video-card")).toHaveLength(2);
    expect(container.querySelectorAll(".library-inner .video-card")).toHaveLength(0);
    expect(container.querySelector(".folder-window-heading")?.textContent).toContain("2 个视频");
    expect(container.querySelector(".library-operation-error")?.textContent).toContain("视频移动失败：Move failed");
  });

  it("shows folder loading and supports retry after a failed folder request", async () => {
    let finish!: (response: LibraryResponse) => void;
    vi.mocked(getLibrary).mockImplementation((id) => id === 7
      ? new Promise((resolve) => { finish = resolve; }) : Promise.resolve(libraryResponse()));
    click(container.querySelector(".folder-card-link"));
    expect(container.querySelector(".folder-window-empty")?.textContent).toContain("正在读取文件夹");
    await act(async () => { finish(libraryResponse(7)); });
    await flushUpdates();
    expect(container.querySelector(".folder-window-empty")?.textContent).toContain("这个文件夹还没有视频");
    click(container.querySelector(".folder-window-close"));
    act(() => root.unmount());
    root = createRoot(container);
    vi.mocked(getLibrary).mockImplementation(async (id) => {
      if (id === 7) throw new Error("Folder unavailable");
      return libraryResponse();
    });
    await act(async () => root.render(<App />));
    await vi.waitFor(async () => { await flushUpdates(); expect(container.querySelector(".folder-card-link")).not.toBeNull(); });
    click(container.querySelector(".folder-card-link"));
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector(".folder-window .local-error")?.textContent).toContain("Folder unavailable");
    });
    vi.mocked(getLibrary).mockImplementation(async (id) => libraryResponse(id ?? null));
    click(container.querySelector('.folder-window button[aria-label="重新加载文件夹"]'));
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector(".folder-window .local-error")).toBeNull();
      expect(container.querySelector(".folder-window-empty")?.textContent).toContain("这个文件夹还没有视频");
    });
  });

  it("closes a directly opened folder route back to the library", async () => {
    window.history.replaceState({}, "", "/library/folder/folder-7");
    await remountWithCachedFolders();
    await vi.waitFor(async () => { await flushUpdates(); expect(container.querySelector("#folder-window-title")?.textContent).toBe("Algebra"); });
    keyDown("Escape");
    await waitForExit(container, ".folder-window");
    expect(window.location.pathname).toBe("/");
    expect(container.querySelector(".folder-window")).toBeNull();
    expect(container.querySelector("h1")?.textContent).toBe("视频库");
    expect(document.body.style.overflow).toBe("");
  });

  it("still opens review from a folder video and restores the body state on navigation", async () => {
    await loadFolderVideos();
    await act(async () => { click(container.querySelector('.folder-window [data-library-key="video-1"] .library-card-body')); });
    expect(window.location.pathname).toBe("/review/1");
    expect(container.querySelector(".folder-window")).toBeNull();
    expect(document.body.style.overflow).toBe("");
  });
});

describe("nested library folders", () => {
  async function mountTree(path = "/") {
    const folders = [
      { ...folder, parent_id: null as number | null },
      { id: 8, name: "Chapter", parent_id: 7, created_at: folder.created_at },
      { id: 9, name: "Section", parent_id: 8, created_at: folder.created_at },
      { id: 10, name: "Empty", parent_id: 7, created_at: folder.created_at },
    ];
    videos[0].folder_id = 9;
    videos[1].folder_id = 7;
    vi.mocked(getLibrary).mockImplementation(async (id) => {
      const current = folders.find((item) => item.id === id);
      const summarize = (item: typeof folders[number]) => {
        const descendants = new Set([item.id]);
        for (const candidate of folders) if (candidate.parent_id != null && descendants.has(candidate.parent_id)) descendants.add(candidate.id);
        const included = videos.filter((video) => video.folder_id != null && descendants.has(video.folder_id));
        return { ...item, video_count: included.length, marker_importance_counts: {
          standard: included.reduce((sum, video) => sum + (video.marker_importance_counts?.standard ?? 0), 0),
          important: included.reduce((sum, video) => sum + (video.marker_importance_counts?.important ?? 0), 0),
          critical: included.reduce((sum, video) => sum + (video.marker_importance_counts?.critical ?? 0), 0),
        } };
      };
      const ancestors: typeof folders = [];
      let ancestor = current;
      while (ancestor) {
        ancestors.unshift(ancestor);
        ancestor = folders.find((item) => item.id === ancestor?.parent_id);
      }
      return {
        folder: current ? summarize(current) : null, ancestors: ancestors.map(summarize), parent_id: id ?? null, sort: "manual", order: "asc",
        items: [
          ...folders.filter((item) => item.parent_id === (id ?? null)).map((item) => ({ type: "folder" as const, folder: summarize(item) })),
          ...videos.filter((video) => video.folder_id === (id ?? null)).map((video) => ({ type: "video" as const, video: { ...video } })),
        ],
      };
    });
    vi.mocked(createLibraryFolder).mockImplementation(async (name, parentId) => {
      const created = { id: 11, name, parent_id: parentId ?? null, created_at: folder.created_at };
      folders.push(created);
      return created;
    });
    vi.mocked(moveLibraryFolder).mockImplementation(async (id, parentId) => {
      const source = folders.find((item) => item.id === id)!;
      source.parent_id = parentId;
      return { ...source };
    });
    act(() => root.unmount());
    window.sessionStorage.clear();
    window.localStorage.setItem("classroom-video-library", "{}");
    window.history.replaceState({}, "", path);
    root = createRoot(container);
    await act(async () => root.render(<App />));
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelectorAll(".library-inner .folder-card")).toHaveLength(1);
    });
    return folders;
  }

  async function openFolder(id: number) {
    click(card(`folder-${id}`).querySelector(".folder-card-link"));
    await flushUpdates();
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(getLibrary).toHaveBeenCalledWith(id);
      expect(container.querySelector(".folder-window-empty .spin")).toBeNull();
    });
  }

  it("moves to a previously unvisited descendant through the collapsed menu tree", async () => {
    await mountTree();
    await openFolder(7);
    expect(getLibrary).not.toHaveBeenCalledWith(8);
    expect(getLibrary).not.toHaveBeenCalledWith(9);
    click(card("video-2").querySelector('[aria-haspopup="menu"]'));
    expect(card("video-2").querySelector('[data-move-folder="8"]')).toBeNull();
    click(card("video-2").querySelector('[data-folder-id="7"] > .move-folder-row .move-folder-toggle'));
    await vi.waitFor(async () => { await flushUpdates(); expect(card("video-2").querySelector('[data-move-folder="8"]')).not.toBeNull(); });
    click(card("video-2").querySelector('[data-folder-id="8"] > .move-folder-row .move-folder-toggle'));
    await vi.waitFor(async () => { await flushUpdates(); expect(card("video-2").querySelector('[data-move-folder="9"]')).not.toBeNull(); });
    expect(card("video-2").querySelector('[data-move-folder="9"]')?.getAttribute("title")).toBe("视频库 / Algebra / Chapter / Section");
    click(card("video-2").querySelector('[data-move-folder="9"]'));
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector('.folder-window [data-library-key="video-2"]')).toBeNull();
    });
    expect(moveVideoToFolder).toHaveBeenCalledTimes(1);
    expect(moveVideoToFolder).toHaveBeenCalledWith(2, 9);
    expect(videos[1].folder_id).toBe(9);
    expect(window.location.pathname).toBe("/");
  });

  it("shows recursive video and label totals before opening descendants and refreshes each subtree", async () => {
    videos[0].marker_importance_counts = { standard: 3, important: 4, critical: 5 };
    videos[1].marker_importance_counts = { standard: 2, important: 1, critical: 0 };
    await mountTree();
    const counts = (id: number) => [...card(`folder-${id}`).querySelectorAll(".video-marker-count")].map(node => node.textContent);
    expect(card("folder-7").textContent).toContain("2 个视频");
    expect(counts(7)).toEqual(["5", "5", "5"]);
    expect(getLibrary).not.toHaveBeenCalledWith(8);
    expect(getLibrary).not.toHaveBeenCalledWith(9);
    await openFolder(7);
    expect(card("folder-8").textContent).toContain("1 个视频");
    expect(counts(8)).toEqual(["3", "4", "5"]);
    expect(counts(10)).toEqual(["0", "0", "0"]);
    expect(container.querySelector(".folder-window-heading")?.textContent).toContain("2 个视频");
    videos[0].folder_id = 10;
    videos[0].marker_importance_counts = { standard: 2, important: 5, critical: 5 };
    act(() => focusManager.setFocused(false));
    await act(async () => focusManager.setFocused(true));
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(card("folder-8").textContent).toContain("0 个视频");
      expect(counts(8)).toEqual(["0", "0", "0"]);
      expect(counts(10)).toEqual(["2", "5", "5"]);
      expect(counts(7)).toEqual(["4", "6", "5"]);
    });
  });

  it("opens multiple levels, keeps descendants out of the root, and navigates back through the path", async () => {
    await mountTree();
    const opener = card("folder-7").querySelector(".folder-card-link");
    await openFolder(7);
    expect(container.querySelectorAll(".folder-window .folder-card")).toHaveLength(2);
    expect(container.querySelectorAll(".folder-window .video-card")).toHaveLength(1);
    await openFolder(8);
    expect(container.querySelector("#folder-window-title")?.textContent).toBe("Chapter");
    await openFolder(9);
    expect(container.querySelector("#folder-window-title")?.textContent).toBe("Section");
    expect(container.querySelector(".folder-breadcrumbs")?.textContent).toContain("AlgebraChapterSection");
    expect(container.querySelectorAll(".library-inner .folder-card")).toHaveLength(1);
    expect(container.querySelectorAll(".folder-window .video-card")).toHaveLength(1);
    click(container.querySelector('[aria-label="返回上一级"]'));
    await flushUpdates();
    expect(container.querySelector("#folder-window-title")?.textContent).toBe("Chapter");
    click([...container.querySelectorAll(".folder-breadcrumbs button")].find((button) => button.textContent === "Algebra")!);
    await flushUpdates();
    expect(container.querySelectorAll(".folder-window .folder-card")).toHaveLength(2);
    keyDown("Escape");
    await waitForExit(container, ".folder-window");
    expect(document.activeElement).toBe(opener);
  });

  it("opens a deep link without a folder cache and restores the same level on reload", async () => {
    await mountTree("/library/folder/folder-9");
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector("#folder-window-title")?.textContent).toBe("Section");
    });
    expect(container.querySelector(".folder-breadcrumbs")?.textContent).toContain("AlgebraChapterSection");
    act(() => root.unmount());
    root = createRoot(container);
    await act(async () => root.render(<App />));
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector("#folder-window-title")?.textContent).toBe("Section");
    });
    click(container.querySelector('[aria-label="返回上一级"]'));
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector("#folder-window-title")?.textContent).toBe("Chapter");
    });
  });

  it("keeps the open nested window and child folders during background refreshes", async () => {
    await mountTree();
    await openFolder(7);
    await openFolder(8);
    const dialog = container.querySelector(".folder-window");
    vi.mocked(getLibrary).mockClear();
    videos[0].status = "processing";
    act(() => focusManager.setFocused(false));
    await act(async () => focusManager.setFocused(true));
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(getLibrary).toHaveBeenCalledWith();
      expect(getLibrary).toHaveBeenCalledWith(8);
    });
    expect(container.querySelector(".folder-window")).toBe(dialog);
    expect(container.querySelector('.folder-window [data-library-key="folder-9"]')).not.toBeNull();
    expect(container.querySelectorAll(".library-inner .folder-card")).toHaveLength(1);
  });

  it("restores each folder scroll on navigation without inheriting the parent scroll on reload", async () => {
    await mountTree();
    await openFolder(7);
    const parentContent = container.querySelector<HTMLElement>(".folder-window-content")!;
    act(() => { parentContent.scrollTop = 120; parentContent.dispatchEvent(new Event("scroll")); });
    await openFolder(8);
    expect(container.querySelector(".folder-window-content")!.scrollTop).toBe(0);
    click(container.querySelector('[aria-label="返回上一级"]'));
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector(".folder-window-content")!.scrollTop).toBe(120);
    });
    await openFolder(8);
    act(() => root.unmount());
    root = createRoot(container);
    await act(async () => root.render(<App />));
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector("#folder-window-title")?.textContent).toBe("Chapter");
      expect(container.querySelector(".folder-window-content")!.scrollTop).toBe(0);
    });
  });

  it("creates a child in the currently opened folder and keeps empty children visible", async () => {
    await mountTree();
    await openFolder(7);
    await openFolder(8);
    click(container.querySelector(".folder-window-toolbar > button:last-child"));
    const input = container.querySelector<HTMLInputElement>(".folder-dialog input")!;
    act(() => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "New section");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    act(() => container.querySelector(".folder-dialog")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(createLibraryFolder).toHaveBeenCalledWith("New section", 8);
      expect(container.querySelector('.folder-window [data-library-key="folder-11"]')).not.toBeNull();
    });
    expect(container.querySelector('.library-inner [data-library-key="folder-11"]')).toBeNull();
    await openFolder(11);
    expect(container.querySelector(".folder-window-empty")?.textContent).toContain("这个文件夹还没有视频");
  });

  it("sorts sibling folders in their own parent rather than the root", async () => {
    await mountTree();
    await openFolder(7);
    const first = card("folder-8"), second = card("folder-10");
    dragEvent("dragstart", first);
    dragEvent("dragover", second, 390);
    await act(async () => { dragEvent("drop", second, 390); });
    expect(reorderLibrary).toHaveBeenCalledWith(7, [
      { type: "folder", id: 10 }, { type: "folder", id: 8 }, { type: "video", id: 2 },
    ]);
    expect(moveLibraryFolder).not.toHaveBeenCalled();
  });

  it("uses different action labels and target shapes for nesting and sorting", async () => {
    await mountTree();
    await openFolder(7);
    dragEvent("dragstart", card("folder-8"));
    const target = card("folder-10");
    dragEvent("dragover", target);
    expect(target.classList.contains("drop-folder")).toBe(true);
    expect(target.querySelector(".library-drop-feedback.is-move")?.textContent).toBe("移入「Empty」");
    dragEvent("dragover", target, 390);
    expect(target.classList.contains("drop-folder")).toBe(false);
    expect(target.classList.contains("drop-after")).toBe(true);
    expect(target.querySelector(".library-drop-feedback.is-order")?.textContent).toBe("放在后面");
    expect(container.querySelectorAll(".folder-drop-destination")).toHaveLength(1);
    dragEvent("dragend", card("folder-8"));
    expect(container.querySelector(".folder-drop-destinations")).toBeNull();
    expect(container.querySelector(".library-drop-feedback")).toBeNull();
    expect(moveLibraryFolder).not.toHaveBeenCalled();
  });

  it("keeps folder borders and exit destinations when drag text is disabled", async () => {
    await mountTree();
    await openFolder(7);
    act(() => { saveDragTextHints(false); });
    dragEvent("dragstart", card("folder-8"));
    dragEvent("dragover", card("folder-10"));
    expect(card("folder-10").classList.contains("drop-folder")).toBe(true);
    expect(container.querySelector(".library-drop-feedback")).toBeNull();
    expect(container.querySelector(".folder-outside-drop-label")).toBeNull();
    expect(container.querySelector(".folder-drop-destination")?.textContent).toBe("移至视频库视频库");
    await act(async () => { dragEvent("drop", container.querySelector(".folder-drop-destination")!); });
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector('.library-inner [data-library-key="folder-8"]')).not.toBeNull();
    });
    expect(moveLibraryFolder).toHaveBeenCalledWith(8, null);
  });

  it("offers an in-window parent target without shifting the folder list or navigating", async () => {
    await mountTree();
    await openFolder(7);
    await openFolder(8);
    expect(container.querySelector(".folder-drop-destinations")).toBeNull();
    const list = container.querySelector(".folder-window-list");
    dragEvent("dragstart", card("folder-9"));
    const target = container.querySelector('.folder-drop-destination[data-drop-destination="folder-7"]')!;
    expect(target.textContent).toBe("移至上一级Algebra");
    expect(container.querySelector(".folder-window-list")).toBe(list);
    expect(dragEvent("dragover", target).defaultPrevented).toBe(true);
    expect(target.getAttribute("data-drop-active")).toBe("true");
    dragEvent("dragover", container.querySelector(".folder-window-toolbar")!);
    expect(target.hasAttribute("data-drop-active")).toBe(false);
    await act(async () => { dragEvent("drop", target); });
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector('.folder-window [data-library-key="folder-9"]')).toBeNull();
    });
    expect(moveLibraryFolder).toHaveBeenCalledTimes(1);
    expect(moveLibraryFolder).toHaveBeenCalledWith(9, 7);
    expect(container.querySelector("#folder-window-title")?.textContent).toBe("Chapter");
    expect(container.querySelector(".folder-drop-destinations")).toBeNull();
    expect(window.location.pathname).toBe("/");
  });

  it("moves a deep child directly to the root using the second destination", async () => {
    await mountTree();
    await openFolder(7);
    await openFolder(8);
    dragEvent("dragstart", card("folder-9"));
    expect(container.querySelectorAll(".folder-drop-destination")).toHaveLength(2);
    const target = container.querySelector('.folder-drop-destination[data-drop-destination="root"]')!;
    expect(target.textContent).toBe("移至根目录视频库");
    await act(async () => { dragEvent("drop", target); });
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector('.library-inner [data-library-key="folder-9"]')).not.toBeNull();
    });
    expect(moveLibraryFolder).toHaveBeenCalledWith(9, null);
    expect(container.querySelector("#folder-window-title")?.textContent).toBe("Chapter");
    expect(videos[0].folder_id).toBe(9);
    expect(reorderLibrary).not.toHaveBeenCalled();
  });

  it("moves to a breadcrumb ancestor without opening it", async () => {
    await mountTree();
    await openFolder(7);
    await openFolder(8);
    await openFolder(9);
    beginVideoDrag();
    const target = container.querySelector('.folder-breadcrumbs button[data-drop-destination="folder-7"]')!;
    expect(dragEvent("dragover", target).defaultPrevented).toBe(true);
    expect(target.getAttribute("data-drop-active")).toBe("true");
    await act(async () => { dragEvent("drop", target); });
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector(".folder-window .video-card")).toBeNull();
    });
    expect(moveVideoToFolder).toHaveBeenCalledTimes(1);
    expect(moveVideoToFolder).toHaveBeenCalledWith(1, 7);
    expect(container.querySelector("#folder-window-title")?.textContent).toBe("Section");
    expect(window.location.pathname).toBe("/");
  });

  it.each([[8, 10], [10, 8]])("moves sibling folder %s into %s at the center and survives reload", async (source, target) => {
    await mountTree();
    await openFolder(7);
    dragEvent("dragstart", card(`folder-${source}`));
    dragEvent("dragover", card(`folder-${target}`));
    expect(card(`folder-${target}`).classList.contains("drop-folder")).toBe(true);
    await act(async () => { dragEvent("drop", card(`folder-${target}`)); });
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector(`.folder-window [data-library-key="folder-${source}"]`)).toBeNull();
    });
    expect(moveLibraryFolder).toHaveBeenCalledTimes(1);
    expect(moveLibraryFolder).toHaveBeenCalledWith(source, target);
    expect(reorderLibrary).not.toHaveBeenCalled();
    expect(videos.map((video) => video.folder_id)).toEqual([9, 7]);
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 260)); });
    await openFolder(target);
    expect(container.querySelector(`.folder-window [data-library-key="folder-${source}"]`)).not.toBeNull();
    act(() => root.unmount());
    root = createRoot(container);
    await act(async () => root.render(<App />));
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector(`.folder-window [data-library-key="folder-${source}"]`)).not.toBeNull();
    });
  });

  it("moves a whole child folder out to the root and then into another root folder", async () => {
    await mountTree();
    await openFolder(7);
    dragEvent("dragstart", card("folder-8"));
    const backdrop = container.querySelector(".folder-window-backdrop")!;
    expect(container.querySelector(".folder-outside-drop-label")?.textContent).toBe("移至一级目录");
    dragEvent("dragover", backdrop);
    await act(async () => { dragEvent("drop", backdrop); });
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector('.folder-window [data-library-key="folder-8"]')).toBeNull();
      expect(container.querySelector('.library-inner [data-library-key="folder-8"]')).not.toBeNull();
    });
    expect(moveLibraryFolder).toHaveBeenCalledWith(8, null);
    expect(container.querySelector("#folder-window-title")?.textContent).toBe("Algebra");
    keyDown("Escape");
    await waitForExit(container, ".folder-window");
    dragEvent("dragstart", card("folder-8"));
    dragEvent("dragover", card("folder-7"));
    await act(async () => { dragEvent("drop", card("folder-7")); });
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector('.library-inner [data-library-key="folder-8"]')).toBeNull();
    });
    expect(moveLibraryFolder).toHaveBeenCalledWith(8, 7);
    expect(reorderLibrary).not.toHaveBeenCalled();
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 260)); });
    await openFolder(7);
    await openFolder(8);
    await openFolder(9);
    expect(container.querySelector('.folder-window [data-library-key="video-1"]')).not.toBeNull();
  });

  it("moves a deep child folder only to its immediate parent", async () => {
    await mountTree();
    await openFolder(7);
    await openFolder(8);
    dragEvent("dragstart", card("folder-9"));
    const backdrop = container.querySelector(".folder-window-backdrop")!;
    dragEvent("dragover", backdrop);
    expect(container.querySelector(".folder-outside-drop-label")?.textContent).toBe("移至上一级文件夹");
    await act(async () => { dragEvent("drop", backdrop); });
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector('.folder-window [data-library-key="folder-9"]')).toBeNull();
    });
    expect(moveLibraryFolder).toHaveBeenCalledTimes(1);
    expect(moveLibraryFolder).toHaveBeenCalledWith(9, 7);
    click(container.querySelector('[aria-label="返回上一级"]'));
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector('.folder-window [data-library-key="folder-9"]')).not.toBeNull();
    });
    expect(container.querySelector('.library-inner [data-library-key="folder-9"]')).toBeNull();
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 260)); });
    await openFolder(9);
    expect(container.querySelector('.folder-window [data-library-key="video-1"]')).not.toBeNull();
  });

  it("keeps folder contents on failure and retries the same move", async () => {
    await mountTree();
    await openFolder(7);
    vi.mocked(moveLibraryFolder).mockRejectedValueOnce(new Error("Move denied"));
    dragEvent("dragstart", card("folder-8"));
    await act(async () => { dragEvent("drop", card("folder-10")); });
    expect(container.querySelector(".folder-window .library-operation-error")?.textContent).toContain("文件夹移动失败：Move denied");
    expect(container.querySelector('.folder-window [data-library-key="folder-8"]')).not.toBeNull();
    expect(card("folder-8").draggable).toBe(true);
    expect(container.querySelector(".toast")).toBeNull();
    click(container.querySelector(".folder-window .library-operation-error button"));
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector('.folder-window [data-library-key="folder-8"]')).toBeNull();
    });
    expect(moveLibraryFolder).toHaveBeenCalledTimes(2);
    expect(moveLibraryFolder).toHaveBeenLastCalledWith(8, 10);
  });

  it("waits for confirmation and prevents duplicate folder moves", async () => {
    const folders = await mountTree();
    await openFolder(7);
    let finish!: (value: typeof folders[number]) => void;
    vi.mocked(moveLibraryFolder).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    dragEvent("dragstart", card("folder-8"));
    dragEvent("drop", card("folder-10"));
    expect(card("folder-8").draggable).toBe(false);
    expect(card("folder-8").getAttribute("aria-busy")).toBe("true");
    expect(container.querySelector(".toast")).toBeNull();
    dragEvent("drop", card("folder-10"));
    expect(moveLibraryFolder).toHaveBeenCalledTimes(1);
    folders.find((item) => item.id === 8)!.parent_id = 10;
    await act(async () => { finish(folders.find((item) => item.id === 8)!); });
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector('.folder-window [data-library-key="folder-8"]')).toBeNull();
    });
    expect(container.querySelector(".toast")?.textContent).toContain("文件夹已移入目标文件夹");
  });

  it("rejects self-drops and moving an ancestor into a descendant", async () => {
    await mountTree();
    await openFolder(7);
    dragEvent("dragstart", card("folder-8"));
    dragEvent("dragover", card("folder-8"));
    dragEvent("drop", card("folder-8"));
    dragEvent("dragstart", card("folder-7"));
    dragEvent("dragover", card("folder-8"));
    expect(card("folder-8").classList.contains("drop-folder")).toBe(false);
    dragEvent("drop", card("folder-8"));
    expect(moveLibraryFolder).not.toHaveBeenCalled();
    expect(reorderLibrary).not.toHaveBeenCalled();
  });

  it("moves a video out of a deep folder to its immediate parent", async () => {
    await mountTree();
    await openFolder(7);
    await openFolder(8);
    await openFolder(9);
    dragEvent("dragstart", card("video-1"));
    const backdrop = container.querySelector(".folder-window-backdrop")!;
    dragEvent("dragover", backdrop);
    expect(container.querySelector(".folder-outside-drop-label")?.textContent).toBe("移至上一级文件夹");
    await act(async () => { dragEvent("drop", backdrop); });
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(moveVideoToFolder).toHaveBeenCalledWith(1, 8);
      expect(container.querySelector(".folder-window .video-card")).toBeNull();
    });
    click(container.querySelector('[aria-label="返回上一级"]'));
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector('.folder-window [data-library-key="video-1"]')).not.toBeNull();
    });
  });
});
