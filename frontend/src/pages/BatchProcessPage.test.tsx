import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../api/client";
import { BatchProcessPage } from "./BatchProcessPage";
import { RECALL_SELECTION_KEY } from "../hooks/useRecallSelection";

vi.mock("../api/client", () => ({
  cancelJob: vi.fn(), getJob: vi.fn(), openPicker: vi.fn(), pollPicker: vi.fn(),
  processBatch: vi.fn(), resumeJob: vi.fn(), getPausedJobs: vi.fn(),
  getAsrSelection: vi.fn(), getAsrResourceProfile: vi.fn(), getAsrModelStatus: vi.fn(),
  previewBatch: vi.fn(), getLatestBatch: vi.fn(), retryBatch: vi.fn(),
  getAiSettings: vi.fn(),
  getRecallSchemes: vi.fn(),
}));

beforeEach(() => {
  window.localStorage.removeItem(`${RECALL_SELECTION_KEY}:default`);
  vi.mocked(api.getRecallSchemes).mockResolvedValue([]);
  vi.mocked(api.getAiSettings).mockResolvedValue({
    provider: "deepseek", base_url: "https://api.example.test", model: "fixture-model", has_api_key: false,
    output_tokens: 2048, token_parameter: "max_tokens", json_mode: false, timeout_seconds: 60, max_retries: 2,
  });
  vi.mocked(api.getLatestBatch).mockResolvedValue(null);
  vi.mocked(api.previewBatch).mockImplementation(async (folders) => ({
    valid: true, folders: folders.map((path) => ({
      path, valid: true, video_count: 2, non_video_count: 0, subfolder_count: 0, examples: [], error: "",
    })),
  }));
});

afterEach(() => vi.resetAllMocks());

