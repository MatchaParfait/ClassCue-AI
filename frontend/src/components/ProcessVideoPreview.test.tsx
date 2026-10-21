import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { Video } from "../types";
import { ProcessVideoPreview } from "./ProcessVideoPreview";

vi.mock("../api/client", () => ({
  getStreamUrl: (id: number) => `/api/videos/${id}/stream`,
  getThumbnailUrl: (video: Video) => `/api/videos/${video.id}/thumbnail`,
}));
const video: Video = {
  id: 3, display_name: "尚未处理的课程", filename: "lesson.mp4",
  video_path: "D:\\lesson.mp4", duration_ms: 60000, status: "uploaded",
};
let container: HTMLDivElement, root: Root;
beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {});
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

it("plays the saved video with controls before processing and retains its time across refresh and navigation", () => {
  act(() => root.render(<ProcessVideoPreview video={video} active />));
  const player = container.querySelector("video")!;
  expect(player.getAttribute("src")).toBe("/api/videos/3/stream");
  expect(player.getAttribute("poster")).toBe("/api/videos/3/thumbnail");
  expect(player.controls).toBe(true);
  expect(player.preload).toBe("metadata");
  expect(player.autoplay).toBe(false);
  player.currentTime = 12;
  act(() => root.render(<ProcessVideoPreview video={{ ...video, status: "processing" }} active />));
  expect(container.querySelector("video")).toBe(player);
  expect(player.currentTime).toBe(12);
  expect(HTMLMediaElement.prototype.load).not.toHaveBeenCalled();
  act(() => root.render(<ProcessVideoPreview video={video} active={false} />));
  expect(HTMLMediaElement.prototype.pause).toHaveBeenCalledTimes(1);
  act(() => root.render(<ProcessVideoPreview video={video} active />));
  expect(container.querySelector("video")).toBe(player);
  expect(player.currentTime).toBe(12);
  act(() => root.render(null));
  expect(HTMLMediaElement.prototype.pause).toHaveBeenCalledTimes(2);
});

it("pauses when the browser tab becomes hidden", () => {
  act(() => root.render(<ProcessVideoPreview video={video} active />));
  vi.spyOn(document, "hidden", "get").mockReturnValue(true);
  act(() => document.dispatchEvent(new Event("visibilitychange")));
  expect(HTMLMediaElement.prototype.pause).toHaveBeenCalledTimes(1);
});

it("offers a local reload after a media error without replacing the player", () => {
  act(() => root.render(<ProcessVideoPreview video={video} active />));
  const player = container.querySelector("video")!;
  act(() => player.dispatchEvent(new Event("error")));
  expect(container.querySelector('[role="alert"]')?.textContent).toContain("视频暂时无法播放");
  act(() => container.querySelector("button")!.click());
  expect(HTMLMediaElement.prototype.load).toHaveBeenCalledTimes(1);
  expect(container.querySelector("video")).toBe(player);
  expect(container.querySelector('[role="alert"]')).toBeNull();
});
