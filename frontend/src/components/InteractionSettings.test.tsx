import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { InteractionSettings } from "./InteractionSettings";
import { DRAG_TEXT_HINTS_KEY } from "../libraryPreferences";

let root: Root;
let host: HTMLDivElement;
const toggle = () => host.querySelector<HTMLInputElement>('[role="switch"]')!;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const storage = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => storage.set(key, value),
    clear: () => storage.clear(),
  });
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  act(() => root.render(<InteractionSettings />));
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("defaults on and remembers the disabled choice across remounts", () => {
  expect(toggle().checked).toBe(true);
  act(() => toggle().click());
  expect(toggle().checked).toBe(false);
  expect(window.localStorage.getItem(DRAG_TEXT_HINTS_KEY)).toBe("false");
  act(() => root.unmount());
  root = createRoot(host);
  act(() => root.render(<InteractionSettings />));
  expect(toggle().checked).toBe(false);
  act(() => toggle().click());
  expect(toggle().checked).toBe(true);
  expect(window.localStorage.getItem(DRAG_TEXT_HINTS_KEY)).toBe("true");
});

it("updates for another window and restores defaults when storage is cleared", () => {
  act(() => {
    window.localStorage.setItem(DRAG_TEXT_HINTS_KEY, "false");
    window.dispatchEvent(new StorageEvent("storage", { key: DRAG_TEXT_HINTS_KEY, newValue: "false" }));
  });
  expect(toggle().checked).toBe(false);
  act(() => {
    window.localStorage.clear();
    window.dispatchEvent(new StorageEvent("storage", { key: null }));
  });
  expect(toggle().checked).toBe(true);
});

it("keeps the old preference on save failure and allows another attempt", () => {
  vi.spyOn(window.localStorage, "setItem").mockImplementationOnce(() => { throw new Error("storage denied"); });
  act(() => toggle().click());
  expect(toggle().checked).toBe(true);
  expect(host.querySelector('[role="alert"]')?.textContent).toContain("无法保存提示设置");
  act(() => toggle().click());
  expect(toggle().checked).toBe(false);
  expect(host.querySelector('[role="alert"]')).toBeNull();
});
