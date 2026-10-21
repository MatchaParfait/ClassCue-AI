import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ProcessingExperience } from "./ProcessingExperience";
import * as api from "../api/client";
import { createTaskSpace } from "../cyberspace/runtime.js";
import { saveAnalysisVariant } from "../cyberspace/preferences";
import type { Job, Marker, Video } from "../types";
vi.mock("../api/client", () => ({ getMarkers: vi.fn(), getSubtitles: vi.fn(), getLiveTranscript: vi.fn(), getAnalysisPreview: vi.fn(), getRecallSchemes: vi.fn() }));
vi.mock("../cyberspace/runtime.js", () => ({ createTaskSpace: vi.fn() }));
const video = { id: 23, filename: "真实课程.mp4", library_id: "test" } as Video;
const marker = { id: 42, video_id: 23, start_ms: 4000, end_ms: 6000, title: "测试考点", source_quote: "递归", note: "说明", analysis_mode: "medium_recall", review_status: "active", importance: "critical", user_start_ms: 3000 } as Marker;
const update = vi.fn(), dispose = vi.fn(), select = vi.fn(), resetView = vi.fn();
let root: Root, host: HTMLDivElement, client: QueryClient;
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.getRecallSchemes).mockResolvedValue([]);
  const settings = new Map<string, string>();
  vi.stubGlobal("localStorage", { getItem: (key: string) => settings.get(key) ?? null, setItem: (key: string, value: string) => settings.set(key, value) });
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.mocked(createTaskSpace).mockReturnValue({ update, dispose, select, resetView });
  vi.mocked(api.getSubtitles).mockResolvedValue({ source: "api", value: [{ id: 1, start_ms: 0, end_ms: 10000, text: "学习递归" }] });
  vi.mocked(api.getMarkers).mockResolvedValue({ source: "api", value: [marker] });
  vi.mocked(api.getLiveTranscript).mockResolvedValue({ video_id: 23, revision: 0, settled_until_ms: 0, subtitles: [] });
  vi.mocked(api.getAnalysisPreview).mockResolvedValue({ video_id: 23, revision: 0, active_mode: null, modes: {} });
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
});
afterEach(async () => { await act(async () => { root.unmount(); client.clear(); }); host.remove(); vi.unstubAllGlobals(); });
async function render(job: Partial<Job>, preference: "dynamic" = "dynamic", asrLabel?: string, aiModel?: string) {
  await act(async () => {
    root.render(<QueryClientProvider client={client}><MemoryRouter><ProcessingExperience
      job={{ id: "job", status: "running", ...job }} video={video} preference={preference} connectionError={false} onClose={() => {}}
      asrLabel={asrLabel} aiModel={aiModel} telemetry={<span>已保存 390/1406 个片段</span>} /></MemoryRouter></QueryClientProvider>);
    await new Promise((resolve) => setTimeout(resolve, 35));
  });
}
async function settle() { await act(async () => { await new Promise((resolve) => setTimeout(resolve, 35)); }); }
it("displays unaligned Qwen chunks during ASR and replaces them with aligned subtitles for analysis", async () => {
  const preview = { id: -1, start_ms: 0, end_ms: 90000, text: "本批识别到的真实课堂文字" };
  await render({ stage: "transcribing" }, "dynamic", "Qwen3-ASR 1.7B"); await settle();
  expect(host.querySelector('.subtitle-preparation-caption')).not.toBeNull();
  await act(async () => { client.setQueryData(["job-transcript", "test", 23, "job"], {
    video_id: 23, revision: 1, settled_until_ms: 0, subtitles: [preview],
  }); });
  await settle();
  expect(update.mock.lastCall?.[0].subtitles).toEqual([preview]);
  expect(host.querySelector('.subtitle-preparation-caption')).toBeNull();
  expect(host.textContent).toContain("精确时间将在对齐后更新");
  expect(api.getSubtitles).not.toHaveBeenCalled();
  await render({ stage: "analyzing" }, "dynamic", "Qwen3-ASR 1.7B"); await settle();
  expect(update.mock.lastCall?.[0].subtitles).toEqual([{ id: 1, start_ms: 0, end_ms: 10000, text: "学习递归" }]);
  expect(host.textContent).not.toContain("精确时间将在对齐后更新");
});
it("shows the scene waiting caption through preparation and removes it on real subtitles or failure", async () => {
  await render({ stage: "extracting_audio" }); await settle();
  expect(host.querySelector('.subtitle-preparation-caption')?.textContent).toContain('先静一会儿');
  await render({ stage: "transcribing" }); await settle();
  expect(host.querySelector('.subtitle-preparation-caption')?.textContent).toContain('正在识别第一段字幕');
  await act(async () => { client.setQueryData(["job-transcript", "test", 23, "job"], {
    video_id: 23, subtitles: [{ id: 1, start_ms: 0, end_ms: 1000, text: "真实字幕" }],
  }); });
  await settle();
  expect(host.querySelector('.subtitle-preparation-caption')).toBeNull();
  await render({ status: "failed" }); await settle();
  expect(host.querySelector('.subtitle-preparation-caption')).toBeNull();
});
it("resets only the scene view from its named toolbar control", async () => {
  await render({ stage: "analyzing" }); await settle();
  act(() => host.querySelector<HTMLButtonElement>('[aria-label="重置视角"]')!.click());
  expect(resetView).toHaveBeenCalledTimes(1);
  expect(dispose).not.toHaveBeenCalled();
});

