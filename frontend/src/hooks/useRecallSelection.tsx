import { createContext, useCallback, useContext, useEffect, useMemo, useSyncExternalStore } from "react";
import type { RecallMode } from "../types";
import { getRecallSelection, saveRecallSelection, type RecallSelection } from "../api/client";

export const RECALL_SELECTION_KEY = "classroom.recall-selection.v1";
export const RecallSelectionScope = createContext("default");
const CHANGE = "classroom-recall-selection-changed";
const fallback = new Map<string, string>();
type SharedSelection = { raw: string; revision: number; loading: boolean; saving: boolean; error?: string; wanted?: string };
const shared = new Map<string, SharedSelection>();
const generations = new Map<string, number>();

function nextGeneration(key: string) {
  const value = (generations.get(key) ?? 0) + 1;
  generations.set(key, value);
  return value;
}

function notify() {
  window.dispatchEvent(new Event(CHANGE));
}

function cacheSelection(key: string, raw: string) {
  try { window.localStorage.setItem(key, raw); fallback.delete(key); }
  catch { fallback.set(key, raw); }
}

function checkLibrary(value: RecallSelection, libraryId: string) {
  if (value.library_id !== libraryId) throw new Error("视频库身份已变化，请重新读取处理方案");
}

async function loadShared(key: string, libraryId: string) {
  if (shared.get(key)?.saving) return;
  const generation = nextGeneration(key);
  try {
    let value = await getRecallSelection();
    if (generations.get(key) !== generation) return;
    checkLibrary(value, libraryId);
    if (!value.initialized) {
      const legacy = fallback.get(key) ?? (() => {
        try { return window.localStorage.getItem(key); } catch { return null; }
      })();
      try {
        value = await saveRecallSelection(libraryId, decodeSelection(legacy), 0);
      } catch (error) {
        // Another window may have initialized it first; never overwrite that choice.
        value = await getRecallSelection();
        checkLibrary(value, libraryId);
        if (!value.initialized) throw error;
      }
    }
    checkLibrary(value, libraryId);
    if (generations.get(key) !== generation || shared.get(key)?.saving
      || value.revision < (shared.get(key)?.revision ?? 0)) return;
    const raw = JSON.stringify(value.analysis_modes);
    cacheSelection(key, raw);
    shared.set(key, { raw, revision: value.revision, loading: false, saving: false });
  } catch (error) {
    if (generations.get(key) !== generation) return;
    const current = shared.get(key)!;
    shared.set(key, { ...current, loading: false, error: error instanceof Error ? error.message : "处理方案读取失败" });
  }
  notify();
}

async function saveShared(key: string, libraryId: string, raw: string) {
  nextGeneration(key);
  const previous = shared.get(key)!;
  shared.set(key, { ...previous, raw, wanted: raw, saving: true, error: undefined });
  notify();
  if (previous.saving) return;
  while (shared.get(key)?.wanted) {
    const current = shared.get(key)!;
    const wanted = current.wanted!;
    try {
      const value = await saveRecallSelection(libraryId, JSON.parse(wanted), current.revision);
      checkLibrary(value, libraryId);
      const latest = shared.get(key)!;
      shared.set(key, { ...latest, revision: value.revision, loading: false, error: undefined,
        saving: latest.wanted !== wanted, wanted: latest.wanted === wanted ? undefined : latest.wanted });
      cacheSelection(key, JSON.stringify(value.analysis_modes));
    } catch (error) {
      shared.set(key, { ...shared.get(key)!, saving: false, wanted: undefined,
        error: error instanceof Error ? error.message : "处理方案保存失败" });
      notify();
      return;
    }
    notify();
  }
}

export function RecallSelectionSync({ libraryId }: { libraryId: string }) {
  const key = `${RECALL_SELECTION_KEY}:${encodeURIComponent(libraryId)}`;
  if (!shared.has(key)) shared.set(key, { raw: "[]", revision: 0, loading: true, saving: false });
  const state = useSyncExternalStore(subscribe, () => shared.get(key)!);
  useEffect(() => {
    void loadShared(key, libraryId);
    const refresh = () => {
      if (!document.hidden && !shared.get(key)?.error) void loadShared(key, libraryId);
    };
    const timer = window.setInterval(refresh, 15_000);
    window.addEventListener("focus", refresh);
    return () => { window.clearInterval(timer); window.removeEventListener("focus", refresh); };
  }, [key, libraryId]);
  if (state.error) return <p className="field-error" role="alert">处理方案同步失败：{state.error}
    <button type="button" className="text-button" onClick={() => void loadShared(key, libraryId)}>重新读取</button></p>;
  if (state.loading || state.saving) return <p role="status">{state.loading ? "正在读取处理方案…" : "正在保存处理方案…"}</p>;
  return null;
}

function decodeSelection(raw: string | null): RecallMode[] {
  if (raw !== null) {
    try {
      const value: unknown = JSON.parse(raw);
      if (Array.isArray(value) && value.length <= 8 && value.every(mode => typeof mode === "string"
        && (/^(high|medium|low)_recall$/.test(mode) || mode === "question_extraction"
          || /^custom_[a-f0-9]{32}$/.test(mode)))) {
        return [...new Set(value)] as RecallMode[];
      }
    } catch { /* Invalid preferences use the first-use default. */ }
  }
  return ["medium_recall"];
}

function readSelection(key: string) {
  if (shared.has(key)) return shared.get(key)!.raw;
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
    if (shared.has(key)) void saveShared(key, scope, value);
    else { cacheSelection(key, value); notify(); }
  }, [key, scope]);
  return [modes, save];
}
