import { useSyncExternalStore } from "react";

export const DRAG_TEXT_HINTS_KEY = "classroom.library.drag-text-hints.v1";
const CHANGE = "library-drag-text-hints-change";

export function readDragTextHints(): boolean {
  try { return window.localStorage.getItem(DRAG_TEXT_HINTS_KEY) !== "false"; }
  catch { return true; }
}

export function saveDragTextHints(enabled: boolean): boolean {
  try {
    window.localStorage.setItem(DRAG_TEXT_HINTS_KEY, String(enabled));
    window.dispatchEvent(new Event(CHANGE));
    return true;
  } catch { return false; }
}

function subscribe(callback: () => void) {
  const storage = (event: StorageEvent) => {
    if (event.key === DRAG_TEXT_HINTS_KEY || event.key === null) callback();
  };
  window.addEventListener(CHANGE, callback);
  window.addEventListener("storage", storage);
  return () => {
    window.removeEventListener(CHANGE, callback);
    window.removeEventListener("storage", storage);
  };
}

export function useDragTextHints() {
  return useSyncExternalStore(subscribe, readDragTextHints, () => true);
}
