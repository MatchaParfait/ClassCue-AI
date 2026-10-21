import { AnimatePresence, motion, useAnimationControls, useIsPresent, type HTMLMotionProps } from "motion/react";
import { forwardRef, useEffect, useId, useImperativeHandle, useLayoutEffect, useRef, useState, useSyncExternalStore, type ReactNode, type RefObject } from "react";

export const motionTiming = { fast: .14, page: .16, preview: .12, item: .18, collapse: .2, list: .22, panel: .24 };
const ease = [.2, .7, .2, 1] as const;
const reducedQuery = "(prefers-reduced-motion: reduce)";
function subscribeReduced(listener: () => void) {
  const media = window.matchMedia?.(reducedQuery);
  media?.addEventListener?.("change", listener);
  return () => media?.removeEventListener?.("change", listener);
}
export function useReducedMotion() {
  return useSyncExternalStore(subscribeReduced, () => window.matchMedia?.(reducedQuery).matches ?? false, () => false);
}
export function useMotionTransition(duration = motionTiming.item) {
  const reduced = useReducedMotion();
  return { duration: reduced ? 0 : duration, ease };
}

export { AnimatePresence, motion, useIsPresent };

// Animate an existing surface on activation, never on query updates or typing.
// Cancellation restores its normal styles immediately; no animation gates input,
// routing, mounting or scroll restoration. Targeted transforms keep fixed-position
// backgrounds, dialogs and the video frame out of moving containing blocks.
export function useEntryMotion(ref: RefObject<HTMLElement>, {
  active = true, changeKey = "", initial = true, duration = motionTiming.page,
  delay = 0, axis = "y", distance = 0, selector, fade = true,
}: {
  active?: boolean; changeKey?: string; initial?: boolean; duration?: number;
  delay?: number; axis?: "x" | "y"; distance?: number; selector?: string; fade?: boolean;
} = {}) {
  const reduced = useReducedMotion();
  const previous = useRef<{ active: boolean; changeKey: string; animations: Animation[] } | null>(null);
  useLayoutEffect(() => {
    const before = previous.current;
    const current = { active, changeKey, animations: [] as Animation[] };
    previous.current = current;
    // Strict Mode performs setup → cleanup → setup on mount. Restart the
    // cancelled run there, rather than silently removing its first entry.
    const interrupted = before?.animations.some(animation => animation.playState === "idle");
    const entering = active && (before ? !before.active || before.changeKey !== changeKey || interrupted : initial);
    if (!entering || reduced || !ref.current) return;
    const nodes = selector ? [...ref.current.querySelectorAll<HTMLElement>(selector)] : [ref.current];
    const animations = nodes.filter(node => !node.closest("[hidden]") && typeof node.animate === "function").map(node =>
      node.animate([
        { ...(fade ? { opacity: .72 } : {}), ...(distance ? { transform: `translate${axis.toUpperCase()}(${distance}px)` } : {}) },
        { ...(fade ? { opacity: 1 } : {}), ...(distance ? { transform: "none" } : {}) },
      ], { duration: duration * 1000, delay: delay * 1000, easing: "cubic-bezier(.2,.7,.2,1)", fill: "backwards" }));
    current.animations = animations;
    return () => animations.forEach(animation => animation.cancel());
    // Read visual options only at entry; a new direction or callback during an
    // active screen must not restart it. Reduced-motion changes still cancel it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ref, active, changeKey, reduced]);
}

// Exiting surfaces remain drawable, but cannot receive input or be read twice.
export const AnimatedPopup = forwardRef<HTMLDivElement, HTMLMotionProps<"div"> & { distance?: number; duration?: number; exitDuration?: number }>(
  function AnimatedPopup({ children, className = "", distance = 4, duration = motionTiming.fast, exitDuration = duration, ...props }, ref) {
    const present = useIsPresent();
    const element = useRef<HTMLDivElement>(null);
    useImperativeHandle(ref, () => element.current!);
    useLayoutEffect(() => { if (element.current) element.current.inert = !present; }, [present]);
    const reduced = useReducedMotion();
    const transition = useMotionTransition(duration);
    return <motion.div {...props} className={`motion-popup ${className}`} ref={element}
      data-exiting={!present || undefined} aria-hidden={!present || props["aria-hidden"]}
      initial={{ opacity: 0, y: reduced ? 0 : distance }} animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: reduced ? 0 : distance, transition: { ...transition, duration: reduced ? 0 : exitDuration } }} transition={transition}>{children}</motion.div>;
  },
);

export function AnimatedOverlay({ children, className = "", duration = motionTiming.item, exitDuration = duration, ...props }: HTMLMotionProps<"div"> & { duration?: number; exitDuration?: number }) {
  const present = useIsPresent();
  const element = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => { if (element.current) element.current.inert = !present; }, [present]);
  const transition = useMotionTransition(duration);
  return <motion.div {...props} className={`motion-overlay ${className}`} initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0, transition: { ...transition, duration: transition.duration ? exitDuration : 0 } }} transition={transition}
    ref={element} data-exiting={!present || undefined} aria-hidden={!present || undefined}>
    {children}
  </motion.div>;
}

