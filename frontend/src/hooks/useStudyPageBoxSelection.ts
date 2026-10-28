import { useEffect, useRef, useState, type PointerEvent, type RefObject } from "react";
import type { StudyBBox, StudyRegion, StudyTextBounds } from "../api/studyMaterials";

type Point = { x: number; y: number };
export type PageSelectionResize = { index: number; anchor: Point };
type Gesture = { pointer: number; block: string; start: Point; client: Point; alt: boolean; crossPage: boolean; replaceIndex?: number };
const clamp = (value: number) => Math.max(0, Math.min(1, value));

export function expandToTextWidth(box: StudyBBox, bounds: StudyTextBounds | undefined, pageWidth: number): StudyBBox {
  if (bounds?.status !== "ready" || !bounds.bbox) return box;
  const padding = 6 / pageWidth;
  return [Math.min(box[0], clamp(bounds.bbox[0] - padding)), box[1],
    Math.max(box[2], clamp(bounds.bbox[2] + padding)), box[3]];
}

export function useStudyPageBoxSelection({ enabled, sourceKey, surface, snap, onSelect, onError }: {
  enabled: boolean; sourceKey: string; surface: RefObject<HTMLDivElement>; snap: boolean;
  onSelect: (regions: StudyRegion[], snap: boolean, replaceIndex?: number) => Promise<void>;
  onError: (message: string) => void;
}) {
  const [regions, setRegions] = useState<StudyRegion[]>([]);
  const gesture = useRef<Gesture | null>(null), frame = useRef(0);
  const callbacks = useRef({ onSelect, onError, snap });
  callbacks.current = { onSelect, onError, snap };

  function reset() {
    cancelAnimationFrame(frame.current); frame.current = 0;
    const current = gesture.current; gesture.current = null;
    if (current && surface.current?.hasPointerCapture?.(current.pointer)) surface.current.releasePointerCapture(current.pointer);
    setRegions([]);
  }
  function calculate() {
    const current = gesture.current;
    if (!current || !surface.current) return [];
    const pages = [...surface.current.querySelectorAll<HTMLElement>("[data-page-block]")]
      .filter(node => current.crossPage ? node.dataset.crossPage === "true" : node.dataset.pageBlock === current.block)
      .map(node => ({ node, rect: node.getBoundingClientRect() }))
      .filter(page => page.rect.width > 0 && page.rect.height > 0);
    const start = pages.findIndex(page => page.node.dataset.pageBlock === current.block);
    if (start < 0) return [];
    let end = 0, distance = Infinity;
    pages.forEach((page, index) => {
      const gap = Math.max(page.rect.top - current.client.y, current.client.y - page.rect.bottom, 0);
      if (gap < distance) { distance = gap; end = index; }
    });
    const endPage = pages[end];
    const last = { x: clamp((current.client.x - endPage.rect.left) / endPage.rect.width),
      y: clamp((current.client.y - endPage.rect.top) / endPage.rect.height) };
    const startY = pages[start].rect.top + current.start.y * pages[start].rect.height;
    const endY = endPage.rect.top + last.y * endPage.rect.height;
    const left = Math.min(current.start.x, last.x), right = Math.max(current.start.x, last.x);
    const useSnap = callbacks.current.snap && !current.alt && current.replaceIndex === undefined;
    const hasText = pages.slice(Math.min(start, end), Math.max(start, end) + 1).some(page => page.node.dataset.textLeft !== undefined);
    if (Math.abs(startY - endY) < 4 || ((right - left) * endPage.rect.width < 4 && !(useSnap && hasText))) {
      setRegions([]); return [];
    }
    const forward = startY <= endY;
    const first = forward ? start : end, final = forward ? end : start;
    const top = forward ? current.start.y : last.y, bottom = forward ? last.y : current.start.y;
    const next: StudyRegion[] = [];
    for (let index = first; index <= final; index++) {
      const { node } = pages[index];
      const bbox: StudyBBox = [left, index === first ? top : 0, right, index === final ? bottom : 1];
      if (bbox[3] <= bbox[1]) continue;
      if (useSnap && node.dataset.textLeft !== undefined) {
        bbox[0] = Math.min(bbox[0], Number(node.dataset.textLeft));
        bbox[2] = Math.max(bbox[2], Number(node.dataset.textRight));
      }
      if (bbox[0] < bbox[2]) next.push({ block_id: node.dataset.pageBlock!, bbox });
    }
    setRegions(next);
    return next;
  }
  function autoScroll() {
    const current = gesture.current, panel = surface.current?.closest<HTMLElement>(".study-reader-scroll");
    if (!current) return;
    if (panel && panel.clientHeight > 0) {
      const rect = panel.getBoundingClientRect(), y = current.client.y;
      const speed = y < rect.top + 40 ? -Math.min(20, (rect.top + 40 - y) / 3)
        : y > rect.bottom - 40 ? Math.min(20, (y - rect.bottom + 40) / 3) : 0;
      if (speed) {
        const before = panel.scrollTop;
        panel.scrollTop += speed;
        if (panel.scrollTop !== before) calculate();
      }
    }
    frame.current = requestAnimationFrame(autoScroll);
  }
  useEffect(() => {
    reset();
    const key = (event: KeyboardEvent) => {
      if (!gesture.current) return;
      if (event.key === "Escape") { event.preventDefault(); reset(); }
      else if (event.key === "Alt") { gesture.current.alt = event.type === "keydown"; calculate(); }
    };
    window.addEventListener("keydown", key); window.addEventListener("keyup", key);
    return () => {
      window.removeEventListener("keydown", key); window.removeEventListener("keyup", key);
      cancelAnimationFrame(frame.current); gesture.current = null;
    };
  }, [enabled, sourceKey, snap]);

  return {
    regions,
    start(event: PointerEvent<HTMLDivElement>, block: string, resize?: PageSelectionResize) {
      if (!enabled || gesture.current || (!resize && event.button !== 0) || !event.isPrimary || !surface.current) return;
      const bounds = event.currentTarget.getBoundingClientRect();
      gesture.current = { pointer: event.pointerId, block, start: resize?.anchor || {
        x: clamp((event.clientX - bounds.left) / bounds.width), y: clamp((event.clientY - bounds.top) / bounds.height),
      }, client: { x: event.clientX, y: event.clientY }, alt: event.altKey,
        crossPage: event.currentTarget.dataset.crossPage === "true", replaceIndex: resize?.index };
      surface.current.setPointerCapture(event.pointerId);
      event.preventDefault();
      calculate();
      frame.current = requestAnimationFrame(autoScroll);
    },
    onPointerMove(event: PointerEvent<HTMLDivElement>) {
      if (gesture.current?.pointer !== event.pointerId) return;
      gesture.current.client = { x: event.clientX, y: event.clientY }; gesture.current.alt = event.altKey;
      calculate();
    },
    async onPointerUp(event: PointerEvent<HTMLDivElement>) {
      const current = gesture.current;
      if (!current || current.pointer !== event.pointerId) return;
      current.client = { x: event.clientX, y: event.clientY }; current.alt = event.altKey;
      const next = calculate(), useSnap = callbacks.current.snap && !current.alt && current.replaceIndex === undefined;
      // Release capture before awaiting metadata; a lost-capture event must not discard the submitted group.
      reset();
      if (!next.length) { callbacks.current.onError("选区太小，请重新框选"); return; }
      await callbacks.current.onSelect(next, useSnap, current.replaceIndex);
    },
    onPointerCancel: reset,
    onLostPointerCapture() { if (gesture.current) reset(); },
  };
}
