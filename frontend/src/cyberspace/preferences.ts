import { useRef, useSyncExternalStore } from "react";

export type CyberspacePreference = "off" | "dynamic";
export const CYBERSPACE_KEY = "classroom.experimental.subtitle-space.v1";
export type AnalysisQuality = "low" | "high";
export const ANALYSIS_QUALITY_KEY = "classroom.experimental.subtitle-space.quality.v1";
export const ANALYSIS_VARIANTS = [
  { id: "auto", name: "自动", description: "根据模型名称自动选择动画主题。" },
  { id: "gpt", name: "GPT", description: "银白紫角色与月白薰衣草光环，薄荷绿标记命中考点。" },
  { id: "dafeiyu-rice", name: "大肥鱼吃白饭", description: "无背景抱盆形象，沿用冰蓝银光与薄荷绿考点标记。" },
  { id: "dafeiyu31", name: "大肥鱼2", description: "圆形深海背景与角色近景填满中央，保留冰蓝光环及考点运镜。" },
  { id: "dafeiyu", name: "大肥鱼1", description: "保留角色原色，冰蓝银光环绕字幕，薄荷绿标记命中考点。" },
  { id: "nailong", name: "奶龙", description: "奶龙位于金色字幕环中央，随运镜保持正面，保留考点聚焦。" },
  { id: "gravity", name: "黑洞原版", description: "保留原有黑洞、金色字幕环与考点运镜。" },
] as const;
export type AnalysisVariant = typeof ANALYSIS_VARIANTS[number]["id"];
export type ResolvedAnalysisVariant = Exclude<AnalysisVariant, "auto">;
export const ANALYSIS_VARIANT_KEY = "classroom.experimental.subtitle-space.variant.v1";
const CHANGE = "subtitle-space-preference";

export function resolveAnalysisVariant(preference: AnalysisVariant, model: string | undefined, key: string): ResolvedAnalysisVariant {
  if (preference !== "auto") return preference;
  const name = (model ?? "").trim().toLowerCase();
  const candidates: readonly ResolvedAnalysisVariant[] = name.includes("deepseek")
    ? ["dafeiyu", "dafeiyu31", "dafeiyu-rice"]
    : name.includes("gpt") ? ["gpt"] : ["gravity", "nailong"];
  // Stable across polling/remounts; different tasks can use different members.
  let hash = 2166136261;
  for (const char of key) hash = Math.imul(hash ^ char.charCodeAt(0), 16777619) >>> 0;
  return candidates[hash % candidates.length];
}

export function useTaskAnalysisVariant(preference: AnalysisVariant, model: string | undefined, key: string) {
  const captured = useRef<{ key: string; model: string } | null>(null);
  // Wait for model metadata, then keep this task's model despite settings changes.
  if (captured.current?.key !== key) captured.current = null;
  if (!captured.current && model !== undefined) captured.current = { key, model };
  return resolveAnalysisVariant(preference, captured.current?.model, key);
}

export function readAnalysisVariant(): AnalysisVariant {
  try {
    const value = window.localStorage.getItem(ANALYSIS_VARIANT_KEY);
    return ANALYSIS_VARIANTS.find(variant => variant.id === value)?.id ?? "nailong";
  } catch { return "nailong"; }
}

export function saveAnalysisVariant(value: AnalysisVariant) {
  try {
    window.localStorage.setItem(ANALYSIS_VARIANT_KEY, value);
    window.dispatchEvent(new Event(CHANGE));
    return true;
  } catch { return false; }
}

// Legacy quality values no longer select a renderer. Enabled always uses full effects.
export function readAnalysisQuality(): AnalysisQuality { return "high"; }

export function readCyberspacePreference(): CyberspacePreference {
  try {
    const value = window.localStorage.getItem(CYBERSPACE_KEY);
    return value === "dynamic" || value === "static" ? "dynamic" : "off";
  } catch { return "off"; }
}

export function saveCyberspacePreference(value: CyberspacePreference) {
  try {
    window.localStorage.setItem(CYBERSPACE_KEY, value);
    window.dispatchEvent(new Event(CHANGE));
    return true;
  } catch { return false; }
}

function subscribe(callback: () => void) {
  const storage = (event: StorageEvent) => {
    if (event.key === CYBERSPACE_KEY || event.key === ANALYSIS_QUALITY_KEY || event.key === ANALYSIS_VARIANT_KEY || event.key === null) callback();
  };
  window.addEventListener(CHANGE, callback);
  window.addEventListener("storage", storage);
  return () => {
    window.removeEventListener(CHANGE, callback);
    window.removeEventListener("storage", storage);
  };
}

export function useCyberspacePreference() {
  return useSyncExternalStore(subscribe, readCyberspacePreference, () => "off" as const);
}

export function useAnalysisQuality() {
  return useSyncExternalStore(subscribe, readAnalysisQuality, () => "high" as const);
}

export function useAnalysisVariant() {
  return useSyncExternalStore(subscribe, readAnalysisVariant, () => "nailong" as const);
}
