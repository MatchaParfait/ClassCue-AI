import { act, type RefObject } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import * as api from "./api/client";
import App from "./App";
import type { AnalysisRun, Job, Marker } from "./types";

vi.mock("./api/client", async importOriginal => ({
  ...await importOriginal<typeof import("./api/client")>(),
  getVideo: vi.fn(), getSubtitles: vi.fn(), getMarkers: vi.fn(),
  getAnalysisRuns: vi.fn(), getVideoProcessingOptions: vi.fn(),
  getRecallSchemes: vi.fn(),
  analyzeVideoRecall: vi.fn(), getJob: vi.fn(),
  getLibraryStatus: vi.fn(async () => ({ data_root: "", database_path: "", temp_audio_dir: "", video_count: 1 })),
  getLatestBatch: vi.fn(async () => null), getPausedJobs: vi.fn(async () => []),
}));
vi.mock("./components/VideoPanel", () => ({
  VideoPanel: ({ videoRef }: { videoRef: RefObject<HTMLVideoElement> }) => <video ref={videoRef} />,
}));

let container: HTMLDivElement;
let root: Root;
let runs: AnalysisRun[];
let markers: Marker[];
let job: Job;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.resetAllMocks();
  vi.mocked(api.getRecallSchemes).mockResolvedValue([]);
  vi.spyOn(window, "scrollTo").mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
  markers = [{
    id: 1, video_id: 1, start_ms: 1000, end_ms: 2000, title: "现有中召回考点",
    note: "", source_quote: "考试", analysis_mode: "medium_recall", review_status: "active",
  }];
  runs = [{ analysis_mode: "medium_recall", status: "completed", candidate_count: 1 }];
  job = { id: "extra", video_id: 1, status: "running", progress: 25, analysis_modes: ["high_recall"] };
  vi.mocked(api.getVideo).mockResolvedValue({ value: {
    id: 1, display_name: "Lesson", filename: "lesson.mp4", video_path: "lesson.mp4",
    duration_ms: 60000, status: "review_ready",
  }, source: "api" });
  vi.mocked(api.getSubtitles).mockResolvedValue({ value: [{ id: 1, start_ms: 0, end_ms: 60000, text: "考试" }], source: "api" });
  vi.mocked(api.getMarkers).mockImplementation(async () => ({ value: markers.map(marker => ({ ...marker })), source: "api" }));
  vi.mocked(api.getAnalysisRuns).mockImplementation(async () => runs.map(run => ({ ...run })));
  vi.mocked(api.getVideoProcessingOptions).mockResolvedValue({ active_job: null });
  vi.mocked(api.getJob).mockImplementation(async () => ({ ...job }));
  vi.mocked(api.analyzeVideoRecall).mockResolvedValue({ id: "extra", job_id: "extra", status: "queued" });
  vi.mocked(api.getLibraryStatus).mockResolvedValue({ data_root: "", database_path: "", temp_audio_dir: "", video_count: 1 });
  vi.mocked(api.getLatestBatch).mockResolvedValue(null);
  vi.mocked(api.getPausedJobs).mockResolvedValue([]);
  window.history.replaceState({}, "", "/review/1");
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});
async function mount() {
  await act(async () => root.render(<App />));
  await vi.waitFor(() => expect(container.textContent).toContain("现有中召回考点"));
}
function button(text: string) {
  return [...container.querySelectorAll<HTMLButtonElement>("button")].find(node => node.textContent?.trim() === text);
}
async function flushCheck(check: () => void, timeout = 3000) {
  await vi.waitFor(async () => {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)); });
    check();
  }, { timeout });
}
async function selectMode(index: number) {
  act(() => (container.querySelectorAll(".mode-switcher button")[index] as HTMLButtonElement).click());
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 180)); });
}

it("runs a missing mode once, updates results and preserves the player and playback position", async () => {
  await mount();
  const player = container.querySelector("video")!;
  player.currentTime = 23.75;
  await selectMode(0);
  await flushCheck(() => expect(button("识别高召回")).toBeDefined());
  expect(container.textContent).toContain("额外使用 token");
  act(() => { button("识别高召回")!.click(); button("识别高召回")!.click(); });
  await flushCheck(() => expect(container.textContent).toContain("高召回识别中"));
  expect(api.analyzeVideoRecall).toHaveBeenCalledTimes(1);
  expect(api.analyzeVideoRecall).toHaveBeenCalledWith(1, "high_recall");
  await flushCheck(() => expect(container.querySelector('[role="progressbar"]')?.getAttribute("aria-valuenow")).toBe("25"));
  runs.push({ analysis_mode: "high_recall", status: "completed", candidate_count: 1 });
  markers.push({ ...markers[0], id: 2, title: "新增高召回考点", analysis_mode: "high_recall" });
  job = { ...job, status: "completed", progress: 100 };
  await flushCheck(() => expect(container.querySelector(".marker-main")?.textContent).toContain("新增高召回考点"));
  expect(container.querySelector("video")).toBe(player);
  expect(player.currentTime).toBe(23.75);
  await selectMode(1);
  expect(container.textContent).toContain("现有中召回考点");
});

