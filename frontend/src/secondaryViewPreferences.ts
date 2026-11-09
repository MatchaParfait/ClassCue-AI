import { useSyncExternalStore } from "react";

export const AUTO_ALIGN_SECONDARY_KEY = "classroom.secondary-view.auto-align-on-import.v1";
const CHANGE = "secondary-view-auto-align-change";

export function readAutoAlignSecondary(): boolean {
  try { return window.localStorage.getItem(AUTO_ALIGN_SECONDARY_KEY) === "true"; }
  catch { return false; }
}

export function saveAutoAlignSecondary(enabled: boolean): boolean {
  try {
    window.localStorage.setItem(AUTO_ALIGN_SECONDARY_KEY, String(enabled));
    window.dispatchEvent(new Event(CHANGE));
    return true;
  } catch { return false; }
}

function subscribe(callback: () => void) {
  const storage = (event: StorageEvent) => {
    if (event.key === AUTO_ALIGN_SECONDARY_KEY || event.key === null) callback();
  };
  window.addEventListener(CHANGE, callback);
  window.addEventListener("storage", storage);
  return () => {
    window.removeEventListener(CHANGE, callback);
    window.removeEventListener("storage", storage);
  };
}

export function useAutoAlignSecondary() {
  return useSyncExternalStore(subscribe, readAutoAlignSecondary, () => false);
}
