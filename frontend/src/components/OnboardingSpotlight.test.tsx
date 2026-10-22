import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { OnboardingSpotlight, spotlightLayout } from "./OnboardingSpotlight";

type Rect = { left: number; top: number; width: number; height: number };
const rect = (value: Rect): DOMRect => ({
  ...value, x: value.left, y: value.top, right: value.left + value.width, bottom: value.top + value.height, toJSON: () => value,
});
let host: HTMLDivElement, root: Root;
let target: Rect, offset: { left: number; top: number };
let observers: FakeResizeObserver[];
let viewportEvents: EventTarget;

class FakeResizeObserver {
  elements = new Set<Element>();
  constructor(private callback: ResizeObserverCallback) { observers.push(this); }
  observe(element: Element) { this.elements.add(element); }
  unobserve(element: Element) { this.elements.delete(element); }
  disconnect() { this.elements.clear(); }
  notify(element: Element) {
    if (this.elements.has(element)) this.callback([{ target: element } as ResizeObserverEntry], this as unknown as ResizeObserver);
  }
}
beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  observers = [];
  offset = { left: 0, top: 0 };
  target = { left: 500, top: 140, width: 500, height: 120 };
  vi.stubGlobal("innerWidth", 1200); vi.stubGlobal("innerHeight", 900); vi.stubGlobal("scrollY", 0);
  vi.stubGlobal("ResizeObserver", FakeResizeObserver);
  viewportEvents = new EventTarget();
  vi.stubGlobal("visualViewport", viewportEvents);
  vi.spyOn(window, "scrollTo").mockImplementation((options: ScrollToOptions | number) => {
    if (typeof options === "object") vi.stubGlobal("scrollY", options.top ?? 0);
  });
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    if (this.classList.contains("onboarding-spotlight")) {
      return rect({ ...offset, width: window.innerWidth, height: window.innerHeight });
    }
    if (this.classList.contains("workbench-topbar")) return rect({ left: 0, top: 0, width: window.innerWidth, height: 64 });
    if (this.classList.contains("resize-target")) return rect({ ...target, top: target.top - window.scrollY });
    if (this.classList.contains("onboarding-tip")) return rect({
      left: parseFloat(this.style.left) || 0, top: parseFloat(this.style.top) || 0,
      width: parseFloat(this.style.width) || 344, height: window.innerWidth < 600 ? 300 : 160,
    });
    if (this.matches("input")) return rect({ left: target.left, top: target.top + target.height - 40 - window.scrollY, width: 120, height: 32 });
    return rect({ left: 0, top: 0, width: 0, height: 0 });
  });
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals();
});
async function mount(showTarget = true) {
  await act(async () => root.render(<>
    <header className="workbench-topbar" />
    {showTarget && <section className="resize-target"><input aria-label="Target setting" /></section>}
    <OnboardingSpotlight selector=".resize-target" phase="resize">
      <h2 id="onboarding-title">Settings</h2><p id="onboarding-description">Confirm settings</p>
    </OnboardingSpotlight>
  </>));
}
async function settle(check: () => void) {
  await vi.waitFor(async () => {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 10)); });
    check();
  });
}
const highlight = () => document.querySelector<HTMLElement>(".onboarding-highlight");
const tip = () => document.querySelector<HTMLElement>(".onboarding-tip")!;
async function resize(width: number, height: number) {
  vi.stubGlobal("innerWidth", width); vi.stubGlobal("innerHeight", height);
  await act(async () => window.dispatchEvent(new Event("resize")));
}
function assertAligned() {
  const box = highlight()!.getAttribute("style")!;
  expect(box).toContain(`left: ${Math.max(4, target.left - offset.left - 5)}px`);
  const frame = highlight()!;
  const top = parseFloat(frame.style.top), height = parseFloat(frame.style.height);
  expect(top).toBeGreaterThanOrEqual(72 - offset.top);
  expect(height).toBeGreaterThan(0);
  expect(top + height).toBeLessThanOrEqual(window.innerHeight - 4);
  expect(parseFloat(tip().style.left)).toBeGreaterThanOrEqual(12);
  expect(parseFloat(tip().style.left) + parseFloat(tip().style.width)).toBeLessThanOrEqual(window.innerWidth - 12);
  expect(parseFloat(tip().style.top) + tip().getBoundingClientRect().height).toBeLessThanOrEqual(window.innerHeight - 12);
}

