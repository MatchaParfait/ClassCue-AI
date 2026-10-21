import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { MarkerImportanceControl } from "./MarkerImportanceControl";
import type { Marker } from "../types";

const marker: Marker = {
  id: 1, video_id: 1, start_ms: 1000, end_ms: 2000, title: "Topic",
  note: "", source_quote: "", analysis_mode: "medium_recall", review_status: "active",
};
let container: HTMLDivElement, root: Root;
const change = vi.fn();
function render(value = marker) {
  act(() => root.render(<MarkerImportanceControl marker={value} onChange={change} />));
}
function button(value: string) {
  return container.querySelector<HTMLButtonElement>(`.importance-${value}[type=button]`)!;
}
async function settle(check: () => void) {
  await vi.waitFor(async () => {
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 5)); });
    check();
  });
}

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  change.mockReset().mockResolvedValue(undefined);
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  render();
});
afterEach(() => { act(() => root.unmount()); container.remove(); });

it("defaults to white and gives all three swatches names and tooltips", () => {
  expect(button("standard").getAttribute("aria-pressed")).toBe("true");
  expect(container.querySelector(".marker-importance-label")?.textContent).toBe("标准");
  expect([...container.querySelectorAll("button")].map((item) => item.title)).toEqual(["标准（白色）", "重要（蓝色）", "很重要（红色）"]);
  act(() => button("standard").click());
  expect(change).not.toHaveBeenCalled();
});

it.each(["important", "critical"] as const)("saves %s without navigating the marker", async (importance) => {
  change.mockImplementation(async () => render({ ...marker, importance }));
  await act(async () => button(importance).click());
  await settle(() => expect(button(importance).getAttribute("aria-pressed")).toBe("true"));
  expect(change).toHaveBeenCalledWith(marker, importance);
});

it("prevents repeated writes while saving and allows restoring standard", async () => {
  let resolve!: () => void;
  change.mockImplementation(() => new Promise<void>((done) => { resolve = done; }));
  act(() => button("critical").click());
  expect([...container.querySelectorAll("button")].every((item) => item.disabled)).toBe(true);
  act(() => button("important").click());
  expect(change).toHaveBeenCalledTimes(1);
  await act(async () => { render({ ...marker, importance: "critical" }); resolve(); });
  await settle(() => expect(button("standard").disabled).toBe(false));
  change.mockResolvedValue(undefined);
  await act(async () => button("standard").click());
  expect(change).toHaveBeenLastCalledWith(expect.objectContaining({ importance: "critical" }), "standard");
});

it("keeps the saved color on failure, shows the error, and supports retry", async () => {
  render({ ...marker, importance: "important" });
  change.mockRejectedValueOnce(new Error("network failure"));
  await act(async () => button("critical").click());
  await settle(() => expect(container.querySelector('[role=alert]')?.textContent).toContain("标签保存失败"));
  expect(button("important").getAttribute("aria-pressed")).toBe("true");
  expect(button("critical").disabled).toBe(false);
  await act(async () => button("critical").click());
  await settle(() => expect(container.querySelector('[role=alert]')).toBeNull());
  expect(change).toHaveBeenCalledTimes(2);
});
