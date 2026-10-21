import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { Subtitle } from "../types";
import { TranscriptTicker } from "./TranscriptTicker";

let host: HTMLDivElement, root: Root;
const words = (count: number): Subtitle[] => Array.from({ length: count }, (_, i) => ({ id: i + 1, start_ms: i * 2000, end_ms: i * 2000 + 1800, text: `第${i + 1}句真实字幕，按顺序衔接。` }));
beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "performance"] });
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});
afterEach(() => { act(() => root.unmount()); host.remove(); vi.useRealTimers(); });
function render(subtitles: Subtitle[], options: { live?: boolean; paused?: boolean; reduced?: boolean; session?: string } = {}) {
  act(() => root.render(<TranscriptTicker key={options.session ?? "job"} subtitles={subtitles} live={options.live ?? true} paused={options.paused ?? false} reduced={options.reduced ?? false} />));
}
const tick = (ms: number) => act(() => vi.advanceTimersByTime(ms));
const rows = () => [...host.querySelectorAll<HTMLElement>("li[data-subtitle-key]")];
const phase = () => host.firstElementChild?.getAttribute("data-feed-phase");
const pending = () => Number(host.firstElementChild?.getAttribute("data-pending"));
function finishLine() { tick(300); tick(1700); tick(180); }

it("queues a batch and reveals just one sentence at a time", () => {
  render(words(3));
  expect(rows()).toHaveLength(1); expect(pending()).toBe(2); expect(phase()).toBe("scrolling");
  expect(rows()[0].dataset.reveal).toBe("pending");
  tick(300); expect(phase()).toBe("revealing");
  expect(host.querySelectorAll('li[data-reveal="active"]')).toHaveLength(1);
  const first = rows()[0];
  tick(1700); expect(phase()).toBe("holding"); tick(180);
  expect(rows()).toHaveLength(2); expect(rows()[0]).toBe(first); expect(first.dataset.reveal).toBe("settled");
  expect(rows()[1].dataset.reveal).toBe("pending");
  tick(300); expect(rows()[1].dataset.reveal).toBe("active");
});
it("scrolls only the oldest row away and preserves the two connecting rows", () => {
  render(words(3)); finishLine(); finishLine(); finishLine();
  expect(phase()).toBe("idle"); const old = rows();
  render(words(6)); expect(rows()).toHaveLength(4);
  expect(rows()[1]).toBe(old[1]); expect(rows()[2]).toBe(old[2]); expect(pending()).toBe(2);
  tick(300); expect(rows()).toHaveLength(3);
  expect(rows()[0]).toBe(old[1]); expect(rows()[1]).toBe(old[2]);
  expect(rows()[2].textContent).toContain("第4句");
});
it("does not restart a reveal or duplicate queued sentences during polling", () => {
  render(words(3)); tick(300); tick(200); const active = rows()[0];
  render(words(3)); render(words(4));
  expect(rows()[0]).toBe(active); expect(pending()).toBe(3);
  tick(1400); expect(phase()).toBe("holding");
  tick(180); expect(rows()[1].textContent).toContain("第2句");
});
it("pauses the queue clock and resumes the remaining scroll time", () => {
  const text = words(3); render(text); tick(100); render(text, { paused: true });
  tick(10000); expect(phase()).toBe("scrolling"); expect(rows()).toHaveLength(1);
  render(text); tick(199); expect(phase()).toBe("scrolling");
  tick(1); expect(phase()).toBe("revealing");
});
it("renders saved history and reduced motion without a replay queue", () => {
  render(words(50), { live: false });
  expect(phase()).toBe("idle"); expect(pending()).toBe(0); expect(rows()).toHaveLength(3);
  expect(rows()[0].textContent).toContain("第48句");
  render(words(51), { reduced: true });
  expect(phase()).toBe("idle"); expect(rows().every(row => row.dataset.reveal === "settled")).toBe(true);
  expect(vi.getTimerCount()).toBe(0);
});
it("bounds only the presentation backlog and preserves the current row during a burst", () => {
  const input = words(100); render(input.slice(0, 1)); const first = rows()[0];
  render(input); expect(pending()).toBe(12); expect(rows()[0]).toBe(first);
  expect(input).toHaveLength(100); expect(input[0].text).toContain("第1句");
});
it("does not replay identical saved sentences when database IDs change", () => {
  render(words(3)); finishLine(); finishLine(); finishLine(); const old = rows();
  render(words(3).map(s => ({ ...s, id: s.id + 100 })), { live: false });
  expect(pending()).toBe(0); expect(phase()).toBe("idle"); expect(rows()[0]).toBe(old[0]);
});
it("does not replay older history when a rolling preview becomes a full saved transcript", () => {
  render(words(50).slice(-3)); finishLine(); finishLine(); finishLine(); const old = rows();
  render(words(50), { live: false });
  expect(pending()).toBe(0); expect(phase()).toBe("idle"); expect(rows()[0]).toBe(old[0]);
  render(words(52), { live: false }); expect(pending()).toBe(1);
  expect(rows().at(-1)?.textContent).toContain("第51句");
});
it("corrects existing text in place and clears pending work on a new task", () => {
  render(words(3)); finishLine(); finishLine(); finishLine(); const first = rows()[0];
  const corrected = words(3); corrected[0].text = "修订后的真实文本"; render(corrected);
  expect(rows()[0]).toBe(first); expect(rows()[0].textContent).toContain("修订后的真实文本");
  expect(pending()).toBe(0);
  render(words(2), { session: "other-job" });
  expect(rows()).toHaveLength(1); expect(rows()[0]).not.toBe(first); expect(pending()).toBe(1);
});
it("allows completion to catch up with only the final rows, without replacing the current view", () => {
  render(words(1)); const first = rows()[0]; render(words(100), { live: false });
  expect(rows()[0]).toBe(first); expect(pending()).toBe(3);
  finishLine(); expect(rows()[1].textContent).toContain("第98句");
});

