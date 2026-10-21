import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it } from "vitest";
import { FireRedDeviceControls } from "./FireRedDeviceControls";

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

it("shows only GPU and leaves out the disabled CPU option", () => {
  function Controls() {
    const [device, setDevice] = useState<"gpu" | "cpu">("gpu");
    return <FireRedDeviceControls device={device} onChange={setDevice} />;
  }
  act(() => root.render(<Controls />));
  expect(container.querySelector<HTMLInputElement>('input[value="gpu"]')!.checked).toBe(true);
  expect(container.querySelector('[role="alert"]')).toBeNull();
  expect(container.querySelector('input[value="cpu"]')).toBeNull();
  expect(container.textContent).not.toContain("CPU");
});

it("preserves legacy CPU until the user explicitly selects GPU", () => {
  function Controls() {
    const [device, setDevice] = useState<"gpu" | "cpu">("cpu");
    return <FireRedDeviceControls device={device} onChange={setDevice} />;
  }
  act(() => root.render(<Controls />));
  expect(container.querySelector('input[value="cpu"]')).toBeNull();
  expect(container.querySelector<HTMLInputElement>('input[value="gpu"]')!.checked).toBe(false);
  expect(container.querySelector('[role="alert"]')!.textContent).toContain("CPU 处理已禁用");
  act(() => container.querySelector<HTMLInputElement>('input[value="gpu"]')!.click());
  expect(container.querySelector<HTMLInputElement>('input[value="gpu"]')!.checked).toBe(true);
  expect(container.querySelector('[role="alert"]')).toBeNull();
});
