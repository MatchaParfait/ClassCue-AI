import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, useLocation } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import * as api from "../api/client";
import { StudyShareDialog } from "./StudyShareDialog";
import { StudyShareExportMenuItem } from "./StudyShareControls";
import type { StudySharePreview, Video } from "../types";

vi.mock("../api/client");
const plan: StudySharePreview = {
  package_hash: "a".repeat(64), library_id: "library-a", video_id: 7, video_name: "本地课程",
  shared_video_name: "分享课程", duration_ms: 60_000, subtitle_count: 83, marker_count: 12,
  analysis_runs: [{ analysis_mode: "medium_recall", name: "中召回", scheme_revision: 0 }],
  can_import: true, already_imported: false, block_reason: null,
};
let root: Root, container: HTMLDivElement, query: QueryClient;
const close = vi.fn();
function Location() { return <output>{useLocation().pathname}</output>; }
beforeEach(() => {
  vi.resetAllMocks();
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  query = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  vi.mocked(api.previewStudyShare).mockResolvedValue(plan);
  vi.mocked(api.importStudyShare).mockResolvedValue({ video: { id: 7 } as Video });
});
afterEach(() => {
  act(() => root.unmount());
  query.clear();
  container.remove();
  vi.restoreAllMocks();
});
async function mount(child = <StudyShareDialog libraryId="library-a" returnFocus={null} onClose={close} />) {
  await act(async () => root.render(<QueryClientProvider client={query}><MemoryRouter>
    {child}<Location />
  </MemoryRouter></QueryClientProvider>));
}
function button(name: string) {
  return [...document.querySelectorAll<HTMLButtonElement>("button")]
    .find(item => item.textContent === name || item.getAttribute("aria-label") === name)!;
}
async function click(name: string) { await act(async () => button(name).click()); }
async function select(file = new File(["fixture"], "课程.studyshare")) {
  const input = document.querySelector<HTMLInputElement>('input[type="file"]')!;
  Object.defineProperty(input, "files", { configurable: true, value: [file] });
  await act(async () => input.dispatchEvent(new Event("change", { bubbles: true })));
  return file;
}

it("previews without importing, then commits the same file and refreshes all relevant caches", async () => {
  const invalidations = vi.spyOn(query, "invalidateQueries");
  await mount();
  expect(document.activeElement).toBe(button("取消"));
  expect(button("导入并复习").disabled).toBe(true);
  const file = await select();
  expect(api.previewStudyShare).toHaveBeenCalledWith(file, expect.any(AbortSignal));
  expect(api.importStudyShare).not.toHaveBeenCalled();
  expect(document.body.textContent).toContain("83 条");
  expect(document.body.textContent).toContain("12 个");
  expect(document.body.textContent).toContain("分享课程");
  await click("导入并复习");
  expect(api.importStudyShare).toHaveBeenCalledWith(file, plan);
  expect(close).toHaveBeenCalledTimes(1);
  expect(container.querySelector("output")?.textContent).toBe("/review/7");
  for (const queryKey of [["videos"], ["library"], ["study", "subtitles", 7], ["settings", "recall-schemes"]])
    expect(invalidations).toHaveBeenCalledWith({ queryKey });
});

it("shows a blocking reason without allowing writes and can explicitly recheck", async () => {
  vi.mocked(api.previewStudyShare).mockResolvedValueOnce({ ...plan, can_import: false, block_reason: "此视频已有字幕" });
  await mount(); await select();
  expect(document.querySelector('[role="alert"]')?.textContent).toContain("已有字幕");
  expect(button("导入并复习").disabled).toBe(true);
  expect(api.importStudyShare).not.toHaveBeenCalled();
  await click("重新校验");
  expect(api.previewStudyShare).toHaveBeenCalledTimes(2);
  expect(button("导入并复习").disabled).toBe(false);
});

