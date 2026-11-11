import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import * as api from "../api/client";
import * as slideApi from "../api/slides";
import type { Video, VideoSlide } from "../types";
import { SlideExtractionControls } from "./SlideExtractionControls";

vi.mock("../api/client", () => ({
  cancelJob: vi.fn(), extractVideoSlides: vi.fn(), getVideoSlides: vi.fn(),
  getStreamUrl: vi.fn(() => "/fixture.mp4"), getJob: vi.fn(),
}));
vi.mock("../api/slides", () => ({ getSlideAcceleration: vi.fn(), prepareSlideAcceleration: vi.fn() }));

const video: Video = {
  id: 7, display_name: "Synthetic lesson", filename: "lesson.mp4",
  video_path: "lesson.mp4", duration_ms: 12000, status: "uploaded",
};
const saved: VideoSlide = {
  id: 1, video_id: 7, start_ms: 0, end_ms: 12000, representative_ms: 11000,
  visible_from_ms: 8000, visible_until_ms: 12000, page_start_ms: 0, page_end_ms: 12000,
  duration_ms: 12000, image_hash: "synthetic", retention_class: "primary", confidence: 0.9,
  is_llm_preferred: true, information_score: 0.9,
  roi: { x: 0, y: 0, width: 640, height: 360 },
  ignore_regions: [{ x: 540, y: 270, width: 80, height: 70 }],
  extraction_version: "slide-v2", image_url: "/fixture.jpg",
};
let container: HTMLDivElement;
let root: Root;
let client: QueryClient;
const settle = async () => act(async () => { await new Promise((resolve) => setTimeout(resolve, 40)); });

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.resetAllMocks();
  vi.mocked(slideApi.getSlideAcceleration).mockResolvedValue({ ready: true, gpu_name: "Synthetic GPU", message: "" });
  vi.mocked(api.getStreamUrl).mockReturnValue("/fixture.mp4");
  vi.mocked(api.getVideoSlides).mockResolvedValue([]);
  vi.mocked(api.extractVideoSlides).mockResolvedValue({ id: "slide-job", status: "queued" });
  vi.mocked(api.getJob).mockResolvedValue({ id: "slide-job", status: "running", progress: 10 });
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
});

afterEach(async () => {
  await act(async () => { root.unmount(); client.clear(); });
  container.remove();
});

async function mount() {
  act(() => root.render(<QueryClientProvider client={client}>
    <SlideExtractionControls video={video} active busy={false} />
  </QueryClientProvider>));
  await settle();
}

function button(label: string) {
  return [...container.querySelectorAll("button")].find((item) => item.textContent === label)!;
}

it("starts new extraction separately with no replacement confirmation", async () => {
  await mount();
  expect(container.querySelector('input[type="checkbox"]')).toBeNull();
  act(() => button("提取 PPT 页面").click());
  await settle();
  expect(api.extractVideoSlides).toHaveBeenCalledWith(7, { force: false, ignore_regions: [] });
});

it("restores regions and requires confirmation before force replacing existing images", async () => {
  vi.mocked(api.getVideoSlides).mockResolvedValue([saved]);
  await mount();
  expect(button("重新提取 PPT").disabled).toBe(true);
  expect(container.textContent).toContain("排除区域 1");
  act(() => container.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click());
  expect(button("重新提取 PPT").disabled).toBe(false);
  act(() => button("重新提取 PPT").click());
  await settle();
  expect(api.extractVideoSlides).toHaveBeenCalledWith(7, {
    force: true, roi: saved.roi, ignore_regions: saved.ignore_regions,
  });
});

it("allows removing saved interference masks without changing the ROI", async () => {
  vi.mocked(api.getVideoSlides).mockResolvedValue([saved]);
  await mount();
  act(() => container.querySelector<HTMLButtonElement>('button[aria-label="删除排除区域 1"]')!.click());
  expect(container.textContent).not.toContain("排除区域 1：");
  act(() => container.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click());
  act(() => button("重新提取 PPT").click());
  await settle();
  expect(api.extractVideoSlides).toHaveBeenCalledWith(7, {
    force: true, roi: saved.roi, ignore_regions: [],
  });
});

it("refreshes slides and library counts after completion", async () => {
  vi.mocked(api.getJob).mockResolvedValue({ id: "slide-job", status: "completed", progress: 100 });
  const invalidation = vi.spyOn(client, "invalidateQueries");
  await mount();
  act(() => button("提取 PPT 页面").click());
  await settle();
  await settle();
  expect(invalidation).toHaveBeenCalledWith({ queryKey: ["slides", 7] });
  expect(invalidation).toHaveBeenCalledWith({ queryKey: ["videos"] });
  expect(api.getVideoSlides).toHaveBeenCalledTimes(2);
});

it("passes the explicitly selected CPU or CUDA backend", async () => {
  await mount();
  const select = container.querySelector<HTMLSelectElement>('select[aria-label="PPT 处理设备"]')!;
  act(() => {
    select.value = "cuda";
    select.dispatchEvent(new Event("change", { bubbles: true }));
  });
  act(() => button("提取 PPT 页面").click());
  await settle();
  expect(api.extractVideoSlides).toHaveBeenCalledWith(7, { force: false, ignore_regions: [], backend: "cuda" });
});

it("prepares CUDA as a separate cancellable task and blocks extraction", async () => {
  vi.mocked(slideApi.getSlideAcceleration).mockResolvedValue({ ready: false, gpu_name: null, message: "" });
  vi.mocked(slideApi.prepareSlideAcceleration).mockResolvedValue({ job_id: "prepare" });
  await mount();
  expect(container.querySelector<HTMLOptionElement>('option[value="cuda"]')?.disabled).toBe(true);
  act(() => button("准备 CUDA").click());
  await settle();
  expect(button("正在准备…").disabled).toBe(true);
  act(() => button("停止准备").click());
  await settle();
  expect(api.cancelJob).toHaveBeenCalledWith("prepare");
  expect(api.extractVideoSlides).not.toHaveBeenCalled();
});

it.each([false, true])("recovers preparation status reads with previous data: %s", async (hasPreviousData) => {
  vi.mocked(slideApi.getSlideAcceleration).mockResolvedValue({ ready: false, gpu_name: null, message: "" });
  vi.mocked(slideApi.prepareSlideAcceleration).mockResolvedValue({ job_id: "prepare" });
  if (!hasPreviousData) vi.mocked(api.getJob).mockRejectedValue(new Error("status unavailable"));
  await mount();
  act(() => button("准备 CUDA").click());
  await settle();
  await settle();
  if (hasPreviousData) {
    vi.mocked(api.getJob).mockRejectedValue(new Error("status unavailable"));
    await act(async () => { await client.invalidateQueries({ queryKey: ["jobs", "prepare"] }); });
    await settle();
  }
  expect(container.textContent).toContain("无法读取 CUDA 准备进度");
  expect(button("正在准备…").disabled).toBe(true);
  expect(button("停止准备").disabled).toBe(false);
  vi.mocked(api.getJob).mockResolvedValue({ id: "prepare", status: "completed", progress: 1 });
  act(() => button("重新读取准备进度").click());
  await settle();
  expect(container.textContent).not.toContain("无法读取 CUDA 准备进度");
  expect(button("提取 PPT 页面").disabled).toBe(false);
});
