import { afterEach, describe, expect, it, vi } from "vitest";
import { pollPicker } from "./client";

const response = (payload: unknown) => ({
  ok: true,
  json: async () => payload,
}) as Response;

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("path picker client cleanup", () => {
  it("keeps completed picker results without issuing DELETE", async () => {
    const fetch = vi.fn().mockResolvedValue(response({ status: "completed", path: "E:/videos" }));
    vi.stubGlobal("fetch", fetch);
    expect(await pollPicker("picker-1")).toBe("E:/videos");
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("cancels on polling failure and preserves the original error when DELETE fails", async () => {
    const original = new Error("polling failed");
    const fetch = vi.fn().mockRejectedValueOnce(original).mockRejectedValueOnce(new Error("cleanup failed"));
    vi.stubGlobal("fetch", fetch);
    await expect(pollPicker("picker-1")).rejects.toBe(original);
    expect(fetch.mock.calls[1][1].method).toBe("DELETE");
  });

  it("cancels when the deadline expires before polling", async () => {
    const fetch = vi.fn().mockResolvedValue(response({ status: "cancelled", path: "" }));
    vi.stubGlobal("fetch", fetch);
    await expect(pollPicker("picker-1", 0)).rejects.toThrow("选择器等待超时");
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0][1].method).toBe("DELETE");
  });

  it("aborts a stuck GET and sends DELETE without waiting indefinitely", async () => {
    vi.useFakeTimers();
    const fetch = vi.fn()
      .mockImplementationOnce((_url: string, init: RequestInit) => new Promise((_resolve, reject) => {
        init.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
      }))
      .mockResolvedValueOnce(response({ status: "cancelled", path: "" }));
    vi.stubGlobal("fetch", fetch);
    const outcome = pollPicker("picker-1", 100).catch((error: Error) => error.message);
    await vi.advanceTimersByTimeAsync(100);
    expect(await outcome).toBe("选择器等待超时");
    expect(fetch.mock.calls[1][1].method).toBe("DELETE");
  });

  it("bounds cleanup time without replacing a polling error", async () => {
    vi.useFakeTimers();
    const original = new Error("polling failed");
    const fetch = vi.fn().mockRejectedValueOnce(original)
      .mockImplementationOnce((_url: string, init: RequestInit) => new Promise((_resolve, reject) => {
        init.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
      }));
    vi.stubGlobal("fetch", fetch);
    const outcome = pollPicker("picker-1", 100).catch((error: Error) => error);
    await vi.advanceTimersByTimeAsync(5_000);
    expect(await outcome).toBe(original);
  });
});
