import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import * as api from "../api/client";
import type { Video, VideoSlide } from "../types";
import { PptViewer } from "./PptViewer";

vi.mock("../api/client", () => ({
  cancelJob: vi.fn(),
  downloadSlidePdf: vi.fn(),
  extractVideoSlides: vi.fn(),
  getJob: vi.fn(),
  getSlideImageUrl: vi.fn((slide: { id: number }) => `/slides/${slide.id}.jpg`),
  getStreamUrl: vi.fn(() => "/videos/lesson.mp4"),
  getVideoSlides: vi.fn(),
}));

vi.mock("react-router-dom", () => ({
  useNavigate: () => vi.fn(),
}));

const video: Video = {
  id: 7,
  display_name: "Synthetic lesson",
  filename: "lesson.mp4",
  video_path: "lesson.mp4",
  duration_ms: 20000,
  status: "uploaded",
  has_transcript: false,
};

const slides: VideoSlide[] = [
  {
    id: 1, video_id: 7, start_ms: 0, end_ms: 9000, representative_ms: 8000,
    visible_from_ms: 1000, visible_until_ms: 9000, page_start_ms: 0, page_end_ms: 9000,
    duration_ms: 9000, image_hash: "one", retention_class: "animation", confidence: .65,
    is_llm_preferred: false, information_score: .72,
    roi: { x: 0, y: 0, width: 640, height: 360 }, ignore_regions: [],
    extraction_version: "slide-v2", image_url: "/slides/1.jpg",
  },
  {
    id: 2, video_id: 7, start_ms: 9000, end_ms: 20000, representative_ms: 15000,
    visible_from_ms: 9000, visible_until_ms: 20000, page_start_ms: 9000, page_end_ms: 20000,
    duration_ms: 11000, image_hash: "two", retention_class: "auxiliary", confidence: .88,
    is_llm_preferred: true, information_score: .86,
    roi: { x: 0, y: 0, width: 640, height: 360 }, ignore_regions: [],
    extraction_version: "slide-v2", image_url: "/slides/2.jpg",
  },
];

let container: HTMLDivElement;
let root: Root;
let client: QueryClient;

const settle = async () => act(async () => { await new Promise((resolve) => setTimeout(resolve, 30)); });

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.resetAllMocks();
  vi.mocked(api.getVideoSlides).mockResolvedValue(slides);
  vi.mocked(api.getSlideImageUrl).mockImplementation((slide) => `/slides/${slide.id}.jpg`);
  vi.mocked(api.getStreamUrl).mockReturnValue("/videos/lesson.mp4");
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
});

afterEach(async () => {
  await act(async () => { root.unmount(); client.clear(); });
  document.querySelector(".ppt-viewer-dialog")?.remove();
  container.remove();
});

async function mount() {
  act(() => root.render(
    <QueryClientProvider client={client}>
      <PptViewer video={video} active backgroundRef={{ current: null }} onClose={() => undefined} />
    </QueryClientProvider>,
  ));
  await settle();
}

it("shows the source video beside the selected PPT screenshot", async () => {
  await mount();
  const dialog = document.querySelector(".ppt-viewer-dialog")!;
  const player = dialog.querySelector<HTMLVideoElement>(".ppt-viewer-video")!;
  expect(player).not.toBeNull();
  expect(player.getAttribute("src")).toBe("/videos/lesson.mp4");
  expect(dialog.querySelector(".ppt-viewer-image-wrap img")?.getAttribute("src")).toBe("/slides/2.jpg");
  expect(dialog.textContent).toContain("视频画面");
  expect(dialog.textContent).toContain("PPT 截图");
});

