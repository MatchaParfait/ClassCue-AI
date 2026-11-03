import { afterEach, expect, it, vi } from "vitest";
import { importSecondaryView } from "./client";

afterEach(() => vi.unstubAllGlobals());

it("imports a local secondary by path without uploading file bytes", async () => {
  const secondary = { video_id: 2, filename: "camera.mp4", duration_ms: 1000, offset_ms: 0 };
  const fetch = vi.fn().mockResolvedValue({
    ok: true, json: async () => ({ video: { id: 2, filename: "camera.mp4" }, secondary_view: secondary }),
  });
  vi.stubGlobal("fetch", fetch);
  const result = await importSecondaryView(1, "D:/synthetic/camera.mp4");
  expect(fetch).toHaveBeenCalledWith("/api/videos/1/secondary-view/import", expect.objectContaining({
    method: "POST", headers: expect.objectContaining({ "Content-Type": "application/json" }),
    body: JSON.stringify({ path: "D:/synthetic/camera.mp4" }),
  }));
  expect(result.secondary_view).toEqual(secondary);
  expect(result.video.id).toBe(2);
});