describe("resource-paused batch", () => {
  it("hides screening controls and disables low-information filtering for new batches", async () => {
    localStorage.setItem(`${RECALL_SELECTION_KEY}:default`, JSON.stringify(["high_recall"]));
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    vi.mocked(api.getPausedJobs).mockResolvedValue([]);
    vi.mocked(api.getAsrSelection).mockResolvedValue({
      id: "whisper-turbo", name: "Whisper", engine: "whisper",
      downloaded: true, cache_size_bytes: 0, selected: true,
    });
    vi.mocked(api.getAsrModelStatus).mockResolvedValue({
      id: "whisper-turbo", name: "Whisper", engine: "whisper",
      downloaded: true, cache_size_bytes: 0, selected: true,
    });
    vi.mocked(api.processBatch).mockResolvedValue({ job_id: "batch", status: "queued", video_ids: [1], errors: [] });
    vi.mocked(api.getJob).mockResolvedValue({ id: "batch", status: "queued" });
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    try {
      act(() => root.render(
        <QueryClientProvider client={client}>
          <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}><BatchProcessPage /></MemoryRouter>
        </QueryClientProvider>,
      ));
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 30)); });
      for (const label of ["关键词预筛选", "排除的公式文本", "过滤低信息字幕"]) {
        expect(container.textContent).not.toContain(label);
      }
      expect(container.querySelector('a[href="/settings?section=ai"]')).not.toBeNull();
      act(() => {
        const field = container.querySelector<HTMLInputElement>(".batch-folder-row input")!;
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(field, "D:\\courses");
        field.dispatchEvent(new Event("input", { bubbles: true }));
      });
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 450)); });
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 30)); });
      act(() => [...container.querySelectorAll("button")].find((item) => item.textContent?.includes("导入并开始批处理"))!.click());
      for (let i = 0; i < 100 && !vi.mocked(api.processBatch).mock.calls.length; i++) {
        await act(async () => { await new Promise((resolve) => setTimeout(resolve, 15)); });
      }
      expect(api.processBatch).toHaveBeenCalledWith(["D:\\courses"], expect.objectContaining({
        filter_formula_noise: false, filter_formula: null, keywords: [], use_keyword_prefilter: false,
        analysis_modes: ["high_recall"],
      }));
    } finally {
      await act(async () => { root.unmount(); client.clear(); });
      container.remove();
    }
  });

  it("restores FireRed readonly routing and recovery details for a paused queue", async () => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    vi.mocked(api.getPausedJobs).mockResolvedValue([
      { id: "firered-paused", status: "resource_paused", job_type: "batch_video_process" },
    ]);
    vi.mocked(api.getAsrSelection).mockResolvedValue({
      id: "fireredasr2-aed", name: "FireRedASR2S", engine: "firered",
      downloaded: true, cache_size_bytes: 0, selected: true,
    });
    vi.mocked(api.getAsrModelStatus).mockResolvedValue({
      id: "fireredasr2-aed", name: "FireRedASR2S", engine: "firered",
      downloaded: true, cache_size_bytes: 0, selected: true,
    });
    vi.mocked(api.getAsrResourceProfile).mockResolvedValue({
      engine: "firered", device: "cuda:0", cuda_available: true,
      gpu_total_mib: 8188, initial_batch_size: 6,
    });
    vi.mocked(api.getJob).mockResolvedValue({
      id: "firered-paused", status: "resource_paused", progress: 30,
      resource: {
        engine: "firered", device: "cuda:0", initial_batch_size: 6, current_batch_size: 3,
        completed_segments: 200, total_segments: 915, recovery_count: 2,
        recovery_reason: "inference: CUDA OOM",
      },
    });
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    try {
      act(() => root.render(
        <QueryClientProvider client={client}>
          <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
            <BatchProcessPage />
          </MemoryRouter>
        </QueryClientProvider>,
      ));
      for (let i = 0; i < 100 && !container.textContent?.includes("当前 BS3"); i++) {
        await act(async () => { await new Promise((resolve) => setTimeout(resolve, 15)); });
      }
      expect(container.textContent).toContain("初始 BS6");
      expect(container.textContent).toContain("当前 BS3");
      expect(container.textContent).toContain("已保存 200/915");
      expect(container.textContent).toContain("inference: CUDA OOM");
      expect(container.textContent).toContain("继续队列");
      expect(container.textContent).toContain("跳过当前文件");
      expect(container.textContent).not.toContain("Qwen 资源模式");
      expect(container.querySelector(".firered-resource-profile input")).toBeNull();
      expect(api.processBatch).not.toHaveBeenCalled();
    } finally {
      await act(async () => { root.unmount(); client.clear(); });
      container.remove();
    }
  });

  it("recovers a durable paused queue and can skip its current file", async () => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    vi.mocked(api.getPausedJobs).mockResolvedValue([
      { id: "paused", status: "resource_paused", job_type: "batch_video_process" },
    ]);
    vi.mocked(api.getJob).mockResolvedValue({
      id: "paused", status: "resource_paused", progress: 30, error_message: "resources",
    });
    vi.mocked(api.getAsrSelection).mockResolvedValue({
      id: "qwen3-asr-1.7b", name: "Qwen", engine: "qwen",
      downloaded: true, cache_size_bytes: 0, selected: true,
    });
    vi.mocked(api.getAsrModelStatus).mockResolvedValue({
      id: "qwen3-asr-1.7b", name: "Qwen", engine: "qwen",
      downloaded: true, cache_size_bytes: 0, selected: true,
    });
    vi.mocked(api.resumeJob).mockResolvedValue({ id: "next", status: "queued" });
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    try {
      act(() => root.render(
        <QueryClientProvider client={client}>
          <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
            <BatchProcessPage />
          </MemoryRouter>
        </QueryClientProvider>,
      ));
      for (let i = 0; i < 100 && !container.textContent?.includes("跳过当前文件"); i++) {
        await act(async () => { await new Promise((resolve) => setTimeout(resolve, 15)); });
      }
      const skip = [...container.querySelectorAll("button")].find((button) => button.textContent?.includes("跳过当前文件"));
      expect(skip).toBeTruthy();
      expect(container.textContent).toContain("低于 6GB 请勿使用此模型");
      expect(container.textContent).toContain("共享显存不计入");
      act(() => skip!.click());
      for (let i = 0; i < 100 && !vi.mocked(api.resumeJob).mock.calls.length; i++) {
        await act(async () => { await new Promise((resolve) => setTimeout(resolve, 15)); });
      }
      expect(api.resumeJob).toHaveBeenCalledWith("paused", true);
    } finally {
      await act(async () => { root.unmount(); client.clear(); });
      container.remove();
    }
  });
});