export function Collapse({ open, children }: { open: boolean; children: ReactNode }) {
  const transition = useMotionTransition(motionTiming.collapse);
  if (transition.duration === 0) return open ? <div className="motion-collapse">{children}</div> : null;
  return <AnimatePresence initial={false}>{open && <CollapsingContent transition={transition}>{children}</CollapsingContent>}</AnimatePresence>;
}
function CollapsingContent({ children, transition }: { children: ReactNode; transition: ReturnType<typeof useMotionTransition> }) {
  const present = useIsPresent();
  const element = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => { if (element.current) element.current.inert = !present; }, [present]);
  return <motion.div className="motion-collapse" initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }}
    style={{ overflow: "clip" }} transition={transition} aria-hidden={!present || undefined} ref={element}>{children}</motion.div>;
}

export function SelectionIndicator({ id, line = false }: { id: string; line?: boolean }) {
  const transition = useMotionTransition();
  return <motion.span aria-hidden="true" className={`selection-indicator${line ? " is-line" : ""}`} layoutId={id} transition={transition} />;
}

// Fade on a user-controlled section change without remounting forms or resetting scroll.
export function FadeRegion({ changeKey, children, className = "" }: { changeKey: string; children: ReactNode; className?: string }) {
  const transition = useMotionTransition(motionTiming.fast);
  const controls = useAnimationControls();
  const previous = useRef(changeKey);
  useEffect(() => {
    if (previous.current === changeKey) return;
    previous.current = changeKey;
    controls.set({ opacity: transition.duration ? 0 : 1 });
    void controls.start({ opacity: 1, transition });
  }, [changeKey, controls, transition.duration]);
  return <motion.div className={className} initial={false} animate={controls}>{children}</motion.div>;
}

// A stable scope prevents layoutId collisions between retained screens and dialogs.
export function useSelectionScope() { return useId(); }

export function AnimatedListItem({ children, className, ...props }: HTMLMotionProps<"article">) {
  const present = useIsPresent();
  const element = useRef<HTMLElement>(null);
  useLayoutEffect(() => { if (element.current) element.current.inert = !present; }, [present]);
  const transition = useMotionTransition();
  const reduced = useReducedMotion();
  return <motion.article {...props} className={`motion-item ${className ?? ""}`} layout={reduced ? false : "position"}
    initial={reduced ? false : { opacity: 0, height: 0 }} animate={{ opacity: 1, height: "auto",
      paddingTop: "var(--motion-item-padding, 0px)", paddingBottom: "var(--motion-item-padding, 0px)",
      borderBottomWidth: "var(--motion-item-border, 0px)" }}
    exit={{ opacity: 0, height: 0, paddingTop: 0, paddingBottom: 0, borderBottomWidth: 0 }} transition={transition}
    data-exiting={!present || undefined} aria-hidden={!present || undefined} ref={element} style={{ overflow: "clip" }}>{children}</motion.article>;
}

export function AnimatedQuote({ expanded, id, children }: { expanded: boolean; id: string; children: ReactNode }) {
  const text = useRef<HTMLParagraphElement>(null);
  const [height, setHeight] = useState<number | "auto">("auto");
  const transition = useMotionTransition(motionTiming.collapse);
  useLayoutEffect(() => {
    const node = text.current;
    if (!node) return;
    const update = () => setHeight(expanded ? node.scrollHeight : Math.min(node.scrollHeight, parseFloat(getComputedStyle(node).lineHeight) * 2 || node.scrollHeight));
    update();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(update); observer.observe(node);
    return () => observer.disconnect();
  }, [expanded, children]);
  return <motion.div className="animated-quote" initial={false} animate={{ height }} transition={transition} style={{ overflow: "clip" }}>
    <p id={id} ref={text} className="marker-quote">{children}</p>
  </motion.div>;
}

// Only new business IDs enter. Mounting/refetching an existing collection never replays it.
export function useNewItems(ids: readonly string[], ready: boolean, scope = "default") {
  const seen = useRef<Set<string> | null>(null);
  const previousScope = useRef(scope);
  if (previousScope.current !== scope) { previousScope.current = scope; seen.current = null; }
  const fresh = seen.current && ready ? new Set(ids.filter((id) => !seen.current!.has(id))) : new Set<string>();
  useEffect(() => { if (ready) seen.current = new Set(ids); }, [ready, ids.join("|")]);
  return fresh;
}

export function spotlight(event: React.PointerEvent<HTMLElement>) {
  if (event.pointerType === "touch" || event.currentTarget.closest('[data-dragging="true"]') || window.matchMedia?.(reducedQuery).matches) return;
  const card = event.currentTarget;
  const bounds = card.getBoundingClientRect();
  card.style.setProperty("--spot-x", `${event.clientX - bounds.left}px`);
  card.style.setProperty("--spot-y", `${event.clientY - bounds.top}px`);
  card.dataset.spotlight = "true";
}
