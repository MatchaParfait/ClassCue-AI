import { createContext, useCallback, useContext, useMemo, useSyncExternalStore } from "react";
import type { RecallMode } from "../types";

export const RECALL_SELECTION_KEY = "classroom.recall-selection.v1";
export const RecallSelectionScope = createContext("default");
const CHANGE = "classroom-recall-selection-changed";
const fallback = new Map<string, string>();

function decodeSelection(raw: string | null): RecallMode[] {
  if (raw !== null) {
    try {
      const value: unknown = JSON.parse(raw);
      if (Array.isArray(value) && value.length <= 8 && value.every(mode => typeof mode === "string"
        && (/^(high|medium|low)_recall$/.test(mode) || /^custom_[a-f0-9]{32}$/.test(mode)))) {
        return [...new Set(value)] as RecallMode[];
      }
    } catch { /* Invalid preferences use the first-use default. */ }
  }
  return ["medium_recall"];
}

function readSelection(key: string) {
  if (fallback.has(key)) return fallback.get(key)!;
  try { return window.localStorage.getItem(key); }
  catch { return fallback.get(key) ?? null; }
}

function subscribe(callback: () => void) {
  const onStorage = (event: StorageEvent) => {
    if (event.key === null || event.key.startsWith(`${RECALL_SELECTION_KEY}:`)) {
      if (event.key === null) fallback.clear();
      else fallback.delete(event.key);
      callback();
    }
  };
  window.addEventListener(CHANGE, callback);
  window.addEventListener("storage", onStorage);
  return () => {
    window.removeEventListener(CHANGE, callback);
    window.removeEventListener("storage", onStorage);
  };
}

export function useRecallSelection(): readonly [RecallMode[], (modes: RecallMode[]) => void] {
  const scope = useContext(RecallSelectionScope);
  const key = `${RECALL_SELECTION_KEY}:${encodeURIComponent(scope)}`;
  const read = useCallback(() => readSelection(key), [key]);
  const raw = useSyncExternalStore(subscribe, read, () => null);
  const modes = useMemo(() => decodeSelection(raw), [raw]);
  const save = useCallback((selection: RecallMode[]) => {
    const value = JSON.stringify(selection);
    try {
      window.localStorage.setItem(key, value);
      fallback.delete(key);
    } catch {
      // Storage restrictions must not prevent selecting modes in this window.
      fallback.set(key, value);
    }
    window.dispatchEvent(new Event(CHANGE));
  }, [key]);
  return [modes, save];
}