it("realigns the cutout after repeated desktop/narrow resize events and remeasures after automatic scrolling", async () => {
  await mount();
  await settle(assertAligned);
  target = { left: 12, top: 900, width: 351, height: 1200 };
  await resize(420, 700); await resize(390, 680); await resize(375, 667);
  await settle(() => {
    expect(window.scrollY).toBe(820);
    expect(parseFloat(highlight()!.style.top)).toBe(75);
    expect(parseFloat(tip().style.top)).toBe(355);
    assertAligned();
  });
  target = { left: 500, top: 140, width: 500, height: 120 };
  await resize(1200, 900);
  await settle(() => {
    expect(window.scrollY).toBe(60);
    expect(parseFloat(highlight()!.style.top)).toBe(75);
    assertAligned();
  });
  expect(document.body.style.overflow).toBe("hidden");
});

it("updates cutout coordinates relative to the overlay and uses fractional rendered heights", async () => {
  offset = { left: 10, top: 20 };
  target = { left: 510.5, top: 160.25, width: 500.5, height: 120.25 };
  await mount();
  await settle(() => {
    expect(highlight()!.style.left).toBe("495.5px");
    expect(highlight()!.style.top).toBe("135.25px");
    expect(highlight()!.style.width).toBe("510.5px");
    expect(highlight()!.style.height).toBe("130.25px");
  });
});

it("observes a target that mounts late and repositions it again on container reflow without a window resize", async () => {
  await mount(false);
  expect(highlight()).toBeNull();
  target = { left: 500, top: 900, width: 500, height: 120 };
  await mount(true);
  const element = document.querySelector(".resize-target")!;
  await settle(() => expect(observers.some(observer => observer.elements.has(element))).toBe(true));
  await settle(() => expect(window.scrollY).toBe(820));
  target = { ...target, top: 1500 };
  await act(async () => observers.forEach(observer => observer.notify(element)));
  await settle(() => {
    expect(window.scrollY).toBe(1420);
    expect(highlight()!.style.top).toBe("75px");
  });
});

it("preserves the focused setting when the window shrinks and visual viewport changes", async () => {
  target = { left: 500, top: 140, width: 500, height: 600 };
  await mount();
  await act(async () => document.querySelector<HTMLInputElement>("input")!.focus({ preventScroll: true }));
  target = { left: 12, top: 140, width: 351, height: 1000 };
  vi.stubGlobal("innerWidth", 375); vi.stubGlobal("innerHeight", 667);
  await act(async () => viewportEvents.dispatchEvent(new Event("resize")));
  await settle(() => {
    const focused = document.activeElement as HTMLElement;
    expect(focused.matches("input")).toBe(true);
    const focusBox = focused.getBoundingClientRect();
    expect(focusBox.top).toBeGreaterThanOrEqual(80);
    expect(focusBox.bottom).toBeLessThanOrEqual(parseFloat(tip().style.top) - 16);
    assertAligned();
  });
});

it("does not reset controlled region navigation during normal scroll measurements", async () => {
  target = { left: 12, top: 140, width: 351, height: 1600 };
  await resize(375, 667); await mount();
  await settle(() => expect(document.querySelector('[aria-label="下一个操作区域"]')).not.toBeNull());
  await act(async () => document.querySelector<HTMLButtonElement>('[aria-label="下一个操作区域"]')!.click());
  const position = window.scrollY;
  await act(async () => window.dispatchEvent(new Event("scroll")));
  await settle(() => expect(parseFloat(highlight()!.style.top)).toBe(72));
  expect(window.scrollY).toBe(position);
});

it.each([
  { left: -900, top: 100, width: 100, height: 100 },
  { left: 900, top: 100, width: 100, height: 100 },
  { left: 100, top: -900, width: 100, height: 100 },
  { left: 100, top: 900, width: 100, height: 100 },
])("never positions the tip outside the viewport for an offscreen target %j", target => {
  const layout = spotlightLayout(target, 375, 667, 240);
  expect(layout.hole).toBeNull();
  expect(layout.tip.left).toBeGreaterThanOrEqual(12);
  expect(layout.tip.top).toBeGreaterThanOrEqual(12);
  expect(layout.tip.left + layout.tip.width).toBeLessThanOrEqual(363);
  expect(layout.tip.top + layout.tip.height).toBeLessThanOrEqual(655);
});
