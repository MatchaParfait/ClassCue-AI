import { afterEach, expect, it, vi } from "vitest";
import { analyzeQuestion, getQuestionState, saveQuestionMaterial, uploadQuestionCapture, questionCaptureUrl } from "./questions";

afterEach(() => vi.unstubAllGlobals());

it("uses an actual same-origin image endpoint, not a display label", () => {
  expect(questionCaptureUrl(42, "a".repeat(32))).toBe(`/api/markers/42/question/captures/${"a".repeat(32)}`);
});

it("uploads binary pixels with their capture time without a model request", async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, status: 201, json: async () => ({ id: "capture" }) });
  vi.stubGlobal("fetch", fetch);
  const blob = new Blob(["synthetic"], { type: "image/png" });
  await uploadQuestionCapture(42, { blob, captured_at_ms: 12345 });
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(fetch.mock.calls[0][0]).toBe("/api/markers/42/question/captures?captured_at_ms=12345");
  expect(fetch.mock.calls[0][1].body).toBe(blob);
  expect(new Headers(fetch.mock.calls[0][1].headers).get("content-type")).toBe("image/png");
});

it("sends material as JSON to the selected marker and does not analyze on save", async () => {
  const material = { text: "合成题目", slide_ids: [12] };
  const fetch = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => material });
  vi.stubGlobal("fetch", fetch);
  expect(await saveQuestionMaterial(42, material)).toEqual(material);
  expect(fetch).toHaveBeenCalledTimes(1);
  const [url, options] = fetch.mock.calls[0];
  expect(url).toBe("/api/markers/42/question");
  expect(options.method).toBe("PUT");
  expect(new Headers(options.headers).get("Content-Type")).toBe("application/json");
  expect(JSON.parse(options.body)).toEqual(material);
});

it("reads history without posting and starts analysis only on explicit call", async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({}) });
  vi.stubGlobal("fetch", fetch);
  await getQuestionState(42);
  expect(fetch.mock.calls[0][1]?.method ?? "GET").toBe("GET");
  await analyzeQuestion(42);
  expect(fetch.mock.calls[1][0]).toBe("/api/markers/42/question/analyze");
  expect(fetch.mock.calls[1][1].method).toBe("POST");
});

it("propagates service failures instead of reporting saved material", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 409,
    json: async () => ({ detail: "已有任务正在运行" }) }));
  await expect(saveQuestionMaterial(42, { text: "题目", slide_ids: [] })).rejects.toThrow("已有任务正在运行");
});
