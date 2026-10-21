import { useSyncExternalStore } from "react";

export const SHORTCUTS_KEY = "classroom.shortcuts.v2";
const LEGACY_SHORTCUTS_KEY = "classroom.shortcuts.v1";
const CHANGE = "classroom-shortcuts-change";
export const shortcutDefinitions = [
  { id: "search", label: "全局搜索", group: "全局", defaults: [], hint: "搜索所有视频的字幕。" },
  { id: "previousMarker", label: "上一个考点", group: "复习", defaults: ["A"], hint: "跳转到当前列表中的上一个考点。" },
  { id: "nextMarker", label: "下一个考点", group: "复习", defaults: ["D"], hint: "跳转到当前列表中的下一个考点。" },
  { id: "captureTime", label: "保存考点跳转时间", group: "复习", defaults: ["S"], hint: "用当前播放位置更新最近选中的考点跳转时间。" },
  { id: "playPause", label: "播放 / 暂停", group: "播放器", defaults: ["Space", "K"], hint: "播放器获得焦点时生效。" },
  { id: "seekBack", label: "后退 5 秒", group: "播放器", defaults: ["ArrowLeft"], hint: "播放器获得焦点时生效。" },
  { id: "seekForward", label: "前进 5 秒", group: "播放器", defaults: ["ArrowRight"], hint: "播放器获得焦点时生效。" },
  { id: "mute", label: "静音 / 取消静音", group: "播放器", defaults: ["M"], hint: "播放器获得焦点时生效。" },
  { id: "subtitles", label: "显示 / 隐藏字幕", group: "播放器", defaults: ["C"], hint: "播放器获得焦点且有字幕时生效。" },
  { id: "fullscreen", label: "进入 / 退出全屏", group: "播放器", defaults: ["F"], hint: "播放器获得焦点时生效。" },
] as const;
export type ShortcutId = typeof shortcutDefinitions[number]["id"];
export type ShortcutMap = Record<ShortcutId, string[]>;
export const defaultShortcuts = (): ShortcutMap => Object.fromEntries(shortcutDefinitions.map(item => [item.id, [...item.defaults]])) as ShortcutMap;

