import { afterEach, expect, it, vi } from "vitest";
import { importVideoPath } from "./client";

afterEach(() => vi.unstubAllGlobals());

it("sends native paths as JSON and preserves duplicate and storage results", async () => {
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({
    video: { id: 3, filename: "camera.mp4", video_path: "videos/camera.mp4", storage_warning: "duplicate" },
    existed: true,
  }), { status: 200 }));
  vi.stubGlobal("fetch", fetch);
  const signal = new AbortController().signal;
  const result = await importVideoPath("D:/synthetic/camera.mp4", signal);
  expect(fetch).toHaveBeenCalledWith("/api/videos/register", expect.objectContaining({
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({ path: "D:/synthetic/camera.mp4" }), signal,
  }));
  expect(result.existed).toBe(true);
  expect(result.video.storage_warning).toBe("duplicate");
});
