import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { SecondaryViewSettings } from "./SecondaryViewSettings";
import { AUTO_ALIGN_SECONDARY_KEY, readAutoAlignSecondary } from "../secondaryViewPreferences";

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
  act(() => root.render(<SecondaryViewSettings />));
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("defaults off and remembers both choices after reopening settings", () => {
  expect(toggle().checked).toBe(false);
  act(() => toggle().click());
  expect(toggle().checked).toBe(true);
  expect(readAutoAlignSecondary()).toBe(true);
  act(() => root.unmount());
  root = createRoot(host);
  act(() => root.render(<SecondaryViewSettings />));
  expect(toggle().checked).toBe(true);
  act(() => toggle().click());
  expect(readAutoAlignSecondary()).toBe(false);
  act(() => root.unmount());
  root = createRoot(host);
  act(() => root.render(<SecondaryViewSettings />));
  expect(toggle().checked).toBe(false);
});

it("synchronizes another window's choice and turns off when preferences are cleared", () => {
  act(() => {
    window.localStorage.setItem(AUTO_ALIGN_SECONDARY_KEY, "true");
    window.dispatchEvent(new StorageEvent("storage", { key: AUTO_ALIGN_SECONDARY_KEY, newValue: "true" }));
  });
  expect(toggle().checked).toBe(true);
  act(() => {
    window.localStorage.clear();
    window.dispatchEvent(new StorageEvent("storage", { key: null }));
  });
  expect(toggle().checked).toBe(false);
});

it("preserves the saved choice if saving fails and allows retry", () => {
  vi.spyOn(window.localStorage, "setItem").mockImplementationOnce(() => { throw new Error("storage denied"); });
  act(() => toggle().click());
  expect(toggle().checked).toBe(false);
  expect(host.querySelector('[role="alert"]')?.textContent).toContain("无法保存副视角设置");
  act(() => toggle().click());
  expect(toggle().checked).toBe(true);
  expect(host.querySelector('[role="alert"]')).toBeNull();
});

it("falls back to manual alignment when preference storage is unavailable", () => {
  vi.spyOn(window.localStorage, "getItem").mockImplementation(() => { throw new Error("storage denied"); });
  expect(readAutoAlignSecondary()).toBe(false);
});
