import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { CodexClipDialog, CodexVideoClip, parseClipTime } from "./CodexVideoClip";
import * as api from "../api/codexBridge";

const desktop = vi.hoisted(() => ({ enabled: true }));
vi.mock("../hooks/useDesktopAPI", () => ({ useDesktopAPI: () => desktop.enabled ? {} : null }));
vi.mock("../api/codexBridge", () => ({
  previewCodexClip: vi.fn(), prepareCodexClip: vi.fn(), getCodexClipDiscussions: vi.fn(),
  openCodexDiscussion: vi.fn(), attachCodexDiscussion: vi.fn(),
}));
const discussion: api.CodexDiscussion = {
  request_id: "r", question_id: "clip-q", content_version: 1, thread_id: "t", status: "ready",
  source_path: "source", course_path: "course", context_file: "context.json", title: "合成片段",
  native_transfer: { status: "pasted" },
};
const preview: api.CodexClipPreview = {
  video_id: 9, start_ms: 60_000, end_ms: 120_000, video_title: "合成课堂", fingerprint: "a".repeat(64),
  subtitle_srt: "1\n00:01:00,000 --> 00:01:30,000\n教师合成原话",
  subtitles: [{ id: 1, start_ms: 60_000, end_ms: 90_000, text: "教师合成原话" }],
  images: [1, 2].map(id => ({ slide_id: id, image_hash: `hash-${id}`, image_url: `/synthetic-${id}.png`,
    time_instances: [{ visible_from_ms: 60_000, visible_until_ms: 120_000, representative_ms: 70_000 }] })),
};
let root: Root, host: HTMLDivElement, cache: QueryClient;
const close = vi.fn();
beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.clearAllMocks(); desktop.enabled = true;
  vi.mocked(api.previewCodexClip).mockResolvedValue(preview);
  vi.mocked(api.prepareCodexClip).mockResolvedValue(discussion);
  vi.mocked(api.getCodexClipDiscussions).mockResolvedValue([]);
  vi.mocked(api.openCodexDiscussion).mockResolvedValue(discussion);
  vi.mocked(api.attachCodexDiscussion).mockResolvedValue(discussion);
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
  cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
});
afterEach(async () => {
  await act(async () => root.unmount()); host.remove(); cache.clear();
});
const props = { videoId: 9, libraryId: "synthetic", title: "合成课堂", durationMs: 300_000, getCurrentTimeMs: () => 150_000 };
async function mount(child = <CodexClipDialog {...props} positionMs={120_000} onClose={close} />) {
  await act(async () => root.render(<QueryClientProvider client={cache}>
    <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>{child}</MemoryRouter>
  </QueryClientProvider>));
}
const button = (label: string) => [...document.querySelectorAll<HTMLButtonElement>("button")].find(
  item => item.textContent?.trim() === label || item.getAttribute("aria-label") === label)!;
