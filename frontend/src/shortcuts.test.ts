import { beforeEach, afterEach, expect, it, vi } from "vitest";
import { SHORTCUTS_KEY, defaultShortcuts, isUndoShortcut, matchesShortcut, readShortcuts, saveShortcuts, validateShortcuts } from "./shortcuts";
beforeEach(() => {
  const storage = new Map<string, string>();
  vi.stubGlobal("localStorage", { getItem: (key: string) => storage.get(key) ?? null, setItem: (key: string, value: string) => storage.set(key, value) });
});
afterEach(() => vi.unstubAllGlobals());
it("has no default search shortcut on either platform", () => {
  expect(defaultShortcuts().search).toEqual([]);
  expect(matchesShortcut(new KeyboardEvent("keydown", { key: "k", ctrlKey: true }), readShortcuts(), "search")).toBe(false);
  expect(matchesShortcut(new KeyboardEvent("keydown", { key: "k", metaKey: true }), readShortcuts(), "search")).toBe(false);
});
it("adds W for manual questions without changing the existing S command", () => {
  window.localStorage.setItem(SHORTCUTS_KEY, JSON.stringify({ nextMarker: ["J"], captureTime: ["Ctrl+U"] }));
  expect(readShortcuts().createQuestion).toEqual(["W"]);
  expect(readShortcuts().captureTime).toEqual(["Ctrl+U"]);
  expect(readShortcuts().nextMarker).toEqual(["J"]);
  expect(defaultShortcuts().captureTime).toEqual(["S"]);
});
it("preserves older custom W bindings and leaves the new action unassigned on conflict", () => {
  window.localStorage.setItem(SHORTCUTS_KEY, JSON.stringify({ nextMarker: ["W"], previousMarker: ["H"] }));
  expect(readShortcuts().nextMarker).toEqual(["W"]);
  expect(readShortcuts().previousMarker).toEqual(["H"]);
  expect(readShortcuts().createQuestion).toEqual([]);
});
it.each([SHORTCUTS_KEY, "classroom.shortcuts.v1"])("ignores retired subtitle movement bindings in %s without resetting other shortcuts", (key) => {
  window.localStorage.setItem(key, JSON.stringify({
    ...defaultShortcuts(), previousMarker: ["H"], nextMarker: ["J"],
    captionLeft: ["H"], captionRight: ["J"], captionUp: ["U"], captionDown: ["not-a-key"],
  }));
  const map = readShortcuts();
  expect(map.previousMarker).toEqual(["H"]);
  expect(map.nextMarker).toEqual(["J"]);
  expect(Object.keys(map).some(id => id.startsWith("caption"))).toBe(false);
  saveShortcuts(map);
  expect(JSON.parse(window.localStorage.getItem(SHORTCUTS_KEY)!)).toEqual(map);
});
it("removes the legacy default search pair without losing other custom bindings", () => {
  window.localStorage.setItem("classroom.shortcuts.v1", JSON.stringify({
    ...defaultShortcuts(), search: ["Ctrl+K", "Meta+K"], nextMarker: ["J"], previousMarker: ["H"],
  }));
  expect(readShortcuts().search).toEqual([]);
  expect(readShortcuts().nextMarker).toEqual(["J"]);
  expect(readShortcuts().previousMarker).toEqual(["H"]);
  expect(readShortcuts().captureTime).toEqual(["S"]);
  const custom = readShortcuts();
  custom.search = ["Ctrl+K"];
  saveShortcuts(custom);
  expect(readShortcuts().search).toEqual(["Ctrl+K"]);
  expect(readShortcuts().nextMarker).toEqual(["J"]);
});
it("preserves an explicitly customized legacy search shortcut", () => {
  window.localStorage.setItem("classroom.shortcuts.v1", JSON.stringify({ search: ["Ctrl+Shift+Y"] }));
  expect(readShortcuts().search).toEqual(["Ctrl+Shift+Y"]);
});
it("persists disabled commands and distinguishes exact modifiers", () => {
  const map = defaultShortcuts(); map.nextMarker = ["Ctrl+J"]; map.playPause = [];
  saveShortcuts(map);
  expect(readShortcuts().playPause).toEqual([]);
  expect(matchesShortcut(new KeyboardEvent("keydown", { key: "j", ctrlKey: true }), readShortcuts(), "nextMarker")).toBe(true);
  expect(matchesShortcut(new KeyboardEvent("keydown", { key: "j", ctrlKey: true, shiftKey: true }), readShortcuts(), "nextMarker")).toBe(false);
  expect(matchesShortcut(new KeyboardEvent("keydown", { key: "d" }), readShortcuts(), "nextMarker")).toBe(false);
  expect(matchesShortcut(new KeyboardEvent("keydown", { key: "j", ctrlKey: true, isComposing: true }), readShortcuts(), "nextMarker")).toBe(false);
});
it("rejects overlapping global and review/player bindings including desktop wake", () => {
  const map = defaultShortcuts(); map.nextMarker = ["K"];
  expect(validateShortcuts(map)).toContain("播放 / 暂停");
  map.nextMarker = ["Ctrl+Alt+J"];
  expect(validateShortcuts(map, "Ctrl+Alt+J")).toContain("启动 / 唤起");
  expect(validateShortcuts(defaultShortcuts())).toBeNull();
  map.nextMarker = ["ArrowLeft"];
  expect(validateShortcuts(map)).toContain("后退 5 秒");
});
it("rejects reserved keys, duplicates and damaged preferences", () => {
  const map = defaultShortcuts(); map.playPause = ["Ctrl+R"];
  expect(validateShortcuts(map)).toContain("保留");
  map.playPause = ["K", "K"];
  expect(validateShortcuts(map)).toContain("同时用于");
  window.localStorage.setItem(SHORTCUTS_KEY, '{"nextMarker": ["Escape"]}');
  expect(readShortcuts()).toEqual(defaultShortcuts());
  window.localStorage.setItem(SHORTCUTS_KEY, 'null');
  expect(readShortcuts()).toEqual(defaultShortcuts());
});
it("does not claim to save when storage refuses the write", () => {
  vi.spyOn(window.localStorage, "setItem").mockImplementation(() => { throw new Error("storage denied"); });
  expect(() => saveShortcuts(defaultShortcuts())).toThrow("storage denied");
});