const sentences = (...texts: string[]): Subtitle[] => texts.map((text, i) => ({ id: i + 1, start_ms: i * 2000, end_ms: i * 2000 + 1800, text }));
const displayed = () => rows().map(row => row.querySelector('.task-space-subtitle-copy')?.getAttribute('title'));

it("joins short fragments to the preceding sentence with spaces without changing source data", () => {
  const input = sentences("这是一句完整的字幕", "对", "然后呢？", "一二三四五。", "一二三四五六");
  const original = JSON.stringify(input);
  render(input, { live: false });
  expect(displayed()).toEqual(["这是一句完整的字幕 对 然后呢？ 一二三四五。", "一二三四五六"]);
  expect(rows()[0].querySelector('time')?.textContent).toBe('0:00');
  expect(JSON.stringify(input)).toBe(original);
});

it("joins leading short fragments to the following sentence and ignores empty text", () => {
  render(sentences("好", "那么", " ", "现在我们来看分治算法"), { live: false });
  expect(displayed()).toEqual(["好 那么 现在我们来看分治算法"]);
});

it("appends streamed fragments without changing the row identity or replaying its glyphs", () => {
  const input = sentences("这是一句完整的字幕", "好", "下一句是独立的长句");
  render(input.slice(0, 1)); finishLine(); const first = rows()[0];
  const glyph = first.querySelector('.task-space-reveal-glyph');
  render(input.slice(0, 2));
  expect(displayed()).toEqual(["这是一句完整的字幕 好"]);
  expect(rows()[0]).toBe(first); expect(first.querySelector('.task-space-reveal-glyph')).toBe(glyph);
  expect(pending()).toBe(0); expect(phase()).toBe('idle');
  render(input); expect(rows()[0]).toBe(first); expect(displayed()[1]).toBe("下一句是独立的长句");
  finishLine();
  render(input.map(s => ({ ...s, id: s.id + 100 })), { live: false });
  expect(rows()[0]).toBe(first); expect(pending()).toBe(0);
});

it("keeps a merged row intact when the rolling preview drops its opening segment", () => {
  const input = sentences("这是一句完整的字幕", "好的", "下一句是独立的长句");
  render(input); finishLine(); finishLine(); const first = rows()[0];
  render(input.slice(1));
  expect(rows()[0]).toBe(first);
  expect(displayed()).toEqual(["这是一句完整的字幕 好的", "下一句是独立的长句"]);
  expect(pending()).toBe(0);
});

it("bounds merged rows during a long sequence of short fragments", () => {
  render(sentences(...Array.from({ length: 100 }, () => "这是短句")), { live: false });
  expect(rows()).toHaveLength(3);
  expect(displayed().every(text => Array.from(text!).length <= 160)).toBe(true);
});