it("keeps file and error after a failed import, requires a fresh preview before retry", async () => {
  vi.mocked(api.importStudyShare).mockRejectedValueOnce(new Error("视频文件已变化"));
  await mount(); await select(); await click("导入并复习");
  expect(close).not.toHaveBeenCalled();
  expect(container.querySelector("output")?.textContent).toBe("/");
  expect(document.querySelector('[role="alert"]')?.textContent).toContain("视频文件已变化");
  expect(button("导入并复习").disabled).toBe(true);
  expect(document.body.textContent).toContain("课程.studyshare");
  await click("重新校验"); await click("导入并复习");
  expect(api.previewStudyShare).toHaveBeenCalledTimes(2);
  expect(api.importStudyShare).toHaveBeenCalledTimes(2);
});

it("rejects empty, oversized and wrong-library packages without importing", async () => {
  await mount();
  await select(new File([], "empty.studyshare"));
  expect(document.body.textContent).toContain("分享包为空");
  const huge = new File(["x"], "big.studyshare");
  Object.defineProperty(huge, "size", { value: 32 * 1024 * 1024 + 1 });
  await select(huge);
  expect(document.body.textContent).toContain("超过 32 MB");
  expect(api.previewStudyShare).not.toHaveBeenCalled();
  vi.mocked(api.previewStudyShare).mockResolvedValue({ ...plan, library_id: "other" });
  await select();
  expect(document.body.textContent).toContain("视频库已切换");
  expect(api.importStudyShare).not.toHaveBeenCalled();
});

it("blocks same-tick double submission, escape and closing until import completes", async () => {
  let resolve!: (value: { video: Video }) => void;
  vi.mocked(api.importStudyShare).mockReturnValue(new Promise(done => { resolve = done; }));
  await mount(); await select();
  await act(async () => { button("导入并复习").click(); button("导入并复习").click(); });
  expect(api.importStudyShare).toHaveBeenCalledTimes(1);
  expect(button("取消").disabled).toBe(true);
  expect(button("关闭分享包").disabled).toBe(true);
  await act(async () => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
  expect(close).not.toHaveBeenCalled();
  await act(async () => resolve({ video: { id: 7 } as Video }));
  expect(close).toHaveBeenCalledTimes(1);
});

it("allows closing during read-only validation and aborts the preview on unmount", async () => {
  vi.mocked(api.previewStudyShare).mockReturnValue(new Promise(() => {}));
  await mount(); await select();
  const signal = vi.mocked(api.previewStudyShare).mock.calls[0][1]!;
  expect(signal.aborted).toBe(false);
  expect(button("取消").disabled).toBe(false);
  await click("取消");
  expect(close).toHaveBeenCalledTimes(1);
  await mount(<span>closed</span>);
  expect(signal.aborted).toBe(true);
  expect(api.importStudyShare).not.toHaveBeenCalled();
});

it("treats an already imported package as an idempotent open action", async () => {
  vi.mocked(api.previewStudyShare).mockResolvedValue({ ...plan, already_imported: true });
  await mount(); await select();
  expect(document.body.textContent).toContain("不会重复添加或覆盖");
  await click("打开复习");
  expect(api.importStudyShare).toHaveBeenCalledTimes(1);
  expect(container.querySelector("output")?.textContent).toBe("/review/7");
});

it("disables exporting incomplete or occupied video and allows an error retry", async () => {
  const video = { id: 7, status: "uploaded", display_name: "课堂" } as Video;
  await mount(<StudyShareExportMenuItem video={video} />);
  expect(button("导出分享包").disabled).toBe(true);
  await mount(<StudyShareExportMenuItem video={{ ...video, status: "review_ready" }} blockReason="任务占用" />);
  expect(button("导出分享包").disabled).toBe(true);
  vi.mocked(api.downloadStudyShare).mockRejectedValueOnce(new Error("校验失败")).mockResolvedValueOnce();
  await mount(<StudyShareExportMenuItem video={{ ...video, status: "review_ready" }} />);
  await click("导出分享包");
  expect(document.querySelector('[role="alert"]')?.textContent).toBe("校验失败");
  await click("重试导出分享包");
  expect(api.downloadStudyShare).toHaveBeenCalledTimes(2);
  expect(document.body.textContent).toContain("分享包已生成");
});
