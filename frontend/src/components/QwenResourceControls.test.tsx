import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";
import { QwenResourceControls } from "./QwenResourceControls";

describe("Qwen GPU requirement", () => {
  it.each(["auto", "low-resource", "manual"] as const)("warns in %s mode, including the lowest tier", (mode) => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    const container = document.createElement("div");
    const root = createRoot(container);
    try {
      act(() => root.render(<QwenResourceControls
        form={{ displayName: "", recallModes: ["medium_recall"], keywords: "", filterFormula: "",
          filterLowInformation: true, qwenMode: mode }}
        onChange={vi.fn()} disabled={false}
      />));
      expect(container.querySelector('[role="note"]')?.textContent).toContain("GPU 原生显存至少 6GB");
      expect(container.textContent).toContain("低于 6GB 请勿使用此模型");
      expect(container.textContent).toContain("共享显存不计入");
    } finally {
      act(() => root.unmount());
    }
  });
});
