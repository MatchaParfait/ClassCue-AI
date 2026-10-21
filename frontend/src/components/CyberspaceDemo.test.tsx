import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { beforeEach, afterEach, expect, it, vi } from "vitest";
import { ExperimentalSettings } from "./ExperimentalSettings";
import { createTaskSpace } from "../cyberspace/runtime";
import { CYBERSPACE_KEY, ANALYSIS_VARIANT_KEY } from "../cyberspace/preferences";
import { getAiSettings } from "../api/client";

vi.mock("../api/client", () => ({ getAiSettings: vi.fn() }));

vi.mock("../cyberspace/runtime.js", () => ({ createTaskSpace: vi.fn() }));
let host: HTMLDivElement, root: Root;
const update = vi.fn(), dispose = vi.fn(), select = vi.fn(), resetView = vi.fn();
beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.useFakeTimers(); vi.clearAllMocks();
  vi.stubGlobal("localStorage", { getItem: () => "off", setItem: vi.fn() });
  vi.stubGlobal("fetch", vi.fn());
  vi.mocked(createTaskSpace).mockImplementation((_canvas, options) => {
    options.onReady?.(true); return { update, dispose, select, resetView };
  });
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});
afterEach(() => { act(() => root.unmount()); host.remove(); vi.useRealTimers(); vi.unstubAllGlobals(); });
const button = (label: string) => document.querySelector<HTMLButtonElement>(`[aria-label="${label}"]`)!;
async function open() {
  act(() => root.render(<ExperimentalSettings />));
  const opener = host.querySelector<HTMLButtonElement>('[aria-haspopup="dialog"]')!;
  act(() => { opener.focus(); opener.click(); });
  await act(async () => { await vi.dynamicImportSettled(); });
  return opener;
}
const tick = (ms: number) => act(() => vi.advanceTimersByTime(ms));
const stage = () => document.querySelector('.subtitle-demo-scene')?.getAttribute('data-stage');

it("previews without enabling the experiment or making processing requests", async () => {
  await open();
  expect(document.querySelector('[role="dialog"]')?.textContent).toContain("不调用 ASR / AI");
  expect(update.mock.lastCall?.[0].subtitles).toHaveLength(0);
  expect(update.mock.lastCall?.[0].phase).toBe('preparing');
  expect(vi.mocked(createTaskSpace).mock.lastCall?.[1].analysisPlaybackRate).toBe(.4);
  expect(vi.mocked(createTaskSpace).mock.lastCall?.[1].analysisVariant).toBe('nailong');
  expect(stage()).toBe('0'); expect(fetch).not.toHaveBeenCalled();
  expect(localStorage.getItem(CYBERSPACE_KEY)).toBe('off'); expect(localStorage.setItem).not.toHaveBeenCalled();
});

it("resolves auto preview from the saved model before creating the renderer", async () => {
  vi.stubGlobal("localStorage", { getItem: (key: string) => key === ANALYSIS_VARIANT_KEY ? "auto" : "off", setItem: vi.fn() });
  vi.mocked(getAiSettings).mockResolvedValue({ model: "vendor/GPT-test" } as Awaited<ReturnType<typeof getAiSettings>>);
  await open();
  expect(getAiSettings).toHaveBeenCalledTimes(1);
  expect(vi.mocked(createTaskSpace).mock.lastCall?.[1].analysisVariant).toBe("gpt");
  expect(fetch).not.toHaveBeenCalled();
});

it("falls back to black hole or Nailong if preview model metadata is unavailable", async () => {
  vi.stubGlobal("localStorage", { getItem: (key: string) => key === ANALYSIS_VARIANT_KEY ? "auto" : "off", setItem: vi.fn() });
  vi.mocked(getAiSettings).mockRejectedValue(new Error("offline"));
  await open();
  expect(["gravity", "nailong"]).toContain(vi.mocked(createTaskSpace).mock.lastCall?.[1].analysisVariant);
});

