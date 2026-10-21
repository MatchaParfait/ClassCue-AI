import { useCallback, useEffect, useRef, type PointerEvent } from "react";

export function usePanelTilt(enabled: boolean) {
  const ref = useRef<HTMLDivElement>(null);
  const frame = useRef(0);
  const target = useRef({ x: 0, y: 0 });
  const reset = useCallback(() => {
    cancelAnimationFrame(frame.current);
    frame.current = 0;
    ref.current?.style.removeProperty("--panel-rx");
    ref.current?.style.removeProperty("--panel-ry");
    ref.current?.removeAttribute("data-hovered");
  }, []);
  useEffect(() => {
    if (!enabled) reset();
    return reset;
  }, [enabled, reset]);
  const onPointerMove = useCallback((event: PointerEvent<HTMLDivElement>) => {
    if (!enabled || event.pointerType === "touch") return;
    // Measure the untransformed shell, not the moving face, to avoid feedback jitter.
    const bounds = event.currentTarget.getBoundingClientRect();
    if (!bounds.width || !bounds.height) return;
    target.current = {
      x: Math.max(-1, Math.min(1, (event.clientX - bounds.left) / bounds.width * 2 - 1)),
      y: Math.max(-1, Math.min(1, (event.clientY - bounds.top) / bounds.height * 2 - 1)),
    };
    if (frame.current) return;
    frame.current = requestAnimationFrame(() => {
      frame.current = 0;
      if (!ref.current) return;
      ref.current.style.setProperty("--panel-rx", `${-target.current.y * 2.2}deg`);
      ref.current.style.setProperty("--panel-ry", `${target.current.x * 2.8}deg`);
      ref.current.dataset.hovered = "true";
    });
  }, [enabled]);
  return { ref, onPointerMove, onPointerLeave: reset, onPointerCancel: reset };
}
