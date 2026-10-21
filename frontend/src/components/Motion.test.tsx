import { act, StrictMode, useRef, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { beforeEach, afterEach, expect, it, vi } from "vitest";
import { AnimatePresence, AnimatedListItem, AnimatedPopup, Collapse, useEntryMotion } from "./Motion";
import { WorkbenchPanel } from "./WorkbenchPanel";
import { waitForExit } from "../test/waitForExit";

let container: HTMLDivElement;
let root: Root;
beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn(), addListener: vi.fn() })));
  vi.spyOn(window, "scrollTo").mockImplementation(() => {});
  container = document.createElement("div"); document.body.append(container); root = createRoot(container);
});
afterEach(() => { act(() => root.unmount()); container.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
const click = (label: string) => act(() => {
  const button = container.querySelector<HTMLButtonElement>(`[aria-label="${label}"]`)!;
  button.focus(); button.click();
});
const tick = async (duration = 30) => act(async () => { await new Promise((resolve) => setTimeout(resolve, duration)); });

it("makes an exiting menu inert and removes it when the animation finishes", async () => {
  function Menu() {
    const [open, setOpen] = useState(true);
    return <><button aria-label="toggle" onClick={() => setOpen(!open)} /><AnimatePresence>{open && <AnimatedPopup className="popup"><button>Action</button></AnimatedPopup>}</AnimatePresence></>;
  }
  act(() => root.render(<Menu />));
  await tick(); click("toggle");
  expect(container.querySelector<HTMLElement>(".popup")!.inert).toBe(true);
  expect(container.querySelector(".popup")!.getAttribute("aria-hidden")).toBe("true");
  await waitForExit(container, ".popup");
});

it("keeps a reopened drawer interactive after the interrupted exit would have completed", async () => {
  function Drawer() {
    const background = useRef<HTMLDivElement>(null);
    const [open, setOpen] = useState(false);
    return <><div ref={background}><button aria-label="open" onClick={() => setOpen(true)} /></div>
      <WorkbenchPanel active={open} title="Test" scrollKey="test" backgroundRef={background} onClose={() => setOpen(false)}><input defaultValue="draft" /></WorkbenchPanel></>;
  }
  act(() => root.render(<Drawer />));
  click("open"); await tick(); click("关闭面板"); click("open"); await tick(350);
  const backdrop = container.querySelector<HTMLElement>(".workbench-panel-backdrop")!;
  expect(backdrop.inert).toBe(false); expect(backdrop.hidden).toBe(false);
  expect(container.querySelector("input")!.value).toBe("draft");
  click("关闭面板");
  await vi.waitFor(async () => { await tick(); expect(backdrop.hidden).toBe(true); });
  expect(document.activeElement).toBe(container.querySelector('[aria-label="open"]'));
});

it("restores a removed list item without leaving an inert duplicate", async () => {
  function List() {
    const [shown, setShown] = useState(true);
    return <><button aria-label="restore" onClick={() => setShown(true)} /><AnimatePresence initial={false}>
      {shown && <AnimatedListItem key="marker-1" className="item"><button aria-label="remove" onClick={() => setShown(false)}>考点</button></AnimatedListItem>}
    </AnimatePresence></>;
  }
  act(() => root.render(<List />)); click("remove");
  expect(container.querySelector<HTMLElement>(".item")!.inert).toBe(true);
  click("restore"); await tick(300);
  expect(container.querySelectorAll(".item")).toHaveLength(1);
  expect(container.querySelector<HTMLElement>(".item")!.inert).toBe(false);
  click("remove"); await waitForExit(container, ".item");
});

it("closes a collapsed form immediately when reduced motion is enabled", () => {
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() })));
  act(() => root.render(<Collapse open><input defaultValue="saved choice" /></Collapse>));
  expect(container.querySelector("input")!.value).toBe("saved choice");
  act(() => root.render(<Collapse open={false}><input defaultValue="saved choice" /></Collapse>));
  expect(container.querySelector("input")).toBeNull();
});

