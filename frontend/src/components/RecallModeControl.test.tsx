import { act, useState } from "react";
import { createRoot } from "react-dom/client";
import { expect, it } from "vitest";
import { RecallModeControl } from "./RecallModeControl";
import type { RecallMode } from "../types";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";

it("defaults to medium and allows selecting and clearing every mode", () => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  function Form() {
    const [modes, setModes] = useState<RecallMode[]>(["medium_recall"]);
    return <RecallModeControl value={modes} onChange={setModes} />;
  }
  const container = document.createElement("div");
  const root = createRoot(container);
  const client = new QueryClient();
  client.setQueryData(["settings", "recall-schemes"], []);
  try {
    act(() => root.render(<QueryClientProvider client={client}><MemoryRouter><Form /></MemoryRouter></QueryClientProvider>));
    const inputs = [...container.querySelectorAll("input")];
    const selected = () => inputs.filter(input => input.checked).map(input => input.value);
    expect(selected()).toEqual(["medium_recall"]);
    expect(inputs[1].disabled).toBe(false);
    expect(container.textContent).toContain("更多 token");
    expect(container.textContent).not.toContain("记住上次选择");
    expect(container.textContent).not.toContain("首次默认中召回");
    expect(container.textContent).not.toContain("增加处理时间");
    act(() => inputs[1].click());
    expect(selected()).toEqual([]);
    expect(container.querySelector('[role="alert"]')).toBeNull();
    act(() => inputs[0].click());
    act(() => inputs[1].click());
    act(() => inputs[2].click());
    expect(selected()).toEqual(["high_recall", "medium_recall", "low_recall"]);
    act(() => inputs[1].click());
    act(() => inputs[0].click());
    expect(selected()).toEqual(["low_recall"]);
    act(() => inputs[2].click());
    expect(selected()).toEqual([]);
    act(() => inputs[1].click());
    expect(selected()).toEqual(["medium_recall"]);
  } finally {
    act(() => root.unmount());
  }
});

it("selects a custom scheme independently of its base and preserves selection after renaming", async () => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const id = `custom_${"a".repeat(32)}` as const;
  const scheme = { id, name: "张老师", base_mode: "medium_recall", rules: [], notes: "", revision: 1, archived: false };
  const client = new QueryClient();
  client.setQueryData(["settings", "recall-schemes"], [scheme]);
  function Form() {
    const [value, setValue] = useState<RecallMode[]>([]);
    return <RecallModeControl value={value} onChange={setValue} active={false} />;
  }
  const container = document.createElement("div");
  const root = createRoot(container);
  try {
    act(() => root.render(<QueryClientProvider client={client}><MemoryRouter><Form /></MemoryRouter></QueryClientProvider>));
    act(() => container.querySelector<HTMLInputElement>(`input[value="${id}"]`)!.click());
    expect([...container.querySelectorAll<HTMLInputElement>("input:checked")].map(input => input.value)).toEqual([id]);
    expect(container.textContent).toContain("基于中召回");
    await act(async () => {
      client.setQueryData(["settings", "recall-schemes"], [{ ...scheme, name: "修改后的名称" }]);
      await new Promise(resolve => setTimeout(resolve, 0));
    });
    expect(container.querySelector<HTMLInputElement>(`input[value="${id}"]`)?.checked).toBe(true);
    expect(container.textContent).toContain("修改后的名称");
  } finally { act(() => root.unmount()); client.clear(); }
});