it("opens custom-only results, displays the chosen name and keeps stale results while explicitly reanalyzing", async () => {
  const mode = `custom_${"a".repeat(32)}` as const;
  markers = [{ ...markers[0], analysis_mode: mode, title: "自定义方案已有考点", importance: "critical", user_start_ms: 500 }];
  runs = [{ analysis_mode: mode, scheme_name: "张老师", status: "completed", candidate_count: 1,
    scheme_revision: 1, latest_scheme_revision: 2 }];
  vi.mocked(api.getRecallSchemes).mockResolvedValue([{
    id: mode, name: "张老师", base_mode: "medium_recall", revision: 2, archived: false, rules: [], notes: "",
  }]);
  vi.mocked(api.analyzeVideoRecall).mockResolvedValue({ id: "custom-job", job_id: "custom-job", status: "queued" });
  job = { id: "custom-job", video_id: 1, status: "running", analysis_modes: [mode], progress: 35 };
  await act(async () => root.render(<App />));
  await flushCheck(() => expect(container.querySelector(".mode-switcher [aria-selected=true]")?.textContent).toContain("张老师"));
  await flushCheck(() => expect(button("重新识别张老师")).toBeDefined());
  const player = container.querySelector("video")!;
  player.currentTime = 12;
  act(() => button("重新识别张老师")!.click());
  await flushCheck(() => expect(api.analyzeVideoRecall).toHaveBeenCalledWith(1, mode));
  expect(container.textContent).toContain("自定义方案已有考点");
  expect(container.querySelector("video")).toBe(player);
  expect(player.currentTime).toBe(12);
  expect(container.textContent).not.toContain("自定义方案尚未识别");
});

it("keeps completed zero-result archived custom schemes reachable after a reload", async () => {
  const mode = `custom_${"a".repeat(32)}` as const;
  markers = [];
  runs = [{ analysis_mode: mode, scheme_name: "停用的教师方案", status: "completed", candidate_count: 0,
    scheme_revision: 1, latest_scheme_revision: 1, archived: true }];
  await act(async () => root.render(<App />));
  await flushCheck(() => expect(container.querySelector(".mode-switcher [aria-selected=true]")?.textContent).toContain("停用的教师方案"));
  await flushCheck(() => expect(container.textContent).toContain("已完成识别"));
  expect(api.analyzeVideoRecall).not.toHaveBeenCalled();
});

it("distinguishes completed zero-result modes and filtered results from unprocessed modes", async () => {
  runs.push({ analysis_mode: "low_recall", status: "completed", candidate_count: 0 });
  await mount();
  await selectMode(2);
  await flushCheck(() => expect(container.textContent).toContain("低召回已完成识别"));
  expect(button("识别低召回")).toBeUndefined();
  expect(api.analyzeVideoRecall).not.toHaveBeenCalled();
  await selectMode(1);
  act(() => (container.querySelectorAll(".status-filter button")[2] as HTMLButtonElement).click());
  expect(container.textContent).toContain("这里暂时没有重点");
  expect(button("识别中召回")).toBeUndefined();
});

it("retains a retry action after request failure and never calls another recall mode", async () => {
  vi.mocked(api.analyzeVideoRecall).mockRejectedValueOnce(new Error("fixture connection unavailable"));
  await mount();
  await selectMode(0);
  await flushCheck(() => expect(button("识别高召回")).toBeDefined());
  act(() => button("识别高召回")!.click());
  await flushCheck(() => {
    expect(container.textContent).toContain("fixture connection unavailable");
    expect(button("识别高召回")?.disabled).toBe(false);
  });
  act(() => button("识别高召回")!.click());
  await flushCheck(() => expect(container.textContent).toContain("高召回识别中"));
  expect(api.analyzeVideoRecall).toHaveBeenCalledTimes(2);
  expect(vi.mocked(api.analyzeVideoRecall).mock.calls.every(call => call[1] === "high_recall")).toBe(true);
});

it("restores an existing job after reloading and offers retry on failure", async () => {
  vi.mocked(api.getVideoProcessingOptions).mockImplementation(async () => ({ active_job: job.status === "running" ? { ...job } : null }));
  runs.push({ analysis_mode: "high_recall", status: "processing", candidate_count: 0 });
  await mount();
  await selectMode(0);
  await flushCheck(() => expect(container.textContent).toContain("高召回识别中"));
  expect(api.analyzeVideoRecall).not.toHaveBeenCalled();
  job = { ...job, status: "failed", error_message: "fixture evidence failure" };
  runs[1] = { ...runs[1], status: "failed" };
  await flushCheck(() => expect(button("重试高召回识别")?.disabled).toBe(false));
  expect(container.textContent).toContain("上次识别未完成");
});

it("reports state loading failures instead of assuming the mode was never processed", async () => {
  vi.mocked(api.getAnalysisRuns).mockRejectedValue(new Error("fixture status unavailable"));
  await mount();
  await selectMode(0);
  await flushCheck(() => expect(container.textContent).toContain("识别状态读取失败"));
  expect(button("识别高召回")).toBeUndefined();
  expect(button("重新读取")).toBeDefined();
});