it("cancels interrupted entry motion, preserves its form, and never replays for data updates", () => {
  const cancel = vi.fn();
  const animate = vi.fn(() => ({ cancel }));
  const prior = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "animate");
  Object.defineProperty(HTMLElement.prototype, "animate", { configurable: true, value: animate });
  function Screen({ active, direction, data }: { active: boolean; direction: number; data: string }) {
    const surface = useRef<HTMLDivElement>(null);
    useEntryMotion(surface, { active, axis: "x", distance: direction * 6 });
    return <div ref={surface} hidden={!active}><input defaultValue="saved draft" /><span>{data}</span></div>;
  }
  try {
    act(() => root.render(<Screen active direction={1} data="first" />));
    const field = container.querySelector("input")!;
    field.value = "unsaved edit";
    field.focus();
    expect(animate).toHaveBeenCalledTimes(1);
    act(() => root.render(<Screen active direction={-1} data="refreshed" />));
    expect(animate).toHaveBeenCalledTimes(1);
    expect(document.activeElement).toBe(field);
    act(() => root.render(<Screen active={false} direction={-1} data="refreshed" />));
    expect(cancel).toHaveBeenCalledTimes(1);
    act(() => root.render(<Screen active direction={-1} data="refreshed" />));
    expect(animate).toHaveBeenCalledTimes(2);
    expect(animate.mock.calls[1]).toEqual([
      [{ opacity: .72, transform: "translateX(-6px)" }, { opacity: 1, transform: "none" }],
      expect.objectContaining({ duration: 160 }),
    ]);
    expect(container.querySelector("input")).toBe(field);
    expect(field.value).toBe("unsaved edit");
  } finally {
    act(() => root.render(null));
    if (prior) Object.defineProperty(HTMLElement.prototype, "animate", prior);
    else delete (HTMLElement.prototype as Partial<HTMLElement>).animate;
  }
});

it("cancels entry motion when reduced motion changes without replaying it on preference changes", () => {
  let reduced = false;
  const listeners = new Set<() => void>();
  vi.stubGlobal("matchMedia", () => ({
    get matches() { return reduced; },
    addEventListener: (_event: string, listener: () => void) => listeners.add(listener),
    removeEventListener: (_event: string, listener: () => void) => listeners.delete(listener),
  }));
  const cancel = vi.fn();
  const animate = vi.fn(() => ({ cancel }));
  const prior = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "animate");
  Object.defineProperty(HTMLElement.prototype, "animate", { configurable: true, value: animate });
  function Screen({ active }: { active: boolean }) {
    const surface = useRef<HTMLDivElement>(null);
    useEntryMotion(surface, { active, distance: 6 });
    return <div ref={surface} hidden={!active}><button>Ready immediately</button></div>;
  }
  try {
    act(() => root.render(<Screen active />));
    expect(animate).toHaveBeenCalledTimes(1);
    act(() => { reduced = true; listeners.forEach(listener => listener()); });
    expect(cancel).toHaveBeenCalledTimes(1);
    act(() => root.render(<Screen active={false} />));
    act(() => root.render(<Screen active />));
    expect(animate).toHaveBeenCalledTimes(1);
    expect(container.querySelector("button")!.disabled).toBe(false);
    act(() => { reduced = false; listeners.forEach(listener => listener()); });
    expect(animate).toHaveBeenCalledTimes(1);
  } finally {
    act(() => root.render(null));
    if (prior) Object.defineProperty(HTMLElement.prototype, "animate", prior);
    else delete (HTMLElement.prototype as Partial<HTMLElement>).animate;
  }
});

it("keeps an entry running after Strict Mode's mount cleanup", () => {
  const runs: { playState: string; cancel: () => void }[] = [];
  const animate = vi.fn(() => {
    const run = { playState: "running", cancel: () => { run.playState = "idle"; } };
    runs.push(run);
    return run;
  });
  const prior = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "animate");
  Object.defineProperty(HTMLElement.prototype, "animate", { configurable: true, value: animate });
  function Screen() {
    const surface = useRef<HTMLDivElement>(null);
    useEntryMotion(surface);
    return <div ref={surface}><input defaultValue="preserved" /></div>;
  }
  try {
    act(() => root.render(<StrictMode><Screen /></StrictMode>));
    expect(runs).toHaveLength(2);
    expect(runs[0].playState).toBe("idle");
    expect(runs[1].playState).toBe("running");
    expect(container.querySelectorAll("input")).toHaveLength(1);
  } finally {
    act(() => root.render(null));
    if (prior) Object.defineProperty(HTMLElement.prototype, "animate", prior);
    else delete (HTMLElement.prototype as Partial<HTMLElement>).animate;
  }
});
