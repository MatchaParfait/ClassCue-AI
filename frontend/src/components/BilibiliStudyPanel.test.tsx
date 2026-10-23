import { act } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../api/bilibili";
import { BilibiliStudyPanel } from "./BilibiliStudyPanel";
import type { BiliRun, BiliState } from "../api/bilibili";
import type { Marker } from "../types";
import { cancelJob } from "../api/client";

const taskState = vi.hoisted(() => ({ status: "completed", progress: 100 }));

vi.mock("../api/bilibili", () => ({
  getBiliState: vi.fn(), saveBiliContext: vi.fn(), planBiliSearch: vi.fn(), startBiliSearch: vi.fn(),
  getBiliAccount: vi.fn(), startBiliLogin: vi.fn(), pollBiliLogin: vi.fn(), cancelBiliLogin: vi.fn(),
  deleteBiliAccount: vi.fn(), resumeBiliReading: vi.fn(),
  continueBiliSearch: vi.fn(),
}));
vi.mock("../hooks/useTaskStatus", () => ({
  useTaskStatus: () => ({ data: { id: "synthetic-job", ...taskState }, isError: false, refetch: vi.fn() }),
  isTaskTerminal: (job: { status: string }) => ["completed", "failed", "interrupted"].includes(job.status),
}));
vi.mock("../api/client", () => ({ cancelJob: vi.fn().mockResolvedValue({ status: "interrupted" }) }));

const marker = {
  id: 42, video_id: 1, title: "单调性", note: "分类讨论", source_quote: "这类题需要分类讨论",
  start_ms: 1000, end_ms: 5000, analysis_mode: "medium_recall", review_status: "active",
} as Marker;
const context = { course: "数学", notes: "" };
const run = {
  id: "run1", job_id: null, status: "ready", stale: false, error: "",
  queries: [{ text: "单调性 例题", purpose: "分类讨论" }], candidates: [],
  target: { course: "数学", keywords: ["单调性"], unknowns: [], requirements: [
    { id: "r1", text: "单调性例题", required: true, depth: "example", sources: ["marker.title"] }] },
  course_queries: [{ text: "高等数学课程", purpose: "课程补搜" }],
} as BiliRun;
let host: HTMLDivElement, root: ReturnType<typeof createRoot>, client: QueryClient;
let state: BiliState;
const closed = vi.fn();

