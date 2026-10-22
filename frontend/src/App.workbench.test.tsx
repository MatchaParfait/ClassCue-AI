import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { Link, useSearchParams } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import App from "./App";
import * as api from "./api/client";
import type { Video } from "./types";

vi.mock("./api/client", async (importOriginal) => ({
  ...await importOriginal<typeof import("./api/client")>(),
  getVideos: vi.fn(), getLibrary: vi.fn(), getLibraryStatus: vi.fn(),
  getLatestBatch: vi.fn(), getPausedJobs: vi.fn(), getJob: vi.fn(), cancelJob: vi.fn(),
  createLibraryFolder: vi.fn(), moveVideoToFolder: vi.fn(), retryBatch: vi.fn(), resumeJob: vi.fn(),
  getStorageDirectories: vi.fn(), getAsrSelection: vi.fn(), getAsrModelStatus: vi.fn(), getAiSettings: vi.fn(),
}));
vi.mock("./components/ProcessPage", () => ({
  ProcessPage: ({ active, onRegistered, onJob, guided = false }: {
    active: boolean; onRegistered: (video: Video) => void; onJob: (id: string) => void;
    guided?: boolean;
  }) => {
    const [draft, setDraft] = useState("");
    const [params] = useSearchParams();
    return <div data-process-active={String(active)} data-guided={String(guided)} className="process-video-section process-options-section"><input aria-label="Process draft" value={draft} onChange={(event) => setDraft(event.target.value)} />
      <span data-video-id>{params.get("videoId") ?? "new"}</span>
      <Link to="/settings?section=asr">修改 ASR</Link>
      <button onClick={() => onRegistered({ id: 3 } as Video)}>Register fixture</button>
      <button onClick={() => onJob("running")}>Start fixture</button></div>;
  },
}));
vi.mock("./pages/SettingsPage", () => ({
  SettingsPage: ({ onConfigurationSaved }: { onConfigurationSaved?: () => void }) => {
    const [params] = useSearchParams();
    return <div data-onboarding="asr" className={params.get("section") === "shortcuts" ? "shortcut-settings" : undefined}>
      <input aria-label="API draft" /><button onClick={onConfigurationSaved}>Save fixture</button></div>;
  },
}));
vi.mock("./pages/BatchProcessPage", () => ({
  BatchProcessPage: () => <div>Batch fixture</div>,
}));
vi.mock("./hooks/useStudyData", () => ({
  useStudyData: () => ({ video: { id: 1, status: "review_ready" }, subtitles: [], markers: [], errors: {}, isLoading: false, source: {} }),
  useMarkerStatusMutation: () => ({}), useMarkerJumpTimeMutation: () => ({}), useMarkerImportanceMutation: () => ({}),
}));
vi.mock("./components/VideoPanel", () => ({ VideoPanel: () => <div>Review fixture</div> }));