it("marks automatic arrivals without opening details and preserves button focus on repeated cues", async () => {
  await open();
  act(() => document.querySelectorAll<HTMLButtonElement>('[aria-label="演示阶段"] button')[3].click());
  await act(async () => { await vi.dynamicImportSettled(); });
  const onEvidence = vi.mocked(createTaskSpace).mock.lastCall![1].onEvidence!;
  const item = document.querySelector<HTMLButtonElement>('.subtitle-demo-results > button')!;
  act(() => { item.focus(); onEvidence({ id: 1, serial: 1, phase: "arrival" }); });
  expect(item.querySelector('[data-evidence-phase="arrival"]')).not.toBeNull();
  expect(document.querySelector('.subtitle-demo-detail')).toBeNull();
  const decoration = item.firstElementChild;
  act(() => onEvidence({ id: 1, serial: 2, phase: "arrival" }));
  expect(item.firstElementChild).not.toBe(decoration);
  expect(document.activeElement).toBe(item);
  act(() => onEvidence(null)); expect(item.querySelector('.evidence-feedback')).toBeNull();
});
it("pauses, continues and reveals example results in stages", async () => {
  await open(); tick(1000);
  act(() => button('暂停演示').click()); tick(12000);
  expect(stage()).toBe('0'); expect(update.mock.lastCall?.[0].paused).toBe(true);
  act(() => button('继续演示').click()); tick(3000);
  expect(stage()).toBe('0'); expect(update.mock.lastCall?.[0].phase).toBe('transcribing');
  expect(update.mock.lastCall?.[0].subtitles).toHaveLength(0);
  expect(document.querySelector('.subtitle-demo-quote')?.textContent).toContain('正在识别第一段字幕');
  tick(1000);
  expect(stage()).toBe('1'); expect(update.mock.lastCall?.[0].markers).toHaveLength(0);
  expect(update.mock.lastCall?.[0].subtitles.length).toBeGreaterThan(0);
  tick(7000); expect(stage()).toBe('2'); expect(update.mock.lastCall?.[0].markers).toHaveLength(0);
  tick(6000); expect(stage()).toBe('2'); expect(update.mock.lastCall?.[0].markers).toHaveLength(0);
  tick(9000); expect(stage()).toBe('3'); expect(update.mock.lastCall?.[0].markers).toHaveLength(2);
  tick(7500); expect(update.mock.lastCall?.[0].markers).toHaveLength(4);
  tick(15000); expect(update.mock.lastCall?.[0].phase).toBe('completed');
  expect(document.querySelector('[role="status"]')?.textContent).toBe('演示结束');
});
it("replays, switches stages, focuses results and resets the view", async () => {
  await open();
  act(() => document.querySelectorAll<HTMLButtonElement>('[aria-label="演示阶段"] button')[3].click());
  await act(async () => { await vi.dynamicImportSettled(); });
  expect(stage()).toBe('3'); expect(dispose).toHaveBeenCalledTimes(1);
  act(() => document.querySelector<HTMLButtonElement>('.subtitle-demo-results > button')!.click());
  expect(select).toHaveBeenLastCalledWith(1);
  expect(document.querySelector('.subtitle-demo-detail')?.textContent).toContain('分解');
  expect(select.mock.calls.filter(call => call[1]?.travel)).toHaveLength(1);
  act(() => document.querySelector<HTMLButtonElement>('.subtitle-demo-results > button')!.click());
  expect(select).toHaveBeenLastCalledWith(1, { travel: true });
  expect(select.mock.calls.filter(call => call[1]?.travel)).toHaveLength(2);
  act(() => button('重置演示视角').click()); expect(resetView).toHaveBeenCalledTimes(1);
  act(() => button('重播演示').click());
  await act(async () => { await vi.dynamicImportSettled(); });
  expect(stage()).toBe('0'); expect(update.mock.lastCall?.[0].markers).toHaveLength(0);
  expect(update.mock.lastCall?.[0].phase).toBe('preparing');
  expect(update.mock.lastCall?.[0].subtitles).toHaveLength(0);
});
it("feeds ASR sample subtitles in batches, freezes them on pause and supplies all text at analysis", async () => {
  await open(); tick(5000);
  const initial = update.mock.lastCall![0].subtitles;
  expect(initial).toHaveLength(24);
  tick(2000); expect(update.mock.lastCall![0].subtitles).toHaveLength(36);
  expect(update.mock.lastCall![0].subtitles.slice(0, 24)).toEqual(initial);
  act(() => button('暂停演示').click()); tick(10000);
  expect(update.mock.lastCall![0].subtitles).toHaveLength(36);
  act(() => button('继续演示').click()); tick(2000);
  expect(update.mock.lastCall![0].subtitles).toHaveLength(48);
  tick(3000);
  expect(update.mock.lastCall![0].phase).toBe('analyzing');
  expect(update.mock.lastCall![0].subtitles.length).toBeGreaterThan(60);
});
it("closes on Escape, releases the scene and returns focus to the opener", async () => {
  const opener = await open();
  expect(document.activeElement).toBe(button('关闭演示'));
  act(() => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })));
  expect(document.querySelector('[role="dialog"]')).toBeNull(); expect(dispose).toHaveBeenCalledTimes(1);
  expect(document.activeElement).toBe(opener); expect(document.body.style.overflow).not.toBe('hidden');
});
it("releases the preview when settings becomes inactive", async () => {
  await open(); act(() => root.render(<ExperimentalSettings active={false} />));
  expect(document.querySelector('[role="dialog"]')).toBeNull(); expect(dispose).toHaveBeenCalledTimes(1);
});
it("keeps close and retry available when the renderer fails", async () => {
  vi.mocked(createTaskSpace).mockImplementation(() => { throw new Error('WebGL unavailable'); });
  await open();
  expect(document.querySelector('[role="alert"]')?.textContent).toContain('演示暂时无法加载');
  expect(button('关闭演示').disabled).toBe(false);
  expect(button('暂停演示').disabled).toBe(true);
});
