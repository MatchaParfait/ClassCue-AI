import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";
import { hasActivePreparation, ModelPreparationProgress, preparationSummary } from "./ModelPreparationProgress";
import type { JobStatus } from "../types";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const cleanups: Array<() => void> = [];
afterEach(() => {
  cleanups.splice(0).forEach((cleanup) => cleanup());
  vi.useRealTimers();
});
function mount(job?: JobStatus, connectionError = false) {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const render = (next = job, error = connectionError) =>
    act(() => root.render(<ModelPreparationProgress job={next} connectionError={error} />));
  render();
  cleanups.push(() => { act(() => root.unmount()); container.remove(); });
  return { container, render };
}
function download(): JobStatus {
  return { id: "model-job", status: "running", preparation: {
    phase: "downloading", label: "下载模型权重", message: "正在下载",
    current_file: "model-00001-of-00002.safetensors", component: "recognizer", source: "ModelScope",
    bytes_completed: 400 * 1024 ** 2, bytes_total: 1000 * 1024 ** 2,
    phase_progress: .4, speed_bytes_per_second: 5 * 1024 ** 2, eta_seconds: 90,
    updated_at: Date.now() / 1000,
  } };
}
it("shows the file, byte progress, transfer rate and an explicitly scoped ETA", () => {
  const { container } = mount(download());
  expect(container.querySelector('[role="progressbar"]')?.getAttribute("aria-valuenow")).toBe("40");
  expect(container.textContent).toContain("model-00001-of-00002.safetensors");
  expect(container.textContent).toContain("400.0 MB / 1000.0 MB");
  expect(container.textContent).toContain("5.0 MB/s");
  expect(container.textContent).toContain("权重下载预计还需约 1 分 30 秒");
});
it("clears stale rates and estimates instead of continuing a false countdown", () => {
  vi.useFakeTimers();
  const { container } = mount(download());
  act(() => vi.advanceTimersByTime(10_000));
  expect(container.textContent).toContain("等待后台更新");
  expect(container.textContent).not.toContain("MB/s");
  expect(container.textContent).not.toContain("预计还需");
});
it("stops showing rate and ETA while saving pause records", () => {
  const { container } = mount({ ...download(), stage: "pausing" });
  expect(container.textContent).toContain("保存续传记录");
  expect(container.textContent).not.toContain("MB/s");
  expect(container.textContent).not.toContain("预计还需");
});
it("does not turn dependency step counts into an invented time estimate", () => {
  const job = download();
  job.preparation = { phase: "installing", label: "安装运行环境", message: "下载并安装 PyTorch",
    step: 2, steps: 4, phase_progress: .25, current_package: "torch", elapsed_seconds: 10,
    updated_at: Date.now() / 1000, eta_seconds: null };
  const { container } = mount(job);
  expect(container.textContent).toContain("步骤 2/4");
  expect(container.textContent).toContain("当前依赖：torch");
  expect(container.textContent).toContain("本步骤已用时");
  expect(container.textContent).toContain("暂时无法可靠估算");
});
it("switches from transfer to verification without leaving the transfer ETA visible", () => {
  const job = download();
  const { container, render } = mount(job);
  render({ ...job, preparation: { phase: "verifying", label: "校验模型文件", message: "正在校验",
    current_file: "model.pt", phase_progress: .5, checked_bytes: 512, check_total_bytes: 1024,
    updated_at: Date.now() / 1000 } });
  expect(container.textContent).toContain("已校验 512 B / 1.0 KB");
  expect(container.textContent).not.toContain("预计还需");
  expect(container.textContent).not.toContain("MB/s");
});
it("keeps the last phase visible and explains polling failures", () => {
  const { container } = mount(download(), true);
  expect(container.textContent).toContain("后台任务继续运行");
  expect(container.textContent).toContain("model-00001");
  expect(container.textContent).not.toContain("预计还需");
});
it("supports indeterminate startup and removes running indicators at completion", () => {
  const { container, render } = mount();
  expect(container.querySelector('[role="progressbar"]')?.hasAttribute("aria-valuenow")).toBe(false);
  render({ id: "model-job", status: "completed", progress: 1 });
  expect(container.querySelector('[role="progressbar"]')).toBeNull();
});
it("keeps preparation telemetry out of later transcription and uses phase percentages in the topbar", () => {
  const job = download();
  job.preparation!.active = true;
  expect(hasActivePreparation(job)).toBe(true);
  expect(preparationSummary(job)).toBe("下载模型权重 · 40%");
  expect(hasActivePreparation({ ...job, preparation: { ...job.preparation!, active: false } })).toBe(false);
  expect(hasActivePreparation({ ...job, status: "completed" })).toBe(false);
});