it("recognizes only plain Ctrl/Cmd+Z as the application undo command", () => {
  expect(isUndoShortcut(new KeyboardEvent("keydown", { key: "z", ctrlKey: true }))).toBe(true);
  expect(isUndoShortcut(new KeyboardEvent("keydown", { key: "Z", metaKey: true }))).toBe(true);
  for (const options of [
    { ctrlKey: true, shiftKey: true }, { ctrlKey: true, altKey: true },
    { ctrlKey: true, repeat: true }, { ctrlKey: true, isComposing: true }, {},
  ]) expect(isUndoShortcut(new KeyboardEvent("keydown", { key: "z", ...options }))).toBe(false);
  const handled = new KeyboardEvent("keydown", { key: "z", ctrlKey: true, cancelable: true });
  handled.preventDefault();
  expect(isUndoShortcut(handled)).toBe(false);
});

it("reserves undo keys and preserves unrelated preferences while removing old conflicting bindings", () => {
  const map = defaultShortcuts();
  map.nextMarker = ["Ctrl+Z"];
  expect(validateShortcuts(map)).toContain("撤销");
  window.localStorage.setItem(SHORTCUTS_KEY, JSON.stringify({
    nextMarker: ["Ctrl+Z", "J"], previousMarker: ["H"], search: ["Ctrl+Shift+Z"],
  }));
  expect(readShortcuts().nextMarker).toEqual(["J"]);
  expect(readShortcuts().previousMarker).toEqual(["H"]);
  expect(readShortcuts().search).toEqual([]);
});
