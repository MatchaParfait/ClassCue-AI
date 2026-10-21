import { useEffect, useRef, type RefObject } from "react";

type Gaze = { visible: boolean; move: (x: number, y: number) => void };
const eyes = new Map<Element, Gaze>();
let stopTracking: (() => void) | undefined;
let observeEye: ((element: Element) => void) | undefined;
let unobserveEye: ((element: Element) => void) | undefined;

// All decorative eyes share one listener and at most one frame per pointer event.
function startTracking() {
  const preference = window.matchMedia("(hover: hover) and (pointer: fine) and (prefers-reduced-motion: no-preference)");
  let listening = false, frame: number | null = null;
  let pointer: { x: number; y: number } | null = null;
  const reset = () => {
    if (frame !== null) cancelAnimationFrame(frame);
    frame = null; pointer = null;
    eyes.forEach(eye => eye.move(0, 0));
  };
  const update = () => {
    frame = null;
    if (!pointer || document.hidden) { reset(); return; }
    eyes.forEach((eye, element) => {
      if (!eye.visible || element.closest("[hidden], [inert]")) { eye.move(0, 0); return; }
      const bounds = element.getBoundingClientRect();
      if (!bounds.width || !bounds.height) { eye.move(0, 0); return; }
      const dx = pointer!.x - bounds.left - bounds.width / 2;
      const dy = pointer!.y - bounds.top - bounds.height / 2;
      const distance = Math.max(160, Math.hypot(dx, dy));
      eye.move(dx / distance, dy / distance);
    });
  };
  const schedule = () => { if (pointer && frame === null) frame = requestAnimationFrame(update); };
  const onPointer = (event: PointerEvent) => {
    if (event.pointerType !== "mouse") return;
    pointer = { x: event.clientX, y: event.clientY }; schedule();
  };
  const synchronize = () => {
    const enabled = preference.matches && document.documentElement.dataset.uiTheme === "character"
      && [...eyes.values()].some(eye => eye.visible);
    if (enabled === listening) return;
    listening = enabled;
    if (enabled) {
      window.addEventListener("pointermove", onPointer, { passive: true });
      window.addEventListener("blur", reset);
      window.addEventListener("resize", schedule, { passive: true });
      window.addEventListener("scroll", schedule, { capture: true, passive: true });
      document.documentElement.addEventListener("pointerleave", reset);
      document.addEventListener("visibilitychange", reset);
    } else {
      window.removeEventListener("pointermove", onPointer);
      window.removeEventListener("blur", reset);
      window.removeEventListener("resize", schedule);
      window.removeEventListener("scroll", schedule, true);
      document.documentElement.removeEventListener("pointerleave", reset);
      document.removeEventListener("visibilitychange", reset);
      reset();
    }
  };
  const observer = typeof IntersectionObserver === "undefined" ? null : new IntersectionObserver(entries => {
    for (const entry of entries) {
      const eye = eyes.get(entry.target);
      if (!eye) continue;
      eye.visible = entry.isIntersecting;
      if (!eye.visible) eye.move(0, 0);
    }
    synchronize(); schedule();
  });
  observeEye = element => { observer?.observe(element); synchronize(); schedule(); };
  unobserveEye = element => { observer?.unobserve(element); synchronize(); };
  const themeObserver = new MutationObserver(synchronize);
  themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ["data-ui-theme"] });
  preference.addEventListener?.("change", synchronize);
  stopTracking = () => {
    observer?.disconnect(); themeObserver.disconnect();
    preference.removeEventListener?.("change", synchronize);
    synchronize(); reset();
    stopTracking = observeEye = unobserveEye = undefined;
  };
}

/** Normalized, bounded gaze direction; callers choose a safe pupil travel distance. */
export function usePointerGaze<T extends Element>(eyeRef: RefObject<T>, move: (x: number, y: number) => void) {
  const moveRef = useRef(move);
  moveRef.current = move;
  useEffect(() => {
    const element = eyeRef.current;
    if (!element || !window.matchMedia) return;
    if (!stopTracking) startTracking();
    eyes.set(element, { visible: typeof IntersectionObserver === "undefined", move: (x, y) => moveRef.current(x, y) });
    observeEye?.(element);
    return () => {
      moveRef.current(0, 0);
      eyes.delete(element);
      unobserveEye?.(element);
      if (!eyes.size) stopTracking?.();
    };
  }, [eyeRef]);
}
