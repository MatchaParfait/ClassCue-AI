import { useEffect, type RefObject } from "react";

type Gesture = {
  pointerId: number;
  source: HTMLElement;
  startX: number;
  startY: number;
  x: number;
  y: number;
  transfer: DataTransfer | null;
  target: Element | null;
  preview: HTMLElement | null;
  lastWheel: number;
};

// Keep the library's drop handlers, but avoid the native drag loop that swallows wheel input.
export function useLibraryPointerDrag(surface: RefObject<HTMLElement>, enabled: boolean) {
  useEffect(() => {
    const container = surface.current;
    if (!enabled || !container) return;
    let gesture: Gesture | null = null;
    let frame = 0, previousTime = 0, blockClickUntil = 0;

    function emit(type: string, target: Element, relatedTarget: Element | null = null) {
      const current = gesture;
      if (!current?.transfer) return;
      return target.dispatchEvent(new DragEvent(type, {
        bubbles: true, cancelable: true, clientX: current.x, clientY: current.y,
        dataTransfer: current.transfer, relatedTarget,
      }));
    }

    function updateTarget() {
      const current = gesture;
      if (!current?.transfer) return;
      const hit = document.elementFromPoint(current.x, current.y);
      const target = hit && container!.contains(hit) ? hit : null;
      if (target !== current.target) {
        if (current.target) emit("dragleave", current.target, target);
        if (target) emit("dragenter", target, current.target);
        current.target = target;
      }
      if (target) emit("dragover", target);
    }

    function scrollPanel() {
      if (!gesture) return null;
      const dialog = container!.querySelector<HTMLElement>(".folder-window");
      if (dialog) {
        const rect = dialog.getBoundingClientRect();
        if (gesture.x < rect.left || gesture.x > rect.right || gesture.y < rect.top || gesture.y > rect.bottom) return null;
        return dialog.querySelector<HTMLElement>(".folder-window-content");
      }
      const hit = document.elementFromPoint(gesture.x, gesture.y);
      return hit && container!.contains(hit) ? document.scrollingElement as HTMLElement | null : null;
    }

    function movePreview() {
      const current = gesture;
      if (!current?.preview) return;
      const preview = current.preview;
      const x = Math.max(8, Math.min(current.x + 16, window.innerWidth - preview.offsetWidth - 8));
      const y = Math.max(8, Math.min(current.y + 16, window.innerHeight - preview.offsetHeight - 8));
      preview.style.transform = `translate3d(${x}px, ${y}px, 0)`;
    }

    function autoScroll(time: number) {
      if (!gesture?.transfer) return;
      const elapsed = previousTime ? Math.min(32, time - previousTime) : 16;
      previousTime = time;
      const panel = scrollPanel();
      if (panel && time - gesture.lastWheel > 200) {
        const root = panel === document.scrollingElement;
        const rect = root ? { top: 0, bottom: window.innerHeight, height: window.innerHeight } : panel.getBoundingClientRect();
        const band = Math.min(104, rect.height / 3);
        const y = gesture.y;
        const proximity = y >= rect.top && y < rect.top + band ? -(rect.top + band - y) / band
          : y <= rect.bottom && y > rect.bottom - band ? (y - rect.bottom + band) / band : 0;
        if (proximity) {
          const before = panel.scrollTop;
          panel.scrollTop += proximity * 900 * elapsed / 1000;
          if (panel.scrollTop !== before) updateTarget();
        }
      }
      movePreview();
      frame = requestAnimationFrame(autoScroll);
    }

    function finish(drop: boolean) {
      const current = gesture;
      if (!current) return;
      if (current.transfer) {
        if (drop) {
          updateTarget();
          if (current.target) emit("drop", current.target);
        }
        emit("dragend", current.source);
        blockClickUntil = Date.now() + 250;
      }
      gesture = null;
      cancelAnimationFrame(frame);
      frame = 0;
      previousTime = 0;
      current.preview?.remove();
      delete document.body.dataset.libraryPointerDrag;
      if (container!.hasPointerCapture?.(current.pointerId)) container!.releasePointerCapture(current.pointerId);
    }

    function down(event: PointerEvent) {
      if (event.button !== 0 || event.pointerType === "touch" || event.defaultPrevented || gesture) return;
      if (!(event.target instanceof Element) || event.target.closest("[inert], [hidden]")) return;
      const source = event.target.closest<HTMLElement>(".library-card[data-library-key]");
      if (!source || !container!.contains(source) || !source.draggable) return;
      const control = event.target.closest("button, a, input, select, textarea, label, [contenteditable], [role='menu']");
      if (control && !control.matches(".folder-card-link, .library-card-link, .library-thumbnail-link")) return;
      gesture = { pointerId: event.pointerId, source, startX: event.clientX, startY: event.clientY,
        x: event.clientX, y: event.clientY, transfer: null, target: null, preview: null, lastWheel: -Infinity };
    }

    function move(event: PointerEvent) {
      const current = gesture;
      if (!current || current.pointerId !== event.pointerId) return;
      if (!(event.buttons & 1)) { finish(false); return; }
      current.x = event.clientX;
      current.y = event.clientY;
      if (!current.transfer) {
        if (Math.hypot(current.x - current.startX, current.y - current.startY) < 6) return;
        current.transfer = new DataTransfer();
        current.transfer.effectAllowed = "move";
        const accepted = emit("dragstart", current.source);
        if (!accepted || !current.source.isConnected) { finish(false); return; }
        window.getSelection()?.removeAllRanges();
        container!.setPointerCapture?.(current.pointerId);
        document.body.dataset.libraryPointerDrag = "true";
        const preview = document.createElement("div");
        preview.className = "library-drag-preview";
        preview.setAttribute("aria-hidden", "true");
        preview.inert = true;
        const visual = current.source.querySelector(".library-thumbnail-link img, .folder-icon");
        if (visual) preview.append(visual.cloneNode(true));
        const title = document.createElement("span");
        title.textContent = current.source.querySelector(".library-card-body strong")?.textContent ?? "";
        preview.append(title);
        document.body.append(preview);
        current.preview = preview;
        frame = requestAnimationFrame(autoScroll);
      }
      event.preventDefault();
      movePreview();
      updateTarget();
    }

    function up(event: PointerEvent) {
      if (gesture?.pointerId !== event.pointerId) return;
      gesture.x = event.clientX; gesture.y = event.clientY;
      finish(true);
    }
    function cancel(event: PointerEvent) {
      if (gesture?.pointerId === event.pointerId) finish(false);
    }
    function key(event: KeyboardEvent) {
      if (!gesture?.transfer || event.key !== "Escape") return;
      event.preventDefault();
      event.stopImmediatePropagation();
      finish(false);
    }
    function click(event: MouseEvent) {
      if (Date.now() >= blockClickUntil) return;
      event.preventDefault();
      event.stopImmediatePropagation();
    }
    function preventNativeDrag(event: DragEvent) {
      if (!gesture || event.dataTransfer === gesture.transfer) return;
      event.preventDefault();
      event.stopImmediatePropagation();
    }
    function wheel(event: WheelEvent) {
      if (!gesture?.transfer || event.ctrlKey || event.defaultPrevented) return;
      const panel = scrollPanel();
      if (!panel) return;
      event.preventDefault();
      const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? panel.clientHeight : 1;
      panel.scrollTop += event.deltaY * unit;
      gesture.lastWheel = performance.now();
      updateTarget();
    }
    const blur = () => finish(false);
    const visibility = () => { if (document.hidden) finish(false); };
    const scroll = () => { if (gesture?.transfer) updateTarget(); };

    container.addEventListener("pointerdown", down);
    document.addEventListener("pointermove", move, { passive: false });
    document.addEventListener("pointerup", up);
    document.addEventListener("pointercancel", cancel);
    document.addEventListener("dragstart", preventNativeDrag, true);
    document.addEventListener("keydown", key, true);
    document.addEventListener("click", click, true);
    document.addEventListener("scroll", scroll, true);
    document.addEventListener("visibilitychange", visibility);
    window.addEventListener("wheel", wheel, { passive: false });
    window.addEventListener("blur", blur);
    return () => {
      finish(false);
      container.removeEventListener("pointerdown", down);
      document.removeEventListener("pointermove", move);
      document.removeEventListener("pointerup", up);
      document.removeEventListener("pointercancel", cancel);
      document.removeEventListener("dragstart", preventNativeDrag, true);
      document.removeEventListener("keydown", key, true);
      document.removeEventListener("click", click, true);
      document.removeEventListener("scroll", scroll, true);
      document.removeEventListener("visibilitychange", visibility);
      window.removeEventListener("wheel", wheel);
      window.removeEventListener("blur", blur);
    };
  }, [surface, enabled]);
}
