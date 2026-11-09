import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, useRef } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import * as api from "../api/client";
import { TopBar } from "./TopBar";
import { taskDestination } from "./TaskCenter";

const materialFeature = vi.hoisted(() => ({ enabled: false }));
vi.mock("../featureFlags", () => ({ get MATERIALS_ENABLED() { return materialFeature.enabled; } }));

vi.mock("../api/client", async original => ({
  ...await original<typeof import("../api/client")>(), getRecentJobs: vi.fn(), getJob: vi.fn(),
}));
vi.mock("./Announcements", () => ({ Announcements: () => null }));
vi.mock("./GlobalSearch", () => ({ GlobalSearchButton: () => null }));
vi.mock("./ReviewTabs", () => ({ ReviewTabs: () => null }));

let container: HTMLDivElement, root: Root, client: QueryClient;
beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.resetAllMocks();
  materialFeature.enabled = false;
  vi.mocked(api.getRecentJobs).mockResolvedValue([]);
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  container = document.createElement("div"); document.body.append(container); root = createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount());
  client.clear(); container.remove(); vi.restoreAllMocks();
});
function Fixture({ demo = false }: { demo?: boolean }) {
  const background = useRef<HTMLDivElement>(null);
  return <div ref={background} data-review-background><TopBar demo={demo} backgroundRef={background} />
    <video data-player /><p>Review content</p></div>;
}
async function mount(demo = false) {
  await act(async () => root.render(<QueryClientProvider client={client}><MemoryRouter>
    <Fixture demo={demo} /></MemoryRouter></QueryClientProvider>));
}
async function settle(check: () => void) {
  await vi.waitFor(async () => {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 5)); }); check();
  });
}
it("shows recent tasks on the video page and preserves the player while viewing details", async () => {
  const latest = { id: "latest", status: "completed", job_type: "video_analysis", video_id: 7,
    video_name: "Lesson", analysis_modes: ["question_extraction" as const], progress: 100 };
  vi.mocked(api.getRecentJobs).mockResolvedValue([latest]);
  vi.mocked(api.getJob).mockResolvedValue(latest);
  await mount();
  await settle(() => expect(container.textContent).toContain("处理完成"));
  expect(container.querySelector(".settings-button")).toBeNull();
  const player = container.querySelector("video")!;
  player.currentTime = 35;
  act(() => container.querySelector<HTMLButtonElement>('[aria-label="查看任务详情"]')!.click());
  await settle(() => expect(document.querySelector('[role="dialog"]')?.textContent).toContain("题目提取"));
  expect(document.querySelector('[role="dialog"]')?.closest("[inert]")).toBeNull();
  expect(container.querySelector("video")).toBe(player);
  expect(player.currentTime).toBe(35);
  act(() => document.querySelector<HTMLButtonElement>('[role="dialog"] [aria-label="关闭面板"]')!.click());
  expect(container.querySelector("video")).toBe(player);
  expect(player.currentTime).toBe(35);
});

it("does not read real tasks in demo mode", async () => {
  await mount(true);
  act(() => container.querySelector<HTMLButtonElement>('[aria-label="查看任务详情"]')!.click());
  expect(api.getRecentJobs).not.toHaveBeenCalled();
  expect(api.getJob).not.toHaveBeenCalled();
  expect(document.querySelector('[role="dialog"]')?.textContent).toContain("暂无处理任务");
});

it("shows course transcription counts and failures and returns to the library", async () => {
  const job = {
    id: "course-job", status: "failed", job_type: "course_transcribe", progress: 100,
    model_id: "whisper-turbo", message: "转写结束，1 个视频失败",
    batch_summary: { total: 3, completed: 1, skipped: 1, failed: 1, awaiting_ai: 0, pending_asr: 0 },
    batch_entries: [{ video_id: 1, label: "第一课", status: "completed" },
      { video_id: 2, label: "第二课", status: "skipped" },
      { video_id: 3, label: "第三课", status: "failed", error: "媒体解码失败" }],
  };
  vi.mocked(api.getRecentJobs).mockResolvedValue([job]);
  vi.mocked(api.getJob).mockResolvedValue(job);
  await mount();
  act(() => container.querySelector<HTMLButtonElement>('[aria-label="查看任务详情"]')!.click());
  await settle(() => expect(document.querySelector('[role="dialog"]')?.textContent).toContain("课程转写"));
  const dialog = document.querySelector('[role="dialog"]')!;
  expect(dialog.textContent).toContain("第一课");
  expect(dialog.textContent).toContain("媒体解码失败");
  expect(dialog.textContent).toContain("打开视频库");
  expect(dialog.textContent).not.toContain("待 AI");
  expect([...dialog.querySelectorAll(".task-summary dt")].map(node => node.textContent))
    .toEqual(["已转写", "已跳过", "失败", "待转写"]);
  expect(taskDestination(job)).toBe("/");
});

it("shows local sequence steps and routes the task back to the library", async () => {
  const job = {
    id: "sequence-job", status: "failed", job_type: "course_sequence", progress: 50,
    batch_summary: { total: 1, completed: 0, skipped: 0, failed: 1, awaiting_ai: 0, pending_asr: 0 },
    batch_entries: [{ video_id: 1, label: "合成课", status: "failed", error: "synthetic PPT failure",
      steps: [{ name: "transcribe", status: "completed" }, { name: "extract_slides", status: "failed" }] }],
  };
  vi.mocked(api.getRecentJobs).mockResolvedValue([job]);
  vi.mocked(api.getJob).mockResolvedValue(job);
  await mount();
  act(() => container.querySelector<HTMLButtonElement>('[aria-label="查看任务详情"]')!.click());
  await settle(() => expect(document.querySelector('[role="dialog"]')?.textContent).toContain("顺序处理"));
  const text = document.querySelector('[role="dialog"]')!.textContent!;
  expect(text).toContain("字幕转写：已完成");
  expect(text).toContain("PPT 提取：失败");
  expect(text).not.toContain("待 AI");
  expect(taskDestination(job)).toBe("/");
});

it("routes historical material tasks home while paused and restores their destination when enabled", () => {
  const job = { id: "materials-history", status: "completed", job_type: "material_preprocess" };
  expect(taskDestination(job)).toBe("/");
  materialFeature.enabled = true;
  expect(taskDestination(job)).toBe("/materials");
  expect(taskDestination({ ...job, job_type: "batch_video_process" })).toBe("/batch-process");
});

it("preserves the selected single-video task and encodes its identifier", () => {
  const job = { id: "queued/analysis + B", status: "queued", job_type: "video_analysis", video_id: 7 };
  expect(taskDestination(job)).toBe("/process?videoId=7&jobId=queued%2Fanalysis%20%2B%20B");
  expect(taskDestination({ ...job, id: 12 })).toBe("/process?videoId=7&jobId=12");
  expect(taskDestination({ ...job, job_id: "canonical" })).toBe("/process?videoId=7&jobId=canonical");
});
