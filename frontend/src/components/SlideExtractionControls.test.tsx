import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import * as api from "../api/client";
import * as slideApi from "../api/slides";
import type { Video, VideoSlide } from "../types";
import { SlideExtractionControls } from "./SlideExtractionControls";

vi.mock("../api/client", () => ({
  cancelJob: vi.fn(), extractVideoSlides: vi.fn(), getVideoSlides: vi.fn(),
  getStreamUrl: vi.fn(() => "/fixture.mp4"), getJob: vi.fn(),
}));
vi.mock("../api/slides", () => ({
  getSlideAcceleration: vi.fn(), prepareSlideAcceleration: vi.fn(),
  getSlideSelection: vi.fn(), saveSlideSelection: vi.fn(),
}));

const acceleration = {
  ready: true, runtime_ready: true, gpu_detected: true, gpu_prepare_available: true,
  gpu_name: "Synthetic GPU", gpu_unavailable_reason: null, message: "",
};

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
  vi.mocked(slideApi.getSlideAcceleration).mockResolvedValue(acceleration);
  vi.mocked(slideApi.getSlideSelection).mockResolvedValue({ backend: "auto" });
  vi.mocked(slideApi.saveSlideSelection).mockImplementation(async backend => ({ backend }));
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
    <MemoryRouter><SlideExtractionControls video={video} active busy={false} /></MemoryRouter>
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

it("shows the saved device with a settings link and no installation or device editing controls", async () => {
  vi.mocked(slideApi.getSlideSelection).mockResolvedValue({ backend: "cpu" });
  await mount();
  expect(container.textContent).toContain("默认设备：CPU");
  expect(container.querySelector('a[href="/settings?section=slides"]')).not.toBeNull();
  expect(container.querySelector("select")).toBeNull();
  expect(slideApi.prepareSlideAcceleration).not.toHaveBeenCalled();
  expect(slideApi.getSlideAcceleration).not.toHaveBeenCalled();
  act(() => button("提取 PPT 页面").click());
  await settle();
  expect(api.extractVideoSlides).toHaveBeenCalledWith(7, { force: false, ignore_regions: [] });
});

it("blocks extraction when the saved GPU is no longer available and points to settings", async () => {
  vi.mocked(slideApi.getSlideAcceleration).mockResolvedValue({
    ...acceleration, ready: false, gpu_detected: false, gpu_prepare_available: false,
    gpu_name: null, gpu_unavailable_reason: "未检测到可用的 NVIDIA 显卡及驱动，GPU 已禁用。",
  });
  vi.mocked(slideApi.getSlideSelection).mockResolvedValue({ backend: "cuda" });
  await mount();
  await settle();
  expect(button("提取 PPT 页面").disabled).toBe(true);
  expect(container.textContent).toContain("请在设置中准备 GPU 或切换设备");
  expect(container.querySelector('a[href="/settings?section=slides"]')).not.toBeNull();
});

it("blocks extraction until the saved preference is readable", async () => {
  vi.mocked(slideApi.getSlideSelection).mockRejectedValue(new Error("unavailable"));
  await mount();
  expect(button("提取 PPT 页面").disabled).toBe(true);
  vi.mocked(slideApi.getSlideSelection).mockResolvedValue({ backend: "cpu" });
  act(() => button("重新读取设备选择").click());
  await settle();
  expect(button("提取 PPT 页面").disabled).toBe(false);
});