async function click(label: string) {
  await act(async () => button(label).click());
}
async function input(label: string, value: string) {
  const field = document.querySelector<HTMLInputElement>(`input[aria-label="${label}"]`)!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(field, value);
    field.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
it("parses original video times without accepting malformed or negative values", () => {
  expect(parseClipTime("01:02:03.125")).toBe(3_723_125);
  for (const value of ["", "-01:00:00", "00:60:00", "NaN", "3", "00:00:60"]) expect(parseClipTime(value)).toBeNull();
});
it("previews the chosen range, removes unneeded images and prepares an editable native draft", async () => {
  await mount();
  await click("预览片段");
  expect(api.previewCodexClip).toHaveBeenCalledWith({ video_id: 9, start_ms: 60_000, end_ms: 120_000 });
  expect(document.body.textContent).toContain("教师合成原话");
  const checkboxes = document.querySelectorAll<HTMLInputElement>('input[type="checkbox"]');
  await act(async () => checkboxes[1].click());
  await click("在 Codex 中准备草稿");
  expect(api.prepareCodexClip).toHaveBeenCalledWith({
    video_id: 9, start_ms: 60_000, end_ms: 120_000, fingerprint: preview.fingerprint,
    slide_ids: [1],
  });
  expect(document.body.textContent).toContain("编辑后自行发送");
  expect(button("已准备草稿").disabled).toBe(true);
  await click("打开原讨论");
  expect(api.openCodexDiscussion).toHaveBeenCalledWith("r");
  expect(api.prepareCodexClip).toHaveBeenCalledTimes(1);
});
it("time edits invalidate the preview and reject reversed or out of duration ranges", async () => {
  await mount(); await click("预览片段");
  await input("结束时间", "00:00:20");
  expect(document.querySelector(".codex-clip-subtitles")).toBeNull();
  expect(button("预览片段").disabled).toBe(true);
  expect(button("在 Codex 中准备草稿").disabled).toBe(true);
  await input("结束时间", "00:06:00");
  expect(button("预览片段").disabled).toBe(true);
  await click("将当前播放位置设为结束时间");
  expect((document.querySelector('input[aria-label="结束时间"]') as HTMLInputElement).value).toBe("00:02:30");
  expect(button("预览片段").disabled).toBe(false);
});
it("supports subtitle-only excerpts and shows actionable failures without a success claim", async () => {
  vi.mocked(api.previewCodexClip).mockResolvedValue({ ...preview, images: [] });
  vi.mocked(api.prepareCodexClip).mockRejectedValue(new Error("字幕或 PPT 已更新，请重新预览"));
  await mount(); await click("预览片段");
  expect(document.body.textContent).toContain("没有精选 PPT");
  await click("在 Codex 中准备草稿");
  expect(document.body.textContent).toContain("请重新预览");
  expect(document.querySelector(".codex-clip-result")).toBeNull();
});
it("unknown paste outcomes require explicit retry on the original discussion", async () => {
  vi.mocked(api.prepareCodexClip).mockResolvedValue({ ...discussion,
    native_transfer: { status: "unknown", error: "先检查 Codex 输入框" } });
  await mount(); await click("预览片段"); await click("在 Codex 中准备草稿");
  expect(button("在 Codex 中准备草稿").disabled).toBe(true);
  await click("检查草稿后重新追加片段");
  expect(api.attachCodexDiscussion).toHaveBeenCalledWith("r");
  expect(api.prepareCodexClip).toHaveBeenCalledTimes(1);
});
it("does not show the desktop entry or fetch history in a browser", async () => {
  desktop.enabled = false;
  await mount(<CodexVideoClip {...props} />);
  expect(host.textContent).toBe("");
  expect(api.getCodexClipDiscussions).not.toHaveBeenCalled();
});
it("locks duplicate requests and keeps Escape from closing during preparation", async () => {
  let resolve: (value: api.CodexDiscussion) => void = () => {};
  vi.mocked(api.prepareCodexClip).mockReturnValue(new Promise(done => { resolve = done; }));
  await mount(); await click("预览片段"); await click("在 Codex 中准备草稿");
  expect(button("正在准备草稿…").disabled).toBe(true);
  await act(async () => (document.querySelector('a[href="/settings?section=codex"]') as HTMLAnchorElement).click());
  expect(close).not.toHaveBeenCalled();
  await act(async () => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
  expect(close).not.toHaveBeenCalled();
  await act(async () => resolve(discussion));
  await act(async () => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
  expect(close).toHaveBeenCalledTimes(1);
});

it("opening a historical discussion does not disable preparation of the current excerpt", async () => {
  vi.mocked(api.getCodexClipDiscussions).mockResolvedValue([{ ...discussion, start_ms: 0, end_ms: 30_000 }]);
  vi.mocked(api.openCodexDiscussion).mockResolvedValue({ ...discussion, start_ms: 0, end_ms: 30_000 });
  await mount(); await click("预览片段");
  await click("打开片段讨论 1：0:00 – 0:30");
  expect(document.body.textContent).toContain("历史片段：0:00 – 0:30");
  expect(button("在 Codex 中准备草稿").disabled).toBe(false);
  await click("在 Codex 中准备草稿");
  expect(api.prepareCodexClip).toHaveBeenCalledWith(expect.objectContaining({ start_ms: 60_000, end_ms: 120_000 }));
});

it("offers an explicit independent discussion after preparation fails with an uncertain thread outcome", async () => {
  vi.mocked(api.prepareCodexClip).mockRejectedValueOnce(new Error("上次操作回执未知，请明确新建讨论"));
  await mount(); await click("预览片段"); await click("在 Codex 中准备草稿");
  expect(button("新建独立讨论").disabled).toBe(false);
  await click("新建独立讨论");
  expect(api.prepareCodexClip).toHaveBeenLastCalledWith(expect.objectContaining({ new_discussion: true }));
});

it("can explicitly reattach a previously pasted excerpt after the user removed its draft", async () => {
  await mount(); await click("预览片段"); await click("在 Codex 中准备草稿");
  await click("检查草稿后重新追加片段");
  expect(api.attachCodexDiscussion).toHaveBeenCalledWith("r");
  expect(api.prepareCodexClip).toHaveBeenCalledTimes(1);
});

it("explains failed image loading and allows continuing after deselecting the failed image", async () => {
  await mount(); await click("预览片段");
  await act(async () => document.querySelector("img")!.dispatchEvent(new Event("error")));
  expect(document.body.textContent).toContain("所选图片读取失败");
  expect(button("在 Codex 中准备草稿").disabled).toBe(true);
  await act(async () => document.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click());
  expect(button("在 Codex 中准备草稿").disabled).toBe(false);
  await click("在 Codex 中准备草稿");
  expect(api.prepareCodexClip).toHaveBeenCalledWith(expect.objectContaining({ slide_ids: [2] }));
});

it("labels a slow preview as reading and discards stale preview content if reloading fails", async () => {
  await mount(); await click("预览片段");
  let reject: (reason: Error) => void = () => {};
  vi.mocked(api.previewCodexClip).mockReturnValueOnce(new Promise((_, fail) => { reject = fail; }));
  await click("预览片段");
  expect(button("正在读取片段…").disabled).toBe(true);
  expect(document.querySelector(".codex-clip-subtitles")).toBeNull();
  expect(document.querySelector('[role="dialog"]')?.getAttribute("aria-busy")).toBe("true");
  await act(async () => reject(new Error("读取失败")));
  expect(button("在 Codex 中准备草稿").disabled).toBe(true);
  expect(document.body.textContent).toContain("读取失败");
});

it("retries failed history loading and distinguishes its material counts", async () => {
  vi.mocked(api.getCodexClipDiscussions).mockRejectedValueOnce(new Error("临时读取失败"))
    .mockResolvedValue([{ ...discussion, start_ms: 0, end_ms: 30_000,
      image_count: 2, created_at: "2026-11-07 10:00:00" }]);
  await mount();
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)); });
  expect(document.body.textContent).toContain("讨论记录读取失败");
  await click("重试读取记录");
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)); });
  expect(api.getCodexClipDiscussions).toHaveBeenCalledTimes(2);
  expect(button("打开片段讨论 1：0:00 – 0:30")).toBeDefined();
  expect(document.querySelector(".codex-clip-history")?.textContent).toContain("2 张 PPT");
  expect(document.querySelector(".codex-clip-history")?.textContent).not.toContain("Invalid Date");
});