async function mountBatch() {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.mocked(api.getPausedJobs).mockResolvedValue([]);
  const model = { id: "whisper-turbo", name: "Whisper", engine: "whisper", downloaded: true, cache_size_bytes: 0, selected: true };
  vi.mocked(api.getAsrSelection).mockResolvedValue(model);
  vi.mocked(api.getAsrModelStatus).mockResolvedValue(model);
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  act(() => root.render(<QueryClientProvider client={client}><MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}><BatchProcessPage /></MemoryRouter></QueryClientProvider>));
  await pause(30);
  return {
    container,
    change(value: string) {
      act(() => {
        const input = container.querySelector<HTMLInputElement>(".batch-folder-row input")!;
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
        input.dispatchEvent(new Event("input", { bubbles: true }));
      });
    },
    async dispose() {
      await act(async () => { root.unmount(); client.clear(); });
      container.remove();
    },
  };
}

async function pause(ms: number) {
  for (let remaining = ms; remaining > 0; remaining -= 20) {
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, Math.min(20, remaining))); });
  }
}

describe("directory checks and persistent retries", () => {
  it("allows clearing recall choices and validates on submission before importing folders", async () => {
    vi.mocked(api.processBatch).mockResolvedValue({ job_id: "batch", status: "queued", video_ids: [1], errors: [] });
    vi.mocked(api.getJob).mockResolvedValue({ id: "batch", status: "queued" });
    const view = await mountBatch();
    try {
      view.change("D:\\course");
      await pause(450);
      const medium = view.container.querySelector<HTMLInputElement>('.recall-mode-control input[value="medium_recall"]')!;
      act(() => medium.click());
      expect(view.container.querySelectorAll(".recall-mode-control input:checked")).toHaveLength(0);
      expect(view.container.textContent).not.toContain("请至少选择一种考点召回类型");
      const button = view.container.querySelector<HTMLButtonElement>(".process-actions button")!;
      expect(button.disabled).toBe(false);
      act(() => button.click());
      await pause(40);
      expect(view.container.textContent).toContain("请至少选择一种考点召回类型");
      expect(api.processBatch).not.toHaveBeenCalled();
      act(() => medium.click());
      expect(view.container.textContent).not.toContain("请至少选择一种考点召回类型");
      act(() => button.click());
      await pause(40);
      expect(api.processBatch).toHaveBeenCalledWith(["D:\\course"], expect.objectContaining({ analysis_modes: ["medium_recall"] }));
    } finally { await view.dispose(); }
  });

  it("disables import until preview completes, then invalidates on path change without losing focus", async () => {
    const view = await mountBatch();
    try {
      const button = view.container.querySelector<HTMLButtonElement>(".process-actions button")!;
      expect(button.disabled).toBe(true);
      const input = view.container.querySelector<HTMLInputElement>("input")!;
      input.focus();
      view.change("D:\\course");
      expect(button.disabled).toBe(true);
      await pause(450);
      expect(button.disabled).toBe(false);
      expect(api.previewBatch).toHaveBeenCalledWith(["D:\\course"]);
      view.change("D:\\other");
      expect(button.disabled).toBe(true);
      expect(document.activeElement).toBe(input);
      expect(api.processBatch).not.toHaveBeenCalled();
    } finally { await view.dispose(); }
  });

  it("offers an import-only action that skips processing", async () => {
    vi.mocked(api.processBatch).mockResolvedValue({ job_id: "import-only", status: "queued", video_ids: [1, 2], total: 2, errors: [] });
    vi.mocked(api.getJob).mockResolvedValue({ id: "import-only", status: "queued", import_only: true });
    const view = await mountBatch();
    try {
      view.change("D:\\course");
      await pause(450);
      const button = [...view.container.querySelectorAll("button")].find((item) => item.textContent?.includes("仅导入"));
      expect(button).toBeTruthy();
      expect((button as HTMLButtonElement).disabled).toBe(false);
      act(() => button!.click());
      await pause(40);
      expect(api.processBatch).toHaveBeenCalledWith(["D:\\course"], expect.objectContaining({ import_only: true }));
      expect(view.container.textContent).toContain("已导入 2 个视频，尚未开始处理");
    } finally { await view.dispose(); }
  });

  it("shows mixed-directory counts and examples and blocks import", async () => {
    vi.mocked(api.previewBatch).mockResolvedValue({
      valid: false, folders: [{
        path: "D:\\mixed", valid: false, video_count: 3, non_video_count: 1, subfolder_count: 1,
        examples: ["subfolder/notes.txt"], error: "该文件夹或其子文件夹包含非视频文件，请移除非视频文件后再处理。",
      }],
    });
    const view = await mountBatch();
    try {
      view.change("D:\\mixed");
      await pause(450);
      expect(view.container.textContent).toContain("1 个非视频文件");
      expect(view.container.textContent).toContain("1 个子文件夹");
      expect(view.container.textContent).toContain("notes.txt");
      expect(view.container.querySelector<HTMLAnchorElement>(".batch-directory-examples a")?.href)
        .toBe("file:///D:/mixed/subfolder/notes.txt");
      expect(view.container.querySelector(".batch-directory-recheck")).not.toBeNull();
      expect(view.container.querySelector<HTMLButtonElement>(".process-actions button")!.disabled).toBe(true);
      expect(api.processBatch).not.toHaveBeenCalled();
    } finally { await view.dispose(); }
  });

  it("rechecks the directory after the user removes the offending file", async () => {
    let checks = 0;
    vi.mocked(api.previewBatch).mockImplementation(async (folders) => {
      checks += 1;
      return checks === 1 ? {
        valid: false, folders: [{ path: folders[0], valid: false, video_count: 1, non_video_count: 1,
          subfolder_count: 0, examples: ["notes.txt"], error: "包含非视频文件" }],
      } : {
        valid: true, folders: [{ path: folders[0], valid: true, video_count: 1, non_video_count: 0,
          subfolder_count: 0, examples: [], error: "" }],
      };
    });
    const view = await mountBatch();
    try {
      view.change("D:\\course");
      await pause(450);
      expect(view.container.querySelector<HTMLButtonElement>(".process-actions button")!.disabled).toBe(true);
      act(() => view.container.querySelector<HTMLButtonElement>(".batch-directory-recheck")!.click());
      await pause(40);
      expect(checks).toBe(2);
      expect(view.container.querySelector<HTMLButtonElement>(".process-actions button")!.disabled).toBe(false);
      expect(view.container.querySelector(".batch-directory-examples")).toBeNull();
    } finally { await view.dispose(); }
  });

  it("allows nested video directories and explains that the library keeps their hierarchy", async () => {
    vi.mocked(api.previewBatch).mockResolvedValue({
      valid: true, folders: [{
        path: "D:\\nested", valid: true, video_count: 3, non_video_count: 0,
        subfolder_count: 2, examples: [], error: "",
      }],
    });
    const view = await mountBatch();
    try {
      view.change("D:\\nested");
      await pause(450);
      expect(view.container.textContent).toContain("2 个子文件夹");
      expect(view.container.textContent).toContain("保留相同层级");
      expect(view.container.querySelector<HTMLButtonElement>(".process-actions button")!.disabled).toBe(false);
    } finally { await view.dispose(); }
  });

  it("keeps import disabled on preview failure and allows an explicit recheck", async () => {
    vi.mocked(api.previewBatch).mockRejectedValueOnce(new Error("permission denied"));
    const view = await mountBatch();
    try {
      view.change("D:\\course");
      await pause(450);
      expect(view.container.textContent).toContain("permission denied");
      expect(view.container.querySelector<HTMLButtonElement>(".process-actions button")!.disabled).toBe(true);
      act(() => view.container.querySelector<HTMLButtonElement>('[aria-label="重新检查文件夹"]')!.click());
      await pause(40);
      expect(view.container.querySelector<HTMLButtonElement>(".process-actions button")!.disabled).toBe(false);
    } finally { await view.dispose(); }
  });

  it("restores an awaiting-api batch and retries by job id without scanning or importing folders", async () => {
    const saved = {
      id: "saved", status: "awaiting_api", stage: "awaiting_api", progress: 100,
      message: "字幕已保存，等待 AI 恢复",
      batch_summary: { total: 3, completed: 1, skipped: 0, failed: 0, awaiting_ai: 2, pending_asr: 0 },
    };
    vi.mocked(api.getLatestBatch).mockResolvedValue(saved);
    vi.mocked(api.getJob).mockResolvedValue(saved);
    vi.mocked(api.retryBatch).mockResolvedValue({ ...saved, id: "retry", status: "running", progress: 0 });
    const view = await mountBatch();
    try {
      await pause(40);
      expect(view.container.textContent).toContain("等待 AI 恢复");
      expect(view.container.querySelectorAll(".batch-summary dd")[2].textContent).toBe("2");
      const retry = [...view.container.querySelectorAll("button")].find((button) => button.textContent?.includes("重试未完成视频"))!;
      expect(retry.disabled).toBe(false);
      act(() => retry.click());
      await pause(40);
      expect(api.retryBatch).toHaveBeenCalledWith("saved");
      expect(api.processBatch).not.toHaveBeenCalled();
      expect(api.previewBatch).not.toHaveBeenCalled();
    } finally { await view.dispose(); }
  });

  it("offers retry for partial failures but not for a fully successful batch", async () => {
    const completed = {
      id: "completed", status: "completed", message: "1 个完成，1 个失败",
      batch_summary: { total: 2, completed: 1, skipped: 0, failed: 1, awaiting_ai: 0, pending_asr: 0 },
    };
    vi.mocked(api.getLatestBatch).mockResolvedValue(completed);
    vi.mocked(api.getJob).mockResolvedValue(completed);
    const view = await mountBatch();
    try {
      await pause(40);
      expect(view.container.textContent).toContain("重试未完成视频");
      expect(view.container.textContent).not.toContain("批处理完成，视频已按原文件夹归档");
    } finally { await view.dispose(); }
    vi.mocked(api.getLatestBatch).mockResolvedValue({
      ...completed, batch_summary: { ...completed.batch_summary, completed: 2, failed: 0 },
    });
    vi.mocked(api.getJob).mockResolvedValue({
      ...completed, batch_summary: { ...completed.batch_summary, completed: 2, failed: 0 },
    });
    const success = await mountBatch();
    try {
      await pause(40);
      expect(success.container.textContent).not.toContain("重试未完成视频");
    } finally { await success.dispose(); }
  });

  it("hides a stale retry action when the backend confirms no retryable entries remain", async () => {
    const repaired = {
      id: "repaired", status: "completed", message: "批处理结束：24 个完成，4 个失败",
      batch_summary: { total: 28, completed: 24, skipped: 0, failed: 4, awaiting_ai: 0, pending_asr: 0, retryable: 0 },
    };
    vi.mocked(api.getLatestBatch).mockResolvedValue(repaired);
    vi.mocked(api.getJob).mockResolvedValue(repaired);
    const view = await mountBatch();
    try {
      await pause(40);
      expect(view.container.textContent).not.toContain("重试未完成视频");
      expect(view.container.textContent).toContain("失败");
    } finally { await view.dispose(); }
  });
});
