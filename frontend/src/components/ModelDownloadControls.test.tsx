import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import * as api from "../api/client";
import { ModelDownloadControls } from "./ModelDownloadControls";
import type { JobStatus, ModelDownloadState } from "../types";

vi.mock("../api/client");

const recovery: ModelDownloadState = {
  status: "paused", retained_bytes: 1500, completed_bytes: 1000,
  partial_bytes: 500, temporary_files: 1, can_cleanup: true, current_file: "model.pt",
};
const downloading: JobStatus = { id: "first", status: "running", preparation: {
  phase: "downloading", label: "下载模型权重", message: "model.pt", updated_at: Date.now() / 1000,
} };

async function waitFor(check: () => void) {
  for (let attempt = 0; ; attempt++) {
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 10)); });
    try { check(); return; } catch (error) { if (attempt > 100) throw error; }
  }
}

function mount() {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const element = document.createElement("div");
  document.body.append(element);
  const root = createRoot(element);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const onJob = vi.fn();
  const render = (job?: JobStatus, state: ModelDownloadState | null = recovery, busy = false) => act(() => root.render(
    <QueryClientProvider client={client}>
      <ModelDownloadControls model={{ id: "paraformer-large-cpu", download_state: state }}
        job={job} preparing={job?.status === "running"} busy={busy} onJob={onJob} />
    </QueryClientProvider>,
  ));
  const button = (text: string) => [...element.querySelectorAll("button")].find((button) => button.textContent?.includes(text))!;
  return { element, render, button, onJob, async dispose() {
    await act(async () => { root.unmount(); client.clear(); });
    element.remove();
  } };
}

it("pauses once and does not leave a resumed job stuck in the pausing state", async () => {
  vi.resetAllMocks();
  let finish!: () => void;
  vi.mocked(api.pauseAsrDownload).mockImplementation(() => new Promise<void>((resolve) => { finish = resolve; }));
  const page = mount();
  try {
    page.render(downloading);
    act(() => { page.button("暂停下载").click(); page.button("暂停下载").click(); });
    await waitFor(() => expect(api.pauseAsrDownload).toHaveBeenCalledTimes(1));
    expect(page.button("暂停").disabled).toBe(true);
    await act(async () => { finish(); });
    await waitFor(() => expect(page.element.textContent).toContain("正在暂停"));
    page.render({ ...downloading, id: "second" });
    expect(page.button("暂停下载").disabled).toBe(false);
  } finally { await page.dispose(); }
});

it("recovers retained files without auto-starting and submits continue explicitly", async () => {
  vi.resetAllMocks();
  vi.mocked(api.resumeAsrDownload).mockResolvedValue({ job_id: "continued" });
  const page = mount();
  try {
    page.render();
    expect(page.element.textContent).toContain("下载已暂停");
    expect(page.element.textContent).toContain("1.5 KB");
    expect(api.resumeAsrDownload).not.toHaveBeenCalled();
    act(() => page.button("继续下载").click());
    await waitFor(() => expect(page.onJob).toHaveBeenCalledWith("continued", "preparing"));
    expect(api.resumeAsrDownload).toHaveBeenCalledWith("paraformer-large-cpu");
  } finally { await page.dispose(); }
});

it("cleans only unfinished download using the dedicated endpoint and reports failure", async () => {
  vi.resetAllMocks();
  vi.mocked(api.cleanupAsrDownload).mockRejectedValueOnce(new Error("任务仍在运行"))
    .mockResolvedValueOnce({ job_id: "cleanup" });
  const page = mount();
  try {
    page.render(undefined, { ...recovery, status: "interrupted" });
    expect(page.element.textContent).toContain("发现未完成");
    act(() => page.button("清理未完成下载").click());
    await waitFor(() => expect(page.element.querySelector('[role="alert"]')?.textContent).toBe("任务仍在运行"));
    expect(api.clearAsrModel).not.toHaveBeenCalled();
    expect(page.element.textContent).toContain("已保留");
    act(() => page.button("清理未完成下载").click());
    await waitFor(() => expect(page.onJob).toHaveBeenCalledWith("cleanup", "cleaning_partial"));
  } finally { await page.dispose(); }
});

it("disables pause during environment installation and finalization", async () => {
  vi.resetAllMocks();
  const page = mount();
  try {
    for (const phase of ["installing", "finalizing"] as const) {
      page.render({ ...downloading, preparation: { ...downloading.preparation!, phase } });
      expect(page.button("暂停下载").disabled).toBe(true);
    }
    expect(api.pauseAsrDownload).not.toHaveBeenCalled();
  } finally { await page.dispose(); }
});

it("offers cleanup for leftover partial files without treating a ready model as unfinished", async () => {
  vi.resetAllMocks();
  const page = mount();
  try {
    page.render(undefined, { ...recovery, resumable: false });
    expect(page.element.textContent).toContain("模型已就绪");
    expect(page.button("继续下载")).toBeUndefined();
    expect(page.button("清理未完成下载").disabled).toBe(false);
  } finally { await page.dispose(); }
});
