import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import * as api from "../api/cacheCleanup";
import { getJobStatus } from "../api/client";
import { CacheCleanupSection } from "./CacheCleanupSection";

vi.mock("../api/cacheCleanup", async importOriginal => ({
  ...await importOriginal<typeof import("../api/cacheCleanup")>(),
  getCleanupStatus: vi.fn(), previewCleanup: vi.fn(),
  saveCleanupPreferences: vi.fn(), startCleanup: vi.fn(),
}));
vi.mock("../api/client", async importOriginal => ({
  ...await importOriginal<typeof import("../api/client")>(),
  getJobStatus: vi.fn(), cancelJob: vi.fn(),
}));
const disposals: (() => void)[] = [];
afterEach(() => { for (const dispose of disposals.splice(0)) dispose(); vi.useRealTimers(); });
const preferences: api.CleanupPreferences = {
  enabled: true, interval_days: 7, retention_days: 30, preview_limit_mb: 512,
  categories: ["transcription", "previews", "logs", "updates"],
};
const inventory: api.CleanupInventory = { categories: [
  { id: "previews", label: "视频封面与搜索预览", bytes: 2048, eligible_bytes: 1024, automatic: true },
  { id: "ai", label: "AI 分块结果缓存", bytes: 1024, eligible_bytes: 512, automatic: false },
  { id: "backups", label: "旧更新回退备份", bytes: 4096, eligible_bytes: 2048, automatic: false },
] };
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.getCleanupStatus).mockResolvedValue({ preferences, last_run: null, busy: false });
  vi.mocked(api.previewCleanup).mockResolvedValue(inventory);
  vi.mocked(api.saveCleanupPreferences).mockImplementation(async value => ({ preferences: value, last_run: null }));
  vi.mocked(api.startCleanup).mockResolvedValue({ job_id: "clean-fixture" });
  vi.mocked(getJobStatus).mockResolvedValue({ id: "clean-fixture", status: "completed", message: "缓存清理已结束" });
});
async function waitFor(check: () => void) {
  let failure: unknown;
  for (let i = 0; i < 80; i++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 10)); });
    try { check(); return; } catch (error) { failure = error; }
  }
  throw failure;
}
function mount(active = true) {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const onJob = vi.fn();
  act(() => root.render(<QueryClientProvider client={client}><MemoryRouter
    future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
    <CacheCleanupSection active={active} onJob={onJob} />
  </MemoryRouter></QueryClientProvider>));
  disposals.push(() => { act(() => { root.unmount(); client.clear(); }); container.remove(); });
  const button = (text: string) => [...container.querySelectorAll<HTMLButtonElement>("button")]
    .find(item => item.textContent?.includes(text))!;
  return { container, button, onJob };
}

it("reads policy only while visible and does not scan disk until asked", async () => {
  const page = mount();
  await waitFor(() => expect(page.container.querySelector('[aria-label="自动清理频率"]')).not.toBeNull());
  expect(api.getCleanupStatus).toHaveBeenCalledTimes(1);
  expect(api.previewCleanup).not.toHaveBeenCalled();
  expect(page.button("清理所选").disabled).toBe(true);
});

it("keeps inactive settings from fetching or scanning", async () => {
  mount(false);
  await act(async () => { await Promise.resolve(); });
  expect(api.getCleanupStatus).not.toHaveBeenCalled();
  expect(api.previewCleanup).not.toHaveBeenCalled();
});

it("cleans the explicitly selected categories and leaves costly caches unchecked", async () => {
  const page = mount();
  await waitFor(() => expect(page.button("检查占用").disabled).toBe(false));
  act(() => page.button("检查占用").click());
  await waitFor(() => expect(page.container.querySelector('[aria-label="清理AI 分块结果缓存"]')).not.toBeNull());
  const ai = page.container.querySelector<HTMLInputElement>('[aria-label="清理AI 分块结果缓存"]')!;
  const backups = page.container.querySelector<HTMLInputElement>('[aria-label="清理旧更新回退备份"]')!;
  expect(ai.checked).toBe(false);
  expect(backups.checked).toBe(false);
  act(() => backups.click());
  expect(page.container.textContent).toContain("当前恢复所需备份及未知或失败备份保留");
  act(() => backups.click());
  act(() => ai.click());
  expect(page.container.textContent).toContain("可能重新请求 AI 服务并产生费用");
  act(() => page.button("清理所选").click());
  await waitFor(() => expect(api.startCleanup).toHaveBeenCalledWith(
    ["transcription", "previews", "logs", "updates", "ai"], "expired"));
  expect(page.onJob).toHaveBeenCalledWith("clean-fixture");
});

it("saves automatic preferences and keeps changes when saving fails", async () => {
  const page = mount();
  await waitFor(() => expect(page.container.querySelector(".cache-cleanup-toggle input")).not.toBeNull());
  act(() => (page.container.querySelector(".cache-cleanup-toggle input") as HTMLInputElement).click());
  vi.mocked(api.saveCleanupPreferences).mockRejectedValueOnce(new Error("保存失败，请重试"));
  act(() => page.button("保存清理设置").click());
  await waitFor(() => expect(page.container.textContent).toContain("保存失败，请重试"));
  expect(page.button("保存清理设置").disabled).toBe(false);
  act(() => page.button("保存清理设置").click());
  await waitFor(() => expect(page.container.textContent).toContain("自动清理设置已保存"));
  expect(vi.mocked(api.saveCleanupPreferences).mock.calls.at(-1)?.[0]).toEqual({ ...preferences, enabled: false });
});

it("blocks manual cleanup while the backend reports protected tasks", async () => {
  vi.mocked(api.getCleanupStatus).mockResolvedValue({ preferences, busy: true, last_run: null });
  const page = mount();
  await waitFor(() => expect(page.container.textContent).toContain("有运行或暂停中的任务"));
  expect(page.button("检查占用").disabled).toBe(true);
  expect(page.button("清理所选").disabled).toBe(true);
  expect(api.startCleanup).not.toHaveBeenCalled();
});

it("restores controls after an external protected task completes", async () => {
  vi.useFakeTimers();
  vi.mocked(api.getCleanupStatus)
    .mockResolvedValueOnce({ preferences, busy: true, last_run: null })
    .mockResolvedValue({ preferences, busy: false, last_run: null });
  const page = mount();
  await act(async () => { await vi.advanceTimersByTimeAsync(10); });
  expect(page.container.textContent).toContain("有运行或暂停中的任务");
  await act(async () => { await vi.advanceTimersByTimeAsync(3100); });
  expect(page.button("检查占用").disabled).toBe(false);
  expect(page.container.textContent).not.toContain("有运行或暂停中的任务");
});

it("shows inspection errors with a working retry", async () => {
  vi.mocked(api.previewCleanup).mockRejectedValueOnce(new Error("disk unavailable"));
  const page = mount();
  act(() => page.button("检查占用").click());
  await waitFor(() => expect(page.container.textContent).toContain("占用检查失败"));
  act(() => page.button("检查占用").click());
  await waitFor(() => expect(page.container.textContent).toContain("视频封面与搜索预览"));
});
