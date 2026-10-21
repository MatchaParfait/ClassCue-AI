import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ExperimentalSettings } from "./ExperimentalSettings";
import { CYBERSPACE_KEY, ANALYSIS_QUALITY_KEY, ANALYSIS_VARIANT_KEY, readAnalysisVariant, readCyberspacePreference, readAnalysisQuality } from "../cyberspace/preferences";

beforeEach(() => {
  const values = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: vi.fn((key: string, value: string) => { values.set(key, value); }),
    removeItem: (key: string) => { values.delete(key); },
  });
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });
function mount() {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const host = document.createElement("div"); document.body.append(host);
  const root = createRoot(host); act(() => root.render(<ExperimentalSettings />));
  return { host, dispose() { act(() => root.unmount()); host.remove(); } };
}
it("defaults off with full effects and persists enablement across remounts", () => {
  let view = mount();
  expect(view.host.textContent).toContain("默认关闭。");
  expect(view.host.textContent).not.toContain("显示偏好保存在此浏览器");
  expect(view.host.textContent).not.toContain("不影响后台处理、字幕及考点数据");
  expect((view.host.querySelector('[role="switch"]') as HTMLInputElement).checked).toBe(false);
  act(() => (view.host.querySelector('[role="switch"]') as HTMLInputElement).click());
  expect(readCyberspacePreference()).toBe("dynamic");
  expect(view.host.querySelectorAll('[type="radio"]')).toHaveLength(7);
  expect(readAnalysisQuality()).toBe("high");
  view.dispose(); view = mount();
  expect((view.host.querySelector('[role="switch"]') as HTMLInputElement).checked).toBe(true);
  act(() => (view.host.querySelector('[role="switch"]') as HTMLInputElement).click());
  expect(readCyberspacePreference()).toBe("off");
  view.dispose();
});
it("ignores legacy low quality preferences and exposes only enablement plus themes", () => {
  localStorage.setItem(ANALYSIS_QUALITY_KEY, "low");
  const view = mount();
  expect(readAnalysisQuality()).toBe("high");
  expect(view.host.querySelector('input[name="analysis-quality"]')).toBeNull();
  expect(view.host.textContent).not.toContain("轻量版");
  expect(view.host.querySelectorAll('[role="switch"]')).toHaveLength(1);
  expect(view.host.querySelectorAll('input[name="analysis-variant"]')).toHaveLength(7);
  expect(view.host.textContent).toContain("大肥鱼1");
  expect(view.host.textContent).toContain("大肥鱼2");
  expect(view.host.querySelector('.analysis-quality-option small')).toBeNull();
  expect(readCyberspacePreference()).toBe("off");
  view.dispose();
});
it("responds to another window and reports storage failures without enabling", () => {
  const view = mount();
  act(() => {
    window.localStorage.setItem(CYBERSPACE_KEY, "static");
    window.dispatchEvent(new StorageEvent("storage", { key: CYBERSPACE_KEY }));
  });
  expect((view.host.querySelector('[role="switch"]') as HTMLInputElement).checked).toBe(true);
  expect(readCyberspacePreference()).toBe("dynamic");
  vi.mocked(window.localStorage.setItem).mockImplementation(() => { throw new Error("blocked"); });
  act(() => (view.host.querySelector('[role="switch"]') as HTMLInputElement).click());
  expect(view.host.querySelector('[role="alert"]')?.textContent).toContain("无法保存");
  expect(readCyberspacePreference()).toBe("dynamic");
  view.dispose();
});

it("defaults to Nailong and keeps the original black hole selectable across windows and remounts", () => {
  let view = mount();
  expect(readAnalysisVariant()).toBe("nailong");
  expect(view.host.querySelector<HTMLInputElement>('input[value="nailong"]')!.checked).toBe(true);
  act(() => view.host.querySelector<HTMLInputElement>('input[value="gravity"]')!.click());
  expect(readAnalysisVariant()).toBe("gravity");
  view.dispose(); view = mount();
  expect(view.host.querySelector<HTMLInputElement>('input[value="gravity"]')!.checked).toBe(true);
  act(() => view.host.querySelector<HTMLInputElement>('input[value="dafeiyu"]')!.click());
  expect(readAnalysisVariant()).toBe("dafeiyu");
  view.dispose(); view = mount();
  expect(view.host.querySelector<HTMLInputElement>('input[value="dafeiyu"]')!.checked).toBe(true);
  act(() => view.host.querySelector<HTMLInputElement>('input[value="dafeiyu31"]')!.click());
  expect(readAnalysisVariant()).toBe("dafeiyu31");
  view.dispose(); view = mount();
  expect(view.host.querySelector<HTMLInputElement>('input[value="dafeiyu31"]')!.checked).toBe(true);
  act(() => view.host.querySelector<HTMLInputElement>('input[value="dafeiyu-rice"]')!.click());
  expect(readAnalysisVariant()).toBe("dafeiyu-rice");
  view.dispose(); view = mount();
  expect(view.host.querySelector<HTMLInputElement>('input[value="dafeiyu-rice"]')!.checked).toBe(true);
  expect(view.host.textContent).toContain("大肥鱼吃白饭");
  act(() => view.host.querySelector<HTMLInputElement>('input[value="gpt"]')!.click());
  expect(readAnalysisVariant()).toBe("gpt");
  view.dispose(); view = mount();
  expect(view.host.querySelector<HTMLInputElement>('input[value="gpt"]')!.checked).toBe(true);
  expect(view.host.querySelectorAll('input[name="analysis-variant"]')).toHaveLength(7);
  act(() => {
    localStorage.setItem(ANALYSIS_VARIANT_KEY, "nailong");
    window.dispatchEvent(new StorageEvent("storage", { key: ANALYSIS_VARIANT_KEY }));
  });
  expect(view.host.querySelector<HTMLInputElement>('input[value="nailong"]')!.checked).toBe(true);
  vi.mocked(localStorage.setItem).mockImplementation(() => { throw new Error("blocked"); });
  act(() => view.host.querySelector<HTMLInputElement>('input[value="gravity"]')!.click());
  expect(readAnalysisVariant()).toBe("nailong");
  expect(view.host.querySelector('[role="alert"]')?.textContent).toContain("无法保存");
  view.dispose();
});


it("persists automatic theme selection without replacing manual choices", () => {
  let view = mount();
  act(() => view.host.querySelector<HTMLInputElement>('input[value="auto"]')!.click());
  expect(readAnalysisVariant()).toBe("auto");
  view.dispose(); view = mount();
  expect(view.host.querySelector<HTMLInputElement>('input[value="auto"]')!.checked).toBe(true);
  expect(view.host.textContent).toContain("DeepSeek");
  act(() => view.host.querySelector<HTMLInputElement>('input[value="gpt"]')!.click());
  expect(readAnalysisVariant()).toBe("gpt");
  view.dispose();
});
