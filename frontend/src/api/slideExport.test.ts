import { afterEach, expect, it, vi } from "vitest";
import { downloadSlidePdf } from "./client";
import type { Video } from "../types";

const video: Video = {
  id: 7, display_name: 'Synthetic: "lesson"', filename: "lesson.mp4",
  video_path: "lesson.mp4", duration_ms: 20000, status: "uploaded",
};

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

it("downloads a PDF with preferred pages by default and supports timeline export", async () => {
  vi.useFakeTimers();
  const revoke = vi.fn();
  const create = vi.fn(() => "blob:synthetic");
  vi.stubGlobal("URL", class extends URL {
    static createObjectURL = create;
    static revokeObjectURL = revoke;
  });
  const fetch = vi.fn().mockResolvedValue({ ok: true, blob: async () => new Blob(["%PDF"]) });
  vi.stubGlobal("fetch", fetch);
  const filenames: string[] = [];
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
    filenames.push(this.download);
  });
  await downloadSlidePdf(video);
  await downloadSlidePdf(video, "timeline_states");
  expect(fetch.mock.calls[0][0]).toContain("/api/videos/7/slides/export?mode=preferred_pages");
  expect(fetch.mock.calls[1][0]).toContain("mode=timeline_states");
  expect(filenames).toEqual([
    "Synthetic_ _lesson_ - PPT 页面精选.pdf", "Synthetic_ _lesson_ - PPT 时间轴状态.pdf",
  ]);
  expect(document.querySelector("a[download]")).toBeNull();
  vi.runAllTimers();
  expect(revoke).toHaveBeenCalledTimes(2);
});

it("shows the server error instead of downloading a failed export", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
    ok: false, status: 409, json: async () => ({ detail: "PPT 图片缺失" }),
  }));
  await expect(downloadSlidePdf(video)).rejects.toThrow("PPT 图片缺失");
});
