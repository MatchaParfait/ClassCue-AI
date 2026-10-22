import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { ChevronDown, ChevronUp } from "lucide-react";
import { matchesShortcut, useShortcuts } from "../shortcuts";
import { lockModalEnvironment } from "../hooks/modalEnvironment";
import { OnboardingMascot, type OnboardingMascotPose } from "./OnboardingMascot";

const focusable = 'button:not([disabled]), a[href], input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])';
export function visibleTourElements(selector: string) {
  return [...document.querySelectorAll<HTMLElement>(selector)].filter(element =>
    !element.closest("[hidden], [inert]") && getComputedStyle(element).display !== "none"
    && getComputedStyle(element).visibility !== "hidden");
}
type Box = { left: number; top: number; width: number; height: number };
type Layout = { hole: Box | null; tip: Box; docked: boolean };
const sameLayout = (a: Layout, b: Layout) => JSON.stringify(a) === JSON.stringify(b);
function targetBox(selector: string, origin = { left: 0, top: 0 }): Box | null {
  const boxes = visibleTourElements(selector).map(element => element.getBoundingClientRect()).filter(box => box.width > 0 && box.height > 0);
  if (!boxes.length) return null;
  return {
    left: Math.min(...boxes.map(box => box.left)) - origin.left, top: Math.min(...boxes.map(box => box.top)) - origin.top,
    width: Math.max(...boxes.map(box => box.right)) - Math.min(...boxes.map(box => box.left)),
    height: Math.max(...boxes.map(box => box.bottom)) - Math.min(...boxes.map(box => box.top)),
  };
}
function viewportTop() {
  const header = document.querySelector<HTMLElement>(".workbench-topbar");
  return Math.max(16, header?.getBoundingClientRect().bottom ?? 80) + 16;
}

export function spotlightLayout(target: Box | null, width: number, height: number, tipHeight: number, preferredWidth = 344): Layout {
  const gap = 16, edge = Math.min(12, width / 4, height / 4), tipWidth = Math.min(preferredWidth, width - edge * 2);
  tipHeight = Math.min(tipHeight, height - edge * 2);
  const tip: Box = { left: Math.max(edge, width - tipWidth - edge), top: Math.max(edge, height - tipHeight - edge), width: tipWidth, height: tipHeight };
  if (!target) return { hole: null, tip, docked: true };
  const left = Math.max(4, target.left - 5), top = Math.max(4, target.top - 5);
  const right = Math.min(width - 4, target.left + target.width + 5);
  const bottom = Math.min(height - 4, target.top + target.height + 5);
  if (right <= left || bottom <= top) return { hole: null, tip, docked: true };
  target = { left, top, width: right - left, height: bottom - top };
  let docked = false;
  if (target.left >= tipWidth + gap + edge) {
    tip.left = target.left - tipWidth - gap;
    tip.top = Math.max(edge, Math.min(target.top, height - tipHeight - edge));
  } else if (target.left + target.width + gap + tipWidth <= width - edge) {
    tip.left = target.left + target.width + gap;
    tip.top = Math.max(edge, Math.min(target.top, height - tipHeight - edge));
  } else if (target.top + target.height + gap + tipHeight <= height - edge) {
    tip.left = Math.max(edge, Math.min(target.left, width - tipWidth - edge));
    tip.top = target.top + target.height + gap;
  } else if (target.top - gap - tipHeight >= edge) {
    tip.left = Math.max(edge, Math.min(target.left, width - tipWidth - edge));
    tip.top = target.top - gap - tipHeight;
  } else docked = true;
  const visibleBottom = Math.min(docked ? tip.top - gap : height - 4, bottom);
  return { hole: visibleBottom > top ? { left, top, width: right - left, height: visibleBottom - top } : null, tip, docked };
}