const modifiers = ["Ctrl", "Alt", "Shift", "Meta"];
const keys = /^(?:[A-Z0-9]|F(?:[1-9]|1[0-2])|Space|ArrowLeft|ArrowRight|ArrowUp|ArrowDown|Home|End|PageUp|PageDown|Insert|Delete|Backspace)$/;
export function normalizeBinding(value: string): string {
  const parts = value.split("+");
  const key = parts.pop() ?? "";
  if (!keys.test(key) || parts.some(part => !modifiers.includes(part)) || new Set(parts).size !== parts.length) throw new Error("请使用字母、数字、方向键或功能键，可搭配 Ctrl / Alt / Shift / Cmd。");
  return [...modifiers.filter(part => parts.includes(part)), key].join("+");
}
type KeyEvent = Pick<KeyboardEvent, "key" | "ctrlKey" | "altKey" | "shiftKey" | "metaKey" | "isComposing">;
export function bindingFromEvent(event: KeyEvent): string | null {
  if (event.isComposing || ["Control", "Alt", "Shift", "Meta", "Dead", "Process", "Unidentified"].includes(event.key)) return null;
  const key = event.key === " " ? "Space" : /^[a-z0-9]$/i.test(event.key) ? event.key.toUpperCase() : event.key;
  return normalizeBinding([event.ctrlKey && "Ctrl", event.altKey && "Alt", event.shiftKey && "Shift", event.metaKey && "Meta", key].filter(Boolean).join("+"));
}
const reserved = new Set(["F5", "F6", "F11", "F12", "Alt+F4", "Alt+Tab", "Alt+ArrowLeft", "Alt+ArrowRight", "Ctrl+R", "Ctrl+Shift+R", "Ctrl+W", "Ctrl+Shift+W", "Ctrl+T", "Ctrl+N", "Ctrl+L", "Ctrl+Shift+T", "Ctrl+Shift+N", "Ctrl+Alt+Delete", "Meta+Q", "Meta+W", "Meta+R", "Meta+L", "Meta+T", "Meta+N"]);
export function validateShortcuts(map: ShortcutMap, wake = ""): string | null {
  if (wake && !/^Ctrl\+(Alt|Shift)\+[A-Z0-9]$/.test(wake)) return "启动 / 唤起仅支持 Ctrl+Alt+字母/数字或 Ctrl+Shift+字母/数字。";
  const used: { binding: string; label: string }[] = wake ? [{ binding: wake, label: "启动 / 唤起" }] : [];
  for (const definition of shortcutDefinitions) {
    const bindings = map[definition.id];
    if (!Array.isArray(bindings) || bindings.length > 2) return `“${definition.label}”最多设置两个快捷键。`;
    for (const raw of bindings) {
      let binding: string;
      try { binding = normalizeBinding(raw); } catch { return `“${definition.label}”的快捷键无效。`; }
      if (reserved.has(binding)) return `“${formatBinding(binding)}”由系统或浏览器保留，请更换。`;
      const collision = used.find(item => item.binding === binding);
      if (collision) return `“${formatBinding(binding)}”同时用于“${collision.label}”和“${definition.label}”，请更换其中一个。`;
      used.push({ binding, label: definition.label });
    }
  }
  return null;
}
const labels: Record<string, string> = { Space: "空格", Meta: "Cmd", ArrowLeft: "←", ArrowRight: "→", ArrowUp: "↑", ArrowDown: "↓" };
export function formatBinding(value: string) { return value.split("+").map(part => labels[part] ?? part).join(" + "); }
export function shortcutLabel(map: ShortcutMap, id: ShortcutId) { return map[id].map(formatBinding).join(" / "); }
export function shortcutTitle(label: string, map: ShortcutMap, id: ShortcutId) { const keys = shortcutLabel(map, id); return keys ? `${label} (${keys})` : label; }
export function shortcutAria(map: ShortcutMap, id: ShortcutId) { return map[id].map(value => value.replace(/Ctrl/g, "Control")).join(" ") || undefined; }
export function matchesShortcut(event: KeyEvent & { defaultPrevented?: boolean }, map: ShortcutMap, id: ShortcutId): boolean {
  if (event.defaultPrevented || event.isComposing) return false;
  try { const binding = bindingFromEvent(event); return binding !== null && map[id].includes(binding); } catch { return false; }
}
export function isShortcutInput(target: EventTarget | null): boolean {
  return target instanceof Element && Boolean(target.closest('input, textarea, select, [contenteditable]:not([contenteditable="false"]), [role="textbox"], [role="combobox"], [data-shortcut-recorder]'));
}
// Stable snapshots let every mounted page adopt changes immediately, including retained pages.
let cachedRaw: string | null | undefined;
let cachedLegacy = false;
let cachedMap = defaultShortcuts();
export function readShortcuts(): ShortcutMap {
  let raw: string | null = null;
  let legacy = false;
  try {
    raw = window.localStorage.getItem(SHORTCUTS_KEY);
    if (raw === null) {
      raw = window.localStorage.getItem(LEGACY_SHORTCUTS_KEY);
      legacy = raw !== null;
    }
  } catch { /* Defaults remain usable. */ }
  if (raw === cachedRaw && legacy === cachedLegacy) return cachedMap;
  cachedRaw = raw;
  cachedLegacy = legacy;
  const map = defaultShortcuts();
  try {
    const saved = JSON.parse(raw ?? "{}");
    for (const { id } of shortcutDefinitions) if (Object.hasOwn(saved, id)) map[id] = saved[id];
    // Old full-map saves included the default search keys even when never customized.
    if (legacy && Array.isArray(map.search) && map.search.length === 2
      && map.search.includes("Ctrl+K") && map.search.includes("Meta+K")) map.search = [];
    cachedMap = validateShortcuts(map) ? defaultShortcuts() : map;
  } catch { cachedMap = defaultShortcuts(); }
  return cachedMap;
}
export function saveShortcuts(map: ShortcutMap) {
  const error = validateShortcuts(map);
  if (error) throw new Error(error);
  window.localStorage.setItem(SHORTCUTS_KEY, JSON.stringify(map));
  window.dispatchEvent(new Event(CHANGE));
}
function subscribe(callback: () => void) {
  const changed = (event: StorageEvent) => { if (event.key === SHORTCUTS_KEY || event.key === LEGACY_SHORTCUTS_KEY || event.key === null) callback(); };
  window.addEventListener(CHANGE, callback); window.addEventListener("storage", changed);
  return () => { window.removeEventListener(CHANGE, callback); window.removeEventListener("storage", changed); };
}
export function useShortcuts() { return useSyncExternalStore(subscribe, readShortcuts, () => cachedMap); }
