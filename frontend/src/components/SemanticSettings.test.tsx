import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import * as semantic from "../api/semantic";
import { cancelJob, getJob } from "../api/client";
import { SemanticSettings } from "./SemanticSettings";

vi.mock("../api/semantic", () => ({
  getSemanticCourses: vi.fn(), getSemanticStatus: vi.fn(),
  prepareSemanticModel: vi.fn(), buildSemanticIndex: vi.fn(), setupSemanticSearch: vi.fn(),
  cleanupSemanticSearch: vi.fn(),
  getSemanticCache: vi.fn(),
}));
vi.mock("../api/client", () => ({ getJob: vi.fn(), cancelJob: vi.fn() }));
let root: Root, host: HTMLDivElement, client: QueryClient;
const status: semantic.SemanticStatus = {
  library_id: "synthetic", model_id: "Qwen", ready: true, device: "cuda", dimensions: 1024,
  busy: false, active_job_id: null,
  weights_ready: true, gpu_detected: true, gpu_unavailable_reason: "",
  runtimes: { cpu: { ready: false }, cuda: { ready: true } },
  cache_present: true,
  download_estimates: { weights_bytes: 0, runtimes: { cpu: 300_000_000, cuda: 0 },
    total_bytes: { cpu: 300_000_000, cuda: 0 } },
  coverage: { video_count: 2, with_subtitles: 2, missing_subtitles: 0,
    indexed_videos: 0, pending_videos: 2, chunk_count: 0 },
};
const cache: semantic.SemanticCache = {
  weights: { present: true, ready: true, bytes: 1024 ** 3, files: 6 },
  runtimes: { cpu: { present: false, ready: false, bytes: 0, files: 0 },
    cuda: { present: true, ready: true, bytes: 5 * 1024 ** 3, files: 100 } },
};
beforeEach(() => {
  vi.resetAllMocks();
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.mocked(semantic.getSemanticStatus).mockResolvedValue(status);
  vi.mocked(semantic.getSemanticCache).mockResolvedValue(cache);
  vi.mocked(semantic.getSemanticCourses).mockResolvedValue([
    { course_id: "folder:7", name: "合成课程", path: "合成课程", video_count: 2, with_subtitles: 2 },
  ]);
  vi.mocked(semantic.buildSemanticIndex).mockResolvedValue({ job_id: "index-job" });
  vi.mocked(semantic.prepareSemanticModel).mockResolvedValue({ job_id: "prepare-job" });
  vi.mocked(semantic.setupSemanticSearch).mockResolvedValue({ job_id: "setup-job" });
  vi.mocked(semantic.cleanupSemanticSearch).mockResolvedValue({ job_id: "cleanup-job" });
  vi.mocked(getJob).mockResolvedValue({ id: "index-job", status: "running", progress: 30, message: "建立索引" });
  vi.mocked(cancelJob).mockResolvedValue({ id: "index-job", status: "interrupted" });
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
});
afterEach(async () => { await act(async () => root.unmount()); host.remove(); client.clear(); });
const settle = () => act(async () => { await new Promise(resolve => setTimeout(resolve, 30)); });
async function mount(active = true) {
  act(() => root.render(<QueryClientProvider client={client}><SemanticSettings active={active} /></QueryClientProvider>));
  await settle();
}
const button = (text: string) => [...host.querySelectorAll<HTMLButtonElement>("button")]
  .find(item => item.textContent?.includes(text))!;
const select = (label: string, value: string) => act(() => {
  const input = host.querySelector<HTMLSelectElement>(`[aria-label="${label}"]`)!;
  input.value = value; input.dispatchEvent(new Event("change", { bubbles: true }));
});
const chooseDevice = (name: "CPU" | "GPU") => act(() =>
  host.querySelector<HTMLInputElement>(`input[type="radio"][aria-label="${name}"]`)!.click());

it("does not load models or courses from an inactive settings section", async () => {
  await mount(false);
  expect(semantic.getSemanticStatus).not.toHaveBeenCalled();
  expect(semantic.getSemanticCourses).not.toHaveBeenCalled();
  expect(semantic.getSemanticCache).not.toHaveBeenCalled();
  expect(semantic.prepareSemanticModel).not.toHaveBeenCalled();
});

