import { afterEach, expect, it, vi } from "vitest";
import { captureVideoFrame } from "./captureVideoFrame";

afterEach(() => vi.restoreAllMocks());
function video(overrides = {}) {
  return { readyState: 2, seeking: false, videoWidth: 1280, videoHeight: 720, currentTime: 12.345,
    pause: vi.fn(), ...overrides } as unknown as HTMLVideoElement;
}
it("captures native pixels at the current player time and pauses before drawing", async () => {
  const source = video();
  const drawImage = vi.fn(() => expect(source.pause).toHaveBeenCalled());
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({ drawImage } as unknown as CanvasRenderingContext2D);
  vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation(function (callback) { callback(new Blob(["synthetic"], { type: "image/png" })); });
  const frame = await captureVideoFrame(source);
  expect(frame.captured_at_ms).toBe(12345);
  expect(frame.blob.type).toBe("image/png");
  expect(drawImage).toHaveBeenCalledWith(source, 0, 0, 1280, 720);
});
it.each([null, video({ readyState: 1 }), video({ seeking: true }), video({ videoWidth: 0 })])("rejects unavailable or seeking video", async source => {
  await expect(captureVideoFrame(source)).rejects.toThrow("尚未就绪");
});
it("rejects oversized frames and failed canvas output", async () => {
  await expect(captureVideoFrame(video({ videoWidth: 8000, videoHeight: 4000 }))).rejects.toThrow("1600 万");
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({ drawImage: vi.fn() } as unknown as CanvasRenderingContext2D);
  vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation(callback => callback(null));
  await expect(captureVideoFrame(video())).rejects.toThrow("截图生成失败");
});
