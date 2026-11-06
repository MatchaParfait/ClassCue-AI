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