it("seeks the inline video when another PPT time instance is selected", async () => {
  await mount();
  const dialog = document.querySelector(".ppt-viewer-dialog")!;
  const player = dialog.querySelector<HTMLVideoElement>(".ppt-viewer-video")!;
  Object.defineProperty(player, "duration", { configurable: true, value: 20 });
  Object.defineProperty(player, "currentTime", { configurable: true, writable: true, value: 0 });

  const timelineTab = dialog.querySelectorAll<HTMLButtonElement>('[role="tab"]')[1];
  act(() => timelineTab.click());
  await settle();
  act(() => dialog.querySelector<HTMLButtonElement>(".ppt-viewer-item")!.click());
  await settle();
  expect(player.currentTime).toBe(8);
  const second = [...dialog.querySelectorAll<HTMLButtonElement>(".ppt-viewer-item")]
    .find((item) => item.textContent?.includes("页面 2"))!;
  act(() => second.click());
  await settle();

  expect(player.currentTime).toBe(15);
  expect(dialog.querySelector(".ppt-viewer-image-wrap img")?.getAttribute("src")).toBe("/slides/2.jpg");
});

it("switches between detailed timeline states and preferred pages", async () => {
  await mount();
  const dialog = document.querySelector(".ppt-viewer-dialog")!;
  expect(dialog.querySelectorAll(".ppt-viewer-item")).toHaveLength(1);
  const tabs = [...dialog.querySelectorAll<HTMLButtonElement>('[role="tab"]')];
  expect(tabs.map((tab) => tab.textContent)).toEqual(["页面精选", "时间轴状态"]);
  expect(tabs[0].getAttribute("aria-selected")).toBe("true");
  act(() => tabs[1].click());
  await settle();
  expect(dialog.querySelectorAll(".ppt-viewer-item")).toHaveLength(2);

  const preferredTab = [...dialog.querySelectorAll<HTMLButtonElement>(".ppt-viewer-mode-switch button")]
    .find((button) => button.textContent?.includes("页面精选"))!;
  act(() => preferredTab.click());
  await settle();

  expect(dialog.querySelectorAll(".ppt-viewer-item")).toHaveLength(1);
  expect(dialog.querySelector(".ppt-viewer-image-wrap img")?.getAttribute("src")).toBe("/slides/2.jpg");
});

function buttonWithText(text: string) {
  return [...document.querySelectorAll<HTMLButtonElement>(".ppt-viewer-dialog button")]
    .find((button) => button.textContent?.includes(text))!;
}

it("defaults PDF exports to preferred pages even while viewing timeline states", async () => {
  await mount();
  act(() => buttonWithText("时间轴状态").click());
  await settle();
  act(() => buttonWithText("导出 PDF").click());
  const checked = document.querySelector<HTMLInputElement>('.ppt-viewer-export input:checked')!;
  expect(checked.value).toBe("preferred_pages");
  act(() => buttonWithText("下载 PDF").click());
  await settle();
  expect(api.downloadSlidePdf).toHaveBeenCalledWith(video, "preferred_pages");
});

it("allows exporting timeline states and reports export errors", async () => {
  vi.mocked(api.downloadSlidePdf).mockRejectedValue(new Error("PPT 图片缺失"));
  await mount();
  act(() => buttonWithText("导出 PDF").click());
  const radio = document.querySelector<HTMLInputElement>('.ppt-viewer-export input[value="timeline_states"]')!;
  act(() => radio.click());
  act(() => buttonWithText("下载 PDF").click());
  await settle();
  expect(api.downloadSlidePdf).toHaveBeenCalledWith(video, "timeline_states");
  expect(document.querySelector(".ppt-viewer-export [role=alert]")?.textContent).toBe("PPT 图片缺失");
});

it("allows switching to the timeline when there are no preferred pages", async () => {
  vi.mocked(api.getVideoSlides).mockResolvedValue(slides.map((slide) => ({ ...slide, is_llm_preferred: false })));
  await mount();
  expect(document.body.textContent).toContain("暂无 PPT 页面精选");
  act(() => buttonWithText("查看 PPT 时间轴状态").click());
  await settle();
  expect(document.querySelectorAll(".ppt-viewer-item")).toHaveLength(2);
});