export function OnboardingSpotlight({ selector, phase, mascot, children }: {
  selector: string; phase: string; mascot?: OnboardingMascotPose; children: ReactNode;
}) {
  const overlay = useRef<HTMLDivElement>(null);
  const tip = useRef<HTMLElement>(null);
  const refresh = useRef<() => void>();
  const shortcuts = useShortcuts();
  const [layout, setLayout] = useState<Layout>(() => spotlightLayout(null, window.innerWidth, window.innerHeight, 240));
  const [nested, setNested] = useState(false);
  const [targetBusy, setTargetBusy] = useState(false);
  const [paging, setPaging] = useState({ previous: false, next: false });
  const layoutRef = useRef(layout);
  layoutRef.current = layout;
  const findDialog = () => visibleTourElements('[role="dialog"][aria-modal="true"]').at(-1);
  const selectorRef = useRef(selector);
  selectorRef.current = selector;
  const preferredWidth = (width: number, height: number) => mascot && width >= 760 && height >= 560 ? 464 : 344;
  const viewport = () => {
    const rect = overlay.current?.getBoundingClientRect();
    return {
      left: rect?.left ?? 0, top: rect?.top ?? 0,
      width: rect?.width || window.innerWidth, height: rect?.height || window.innerHeight,
    };
  };
  useLayoutEffect(() => lockModalEnvironment(), []);
  const revealControl = (control: HTMLElement, availableBottom?: number) => {
    if (control.closest(".workbench-topbar")) return;
    const rect = control.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const top = viewportTop(), tipRect = tip.current?.getBoundingClientRect();
    const bottom = availableBottom ?? (tipRect && rect.left < tipRect.right && rect.right > tipRect.left
      ? tipRect.top - 16 : window.innerHeight - 16);
    if (rect.top < top || rect.height > bottom - top) {
      window.scrollTo({ top: Math.max(0, window.scrollY + rect.top - top), behavior: "instant" });
    } else if (rect.bottom > bottom) {
      window.scrollTo({ top: window.scrollY + rect.bottom - bottom, behavior: "instant" });
    }
  };
  const revealRef = useRef(revealControl);
  revealRef.current = revealControl;
  useLayoutEffect(() => {
    let frame = 0;
    let positionNeeded = true;
    const observed = new Set<Element>();
    const resize = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(entries => {
      const focused = document.activeElement;
      const editingTarget = focused instanceof HTMLElement && visibleTourElements(selector).some(element => element.contains(focused));
      schedule(editingTarget || entries.some(entry => entry.target !== tip.current));
    });
    const observeElements = (targets: HTMLElement[]) => {
      const next = new Set<Element>([...targets, ...[overlay.current, tip.current, document.querySelector(".workbench-topbar")].filter((element): element is Element => Boolean(element))]);
      for (const element of observed) if (!next.has(element)) { resize?.unobserve(element); observed.delete(element); }
      for (const element of next) if (!observed.has(element)) { resize?.observe(element); observed.add(element); }
    };
    const measure = () => {
      frame = 0;
      const bounds = viewport();
      const targets = visibleTourElements(selector);
      observeElements(targets);
      let target = targetBox(selector, bounds);
      const dialog = findDialog();
      const headerTarget = Boolean(targets[0]?.closest(".workbench-topbar"));
      if (target && positionNeeded && !dialog) {
        positionNeeded = false;
        const focused = document.activeElement;
        if (focused instanceof HTMLElement && targets.some(element => element.contains(focused)) && !headerTarget) {
          const placement = spotlightLayout(target, bounds.width, bounds.height, tip.current?.getBoundingClientRect().height || 240,
            preferredWidth(bounds.width, bounds.height));
          revealRef.current(focused, bounds.top + (placement.docked ? placement.tip.top - 16 : bounds.height - 16));
        } else if (!headerTarget) {
          const top = viewportTop() - bounds.top;
          if (target.top < top || target.top + target.height > bounds.height - 180) {
            window.scrollTo({ top: Math.max(0, window.scrollY + target.top - top), behavior: "instant" });
          }
        }
        // Scrolling changes client rects immediately; never draw using the pre-scroll measurement.
        target = targetBox(selector, bounds);
      }
      const next = spotlightLayout(target, bounds.width, bounds.height, tip.current?.getBoundingClientRect().height || 240,
        preferredWidth(bounds.width, bounds.height));
      if (next.hole && !headerTarget) {
        const bottom = next.hole.top + next.hole.height;
        next.hole.top = Math.max(next.hole.top, viewportTop() - bounds.top - 8);
        next.hole.height = Math.max(0, bottom - next.hole.top);
        if (!next.hole.height) next.hole = null;
      }
      setLayout(current => sameLayout(current, next) ? current : next);
      setNested(Boolean(dialog));
      setTargetBusy(targets.some(element => element.matches('[aria-busy="true"]') || element.querySelector('[aria-busy="true"]')));
      const nextPaging = {
        previous: Boolean(target && !headerTarget && target.top < viewportTop() - bounds.top - 8),
        next: Boolean(target && !headerTarget && target.top + target.height > (next.docked ? next.tip.top - 16 : bounds.height - 16) + 8),
      };
      setPaging(current => current.previous === nextPaging.previous && current.next === nextPaging.next ? current : nextPaging);
    };
    const schedule = (reposition = false) => {
      positionNeeded ||= reposition;
      if (!frame) frame = requestAnimationFrame(measure);
    };
    refresh.current = () => schedule();
    const observer = new MutationObserver(() => schedule());
    observer.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["hidden", "class", "aria-modal", "aria-busy"] });
    const onResize = () => schedule(true);
    const onScroll = () => schedule();
    window.addEventListener("resize", onResize);
    window.addEventListener("scroll", onScroll, true);
    const visualViewport = window.visualViewport;
    visualViewport?.addEventListener("resize", onResize);
    visualViewport?.addEventListener("scroll", onResize);
    const timer = window.setInterval(onScroll, 250);
    measure();
    return () => {
      refresh.current = undefined;
      observer.disconnect(); resize?.disconnect(); cancelAnimationFrame(frame); window.clearInterval(timer);
      window.removeEventListener("resize", onResize); window.removeEventListener("scroll", onScroll, true);
      visualViewport?.removeEventListener("resize", onResize);
      visualViewport?.removeEventListener("scroll", onResize);
    };
  }, [selector, phase, Boolean(mascot)]);
  useLayoutEffect(() => { refresh.current?.(); }, [layout.tip.width, paging.previous, paging.next]);
  useEffect(() => {
    const timer = window.setTimeout(() => {
      const focused = document.activeElement;
      const editingTarget = focused instanceof HTMLElement && visibleTourElements(selector).some(element => element.contains(focused));
      if (!findDialog() && !editingTarget) tip.current?.focus({ preventScroll: true });
    }, 60);
    return () => window.clearTimeout(timer);
  }, [selector, phase]);
  useEffect(() => {
    const permitted = (node: EventTarget | null) => {
      if (!(node instanceof Node)) return false;
      const dialog = findDialog();
      if (dialog) return dialog.contains(node);
      return Boolean(tip.current?.contains(node))
        || visibleTourElements(selectorRef.current).some(element => element.contains(node));
    };
    const block = (event: Event) => {
      if (!permitted(event.target)) { event.preventDefault(); event.stopImmediatePropagation(); }
    };
    const focus = (event: FocusEvent) => {
      if (!permitted(event.target)) {
        const dialog = findDialog();
        const destination = dialog?.querySelector<HTMLElement>(focusable) ?? dialog ?? tip.current;
        destination?.focus({ preventScroll: true });
      } else if (!findDialog() && event.target instanceof HTMLElement && !tip.current?.contains(event.target)) {
        revealRef.current(event.target);
      }
    };
    const blockScroll = (event: Event) => {
      const node = event.target;
      if (node instanceof Node && (findDialog()?.contains(node) || tip.current?.contains(node))) return;
      event.preventDefault(); event.stopImmediatePropagation();
    };
    const key = (event: KeyboardEvent) => {
      if (event.isComposing) return;
      const recorder = event.target instanceof Element && event.target.closest("[data-shortcut-recorder]");
      if (!recorder && (matchesShortcut(event, shortcuts, "search")
        || event.altKey && ["ArrowLeft", "ArrowRight"].includes(event.key))) {
        event.preventDefault(); event.stopImmediatePropagation(); return;
      }
      if (findDialog()) return;
      const control = event.target instanceof Element ? event.target : null;
      const editing = control?.closest('input, textarea, select, [contenteditable]:not([contenteditable="false"]), [role="combobox"], [role="slider"]');
      const activation = [" ", "Spacebar"].includes(event.key) && control?.closest('button, summary, [role="button"]');
      if (!recorder && !editing && !activation && !tip.current?.contains(control)
        && ["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "PageUp", "PageDown", "Home", "End", " ", "Spacebar"].includes(event.key)) {
        event.preventDefault(); event.stopImmediatePropagation(); return;
      }
      if (event.key === "Escape" && !recorder) { event.preventDefault(); event.stopImmediatePropagation(); }
      if (event.key !== "Tab") return;
      const areas = [...visibleTourElements(selectorRef.current), ...(tip.current ? [tip.current] : [])];
      const controls = [...new Set(areas.flatMap(area => [
        ...(area.matches(focusable) ? [area] : []), ...area.querySelectorAll<HTMLElement>(focusable),
      ]))].filter(element => !element.closest("[hidden], [inert]") && getComputedStyle(element).display !== "none");
      event.preventDefault();
      const index = controls.indexOf(document.activeElement as HTMLElement);
      const next = index < 0 ? (event.shiftKey ? controls.length - 1 : 0)
        : (index + (event.shiftKey ? -1 : 1) + controls.length) % controls.length;
      const destination = controls[next] ?? tip.current;
      destination?.focus({ preventScroll: true });
    };
    document.body.setAttribute("data-onboarding-active", "true");
    for (const type of ["pointerdown", "click", "dblclick", "contextmenu", "drop", "dragover"]) document.addEventListener(type, block, true);
    document.addEventListener("focusin", focus, true);
    document.addEventListener("keydown", key, true);
    document.addEventListener("wheel", blockScroll, { capture: true, passive: false });
    document.addEventListener("touchmove", blockScroll, { capture: true, passive: false });
    return () => {
      document.body.removeAttribute("data-onboarding-active");
      for (const type of ["pointerdown", "click", "dblclick", "contextmenu", "drop", "dragover"]) document.removeEventListener(type, block, true);
      document.removeEventListener("focusin", focus, true); document.removeEventListener("keydown", key, true);
      document.removeEventListener("wheel", blockScroll, true); document.removeEventListener("touchmove", blockScroll, true);
    };
  }, [shortcuts]);
  const moveRegion = (direction: -1 | 1) => {
    const bounds = viewport();
    const target = targetBox(selectorRef.current, bounds);
    if (!target) return;
    const top = viewportTop() - bounds.top, current = layoutRef.current;
    const bottom = current.docked ? current.tip.top - 16 : bounds.height - 16;
    const amount = Math.max(64, (bottom - top) * 0.85);
    const delta = direction > 0 ? Math.min(amount, target.top + target.height - bottom)
      : Math.max(-amount, target.top - top);
    window.scrollTo({ top: Math.max(0, window.scrollY + delta), behavior: "instant" });
  };
  const hole = layout.hole;
  const direction = hole && hole.left + hole.width <= layout.tip.left ? "left"
    : hole && hole.left >= layout.tip.left + layout.tip.width ? "right" : null;
  const pose = mascot && targetBusy ? "reading" : mascot;
  return createPortal(<div ref={overlay} className={`onboarding-spotlight${nested ? " has-nested-dialog" : ""}`}>
    <div className="onboarding-mask" aria-hidden="true" style={hole ? {
      clipPath: `polygon(evenodd, 0 0, 100% 0, 100% 100%, 0 100%, 0 0, ${hole.left}px ${hole.top}px, ${hole.left}px ${hole.top + hole.height}px, ${hole.left + hole.width}px ${hole.top + hole.height}px, ${hole.left + hole.width}px ${hole.top}px, ${hole.left}px ${hole.top}px)`,
    } : undefined} />
    {hole && <div className="onboarding-highlight" aria-hidden="true" style={hole} />}
    <section className={`onboarding-tip${mascot ? " has-mascot" : ""}`}
      ref={tip} role="region" aria-labelledby="onboarding-title"
      aria-describedby="onboarding-description" tabIndex={-1} style={{ left: layout.tip.left, top: layout.tip.top, width: layout.tip.width }}>
      <div className="onboarding-tip-body" data-mascot-side={direction === "left" ? "left" : "right"}>
        {pose && <OnboardingMascot pose={pose} direction={direction} />}
        <div className="onboarding-tip-content">
          {children}
          {(paging.previous || paging.next) && <div className="onboarding-region-controls">
            <button className="icon-button" type="button" title="上一个操作区域" aria-label="上一个操作区域"
              disabled={!paging.previous} onClick={() => moveRegion(-1)}><ChevronUp size={16} /></button>
            <button className="icon-button" type="button" title="下一个操作区域" aria-label="下一个操作区域"
              disabled={!paging.next} onClick={() => moveRegion(1)}><ChevronDown size={16} /></button>
          </div>}
        </div>
      </div>
    </section>
  </div>, document.body);
}
