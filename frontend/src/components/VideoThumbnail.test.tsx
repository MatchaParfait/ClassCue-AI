import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { VideoThumbnail } from "./VideoThumbnail";
import type { Video } from "../types";

const video: Video = {
  id: 2, display_name: "Lesson", filename: "lesson.mp4", video_path: "lesson.mp4",
  duration_ms: 1000, status: "pending", library_id: "library-a",
};
let container: HTMLDivElement, root: Root;
function render(value = video) {
  act(() => root.render(<VideoThumbnail video={value} />));
}
beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  render();
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
  document.getElementById("classroom-startup-cover")?.remove();
  vi.restoreAllMocks();
});

const cachedCover = "data:image/jpeg;base64,/9j/preview";
function mountWithStartupCover(libraryId = video.library_id) {
  act(() => root.render(null));
  const cover = document.createElement("div");
  cover.id = "classroom-startup-cover";
  cover.dataset.libraryId = libraryId;
  const frame = document.createElement("iframe");
  frame.srcdoc = `<article data-library-key="video-2"><img src="${cachedCover}"></article>`;
  cover.append(frame);
  document.body.append(cover);
  render();
  return cover;
}

it("loads frame 24 lazily without enabling image dragging", () => {
  const image = container.querySelector("img")!;
  expect(image.alt).toBe("Lesson的第 24 帧截图");
  expect(image.getAttribute("loading")).toBe("lazy");
  expect(image.draggable).toBe(false);
  expect(image.getAttribute("src")).toContain("/api/videos/2/thumbnail?v=");
});

it("keeps the icon on failure and avoids retrying every library poll", () => {
  act(() => container.querySelector("img")!.dispatchEvent(new Event("error")));
  expect(container.querySelector("img")).toBeNull();
  expect(container.querySelector("svg")).not.toBeNull();
  render({ ...video, status: "review_ready" });
  expect(container.querySelector("img")).toBeNull();
});

it.each([
  { ...video, video_path: "renamed.mp4" },
  { ...video, source_mtime_ns: 999 },
  { ...video, library_id: "library-b" },
])("tries again when the source or library identity changes: %o", (changed) => {
  act(() => container.querySelector("img")!.dispatchEvent(new Event("error")));
  render(changed);
  expect(container.querySelector("img")).not.toBeNull();
});

it("retains the cached cover after the page overlay goes away, until the live image is decoded", async () => {
  const cover = mountWithStartupCover();
  expect(container.querySelector('img[src^="data:"]')?.getAttribute("src")).toBe(cachedCover);
  const live = container.querySelector<HTMLImageElement>('img[src^="/api/"]')!;
  expect(live.style.opacity).toBe("0");
  cover.remove();
  let decoded!: () => void;
  live.decode = vi.fn(() => new Promise<void>(resolve => { decoded = resolve; }));
  await act(async () => live.dispatchEvent(new Event("load")));
  expect(container.querySelector('img[src^="data:"]')).not.toBeNull();
  await act(async () => decoded());
  expect(container.querySelector('img[src^="data:"]')).toBeNull();
  expect(live.style.opacity).not.toBe("0");
});

it("keeps a usable cached cover when the live thumbnail fails without retrying on status polls", () => {
  mountWithStartupCover().remove();
  act(() => container.querySelector('img[src^="/api/"]')!.dispatchEvent(new Event("error")));
  render({ ...video, status: "review_ready" });
  expect(container.querySelector('img[src^="data:"]')).not.toBeNull();
  expect(container.querySelector('img[src^="/api/"]')).toBeNull();
});

it("never carries a startup cover into a different library or a changed source", () => {
  const cover = mountWithStartupCover("other-library");
  expect(container.querySelector('img[src^="data:"]')).toBeNull();
  cover.remove();
  mountWithStartupCover().remove();
  render({ ...video, video_path: "new-source.mp4" });
  expect(container.querySelector('img[src^="data:"]')).toBeNull();
});

it("discards a broken preview and safely ignores decoding from an obsolete source", async () => {
  mountWithStartupCover().remove();
  const live = container.querySelector<HTMLImageElement>('img[src^="/api/"]')!;
  let decoded!: () => void;
  live.decode = vi.fn(() => new Promise<void>(resolve => { decoded = resolve; }));
  await act(async () => live.dispatchEvent(new Event("load")));
  act(() => container.querySelector('img[src^="data:"]')!.dispatchEvent(new Event("error")));
  expect(container.querySelector('img[src^="data:"]')).toBeNull();
  render({ ...video, video_path: "new-source.mp4" });
  await act(async () => decoded());
  expect(container.querySelector("img")?.getAttribute("src")).toContain("new-source.mp4");
});
