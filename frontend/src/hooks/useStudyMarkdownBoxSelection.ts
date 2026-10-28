import { useEffect, useRef, useState, type PointerEvent, type RefObject } from "react";

type Point = { x: number; y: number };
type Box = { left: number; top: number; width: number; height: number };
type Gesture = { pointerId: number; start: Point; client: Point; dragged: boolean };

export function useStudyMarkdownBoxSelection({ enabled, sourceKey, surface, onSelect }: {
  enabled: boolean; sourceKey: string; surface: RefObject<HTMLDivElement>; onSelect: (ids: string[]) => void;
}) {
  const [box, setBox] = useState<Box | null>(null);
  const [targetIds, setTargetIds] = useState<string[]>([]);
  const gesture = useRef<Gesture | null>(null);
  const frame = useRef(0);
  const select = useRef(onSelect);
  select.current = onSelect;

  function reset() {
    cancelAnimationFrame(frame.current);
    frame.current = 0;
    const current = gesture.current;
    gesture.current = null;
    if (current && surface.current?.hasPointerCapture?.(current.pointerId)) surface.current.releasePointerCapture(current.pointerId);
    setBox(null); setTargetIds([]);
  }
  function calculate() {
    const node = surface.current, current = gesture.current;
    if (!node || !current) return [];
    const bounds = node.getBoundingClientRect();
    const end = { x: Math.max(0, Math.min(bounds.width, current.client.x - bounds.left)),
      y: Math.max(0, Math.min(bounds.height, current.client.y - bounds.top)) };
    const next = { left: Math.min(current.start.x, end.x), top: Math.min(current.start.y, end.y),
      width: Math.abs(current.start.x - end.x), height: Math.abs(current.start.y - end.y) };
    if (!current.dragged && (next.width < 4 || next.height < 4)) return [];
    current.dragged = true;
    const ids = [...node.querySelectorAll<HTMLElement>(".study-markdown-block")].filter(block => {
      const rect = block.getBoundingClientRect();
      return rect.right > bounds.left + next.left && rect.left < bounds.left + next.left + next.width
        && rect.bottom > bounds.top + next.top && rect.top < bounds.top + next.top + next.height;
    }).map(block => block.dataset.studyBlock!);
    setBox(next); setTargetIds(ids);
    return ids;
  }
  function autoScroll() {
    const current = gesture.current, node = surface.current;
    if (!current || !node) return;
    const panel = node.closest<HTMLElement>(".study-reader-scroll");
    if (panel && panel.clientHeight > 0 && current.dragged) {
      const bounds = panel.getBoundingClientRect();
      const y = current.client.y;
      const speed = y < bounds.top + 36 ? -Math.min(18, (bounds.top + 36 - y) / 3)
        : y > bounds.bottom - 36 ? Math.min(18, (y - bounds.bottom + 36) / 3) : 0;
      if (speed && current.client.x >= bounds.left && current.client.x <= bounds.right) {
        const before = panel.scrollTop;
        panel.scrollTop += speed;
        if (before !== panel.scrollTop) calculate();
      }
    }
    frame.current = requestAnimationFrame(autoScroll);
  }
  useEffect(() => {
    if (!enabled) reset();
    const key = (event: KeyboardEvent) => {
      if (event.key === "Escape" && gesture.current) { event.preventDefault(); reset(); }
    };
    window.addEventListener("keydown", key);
    return () => {
      window.removeEventListener("keydown", key);
      cancelAnimationFrame(frame.current);
      gesture.current = null;
    };
  }, [enabled, sourceKey]);

  return {
    box, targetIds,
    onPointerDown(event: PointerEvent<HTMLDivElement>) {
      const target = event.target as HTMLElement;
      if (!enabled || event.button !== 0 || !event.isPrimary || gesture.current
          || target.closest("input,button,a,.study-block-check,.study-source-page")) return;
      const bounds = event.currentTarget.getBoundingClientRect();
      gesture.current = { pointerId: event.pointerId, start: { x: event.clientX - bounds.left, y: event.clientY - bounds.top },
        client: { x: event.clientX, y: event.clientY }, dragged: false };
      event.currentTarget.setPointerCapture(event.pointerId);
      event.preventDefault();
      frame.current = requestAnimationFrame(autoScroll);
    },
    onPointerMove(event: PointerEvent<HTMLDivElement>) {
      if (gesture.current?.pointerId !== event.pointerId) return;
      gesture.current.client = { x: event.clientX, y: event.clientY };
      calculate();
    },
    onPointerUp(event: PointerEvent<HTMLDivElement>) {
      if (gesture.current?.pointerId !== event.pointerId) return;
      gesture.current.client = { x: event.clientX, y: event.clientY };
      const ids = calculate();
      if (gesture.current?.dragged && ids.length) select.current(ids);
      reset();
    },
    onPointerCancel() { reset(); },
    onLostPointerCapture() { if (gesture.current) reset(); },
  };
}
