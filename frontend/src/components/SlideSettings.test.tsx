import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import * as api from "../api/slides";
import { cancelJob, getJob } from "../api/client";
import { SlideSettings } from "./SlideSettings";

vi.mock("../api/slides", () => ({
  getSlideResources: vi.fn(), getSlideSelection: vi.fn(), saveSlideSelection: vi.fn(),
  prepareSlideAcceleration: vi.fn(), clearSlideResources: vi.fn(),
}));
vi.mock("../api/client", () => ({ getJob: vi.fn(), cancelJob: vi.fn() }));

const resources: api.SlideResources = {
  ready: false, runtime_ready: false, gpu_detected: true, gpu_prepare_available: true,
  gpu_name: "Synthetic GPU", gpu_unavailable_reason: "GPU 环境尚未准备。", message: "",
  runtime_size_bytes: 1024 ** 3, download_cache_size_bytes: 512 * 1024 ** 2,
  busy: false, active_job_id: null, active_job_kind: null,
};
let root: Root, host: HTMLDivElement, client: QueryClient;
const onJob = vi.fn();
const settle = () => act(async () => { await new Promise(resolve => setTimeout(resolve, 40)); });
const button = (name: string) => [...host.querySelectorAll<HTMLButtonElement>("button")]
  .find(item => item.textContent === name || item.getAttribute("aria-label") === name)!;
const device = (value: api.SlideBackend) =>
  host.querySelector<HTMLInputElement>(`input[name="ppt-default-device"][value="${value}"]`)!;
const chooseDevice = (value: api.SlideBackend) => act(() => device(value).click());

beforeEach(() => {
  vi.resetAllMocks();
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.mocked(api.getSlideResources).mockResolvedValue(resources);
  vi.mocked(api.getSlideSelection).mockResolvedValue({ backend: "cpu" });
  vi.mocked(api.saveSlideSelection).mockImplementation(async backend => ({ backend }));
  vi.mocked(api.prepareSlideAcceleration).mockResolvedValue({ job_id: "prepare" });
  vi.mocked(api.clearSlideResources).mockResolvedValue({ job_id: "clear", job_type: "slide_cache_cleanup" });
  vi.mocked(getJob).mockResolvedValue({ id: "prepare", status: "running", job_type: "slide_prepare",
    message: "正在安装 PyTorch", preparation: { phase: "installing", label: "安装运行环境",
      message: "正在安装", step: 1, steps: 2, phase_progress: .5, elapsed_seconds: 24, updated_at: Date.now() / 1000 } });
  vi.mocked(cancelJob).mockResolvedValue({ id: "prepare", status: "interrupted" });
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
});
afterEach(async () => { await act(async () => root.unmount()); host.remove(); client.clear(); });
async function mount(active = true) {
  act(() => root.render(<QueryClientProvider client={client}><SlideSettings active={active} onJob={onJob} /></QueryClientProvider>));
  await settle();
}

it("loads only when the PPT settings are active", async () => {
  await mount(false);
  expect(api.getSlideResources).not.toHaveBeenCalled();
  expect(api.getSlideSelection).not.toHaveBeenCalled();
});

it("shows only GPU resources and caches while keeping the CPU device option", async () => {
  await mount();
  expect(host.querySelector('[aria-label="PPT GPU 环境占用"]')?.textContent).toBe("1.00 GB");
  expect(host.querySelector('[aria-label="PPT 下载缓存占用"]')?.textContent).toBe("512.0 MB");
  expect([...host.querySelectorAll(".ppt-resource-name strong")].map(element => element.textContent))
    .toEqual(["GPU（CUDA）", "PPT 下载与计算缓存"]);
  expect(host.textContent).not.toContain("无额外占用");
  expect(host.textContent).toContain("安装成功后自动清理安装包");
  expect(host.querySelectorAll('input[type="radio"]')).toHaveLength(3);
  expect(host.querySelector("select")).toBeNull();
  expect(device("cpu").checked).toBe(true);
  expect(api.prepareSlideAcceleration).not.toHaveBeenCalled();
  expect(api.clearSlideResources).not.toHaveBeenCalled();
});

it("persists device selection independently of preparation", async () => {
  vi.mocked(api.getSlideResources).mockResolvedValue({ ...resources, ready: true });
  await mount();
  chooseDevice("cuda");
  expect(device("cuda").checked).toBe(true);
  expect(device("cpu").disabled).toBe(true);
  await settle();
  expect(api.saveSlideSelection).toHaveBeenCalledWith("cuda", expect.anything());
  expect(device("cuda").checked).toBe(true);
  expect(device("cpu").checked).toBe(false);
  expect(host.textContent).toContain("默认设备已保存");
  expect(button("清理 PPT GPU 环境及缓存").disabled).toBe(true);
  expect(button("清理 PPT 下载缓存").disabled).toBe(false);
  expect(api.prepareSlideAcceleration).not.toHaveBeenCalled();
});

it("keeps the previous saved device if persistence fails", async () => {
  vi.mocked(api.saveSlideSelection).mockRejectedValue(new Error("保存失败"));
  await mount(); chooseDevice("auto"); await settle();
  expect(device("cpu").checked).toBe(true);
  expect(device("auto").checked).toBe(false);
  expect(host.textContent).toContain("保存失败");
});

