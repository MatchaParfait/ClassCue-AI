import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { uploadVideo } from "./client";

class UploadRequest {
  static current: UploadRequest;
  upload = { onprogress: null as null | ((event: { loaded: number; total: number; lengthComputable: boolean }) => void),
    onload: null as null | (() => void) };
  onload: null | (() => void) = null;
  onerror: null | (() => void) = null;
  onabort: null | (() => void) = null;
  status = 200;
  responseText = "";
  open = vi.fn();
  setRequestHeader = vi.fn();
  send = vi.fn();
  abort = vi.fn(() => this.onabort?.());
  constructor() { UploadRequest.current = this; }
  progress(loaded: number, total = 10) {
    this.upload.onprogress?.({ loaded, total, lengthComputable: true });
  }
  respond(value: unknown, status = 200) {
    this.status = status;
    this.responseText = JSON.stringify(value);
    this.onload?.();
  }
}

const file = () => new File(["0123456789"], "课堂 #1.mp4", { type: "video/mp4" });
let controller: AbortController, onProgress: ReturnType<typeof vi.fn>;
beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("XMLHttpRequest", UploadRequest);
  controller = new AbortController();
  onProgress = vi.fn();
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });
const start = () => uploadVideo(file(), { signal: controller.signal, onProgress });

it("sends the file body, reports upload completion and keeps waiting for the normalized server result", async () => {
  const pending = start(), xhr = UploadRequest.current;
  expect(xhr.open).toHaveBeenCalledWith("POST", `/api/videos/upload?filename=${encodeURIComponent(file().name)}`);
  expect(xhr.send).toHaveBeenCalledWith(expect.any(File));
  expect(xhr.setRequestHeader).toHaveBeenCalledWith("Content-Type", "application/octet-stream");
  xhr.progress(4);
  expect(onProgress).toHaveBeenLastCalledWith(4, 10);
  xhr.upload.onload?.();
  expect(onProgress).toHaveBeenLastCalledWith(10, 10);
  xhr.respond({ video: { id: 3, filename: file().name, duration_ms: "1000" }, existed: false });
  expect(await pending).toMatchObject({ video: { id: 3, display_name: file().name, duration_ms: 1000 }, existed: false });
  controller.abort();
  vi.advanceTimersByTime(240_000);
  expect(xhr.abort).not.toHaveBeenCalled();
  expect(vi.getTimerCount()).toBe(0);
});

it("resets inactivity only on advancing bytes, then releases a stalled upload without claiming rollback", async () => {
  const pending = start(), xhr = UploadRequest.current;
  const rejected = expect(pending).rejects.toThrow("刷新视频库核对结果");
  vi.advanceTimersByTime(119_000);
  xhr.progress(4);
  vi.advanceTimersByTime(119_000);
  expect(xhr.abort).not.toHaveBeenCalled();
  xhr.progress(4);
  vi.advanceTimersByTime(1_000);
  await rejected;
  expect(xhr.abort).toHaveBeenCalledOnce();
  expect(vi.getTimerCount()).toBe(0);
});

it("times out a connected upload whose server response never arrives", async () => {
  const pending = start(), xhr = UploadRequest.current;
  const rejected = expect(pending).rejects.toThrow("连续两分钟没有进展");
  vi.advanceTimersByTime(119_000);
  xhr.upload.onload?.();
  vi.advanceTimersByTime(119_000);
  expect(xhr.abort).not.toHaveBeenCalled();
  vi.advanceTimersByTime(1_000);
  await rejected;
  expect(xhr.abort).toHaveBeenCalledOnce();
});

it.each([false, true])("honors cancellation before or during transmission (already aborted: %s)", async alreadyAborted => {
  if (alreadyAborted) controller.abort();
  const pending = start(), xhr = UploadRequest.current;
  const rejected = expect(pending).rejects.toThrow("入库可能仍在完成");
  if (!alreadyAborted) controller.abort();
  await rejected;
  expect(xhr.send).toHaveBeenCalledTimes(alreadyAborted ? 0 : 1);
  expect(xhr.abort).toHaveBeenCalledOnce();
  expect(vi.getTimerCount()).toBe(0);
});

it.each(["http", "network", "invalid-json"] as const)("releases failed requests and cancels the watchdog (%s)", async failure => {
  const pending = start(), xhr = UploadRequest.current;
  const rejected = expect(pending).rejects.toThrow(failure === "http" ? "磁盘空间不足" : /刷新视频库核对/);
  if (failure === "http") xhr.respond({ detail: "磁盘空间不足" }, 400);
  if (failure === "network") xhr.onerror?.();
  if (failure === "invalid-json") { xhr.responseText = "<html>unavailable</html>"; xhr.onload?.(); }
  await rejected;
  expect(vi.getTimerCount()).toBe(0);
  controller.abort();
  expect(xhr.abort).not.toHaveBeenCalled();
});

it("cleans up if the browser cannot read or send the dropped file", async () => {
  class BrokenRequest extends UploadRequest {
    send = vi.fn(() => { throw new Error("unreadable"); });
  }
  vi.stubGlobal("XMLHttpRequest", BrokenRequest);
  await expect(start()).rejects.toThrow("文件是否仍可读取");
  const xhr = UploadRequest.current;
  expect(xhr.abort).toHaveBeenCalledOnce();
  expect(vi.getTimerCount()).toBe(0);
  controller.abort();
  expect(xhr.abort).toHaveBeenCalledOnce();
});
