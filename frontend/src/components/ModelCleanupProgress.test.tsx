import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";
import { cleanupSummary, ModelCleanupProgress } from "./ModelCleanupProgress";
import type { JobStatus } from "../types";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const cleanup: Array<() => void> = [];
afterEach(() => { cleanup.splice(0).forEach((fn) => fn()); vi.useRealTimers(); });

function job(): JobStatus {
  return { id: "cleanup", status: "running", cleanup: {
    phase: "deleting", scope: "Dolphin GPU 环境", message: "正在删除文件",
    current_path: "torch.dll", files_scanned: 100, directories_scanned: 10,
    files_deleted: 40, directories_deleted: 4, files_total: 100, directories_total: 10,
    bytes_deleted: 100 * 1024 ** 2, bytes_total: 250 * 1024 ** 2, phase_progress: .4,
    eta_seconds: 30, elapsed_seconds: 12, updated_at: Date.now() / 1000,
  } };
}

function mount(value?: JobStatus, connectionError = false) {
  const element = document.createElement("div");
  document.body.append(element);
  const root = createRoot(element);
  const render = (next = value) => act(() => root.render(<ModelCleanupProgress job={next} connectionError={connectionError} />));
  render();
  cleanup.push(() => { act(() => root.unmount()); element.remove(); });
  return { element, render };
}

it("shows actual file and directory counts, released bytes and numeric ETA", () => {
  const value = job(), { element } = mount(value);
  expect(element.querySelector('[role="progressbar"]')?.getAttribute("aria-valuenow")).toBe("40");
  expect(element.textContent).toContain("40 / 100 个文件");
  expect(element.textContent).toContain("4 / 10 个目录");
  expect(element.textContent).toContain("100.0 MB / 250.0 MB");
  expect(element.textContent).toContain("torch.dll");
  expect(element.textContent).toContain("还需约 30 秒");
  expect(cleanupSummary(value)).toContain("40/100");
});

it("does not invent a percentage while discovering the number of files", () => {
  const value = job();
  value.cleanup = { ...value.cleanup!, phase: "scanning", phase_progress: null,
    files_total: null, directories_total: null, eta_seconds: null };
  const { element } = mount(value);
  expect(element.querySelector('[role="progressbar"]')?.hasAttribute("aria-valuenow")).toBe(false);
  expect(element.textContent).toContain("已检查 100 个文件");
  expect(element.textContent).toContain("总数量尚在统计");
  expect(element.textContent).not.toContain("还需约");
});

it("withdraws stale ETA and explains a blocked system file operation", () => {
  vi.useFakeTimers();
  const { element } = mount(job());
  act(() => vi.advanceTimersByTime(10_000));
  expect(element.textContent).toContain("等待系统文件操作返回");
  expect(element.textContent).not.toContain("还需约");
});

it("preserves the last counts while explaining a progress connection failure", () => {
  const { element } = mount(job(), true);
  expect(element.textContent).toContain("40 / 100");
  expect(element.textContent).toContain("后台任务继续运行");
  expect(element.textContent).not.toContain("还需约");
});

it("supports historical tasks without telemetry and stops animating at completion", () => {
  const { element, render } = mount({ id: "old", status: "running", progress: .05 });
  expect(element.textContent).not.toContain("5%");
  expect(element.querySelector(".indeterminate")).not.toBeNull();
  render({ id: "old", status: "completed" });
  expect(element.textContent).toContain("清理完成");
  expect(element.textContent).not.toContain("已停止");
  expect(element.querySelector(".indeterminate")).toBeNull();
  expect(element.querySelector('[role="progressbar"]')?.getAttribute("aria-valuenow")).toBe("100");
});