it("requires an explicit course, starts indexing and offers cancellation", async () => {
  await mount();
  expect(button("建立课程索引").disabled).toBe(true);
  act(() => {
    const select = host.querySelector<HTMLSelectElement>('[aria-label="建立索引的课程"]')!;
    select.value = "folder:7"; select.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await settle();
  act(() => button("建立课程索引").click());
  await settle(); await settle();
  expect(semantic.buildSemanticIndex).toHaveBeenCalledWith("folder:7");
  expect(host.querySelector("progress")?.value).toBe(30);
  expect(host.querySelector("progress")?.max).toBe(100);
  act(() => button("停止任务").click()); await settle();
  expect(cancelJob).toHaveBeenCalledWith("index-job");
});

it("defaults a fresh installation to CPU and blocks GPU when hardware is absent", async () => {
  vi.mocked(semantic.getSemanticStatus).mockResolvedValue({
    ...status, ready: false, weights_ready: false, device: "cpu", gpu_detected: false,
    gpu_unavailable_reason: "未检测到 NVIDIA 显卡",
    runtimes: { cpu: { ready: false }, cuda: { ready: false } },
  });
  await mount();
  expect(host.querySelector<HTMLInputElement>('[aria-label="CPU"]')?.checked).toBe(true);
  expect(host.querySelector<HTMLInputElement>('[aria-label="GPU"]')?.disabled).toBe(true);
  act(() => button("下载并准备 CPU").click()); await settle();
  expect(semantic.prepareSemanticModel).toHaveBeenCalledWith("cpu");
  expect(semantic.buildSemanticIndex).not.toHaveBeenCalled();
});

it("selects a device without downloading and reuses a prepared device through explicit switching", async () => {
  vi.mocked(semantic.getSemanticStatus).mockResolvedValue({
    ...status, runtimes: { cpu: { ready: true }, cuda: { ready: true } },
  });
  await mount();
  chooseDevice("CPU"); await settle();
  expect(semantic.prepareSemanticModel).not.toHaveBeenCalled();
  expect(button("切换到 CPU")).toBeDefined();
  act(() => button("切换到 CPU").click()); await settle();
  expect(semantic.prepareSemanticModel).toHaveBeenCalledWith("cpu");
});

it("prepares the selected device and indexes the chosen course in one setup job", async () => {
  await mount();
  chooseDevice("CPU");
  select("建立索引的课程", "folder:7"); await settle();
  act(() => button("准备并建立课程索引").click()); await settle();
  expect(semantic.setupSemanticSearch).toHaveBeenCalledWith("folder:7", "cpu");
  expect(semantic.buildSemanticIndex).not.toHaveBeenCalled();
  expect(semantic.prepareSemanticModel).not.toHaveBeenCalled();
});

it("follows a later MCP job after a previously viewed job has finished", async () => {
  vi.mocked(getJob).mockResolvedValueOnce({ id: "index-job", status: "completed" });
  await mount();
  select("建立索引的课程", "folder:7"); await settle();
  act(() => button("建立课程索引").click()); await settle(); await settle();
  vi.mocked(getJob).mockResolvedValue({ id: "mcp-setup", status: "running", progress: 45,
    message: "正在准备 CPU 环境" });
  await act(async () => {
    client.setQueryData(["semantic", "status", "folder:7"], {
      ...status, busy: true, active_job_id: "mcp-setup",
    });
  });
  await settle(); await settle();
  expect(getJob).toHaveBeenCalledWith("mcp-setup");
  expect(host.textContent).toContain("正在准备 CPU 环境");
  expect(host.querySelector("progress")?.value).toBe(45);
  await act(async () => {
    client.setQueryData(["jobs", "mcp-setup"], {
      id: "mcp-setup", status: "failed", error_message: "模型下载失败，请检查网络后重试",
    });
    client.setQueryData(["semantic", "status", "folder:7"], status);
  });
  await settle();
  expect(host.textContent).toContain("模型下载失败，请检查网络后重试");
  expect(host.querySelector("progress")).toBeNull();
});

it("checks GPU after detection and shows the remaining download estimate for each device", async () => {
  vi.mocked(semantic.getSemanticStatus).mockResolvedValue({
    ...status, ready: false, weights_ready: false, cache_present: false,
    runtimes: { cpu: { ready: false }, cuda: { ready: false } },
    download_estimates: { weights_bytes: 1024 ** 3, runtimes: { cpu: 256 * 1024 ** 2, cuda: 3 * 1024 ** 3 },
      total_bytes: { cpu: 1.25 * 1024 ** 3, cuda: 4 * 1024 ** 3 } },
  });
  await mount();
  expect(host.querySelector<HTMLInputElement>('[aria-label="GPU"]')?.checked).toBe(true);
  expect(host.textContent).toContain("预计还需下载约 4.00 GB");
  expect(host.textContent).toContain("模型权重：约 1.00 GB");
  expect(host.textContent).toContain("GPU 运行依赖：约 3.00 GB");
  expect(semantic.prepareSemanticModel).not.toHaveBeenCalled();
  chooseDevice("CPU"); await settle();
  expect(host.textContent).toContain("预计还需下载约 1.25 GB");
  expect(host.textContent).toContain("CPU 运行依赖：约 256.0 MB");
});

it("shows actual download bytes, speed and percentage after a manual prepare click", async () => {
  vi.mocked(semantic.getSemanticStatus).mockResolvedValue({
    ...status, ready: false, weights_ready: false,
    runtimes: { cpu: { ready: false }, cuda: { ready: false } },
  });
  vi.mocked(getJob).mockResolvedValue({ id: "prepare-job", status: "running",
    job_type: "semantic_prepare", stage: "preparing", progress: .4,
    preparation: { active: true, phase: "downloading", label: "下载检索权重", message: "下载权重",
      component: "embedding", current_file: "model.safetensors", phase_progress: .4,
      bytes_completed: 400 * 1024 ** 2, bytes_total: 1000 * 1024 ** 2,
      speed_bytes_per_second: 5 * 1024 ** 2, eta_seconds: 120, updated_at: Date.now() / 1000 } });
  await mount();
  act(() => button("下载并准备 GPU").click()); await settle(); await settle();
  expect(semantic.prepareSemanticModel).toHaveBeenCalledWith("cuda");
  expect(host.querySelector('[role="progressbar"]')?.getAttribute("aria-valuenow")).toBe("40");
  expect(host.textContent).toContain("400.0 MB / 1000.0 MB");
  expect(host.textContent).toContain("5.0 MB/s");
  expect(host.textContent).toContain("课程检索权重");
  expect(host.querySelector('[aria-label="GPU 环境缓存"]')?.textContent).toContain("正在准备");
  expect(host.querySelector('[aria-label="GPU"]')?.closest("fieldset")?.disabled).toBe(true);
});

it("uninstalls RAG through its own background job and refreshes the available cache state", async () => {
  vi.mocked(getJob).mockResolvedValue({ id: "cleanup-job", status: "running",
    job_type: "semantic_cleanup", stage: "cleaning", message: "清理检索环境" });
  await mount();
  act(() => button("卸载 RAG").click()); await settle(); await settle();
  expect(semantic.cleanupSemanticSearch).toHaveBeenCalledWith("all");
  expect(host.querySelector('[aria-label="RAG 清理进度"]')).not.toBeNull();
  expect(button("下载并准备 CPU")).toBeUndefined();
  expect(button("卸载 RAG").disabled).toBe(true);
  expect(button("卸载 RAG").textContent).toContain("AI 检索功能能力可能下降");
  expect(host.querySelector('[aria-label="清理共享模型权重"]')).toBeNull();
  vi.mocked(semantic.getSemanticStatus).mockResolvedValue({
    ...status, ready: false, weights_ready: false, cache_present: false,
    runtimes: { cpu: { ready: false }, cuda: { ready: false } },
  });
  vi.mocked(semantic.getSemanticCache).mockResolvedValue({
    weights: { present: false, ready: false, bytes: 0, files: 0 },
    runtimes: { cpu: { present: false, ready: false, bytes: 0, files: 0 },
      cuda: { present: false, ready: false, bytes: 0, files: 0 } },
  });
  await act(async () => {
    client.setQueryData(["jobs", "cleanup-job"], { id: "cleanup-job", status: "completed", message: "RAG 已卸载" });
  });
  await settle(); await settle();
  expect(host.querySelector('[aria-label="GPU 环境缓存"]')?.textContent).toContain("无");
  expect(host.querySelector('[aria-label="CPU 环境缓存"]')?.textContent).toContain("无");
  expect(button("卸载 RAG").disabled).toBe(true);
  expect(button("下载并准备 GPU").disabled).toBe(false);
});

it("keeps GPU processing while clearing CPU cache and preserves actual residual state on interruption", async () => {
  vi.mocked(semantic.getSemanticCache).mockResolvedValue({
    ...cache, runtimes: { ...cache.runtimes, cpu: { present: true, ready: false, bytes: 12 * 1024 ** 2, files: 40 } },
  });
  vi.mocked(getJob).mockResolvedValue({ id: "cleanup-job", status: "running",
    job_type: "semantic_cleanup", stage: "cleaning", message: "正在清理 CPU 环境" });
  await mount();
  expect(host.querySelector('[aria-label="GPU 环境缓存"]')?.textContent).toContain("已准备");
  expect(host.querySelector('[aria-label="CPU 环境缓存"]')?.textContent).toContain("12.0 MB");
  expect(host.querySelector('[aria-label="CPU 环境缓存"]')?.textContent).toContain("未准备");
  act(() => button("仅使用 GPU 处理").click());
  await settle(); await settle();
  expect(semantic.cleanupSemanticSearch).toHaveBeenCalledWith("cpu", "cuda");
  expect(host.textContent).toContain("正在清理 CPU 环境");
  expect(button("停止")).toBeUndefined();
  expect(host.textContent).not.toContain("暂停下载");
  vi.mocked(semantic.getSemanticCache).mockResolvedValue({
    ...cache, runtimes: { ...cache.runtimes, cpu: { present: true, ready: false, bytes: 5 * 1024 ** 2, files: 10 } },
  });
  await act(async () => {
    client.setQueryData(["jobs", "cleanup-job"], { id: "cleanup-job", status: "interrupted",
      job_type: "semantic_cleanup", message: "清理意外中断" });
  });
  await settle(); await settle();
  expect(host.querySelector('[aria-label="CPU 环境缓存"]')?.textContent).toContain("5.0 MB");
  expect(host.querySelector('[aria-label="CPU 环境缓存"]')?.textContent).toContain("有缓存");
  expect(host.querySelector('[aria-label="GPU 环境缓存"]')?.textContent).toContain("5.00 GB");
  expect(host.querySelector('[aria-label="GPU 环境缓存"]')?.textContent).toContain("已准备");
  expect(host.textContent).not.toContain("已卸载");
  expect(button("仅使用 GPU 处理").disabled).toBe(false);
});

it("offers no stop during cleanup and recovers remaining files after an unexpected interruption", async () => {
  vi.mocked(semantic.getSemanticStatus).mockResolvedValue({
    ...status, runtimes: { cpu: { ready: true }, cuda: { ready: true } },
  });
  vi.mocked(getJob).mockResolvedValue({ id: "cleanup-job", status: "running", job_type: "semantic_cleanup",
    stage: "cleaning", message: "正在清理 GPU 环境" });
  await mount();
  act(() => button("仅使用 CPU 处理").click());
  await settle(); await settle();
  expect(button("停止")).toBeUndefined();
  expect(button("暂停")).toBeUndefined();
  expect(cancelJob).not.toHaveBeenCalled();
  expect(host.textContent).not.toContain("已卸载");
  expect(host.querySelector('[aria-label="预计下载大小"]')).toBeNull();
  vi.mocked(semantic.getSemanticStatus).mockResolvedValue({
    ...status, ready: false, runtimes: { cpu: { ready: true }, cuda: { ready: false } },
  });
  vi.mocked(semantic.getSemanticCache).mockResolvedValue({
    ...cache, runtimes: { ...cache.runtimes, cuda: { present: true, ready: false, bytes: 1024 ** 3, files: 20 } },
  });
  await act(async () => {
    client.setQueryData(["jobs", "cleanup-job"], { id: "cleanup-job", status: "interrupted",
      job_type: "semantic_cleanup", message: "GPU 清理已停止，已删除文件不会恢复" });
  });
  await settle(); await settle();
  expect(host.querySelector('[aria-label="GPU 环境缓存"]')?.textContent).toContain("未准备 · 有缓存");
  expect(host.querySelector('[aria-label="GPU 环境缓存"]')?.textContent).toContain("1.00 GB");
  expect(button("仅使用 CPU 处理").disabled).toBe(false);
  expect(button("下载并准备 GPU").disabled).toBe(false);
});

it("requires the retained device to be prepared and disables an already satisfied GPU-only choice", async () => {
  await mount();
  expect(button("仅使用 CPU 处理").disabled).toBe(true);
  expect(host.textContent).toContain("未启用 CPU");
  expect(host.textContent).not.toContain("请先下载并准备");
  expect(button("仅使用 GPU 处理").disabled).toBe(true);
  expect(host.textContent).toContain("当前已仅使用 GPU");
  expect(semantic.cleanupSemanticSearch).not.toHaveBeenCalled();
});

it("switches to CPU and clears GPU assets regardless of the processing-device radio selection", async () => {
  vi.mocked(semantic.getSemanticStatus).mockResolvedValue({
    ...status, runtimes: { cpu: { ready: true }, cuda: { ready: true } },
  });
  await mount();
  expect(host.querySelector<HTMLInputElement>('[aria-label="GPU"]')?.checked).toBe(true);
  act(() => button("仅使用 CPU 处理").click()); await settle();
  expect(semantic.cleanupSemanticSearch).toHaveBeenCalledWith("cuda", "cpu");
  expect(semantic.prepareSemanticModel).not.toHaveBeenCalled();
});

it("recovers an explicit cleanup target after reopening the settings page", async () => {
  vi.mocked(semantic.getSemanticStatus).mockResolvedValue({ ...status, active_job_id: "external", busy: true });
  vi.mocked(getJob).mockResolvedValue({ id: "external", status: "running",
    job_type: "semantic_cleanup", stage: "cleaning", cleanup_target: "cpu" });
  await mount(); await settle();
  expect(host.textContent).toContain("正在清理 CPU 环境");
  expect(host.querySelector('[aria-label="CPU 环境缓存"]')?.textContent).toContain("正在清理");
  expect(host.querySelector('[aria-label="GPU 环境缓存"]')?.textContent).toContain("已准备");
  expect(host.querySelector('[aria-label="预计下载大小"]')).toBeNull();
  expect(button("停止")).toBeUndefined();
});

it("replaces finished download telemetry with actual indexing progress in a setup task", async () => {
  vi.mocked(getJob).mockResolvedValue({ id: "setup-job", status: "running",
    job_type: "semantic_setup", stage: "preparing", progress: .45 });
  await mount();
  chooseDevice("CPU"); select("建立索引的课程", "folder:7"); await settle();
  act(() => button("准备并建立课程索引").click()); await settle(); await settle();
  expect(host.querySelector('[aria-label="模型准备进度"]')).not.toBeNull();
  await act(async () => {
    client.setQueryData(["jobs", "setup-job"], { id: "setup-job", status: "running",
      job_type: "semantic_setup", stage: "indexing", progress: 70, message: "建立课程索引 · 片段 8/24" });
  });
  await settle();
  expect(host.querySelector('[aria-label="模型准备进度"]')).toBeNull();
  expect(host.querySelector("progress")?.value).toBe(70);
  expect(host.textContent).toContain("片段 8/24");
});

it("keeps the failed-read state recoverable", async () => {
  vi.mocked(semantic.getSemanticStatus).mockRejectedValue(new Error("offline"));
  await mount();
  expect(host.querySelector('[role="alert"]')?.textContent).toContain("状态读取失败");
  vi.mocked(semantic.getSemanticStatus).mockResolvedValue(status);
  act(() => button("重试").click()); await settle();
  expect(host.textContent).toContain("已准备");
});