it("locks GPU selection and installation without hardware but can remove old GPU files", async () => {
  vi.mocked(api.getSlideResources).mockResolvedValue({ ...resources, gpu_detected: false,
    gpu_prepare_available: false, gpu_name: null, gpu_unavailable_reason: "未检测到 NVIDIA 显卡。" });
  await mount();
  expect(device("cuda").disabled).toBe(true);
  chooseDevice("cuda");
  expect(api.saveSlideSelection).not.toHaveBeenCalled();
  expect(button("准备 GPU 环境").disabled).toBe(true);
  expect(button("清理 PPT GPU 环境及缓存").disabled).toBe(false);
});

it("starts a cancellable installation with shared preparation telemetry", async () => {
  await mount();
  act(() => button("准备 GPU 环境").click()); await settle(); await settle();
  expect(host.querySelector('[aria-label="PPT GPU 安装进度"]')).not.toBeNull();
  expect(host.textContent).toContain("步骤 1/2");
  expect(host.textContent).not.toContain("本步骤已用时");
  expect(host.textContent).not.toContain("依赖安装耗时取决于");
  expect(host.querySelector('[role="progressbar"]')?.getAttribute("aria-valuenow")).toBe("50");
  expect(onJob).toHaveBeenCalledWith("prepare");
  expect(button("清理 PPT GPU 环境及缓存").disabled).toBe(true);
  expect([...host.querySelectorAll<HTMLInputElement>('input[type="radio"]')].every(input => input.disabled)).toBe(true);
  act(() => button("停止准备").click()); await settle();
  expect(cancelJob).toHaveBeenCalledWith("prepare");
});

it("recovers an installation that was started before the settings were opened", async () => {
  vi.mocked(api.getSlideResources).mockResolvedValue({ ...resources, busy: true,
    active_job_id: "external", active_job_kind: "slide_prepare" });
  await mount(); await settle();
  expect(getJob).toHaveBeenCalledWith("external");
  expect(host.querySelector('[aria-label="PPT GPU 安装进度"]')).not.toBeNull();
  expect(button("停止准备").disabled).toBe(false);
  vi.mocked(api.getSlideResources).mockResolvedValue(resources);
  act(() => button("停止准备").click()); await settle(); await settle();
  expect(host.textContent).toContain("PPT 资源任务已停止");
  expect(getJob).toHaveBeenCalledWith("external");
});

it("shows automatic installer cleanup within the preparation task", async () => {
  vi.mocked(api.getSlideResources).mockResolvedValue({ ...resources, busy: true,
    active_job_id: "prepare", active_job_kind: "slide_prepare" });
  vi.mocked(getJob).mockResolvedValue({ id: "prepare", status: "running", job_type: "slide_prepare",
    stage: "cleaning", cleanup: { phase: "deleting", scope: "PPT 安装包", message: "正在删除文件",
      phase_progress: .5, files_scanned: 2, directories_scanned: 1, files_deleted: 1,
      directories_deleted: 0, files_total: 2, directories_total: 1, bytes_deleted: 10, bytes_total: 20,
      eta_seconds: null, elapsed_seconds: 1, updated_at: Date.now() / 1000 } });
  await mount(); await settle();
  expect(host.querySelector('[aria-label="PPT 资源清理进度"]')).not.toBeNull();
  expect(host.querySelector('[aria-label="PPT GPU 安装进度"]')).toBeNull();
  expect(host.textContent).toContain("1 / 2 个文件");
  expect(button("停止清理").disabled).toBe(false);
});

it.each(["downloads", "all"] as const)("cleans only the requested %s scope with real file counters", async scope => {
  vi.mocked(getJob).mockResolvedValue({ id: "clear", status: "running", job_type: "slide_cache_cleanup",
    cleanup: { phase: "deleting", scope: "PPT 资源", message: "正在删除文件",
      phase_progress: .5, files_scanned: 4, directories_scanned: 1, files_deleted: 2,
      directories_deleted: 0, files_total: 4, directories_total: 1,
      bytes_deleted: 10, bytes_total: 20, eta_seconds: null, elapsed_seconds: 1,
      updated_at: Date.now() / 1000 } });
  await mount();
  act(() => button(scope === "downloads" ? "清理 PPT 下载缓存" : "清理 PPT GPU 环境及缓存").click());
  await settle(); await settle();
  expect(api.clearSlideResources).toHaveBeenCalledWith(scope);
  expect(host.querySelector('[aria-label="PPT 资源清理进度"]')).not.toBeNull();
  expect(host.textContent).toContain("2 / 4 个文件");
  expect(button("停止清理").disabled).toBe(false);
});

it("keeps cancellation available if progress reads fail", async () => {
  vi.mocked(getJob).mockRejectedValue(new Error("unavailable"));
  await mount(); act(() => button("准备 GPU 环境").click()); await settle(); await settle();
  expect(host.textContent).toContain("任务进度读取失败");
  expect(button("停止准备").disabled).toBe(false);
  expect(button("清理 PPT 下载缓存").disabled).toBe(true);
});

it("disables deletion when occupancy cannot be read or other tasks are running", async () => {
  vi.mocked(api.getSlideResources).mockRejectedValue(new Error("unavailable"));
  await mount();
  expect(button("清理 PPT 下载缓存").disabled).toBe(true);
  expect(button("清理 PPT GPU 环境及缓存").disabled).toBe(true);
  vi.mocked(api.getSlideResources).mockResolvedValue({ ...resources, busy: true });
  act(() => button("重试").click()); await settle();
  expect(button("准备 GPU 环境").disabled).toBe(true);
  expect(host.textContent).toContain("后台有任务正在运行");
});