async function settle() { await act(async () => { await new Promise(resolve => setTimeout(resolve, 30)); }); }
function button(label: string) {
  return [...host.querySelectorAll<HTMLButtonElement>("button")].find(item => item.textContent?.trim() === label)!;
}
function setInput(input: HTMLInputElement, value: string) {
  act(() => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
async function render() {
  await act(async () => root.render(<QueryClientProvider client={client}>
    <BilibiliStudyPanel marker={marker} backgroundRef={{ current: null }} onClose={closed} />
  </QueryClientProvider>));
  await settle();
}

beforeEach(() => {
  vi.clearAllMocks();
  taskState.status = "completed"; taskState.progress = 100;
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  state = { context: { ...context }, runs: [] };
  vi.mocked(api.getBiliState).mockImplementation(async () => structuredClone(state));
  vi.mocked(api.getBiliAccount).mockResolvedValue({ configured: false, pause: null });
  vi.mocked(api.saveBiliContext).mockImplementation(async (_, value) => {
    state.context = value; return value;
  });
});
afterEach(async () => {
  await act(async () => root.unmount());
  client.clear(); host.remove();
  vi.unstubAllGlobals();
});

describe("marker-scoped Bilibili panel", () => {
  it("loads and saves only the selected marker context without calling the model on open", async () => {
    await render();
    expect(api.getBiliState).toHaveBeenCalledWith(42);
    expect(button("生成搜索词").disabled).toBe(false);
    expect(host.textContent).not.toContain("同意将本考点");
    const inputs = host.querySelectorAll<HTMLInputElement>(".bili-fields input");
    expect(inputs).toHaveLength(1);
    expect(host.textContent).not.toContain("学段或考试类型");
    expect(host.textContent).not.toContain("教材版本");
    expect(host.querySelector(".bili-fields textarea")).not.toBeNull();
    expect(inputs[0].value).toBe("数学");
    setInput(inputs[0], "概率论");
    await act(async () => button("保存补充信息").click());
    await settle();
    expect(api.saveBiliContext).toHaveBeenCalledWith(42, { course: "概率论", notes: "" });
    expect(api.planBiliSearch).not.toHaveBeenCalled();
  });

  it("shows generated queries after the job changes state and sends edited queries only after confirmation", async () => {
    vi.mocked(api.planBiliSearch).mockImplementation(async () => {
      state.runs = [structuredClone(run)]; return { run_id: "run1", job_id: "job1" };
    });
    vi.mocked(api.startBiliSearch).mockImplementation(async () => {
      state.runs = [{ ...run, id: "run2", status: "completed" }, run];
      return { run_id: "run2", job_id: "job2" };
    });
    await render();
    await act(async () => button("生成搜索词").click());
    await settle();
    expect(api.planBiliSearch).toHaveBeenCalledWith(42, true);
    const query = host.querySelector<HTMLInputElement>(".bili-queries input")!;
    expect(query.value).toBe("单调性 例题");
    setInput(query, "单调性 参数 分类讨论");
    expect(button("搜索并核验").disabled).toBe(true);
    act(() => host.querySelector<HTMLInputElement>(".bili-consent input")!.click());
    await act(async () => button("搜索并核验").click());
    await settle();
    expect(api.startBiliSearch).toHaveBeenCalledWith(42, "run1",
      [{ text: "单调性 参数 分类讨论", purpose: "分类讨论" }], true,
      { target: run.target, course_queries: run.course_queries, auto_rewrite: false });
  });

  it("separates evidence-backed recommendations from unverified candidates and preserves exact timestamps", async () => {
    state.runs = [{ ...run, status: "completed", candidates: [
      { bvid: "BV1xx411c7mD", title: "例题讲解", author: "合成作者", url: "https://www.bilibili.com/video/BV1xx411c7mD/",
        status: "unverified", unchecked_pages: 2, units: [{
          id: "unit1", page: 1, cid: 1, part: "分类讨论", url: "https://www.bilibili.com/video/BV1xx411c7mD/?p=1",
          status: "checked", verdict: "partial", reason: "只覆盖部分方法",
          source: { language: "zh-CN", automatic: true },
          coverage: { partial: true, selected_segments: 20, total_segments: 100 },
          evidence: [{ id: 7, start_ms: 12500, end_ms: 15000, text: "先判断参数范围",
            url: "https://www.bilibili.com/video/BV1xx411c7mD/?p=1&t=12" }],
        }] },
      { bvid: "BV1yy411c7mD", title: "无字幕候选", author: "", status: "unverified", units: [],
        url: "https://www.bilibili.com/video/BV1yy411c7mD/" },
    ] }];
    await render();
    expect(host.textContent).toContain("部分相关");
    expect(host.textContent).toContain("20/100");
    expect(host.textContent).toContain("另有 2 个分 P 未核验");
    expect(host.querySelector(".bili-other")?.textContent).toContain("无字幕候选");
    expect(host.querySelector('a[href$="t=12"]')?.textContent).toContain("0:12");
    expect(host.textContent).not.toContain("原题匹配");
  });

  it("disables stale searches and replaces manual credentials with QR login", async () => {
    const storeSecret = vi.fn();
    vi.stubGlobal("localStorage", { setItem: storeSecret });
    state.runs = [{ ...run, stale: true }];
    await render();
    expect(host.textContent).toContain("以下为历史结果");
    act(() => host.querySelector<HTMLInputElement>(".bili-consent input")!.click());
    expect(button("搜索并核验").disabled).toBe(true);
    expect(host.querySelector('input[type="password"]')).toBeNull();
    expect(button("扫码登录")).toBeDefined();
    expect(api.startBiliLogin).not.toHaveBeenCalled();
    expect(storeSecret).not.toHaveBeenCalled();
  });

  it("closes without triggering searches", async () => {
    await render();
    await act(async () => host.querySelector<HTMLButtonElement>('[aria-label="关闭 B 站讲解"]')!.click());
    expect(closed).toHaveBeenCalledTimes(1);
    expect(api.startBiliSearch).not.toHaveBeenCalled();
  });

  it("edits course fallback and target depth before confirming", async () => {
    state.runs = [structuredClone(run)];
    vi.mocked(api.startBiliSearch).mockResolvedValue({ run_id: "run1", job_id: "job1" });
    await render();
    setInput(host.querySelector<HTMLInputElement>('[aria-label="课程搜索词 1"]')!, "大学数学课程");
    const depth = host.querySelector<HTMLSelectElement>('[aria-label="讲解深度 1"]')!;
    act(() => { depth.value = "method"; depth.dispatchEvent(new Event("change", { bubbles: true })); });
    act(() => host.querySelector<HTMLInputElement>(".bili-consent input")!.click());
    await act(async () => button("搜索并核验").click());
    expect(api.startBiliSearch).toHaveBeenCalledWith(42, "run1", run.queries, true, expect.objectContaining({
      course_queries: [{ text: "大学数学课程", purpose: "课程补搜" }],
      target: expect.objectContaining({ requirements: [expect.objectContaining({ depth: "method", sources: ["user.confirmed"] })] }),
    }));
  });

  it("continues the same interrupted run without creating a new search", async () => {
    state.runs = [{ ...run, status: "interrupted", phase: "course", stop_reason: "cancelled" }];
    vi.mocked(api.continueBiliSearch).mockResolvedValue({ run_id: "run1", job_id: "continued" });
    await render();
    expect(host.textContent).toContain("课程类别检索");
    act(() => host.querySelector<HTMLInputElement>(".bili-consent input")!.click());
    await act(async () => button("继续搜索与核验").click());
    expect(api.continueBiliSearch).toHaveBeenCalledWith(42, "run1", undefined, false);
    expect(api.startBiliSearch).not.toHaveBeenCalled();
  });

  it("waits for approval before submitting rewritten course queries", async () => {
    state.runs = [{ ...run, status: "awaiting_approval", pending_queries: [{ text: "数学习题课程", purpose: "缺少例题" }] }];
    vi.mocked(api.continueBiliSearch).mockResolvedValue({ run_id: "run1", job_id: "continued" });
    await render();
    expect(button("确认并补搜").disabled).toBe(true);
    expect(api.continueBiliSearch).not.toHaveBeenCalled();
    act(() => host.querySelector<HTMLInputElement>(".bili-consent input")!.click());
    await act(async () => button("确认并补搜").click());
    expect(api.continueBiliSearch).toHaveBeenCalledWith(42, "run1", state.runs[0].pending_queries, false);
  });

  it("keeps partial matches outside verified recommendations and labels plain videos", async () => {
    state.runs = [{ ...run, status: "completed", stop_reason: "partial", candidates: [{
      bvid: "BV1xx411c7mD", title: "普通视频", author: "", url: "https://www.bilibili.com/video/BV1xx411c7mD/",
      status: "done", units: [{ id: "one", page: 1, cid: 1, part: "概念解释", multipart: false,
        url: "https://www.bilibili.com/video/BV1xx411c7mD/", status: "checked", verdict: "partial", reason: "缺少例题" }],
    }] }];
    await render();
    expect(host.querySelector(".bili-other")?.textContent).toContain("普通视频");
    expect(host.textContent).not.toContain("P1");
    expect(host.textContent).toContain("仅找到部分相关讲解");
  });

  it("shows progress and cancels the active job without restarting search", async () => {
    taskState.status = "running"; taskState.progress = 42;
    state.runs = [{ ...run, status: "checking", job_id: "running-job" }];
    await render();
    expect(host.querySelector<HTMLProgressElement>("progress")?.value).toBe(42);
    await act(async () => button("停止").click());
    expect(cancelJob).toHaveBeenCalledWith("running-job");
    expect(api.startBiliSearch).not.toHaveBeenCalled();
    expect(api.continueBiliSearch).not.toHaveBeenCalled();
  });
});