let container: HTMLDivElement, root: Root;
let videos: Video[];
const readyModel = { id: "model", name: "Model", engine: "whisper", downloaded: true, selected: true, cache_size_bytes: 0 };
beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.resetAllMocks();
  const storage = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => storage.set(key, value),
  });
  window.sessionStorage.clear();
  vi.spyOn(window, "scrollTo").mockImplementation(() => {});
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: false })));
  videos = [1, 2].map((id) => ({ id, display_name: `Lesson ${id}`, filename: `lesson-${id}.mp4`, video_path: `lesson-${id}.mp4`,
    duration_ms: 10000, folder_id: id === 2 ? 7 : null, status: id === 1 ? "review_ready" : "uploaded" }));
  vi.mocked(api.getVideos).mockImplementation(async () => videos);
  vi.mocked(api.getLibrary).mockImplementation(async (id) => ({
    folder: id ? { id: 7, name: "Course" } : null, parent_id: id ?? null, sort: "manual", order: "asc",
    items: [...(!id ? [{ type: "folder" as const, folder: { id: 7, name: "Course" } }] : []),
      ...videos.filter((video) => video.folder_id === (id ?? null)).map((video) => ({ type: "video" as const, video }))],
  }));
  vi.mocked(api.getLibraryStatus).mockResolvedValue({ data_root: "D:\\Course", database_path: "", temp_audio_dir: "", video_count: 2 });
  vi.mocked(api.getLatestBatch).mockResolvedValue(null);
  vi.mocked(api.getPausedJobs).mockResolvedValue([]);
  window.history.replaceState({}, "", "/");
  container = document.createElement("div"); document.body.append(container); root = createRoot(container);
});
afterEach(() => { act(() => root.unmount()); container.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
async function mount(path = "/") {
  window.history.replaceState({}, "", path);
  await act(async () => root.render(<App />));
  await settle(() => expect(api.getLibraryStatus).toHaveBeenCalled());
  expect(container.querySelector(".workspace-caption")).toBeNull();
}
async function settle(check: () => void) {
  await vi.waitFor(async () => { await act(async () => { await new Promise((resolve) => setTimeout(resolve, 5)); }); check(); });
}
function button(label: string) { return container.querySelector<HTMLElement>(`button[aria-label="${label}"], a[aria-label="${label}"]`)!; }
function click(element: Element) { act(() => (element as HTMLElement).click()); }
function visibleScreen() { return container.querySelector(".workbench-screen:not([hidden])")!; }
function leavePage() { click(visibleScreen().querySelector(".page-back-row button")!); }
function type(element: HTMLInputElement, value: string) {
  act(() => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(element, value);
    element.dispatchEvent(new Event("input", { bubbles: true })); });
}
async function back() { await act(async () => { window.history.back(); await new Promise((resolve) => setTimeout(resolve, 20)); }); }

it("shows both import links directly and opens complete pages", async () => {
  await mount();
  expect([...container.querySelectorAll('.library-heading-actions a')].map((item) => item.textContent)).toEqual(["导入视频", "导入文件夹"]);
  expect(container.querySelector('a[href="/demo"]')).toBeNull();
  click(container.querySelector('.library-heading-actions a[href="/process"]')!);
  await settle(() => expect(visibleScreen().querySelector("[data-process-active]")).not.toBeNull());
  expect(window.location.pathname).toBe("/process");
  expect(container.querySelector(".library-page")).not.toBeNull();
  expect(container.querySelector(".library-page")?.closest("[hidden]")).not.toBeNull();
  expect(container.querySelectorAll('[aria-modal="true"]')).toHaveLength(0);
});

it("resumes model onboarding from home through the actual settings link", async () => {
  localStorage.setItem("classroom-onboarding:v1:D:\\Course", JSON.stringify({ started: true, step: "model", storageConfirmed: true }));
  vi.mocked(api.getStorageDirectories).mockResolvedValue({ library_id: "test", delete_source_after_import: false } as Awaited<ReturnType<typeof api.getStorageDirectories>>);
  vi.mocked(api.getAsrSelection).mockResolvedValue(readyModel);
  vi.mocked(api.getAsrModelStatus).mockResolvedValue(readyModel);
  vi.mocked(api.getAiSettings).mockResolvedValue({ configured: true } as Awaited<ReturnType<typeof api.getAiSettings>>);
  await mount();
  await settle(() => expect(document.querySelector(".onboarding-tip h2")?.textContent).toBe("准备字幕模型"));
  click(button("设置"));
  await settle(() => expect(window.location.pathname).toBe("/settings"));
  expect(document.querySelector("#onboarding-description")?.textContent).toContain("ASR 模型");
});

it("ends onboarding at shortcuts without opening existing videos or changing normal processing", async () => {
  localStorage.setItem("classroom-onboarding:v1:D:\\Course", JSON.stringify({
    started: true, step: "shortcuts", storageConfirmed: true, modelConfirmed: true, aiConfirmed: true,
  }));
  vi.mocked(api.getStorageDirectories).mockResolvedValue({ library_id: "test", delete_source_after_import: false } as Awaited<ReturnType<typeof api.getStorageDirectories>>);
  vi.mocked(api.getAsrSelection).mockResolvedValue(readyModel);
  vi.mocked(api.getAsrModelStatus).mockResolvedValue(readyModel);
  vi.mocked(api.getAiSettings).mockResolvedValue({ configured: true } as Awaited<ReturnType<typeof api.getAiSettings>>);
  await mount("/settings?section=shortcuts");
  await settle(() => expect(document.querySelector(".onboarding-tip")?.textContent).toContain("4 / 4"));
  click([...document.querySelectorAll(".onboarding-tip button")].find(item => item.textContent?.includes("完成引导"))!);
  await settle(() => expect(document.querySelector(".onboarding-tip")).toBeNull());
  expect(window.location.pathname + window.location.search).toBe("/settings?section=shortcuts");
  expect(document.body.style.overflow).not.toBe("hidden");
  leavePage();
  await settle(() => expect(window.location.pathname).toBe("/"));
  click(container.querySelector('.library-heading-actions a[href="/process"]')!);
  await settle(() => expect(visibleScreen().querySelector("[data-guided]")?.getAttribute("data-guided")).toBe("false"));
  expect(document.querySelector(".onboarding-tip")).toBeNull();
});

it("keeps the selected video and draft across settings, save, close and forward navigation", async () => {
  await mount();
  click(container.querySelector(".folder-card-link")!);
  await settle(() => expect(container.querySelector(".folder-window .video-card")).not.toBeNull());
  click(container.querySelector(".folder-window .library-thumbnail-link")!);
  await settle(() => expect(visibleScreen().querySelector("[data-video-id]")?.textContent).toBe("2"));
  type(visibleScreen().querySelector("input")!, "unsaved choice");
  click(visibleScreen().querySelector('a[href="/settings?section=asr"]')!);
  await settle(() => expect(visibleScreen().querySelector('[aria-label="API draft"]')).not.toBeNull());
  click(visibleScreen().querySelector("button")!);
  await settle(() => expect(visibleScreen().querySelector("input")?.value).toBe("unsaved choice"));
  expect(window.location.search).toBe("?videoId=2");
  leavePage();
  await settle(() => expect(window.location.pathname).toBe("/"));
  await act(async () => { window.history.forward(); await new Promise((resolve) => setTimeout(resolve, 20)); });
  await settle(() => expect(visibleScreen().querySelector("input")?.value).toBe("unsaved choice"));
  await back();
  await settle(() => expect(container.querySelector(".folder-window")).not.toBeNull());
});

it("releases the folder focus trap for a processing page and restores the folder", async () => {
  await mount();
  click(container.querySelector(".folder-card-link")!);
  await settle(() => expect(container.querySelector(".folder-window .video-card")).not.toBeNull());
  const content = container.querySelector<HTMLElement>(".folder-window-content")!;
  act(() => { content.scrollTop = 120; content.dispatchEvent(new Event("scroll", { bubbles: true })); });
  click(container.querySelector(".folder-window .library-thumbnail-link")!);
  await settle(() => expect(visibleScreen().querySelector("[data-video-id]")?.textContent).toBe("2"));
  expect(container.querySelector(".folder-window")).toBeNull();
  expect(container.querySelectorAll('[aria-modal="true"]')).toHaveLength(0);
  expect(document.body.style.overflow).not.toBe("hidden");
  leavePage();
  await settle(() => expect(container.querySelector(".folder-window .video-card")).not.toBeNull());
  expect(container.querySelector<HTMLElement>(".folder-window-content")!.scrollTop).toBe(120);
});

it("retains the registration draft when updating the URL for reload recovery", async () => {
  await mount("/process");
  await settle(() => expect(visibleScreen().querySelector("input")).not.toBeNull());
  type(visibleScreen().querySelector("input")!, "selected source");
  click([...visibleScreen().querySelectorAll("button")].find((item) => item.textContent === "Register fixture")!);
  await settle(() => expect(window.location.search).toBe("?videoId=3"));
  expect(visibleScreen().querySelector("input")?.value).toBe("selected source");
  expect(container.querySelectorAll("[data-process-active]")).toHaveLength(1);
});

it("keeps the processing draft when library status and job metadata refresh", async () => {
  await mount("/process");
  await settle(() => expect(visibleScreen().querySelector("input")).not.toBeNull());
  const input = visibleScreen().querySelector<HTMLInputElement>("input")!;
  type(input, "retained across polling");
  vi.mocked(api.getLibraryStatus).mockResolvedValue({
    data_root: "D:\\Course", database_path: "", temp_audio_dir: "", video_count: 3, active_job_id: "running",
  });
  vi.mocked(api.getJob).mockResolvedValue({ id: "running", status: "running", progress: 3 });
  click([...visibleScreen().querySelectorAll("button")].find(item => item.textContent === "Start fixture")!);
  await settle(() => expect(api.getLibraryStatus).toHaveBeenCalledTimes(2));
  expect(visibleScreen().querySelector("input")).toBe(input);
  expect(input.value).toBe("retained across polling");
  expect(window.location.pathname).toBe("/process");
});

it("keeps the registered video's draft without reusing it for a new import", async () => {
  videos.push({ ...videos[1], id: 3, folder_id: null });
  await mount("/process");
  await settle(() => expect(visibleScreen().querySelector("input")).not.toBeNull());
  type(visibleScreen().querySelector("input")!, "registered choices");
  click([...visibleScreen().querySelectorAll("button")].find((item) => item.textContent === "Register fixture")!);
  await settle(() => expect(window.location.search).toBe("?videoId=3"));
  leavePage();
  await settle(() => expect(window.location.pathname).toBe("/"));
  click(container.querySelector('.library-heading-actions a[href="/process"]')!);
  await settle(() => expect(visibleScreen().querySelector("[data-video-id]")?.textContent).toBe("new"));
  expect(visibleScreen().querySelector("input")?.value).toBe("");
  leavePage();
  await settle(() => expect(window.location.pathname).toBe("/"));
  click(container.querySelector('[data-library-key="video-3"] .library-thumbnail-link')!);
  await settle(() => expect(visibleScreen().querySelector("[data-video-id]")?.textContent).toBe("3"));
  expect(visibleScreen().querySelector("input")?.value).toBe("registered choices");
});

it("shows awaiting API separately and only retries on a deliberate action", async () => {
  const waiting = { id: "batch", status: "awaiting_api", job_type: "batch_video_process", progress: 100,
    batch_summary: { total: 4, completed: 1, skipped: 0, failed: 0, awaiting_ai: 3, pending_asr: 0 } };
  vi.mocked(api.getLatestBatch).mockResolvedValue(waiting);
  const retried = { ...waiting, id: "retry", status: "queued" };
  vi.mocked(api.getJob).mockImplementation(async (id) => id === "retry" ? retried : waiting);
  vi.mocked(api.retryBatch).mockResolvedValue(retried);
  await mount();
  await settle(() => expect(button("查看任务详情").textContent).toContain("等待 AI 恢复"));
  expect(api.retryBatch).not.toHaveBeenCalled();
  click(button("查看任务详情"));
  await settle(() => expect(container.querySelector(".task-summary")?.textContent).toContain("待 AI3"));
  click([...container.querySelectorAll(".task-details button")].find((item) => item.textContent?.includes("重试未完成视频"))!);
  await settle(() => expect(api.retryBatch).toHaveBeenCalledWith("batch"));
  await settle(() => expect(container.querySelector(".task-details h3")?.textContent).toBe("排队中"));
});

it("closing a running task panel never cancels it", async () => {
  vi.mocked(api.getLibraryStatus).mockResolvedValue({ data_root: "D:\\Course", database_path: "", temp_audio_dir: "", video_count: 2, active_job_id: "run" });
  vi.mocked(api.getJob).mockResolvedValue({ id: "run", status: "running", progress: 32, video_id: 1 });
  await mount("/tasks");
  await settle(() => expect(container.querySelector(".task-details h3")?.textContent).toBe("处理中"));
  click(button("关闭面板"));
  expect(api.cancelJob).not.toHaveBeenCalled();
  expect(window.location.pathname).toBe("/");
});

it("keeps full-page navigation accessible and traps only the task drawer", async () => {
  await mount("/process");
  await settle(() => expect(visibleScreen().querySelector("input")).not.toBeNull());
  type(visibleScreen().querySelector("input")!, "retained");
  act(() => button("设置").focus());
  expect(document.activeElement).toBe(button("设置"));
  await act(async () => { document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); });
  expect(window.location.pathname).toBe("/process");
  click(button("查看任务详情"));
  await settle(() => expect(container.querySelector('[aria-modal="true"]')).not.toBeNull());
  act(() => button("设置").focus());
  expect(document.activeElement).toBe(button("关闭面板"));
  await act(async () => { document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); });
  await settle(() => expect(window.location.pathname).toBe("/process"));
  expect(visibleScreen().querySelector("input")?.value).toBe("retained");
});

it("restores the single library's folder context on refresh", async () => {
  await mount();
  click(container.querySelector(".folder-card-link")!);
  await settle(() => expect(container.querySelector(".folder-window")).not.toBeNull());
  act(() => root.unmount()); root = createRoot(container);
  await mount();
  await settle(() => expect(container.querySelector(".folder-window")).not.toBeNull());
});
