import { act, useRef } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useLibraryPointerDrag } from "./useLibraryPointerDrag";

class Transfer {
  effectAllowed = "none";
  dropEffect = "none";
  types: string[] = [];
}
class Drag extends MouseEvent {
  readonly dataTransfer: DataTransfer | null;
  constructor(type: string, init: DragEventInit = {}) {
    super(type, init);
    this.dataTransfer = init.dataTransfer ?? null;
  }
}

let host: HTMLDivElement, root: Root, surface: HTMLElement, source: HTMLElement, panel: HTMLElement, target: HTMLElement;
let hit: Element | null, time: number, frames: Map<number, FrameRequestCallback>, nextFrame: number;
const events: string[] = [];
const rect = (top: number, height: number) => ({ top, bottom: top + height, left: 0, right: 600, width: 600, height, x: 0, y: top, toJSON() {} });

function Harness({ enabled = true, reject = false }: { enabled?: boolean; reject?: boolean }) {
  const ref = useRef<HTMLElement>(null);
  useLibraryPointerDrag(ref, enabled);
  return <main ref={ref}>
    <section className="folder-window">
      <div className="folder-window-content">
        <article data-library-key="video-1" className="library-card video-card" draggable
          onDragStart={event => { events.push("start"); if (reject) event.preventDefault(); }}
          onDragEnd={() => events.push("end")}>
          <button className="library-thumbnail-link"><span>preview</span></button>
          <button className="library-card-body library-card-link"><strong>Lesson</strong></button>
          <button className="rename">Rename</button>
          <input type="checkbox" />
        </article>
        <article className="library-card folder-card" data-library-key="folder-2" draggable
          onDragOver={event => { event.preventDefault(); events.push("over"); }}
          onDragLeave={() => events.push("leave")}
          onDrop={event => { event.preventDefault(); events.push("drop"); }} />
      </div>
    </section>
  </main>;
}

function pointer(type: string, element: Element | Document, x: number, y: number, options = {}) {
  const event = new MouseEvent(type, { bubbles: true, cancelable: true, button: 0,
    buttons: type === "pointerup" ? 0 : 1, clientX: x, clientY: y, ...options });
  Object.defineProperties(event, { pointerId: { value: 1 }, pointerType: { value: "mouse" } });
  act(() => { element.dispatchEvent(event); });
  return event;
}
function start(x = 240, y = 300) {
  pointer("pointerdown", source.querySelector(".library-thumbnail-link")!, x, y);
  pointer("pointermove", document, x + 10, y);
}
function tick(value: number) {
  time = value;
  const callbacks = [...frames.values()];
  frames.clear();
  act(() => { for (const callback of callbacks) callback(value); });
}
function wheel(deltaY: number, deltaMode = 0) {
  const event = new WheelEvent("wheel", { bubbles: true, cancelable: true, deltaY, deltaMode });
  act(() => { target.dispatchEvent(event); });
  return event;
}

beforeEach(async () => {
  events.length = 0;
  frames = new Map(); nextFrame = 0; time = 0;
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.stubGlobal("DataTransfer", Transfer);
  vi.stubGlobal("DragEvent", Drag);
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => { frames.set(++nextFrame, callback); return nextFrame; });
  vi.stubGlobal("cancelAnimationFrame", (id: number) => frames.delete(id));
  vi.spyOn(performance, "now").mockImplementation(() => time);
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
  await act(async () => { root.render(<Harness />); });
  surface = host.querySelector("main")!;
  source = host.querySelector('[data-library-key="video-1"]')!;
  panel = host.querySelector(".folder-window-content")!;
  target = host.querySelector('[data-library-key="folder-2"]')!;
  panel.getBoundingClientRect = () => rect(100, 500);
  host.querySelector<HTMLElement>(".folder-window")!.getBoundingClientRect = () => rect(30, 570);
  Object.defineProperty(panel, "clientHeight", { configurable: true, value: 500 });
  hit = target;
  Object.defineProperty(document, "elementFromPoint", { configurable: true, value: vi.fn(() => hit) });
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  vi.restoreAllMocks(); vi.unstubAllGlobals();
  delete (document as Partial<Document>).elementFromPoint;
});

