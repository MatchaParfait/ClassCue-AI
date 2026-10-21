import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ParaformerDeviceControls } from "./ParaformerDeviceControls";

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

it("disables GPU and shows the native-memory rejection reason", () => {
  const onChange = vi.fn();
  act(() => root.render(<ParaformerDeviceControls device="cpu" onChange={onChange} model={{
    id: "paraformer-large-cpu", name: "Paraformer", engine: "paraformer",
    downloaded: true, cache_size_bytes: 0, qwen_ready: null, selected: true,
    gpu_available: false, gpu_runtime_ready: true, gpu_prepare_available: true,
    gpu_unavailable_reason: "Paraformer GPU 要求原生显存至少 2 GiB",
  }} />));
  const gpu = container.querySelector<HTMLInputElement>('input[value="gpu"]')!;
  expect(gpu.disabled).toBe(true);
  expect(container.querySelector('[role="status"]')!.textContent).toContain("2 GiB");
  act(() => gpu.click());
  expect(onChange).not.toHaveBeenCalled();
});