it("links arrival feedback to the actual result without opening the drawer or stealing focus", async () => {
  await render({ status: "completed" }); await settle();
  const onEvidence = vi.mocked(createTaskSpace).mock.lastCall![1].onEvidence!;
  act(() => onEvidence({ id: 42, serial: 1, phase: "travel" }));
  expect(host.querySelector('.task-space-results')).toBeNull();
  act(() => host.querySelector<HTMLButtonElement>('[aria-label="已保存考点"]')!.click());
  const item = host.querySelector<HTMLButtonElement>('.task-space-result-list button')!;
  act(() => { item.focus(); onEvidence({ id: 42, serial: 1, phase: "arrival" }); });
  expect(item.querySelector('[data-evidence-phase="arrival"]')).not.toBeNull();
  expect(host.querySelector('.task-space-detail')).toBeNull();
  expect(document.activeElement).toBe(item);
  act(() => onEvidence(null)); expect(item.querySelector('.evidence-feedback')).toBeNull();
});
it("waits for durable subtitles and only reads results at termination", async () => {
  await render({ stage: "transcribing" }); await settle();
  expect(api.getSubtitles).not.toHaveBeenCalled(); expect(api.getMarkers).not.toHaveBeenCalled();
  expect(update.mock.lastCall?.[0].subtitles).toEqual([]);
  await render({ stage: "analyzing" }); await settle();
  expect(api.getSubtitles).toHaveBeenCalledWith(23); expect(api.getMarkers).not.toHaveBeenCalled();
  await render({ status: "completed" }); await settle();
  expect(update.mock.lastCall?.[0].markers).toEqual([marker]);
  expect(update.mock.lastCall?.[0].phase).toBe("completed");
});
it("opens real results, links to adjusted time and closes by Escape", async () => {
  await render({ status: "completed" }); await settle();
  act(() => (host.querySelector('[aria-label="已保存考点"]') as HTMLButtonElement).click());
  act(() => (host.querySelector('.task-space-result-list button') as HTMLButtonElement).click());
  expect(select).toHaveBeenLastCalledWith(42);
  expect(host.querySelector('.task-space-detail a')?.getAttribute("href")).toBe("/review/23?start_ms=3000&library_id=test");
  act(() => host.querySelector('section')!.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
  expect(host.querySelector('aside')).toBeNull();
  expect(document.activeElement?.getAttribute("aria-label")).toBe("已保存考点");
});
it("disposes on departure without task mutations", async () => {
  await render({ stage: "transcribing" }); await settle();
  await act(async () => root.render(null));
  expect(dispose).toHaveBeenCalledTimes(1);
});
it("keeps controls and data available when WebGL initialization fails", async () => {
  vi.mocked(createTaskSpace).mockImplementation(() => { throw new Error("WebGL disabled"); });
  await render({ status: "failed" }); await settle();
  expect(host.textContent).toContain("后台任务不受影响");
  expect(host.querySelector('[aria-label="已保存考点"]')).not.toBeNull();
  expect(host.querySelector('.task-space-telemetry')?.textContent).toContain("已保存 390/1406 个片段");
  expect(api.getMarkers).toHaveBeenCalled();
});
it("pauses the page atmosphere with completed jobs", async () => {
  await render({ status: "completed" });
  expect(host.querySelector('.task-space')?.getAttribute('data-moving')).toBe('false');
});
it("renders current job words during ASR and never substitutes another video's preview", async () => {
  const words = [{ id: 1, start_ms: 0, end_ms: 4000, text: "本次转写的真实文字" }];
  vi.mocked(api.getLiveTranscript).mockResolvedValue({ video_id: 23, revision: 1, settled_until_ms: 4000, subtitles: words });
  await render({ stage: "transcribing" }); await settle();
  expect(update.mock.lastCall?.[0].subtitles).toEqual(words);
  expect(host.textContent).toContain("本次转写片段");
  expect(host.textContent).toContain(words[0].text);
  await act(async () => { client.setQueryData(["job-transcript", "test", 23, "job"], { video_id: 99, subtitles: words }); });
  await settle();
  expect(update.mock.lastCall?.[0].subtitles).toEqual([]);
});
it("starts panel choreography with the first scene frame and does not restart on polling", async () => {
  await render({ stage: "transcribing" }); await settle();
  const callbacks = vi.mocked(createTaskSpace).mock.calls.at(-1)![1];
  expect(host.querySelector('.task-space')?.getAttribute('data-entrance')).toBe('waiting');
  act(() => callbacks.onReady?.(true));
  expect(host.querySelector('.task-space')?.getAttribute('data-entrance')).toBe('running');
  await render({ stage: "transcribing", progress: 30 });
  expect(host.querySelector('.task-space')?.getAttribute('data-entrance')).toBe('running');
  const event = new Event('animationend', { bubbles: true }); Object.defineProperty(event, 'animationName', { value: 'space-unroll-up' });
  act(() => host.querySelector('.task-space-transcript')!.dispatchEvent(event));
  expect(host.querySelector('.task-space')?.getAttribute('data-entrance')).toBe('done');
  act(() => callbacks.onReady?.(true));
  expect(host.querySelector('.task-space')?.getAttribute('data-entrance')).toBe('done');
});
it("reveals status immediately on early completion or a rendering failure", async () => {
  await render({ stage: "transcribing" }); await settle();
  const callbacks = vi.mocked(createTaskSpace).mock.calls.at(-1)![1];
  act(() => callbacks.onReady?.(true));
  await render({ status: "completed" });
  expect(host.querySelector('.task-space')?.getAttribute('data-entrance')).toBe('done');
  act(() => callbacks.onError('测试渲染失败'));
  expect(host.querySelector('.task-space')?.getAttribute('data-entrance')).toBe('done');
  expect(host.textContent).toContain('测试渲染失败');
});
it("does not hide the result panel when it is opened during entry", async () => {
  await render({ stage: "transcribing" }); await settle();
  act(() => vi.mocked(createTaskSpace).mock.calls.at(-1)![1].onReady?.(true));
  act(() => (host.querySelector('[aria-label="本次考点预览"]') as HTMLButtonElement).click());
  expect(host.querySelector('.task-space')?.getAttribute('data-entrance')).toBe('done');
  expect(host.querySelector('.task-space-results')).not.toBeNull();
});

const previewResult = { ...marker, id: -1, preview: true };
const previewState = (markers = [previewResult]) => ({ video_id: 23, revision: 1, active_mode: "medium_recall" as const,
  modes: { medium_recall: { completed_chunks: 1, total_chunks: 3, current_chunk: 2, candidate_count: markers.length, truncated: false, markers } } });

it("shows validated chunks during analysis without claiming they are published", async () => {
  vi.mocked(api.getAnalysisPreview).mockResolvedValue(previewState());
  await render({ stage: "analyzing" }); await settle();
  expect(update.mock.lastCall?.[0].markers).toEqual([previewResult]);
  expect(update.mock.lastCall?.[0].resultScope).toBe("medium_recall");
  expect(api.getMarkers).not.toHaveBeenCalled();
  expect(host.textContent).toContain("正在分析第 2 / 3 块");
  act(() => (host.querySelector('[aria-label="本次考点预览"]') as HTMLButtonElement).click());
  act(() => (host.querySelector('.task-space-result-list button') as HTMLButtonElement).click());
  expect(host.textContent).toContain("已校验，待最终汇总");
  expect(host.querySelector('.task-space-detail a')).toBeNull();
  const next = { ...previewResult, id: -2, start_ms: 600000, end_ms: 602000, title: "下一块考点" };
  await act(async () => { client.setQueryData(["job-analysis-preview", "test", 23, "job"], previewState([previewResult, next])); });
  await settle();
  expect(update.mock.lastCall?.[0].markers).toEqual([previewResult, next]);
  expect(host.querySelectorAll('.task-space-result-list button').length).toBe(2);
});

it("retains preview through a failure and replaces it only when final markers arrive", async () => {
  vi.mocked(api.getAnalysisPreview).mockResolvedValue(previewState());
  await render({ stage: "analyzing" }); await settle();
  await render({ status: "failed" }); await settle();
  expect(update.mock.lastCall?.[0].markers).toEqual([previewResult]);
  expect(update.mock.lastCall?.[0].phase).toBe("failed");
  await render({ status: "completed" }); await settle();
  expect(update.mock.lastCall?.[0].markers).toEqual([marker]);
  expect(host.querySelector('[aria-label="已保存考点"]')).not.toBeNull();
});

it("isolates previews by video and reports empty chunks without inventing markers", async () => {
  vi.mocked(api.getAnalysisPreview).mockResolvedValue({ ...previewState(), video_id: 99 });
  await render({ stage: "analyzing" }); await settle();
  expect(update.mock.lastCall?.[0].markers).toEqual([]);
  await act(async () => { client.setQueryData(["job-analysis-preview", "test", 23, "job"], previewState([])); });
  await settle();
  expect(host.textContent).toContain("0 个已校验考点");
  expect(update.mock.lastCall?.[0].markers).toEqual([]);
  expect(host.querySelector('.task-space-evidence')).toBeNull();
});

it("shows returned evidence once, keeps its node on polling/final IDs and opens the actual result", async () => {
  vi.mocked(api.getAnalysisPreview).mockResolvedValue(previewState());
  await render({ stage: "analyzing" }); await settle();
  const evidence = host.querySelector<HTMLButtonElement>('.task-space-evidence')!;
  expect(evidence.textContent).toContain(previewResult.source_quote);
  const confirmed = { ...previewResult, id: 101 };
  await act(async () => { client.setQueryData(["job-analysis-preview", "test", 23, "job"], previewState([confirmed])); });
  await settle();
  expect(host.querySelector('.task-space-evidence')).toBe(evidence);
  act(() => evidence.click());
  expect(host.querySelector('.task-space-detail blockquote')?.textContent).toBe(confirmed.source_quote);
  expect(select).toHaveBeenLastCalledWith(101);
});

it("changes visual versions without closing selected results or changing the task snapshot", async () => {
  vi.mocked(api.getAnalysisPreview).mockResolvedValue(previewState());
  await render({ stage: "analyzing" }); await settle();
  act(() => host.querySelector<HTMLButtonElement>('.task-space-evidence')!.click());
  expect(createTaskSpace).toHaveBeenCalledTimes(1);
  expect(vi.mocked(createTaskSpace).mock.lastCall?.[1].analysisQuality).toBe("high");
  act(() => { saveAnalysisVariant("gpt"); }); await settle();
  expect(dispose).toHaveBeenCalledTimes(1);
  expect(vi.mocked(createTaskSpace).mock.lastCall?.[1].analysisQuality).toBe("high");
  expect(host.querySelector('.task-space-detail blockquote')?.textContent).toBe(previewResult.source_quote);
  expect(select).toHaveBeenLastCalledWith(previewResult.id);
  expect(update.mock.lastCall?.[0].phase).toBe("analyzing");
  expect(update.mock.lastCall?.[0].markers).toEqual([previewResult]);
});

it("requests one camera move for each explicit evidence click, including the same selected item", async () => {
  vi.mocked(api.getAnalysisPreview).mockResolvedValue(previewState());
  await render({ stage: "analyzing" }); await settle();
  act(() => host.querySelector<HTMLButtonElement>('.task-space-evidence')!.click());
  expect(host.querySelector('.task-space-detail blockquote')?.textContent).toBe(previewResult.source_quote);
  expect(select.mock.calls.filter(call => call[1]?.travel)).toHaveLength(1);
  act(() => host.querySelector<HTMLButtonElement>('.task-space-result-list button')!.click());
  act(() => host.querySelector<HTMLButtonElement>('.task-space-result-list button')!.click());
  expect(select.mock.calls.filter(call => call[1]?.travel)).toHaveLength(3);
  expect(dispose).not.toHaveBeenCalled();
});


it("routes auto by model metadata, pins it during polling and resolves a new task separately", async () => {
  act(() => { saveAnalysisVariant("auto"); });
  await render({ stage: "transcribing" }); await settle();
  expect(["gravity", "nailong"]).toContain(vi.mocked(createTaskSpace).mock.lastCall![1].analysisVariant);
  await render({ stage: "transcribing" }, "dynamic", undefined, "vendor/DeepSeek-V3"); await settle();
  const selected = vi.mocked(createTaskSpace).mock.lastCall![1].analysisVariant;
  expect(["dafeiyu", "dafeiyu31", "dafeiyu-rice"]).toContain(selected);
  const creations = vi.mocked(createTaskSpace).mock.calls.length;
  await render({ stage: "analyzing" }, "dynamic", undefined, "gpt-5"); await settle();
  expect(vi.mocked(createTaskSpace).mock.calls.length).toBe(creations);
  expect(host.querySelector('.task-space')?.getAttribute('data-analysis-variant')).toBe(selected);
  await render({ id: "next-job", stage: "analyzing" }, "dynamic", undefined, "GPT-5"); await settle();
  expect(vi.mocked(createTaskSpace).mock.lastCall![1].analysisVariant).toBe("gpt");
  act(() => { saveAnalysisVariant("gravity"); }); await settle();
  expect(vi.mocked(createTaskSpace).mock.lastCall![1].analysisVariant).toBe("gravity");
});