describe("library pointer dragging", () => {
  it("scrolls with the wheel while holding a video, then drops exactly once", () => {
    start();
    expect(events).toContain("start");
    expect(wheel(180).defaultPrevented).toBe(true);
    expect(panel.scrollTop).toBe(180);
    expect(wheel(-3, 1).defaultPrevented).toBe(true);
    expect(panel.scrollTop).toBe(132);
    pointer("pointerup", document, 250, 300);
    expect(events.filter(event => event === "drop")).toHaveLength(1);
    expect(events.at(-1)).toBe("end");
    expect(document.querySelector(".library-drag-preview")).toBeNull();
    expect(document.body.dataset.libraryPointerDrag).toBeUndefined();
    expect(wheel(100).defaultPrevented).toBe(false);
    expect(panel.scrollTop).toBe(132);
  });

  it("starts auto scrolling 90 pixels from the edge and lets the wheel override it", () => {
    start(240, 510);
    tick(16);
    expect(panel.scrollTop).toBeGreaterThan(0);
    wheel(-120);
    const afterWheel = panel.scrollTop;
    tick(100);
    expect(panel.scrollTop).toBe(afterWheel);
    tick(240);
    expect(panel.scrollTop).toBeGreaterThan(afterWheel);
    pointer("pointermove", document, 250, 300);
    const center = panel.scrollTop;
    tick(280);
    expect(panel.scrollTop).toBe(center);
  });

  it("updates the drop target under a stationary pointer after scrolling", () => {
    start();
    const next = document.createElement("div");
    panel.append(next);
    next.addEventListener("drop", () => events.push("next-drop"));
    hit = next;
    wheel(120);
    expect(events).toContain("leave");
    pointer("pointerup", document, 250, 300);
    expect(events).toContain("next-drop");
    expect(events).not.toContain("drop");
  });

  it("cancels on Escape without dropping or closing the folder", () => {
    start();
    const bubbled = vi.fn();
    surface.addEventListener("keydown", bubbled);
    const key = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
    act(() => { document.dispatchEvent(key); });
    expect(key.defaultPrevented).toBe(true);
    expect(bubbled).not.toHaveBeenCalled();
    expect(events.at(-1)).toBe("end");
    expect(events).not.toContain("drop");
    pointer("pointerup", document, 250, 300);
    expect(events).not.toContain("drop");
    expect(frames.size).toBe(0);
  });

  it.each(["pointercancel", "blur"])("cleans up a %s without changing the library", type => {
    start();
    act(() => { if (type === "blur") window.dispatchEvent(new Event(type)); });
    if (type === "pointercancel") pointer(type, document, 250, 300);
    expect(events.at(-1)).toBe("end");
    expect(events).not.toContain("drop");
    expect(document.querySelector(".library-drag-preview")).toBeNull();
  });

  it("keeps clicks, checkboxes and action buttons outside the drag gesture", () => {
    pointer("pointerdown", source.querySelector(".rename")!, 100, 300);
    pointer("pointermove", document, 120, 300);
    pointer("pointerup", document, 120, 300);
    pointer("pointerdown", source.querySelector("input")!, 100, 300);
    pointer("pointermove", document, 120, 300);
    pointer("pointerup", document, 120, 300);
    pointer("pointerdown", source, 100, 300);
    pointer("pointermove", document, 102, 302);
    pointer("pointerup", document, 102, 302);
    expect(events).toEqual([]);
    expect(wheel(120).defaultPrevented).toBe(false);
  });

  it("respects a rejected drag and cleans up when the library becomes inactive", async () => {
    await act(async () => root.render(<Harness reject />));
    start();
    expect(document.querySelector(".library-drag-preview")).toBeNull();
    expect(events).not.toContain("drop");
    await act(async () => root.render(<Harness />));
    start();
    expect(document.querySelector(".library-drag-preview")).not.toBeNull();
    await act(async () => root.render(<Harness enabled={false} />));
    expect(document.querySelector(".library-drag-preview")).toBeNull();
    expect(frames.size).toBe(0);
    expect(wheel(120).defaultPrevented).toBe(false);
  });
});
