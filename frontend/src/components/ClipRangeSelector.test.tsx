import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ClipRangeSelector, type ClipRange } from "./ClipRangeSelector";

let root: Root, host: HTMLDivElement;
const seek = vi.fn();
beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  seek.mockClear();
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
    x: 100, left: 100, y: 0, top: 0, right: 400, bottom: 18, width: 300, height: 18, toJSON: () => ({}),
  });
});
afterEach(() => { act(() => root.unmount()); host.remove(); vi.restoreAllMocks(); });
function mount(durationMs = 300_000, maxMs?: number) {
  function Selection() {
    const [range, setRange] = useState<ClipRange>({ startMs: 60_000, endMs: 120_000 });
    return <ClipRangeSelector range={range} durationMs={durationMs} maxMs={maxMs} onChange={setRange} onSeek={seek} />;
  }
  act(() => root.render(<Selection />));
  for (const handle of host.querySelectorAll<HTMLButtonElement>('[role="slider"]')) {
    handle.setPointerCapture = vi.fn();
    handle.releasePointerCapture = vi.fn();
  }
}
function handle(label: string) { return host.querySelector<HTMLButtonElement>(`[aria-label="${label}"]`)!; }
function pointer(element: HTMLElement, type: string, x: number, pointerId = 1, button = 0) {
  const event = new MouseEvent(type, { bubbles: true, clientX: x, button });
  Object.defineProperty(event, "pointerId", { value: pointerId });
  act(() => element.dispatchEvent(event));
}
function key(element: HTMLElement, value: string, shiftKey = false) {
  act(() => element.dispatchEvent(new KeyboardEvent("keydown", { key: value, shiftKey, bubbles: true })));
}
it("drags both boundaries on the player timeline without jumping when grabbing a handle", () => {
  mount();
  const start = handle("片段开始时间"), end = handle("片段结束时间");
  expect(document.activeElement).toBe(start);
  pointer(start, "pointerdown", 151);
  pointer(start, "pointermove", 181);
  expect(start.getAttribute("aria-valuenow")).toBe("90000");
  expect(seek).toHaveBeenLastCalledWith(90_000);
  pointer(start, "pointerup", 181);
  expect(start.releasePointerCapture).toHaveBeenCalledWith(1);
  pointer(end, "pointerdown", 229);
  pointer(end, "pointermove", 259);
  expect(end.getAttribute("aria-valuenow")).toBe("150000");
  expect(seek).toHaveBeenLastCalledWith(150_000);
});
it("clamps drags to the video and keeps boundaries from crossing even outside the track", () => {
  mount();
  const start = handle("片段开始时间"), end = handle("片段结束时间");
  pointer(start, "pointerdown", 160);
  pointer(start, "pointermove", -100);
  expect(start.getAttribute("aria-valuenow")).toBe("0");
  pointer(start, "pointermove", 900);
  expect(start.getAttribute("aria-valuenow")).toBe("119999");
  pointer(start, "pointerup", 900);
  pointer(end, "pointerdown", 220);
  pointer(end, "pointermove", 900);
  expect(end.getAttribute("aria-valuenow")).toBe("300000");
  pointer(end, "pointermove", -100);
  expect(end.getAttribute("aria-valuenow")).toBe("120000");
});
it("supports keyboard adjustment without triggering playback shortcuts", () => {
  mount();
  const start = handle("片段开始时间"), end = handle("片段结束时间");
  key(start, "ArrowRight");
  expect(start.getAttribute("aria-valuenow")).toBe("61000");
  key(start, "ArrowLeft", true);
  expect(start.getAttribute("aria-valuenow")).toBe("51000");
  key(start, "Home");
  expect(start.getAttribute("aria-valuenow")).toBe("0");
  key(end, "End");
  expect(end.getAttribute("aria-valuenow")).toBe("300000");
  key(end, "PageDown");
  expect(seek).toHaveBeenLastCalledWith(290_000);
});
it.each([
  [300_000.75, undefined, 300_000],
  [300_000.75, 200_000, 200_000],
])("limits selections to integer milliseconds while retaining the full %s ms timeline", (durationMs, maxMs, expected) => {
  mount(durationMs, maxMs);
  const end = handle("片段结束时间");
  key(end, "End");
  expect(end.getAttribute("aria-valuenow")).toBe(String(expected));
  expect(end.getAttribute("aria-valuemax")).toBe(String(expected));
  expect(parseFloat(end.style.left)).toBeCloseTo(expected / durationMs * 100);
  key(end, "ArrowLeft");
  pointer(end, "pointerdown", 220);
  pointer(end, "pointermove", 1000);
  expect(end.getAttribute("aria-valuenow")).toBe(String(expected));
  expect(seek).toHaveBeenLastCalledWith(expected);
});
it("restores the selection after pointer cancellation and ignores unrelated pointers", () => {
  mount();
  const start = handle("片段开始时间");
  pointer(start, "pointerdown", 160, 1, 2);
  pointer(start, "pointermove", 190);
  expect(seek).not.toHaveBeenCalled();
  pointer(start, "pointerdown", 160);
  pointer(start, "pointermove", 190, 2);
  expect(seek).not.toHaveBeenCalled();
  pointer(start, "pointermove", 190);
  expect(start.getAttribute("aria-valuenow")).toBe("90000");
  pointer(start, "pointercancel", 190);
  expect(start.getAttribute("aria-valuenow")).toBe("60000");
  expect(seek).toHaveBeenLastCalledWith(60_000);
  pointer(start, "pointermove", 200);
  expect(start.getAttribute("aria-valuenow")).toBe("60000");
});
