import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { CodexClipDialog, CodexVideoClip } from "./CodexVideoClip";
import type { ClipRange } from "./ClipRangeSelector";
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
async function mount(child = <CodexClipDialog {...props} range={{ startMs: 60_000, endMs: 120_000 }} onClose={close} />) {
  await act(async () => root.render(<QueryClientProvider client={cache}>
    <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>{child}</MemoryRouter>
  </QueryClientProvider>));
}
const button = (label: string) => [...document.querySelectorAll<HTMLButtonElement>("button")].find(
  item => item.textContent?.trim() === label || item.getAttribute("aria-label") === label)!;
async function click(label: string) {
  await act(async () => button(label).click());
}
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
it.each([
  { startMs: 60_000, endMs: 20_000 }, { startMs: 60_000, endMs: 360_000 },
  { startMs: -1, endMs: 20_000 }, { startMs: NaN, endMs: 20_000 },
  { startMs: 1000.5, endMs: 20_000 }, { startMs: 1000, endMs: 20_000.5 },
])("rejects an invalid selected range before reading materials: %o", async range => {
  await mount(<CodexClipDialog {...props} range={range} onClose={close} />);
  expect(button("预览片段").disabled).toBe(true);
  expect(button("在 Codex 中准备草稿").disabled).toBe(true);
  expect(api.previewCodexClip).not.toHaveBeenCalled();
});
function ClipTools({ now = 120_000, durationMs = props.durationMs }: { now?: number; durationMs?: number }) {
  const [range, setRange] = useState<ClipRange | null>(null);
  return <>
    <CodexVideoClip {...props} durationMs={durationMs} getCurrentTimeMs={() => now} range={range} onRangeChange={setRange} />
    {range && <button onClick={() => setRange({ startMs: 90_000, endMs: 150_000 })}>模拟拖动进度条</button>}
  </>;
}
it("selects on the player first, previews automatically, and discards old materials after adjusting the range", async () => {
  await mount(<ClipTools />);
  await click("讲解片段");
  expect(document.querySelector('[role="dialog"]')).toBeNull();
  expect(api.previewCodexClip).not.toHaveBeenCalled();
  expect(document.querySelector("output")?.textContent).toBe("1:00 – 2:00");
  await click("预览片段");
  expect(api.previewCodexClip).toHaveBeenLastCalledWith({ video_id: 9, start_ms: 60_000, end_ms: 120_000 });
  expect(document.querySelector('input[type="text"]')).toBeNull();
  await click("调整片段");
  expect(document.querySelector('[role="dialog"]')).toBeNull();
  await click("模拟拖动进度条");
  await click("预览片段");
  expect(api.previewCodexClip).toHaveBeenLastCalledWith({ video_id: 9, start_ms: 90_000, end_ms: 150_000 });
  await click("关闭片段讲解");
  await click("取消片段选择");
  expect(document.querySelector("output")).toBeNull();
  expect(button("讲解片段")).toBeDefined();
});
it.each([
  [0, 300_000, "0:00 – 1:00"], [500, 300_000, "0:00 – 0:00.500"],
  [900_000, 300_000, "4:00 – 5:00"], [0, 20_000, "0:00 – 0:20"],
  [.2, 300_000, "0:00 – 0:00.001"], [0, 20_000.8, "0:00 – 0:20"],
  [20_000.8, 20_000.8, "0:00 – 0:20"],
])("bounds the initial selection to the video at %s ms", async (now, durationMs, label) => {
  await mount(<ClipTools now={now} durationMs={durationMs} />);
  await click("讲解片段");
  expect(document.querySelector("output")?.textContent).toBe(label);
});
it.each([0, .5, NaN, Infinity])("disables selection when the duration has no valid millisecond range: %s", async durationMs => {
  await mount(<ClipTools durationMs={durationMs} />);
  expect(button("讲解片段").disabled).toBe(true);
  expect(document.querySelector("output")).toBeNull();
});
it("clamps an existing selection and refreshes its preview when loaded media shortens the timeline", async () => {
  await mount(<ClipTools now={299_000} />);
  await click("讲解片段");
  await click("预览片段");
  expect(api.previewCodexClip).toHaveBeenLastCalledWith({ video_id: 9, start_ms: 239_000, end_ms: 299_000 });
  await mount(<ClipTools now={299_000} durationMs={20_000.8} />);
  expect(document.querySelector("output")?.textContent).toBe("0:19.999 – 0:20");
  expect(api.previewCodexClip).toHaveBeenLastCalledWith({ video_id: 9, start_ms: 19_999, end_ms: 20_000 });
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
  await mount(<ClipTools />);
  expect(host.textContent).toBe("");
  expect(api.getCodexClipDiscussions).not.toHaveBeenCalled();
});
it("locks duplicate requests and keeps Escape from closing during preparation", async () => {
  let resolve: (value: api.CodexDiscussion) => void = () => {};
  vi.mocked(api.prepareCodexClip).mockReturnValue(new Promise(done => { resolve = done; }));
  await mount(); await click("预览片段"); await click("在 Codex 中准备草稿");
  expect(button("正在准备草稿…").disabled).toBe(true);
  await act(async () => (document.querySelector('a[href="/settings?section=discussion"]') as HTMLAnchorElement).click());
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
