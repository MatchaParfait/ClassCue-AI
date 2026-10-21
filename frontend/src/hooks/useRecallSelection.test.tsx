import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { RecallSelectionScope, RECALL_SELECTION_KEY, useRecallSelection } from "./useRecallSelection";

const custom = `custom_${"a".repeat(32)}` as const;
const key = (scope = "default") => `${RECALL_SELECTION_KEY}:${encodeURIComponent(scope)}`;
let container: HTMLDivElement;
let root: Root;

function Form({ name }: { name: string }) {
  const [modes, save] = useRecallSelection();
  return <section data-form={name}>
    <output>{JSON.stringify(modes)}</output>
    <button onClick={() => save(["high_recall"])}>高</button>
    <button onClick={() => save(["low_recall", custom])}>多选</button>
    <button onClick={() => save([])}>清空</button>
  </section>;
}

function render(scope = "default", version = 0) {
  act(() => root.render(<RecallSelectionScope.Provider value={scope} key={version}>
    <Form name="single" /><Form name="batch" /><Form name="library" />
  </RecallSelectionScope.Provider>));
}

function selections() {
  return [...container.querySelectorAll("output")].map(item => JSON.parse(item.textContent!));
}

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  window.localStorage.clear();
  container = document.createElement("div");
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  vi.restoreAllMocks();
  act(() => window.dispatchEvent(new StorageEvent("storage", { key: null })));
  window.localStorage.clear();
});

it("defaults to medium only on first use without writing the default", () => {
  render();
  expect(selections()).toEqual(Array(3).fill(["medium_recall"]));
  expect(localStorage.getItem(key())).toBeNull();
});

it("shares the last manual selection across entry points and survives a fresh mount", () => {
  render();
  act(() => container.querySelector<HTMLButtonElement>('[data-form="batch"] button')!.click());
  expect(selections()).toEqual(Array(3).fill(["high_recall"]));
  expect(JSON.parse(localStorage.getItem(key())!)).toEqual(["high_recall"]);
  render("default", 1);
  expect(selections()).toEqual(Array(3).fill(["high_recall"]));
});

it("restores multiple modes and custom IDs without falling back to their base", () => {
  render();
  act(() => container.querySelectorAll<HTMLButtonElement>("button")[1].click());
  render("default", 1);
  expect(selections()).toEqual(Array(3).fill(["low_recall", custom]));
});

it("preserves explicitly cleared choices rather than silently running medium", () => {
  render();
  act(() => container.querySelectorAll<HTMLButtonElement>("button")[2].click());
  render("default", 1);
  expect(selections()).toEqual([[], [], []]);
});

it("isolates preferences by library and restores them when returning", () => {
  render("library A");
  act(() => container.querySelector<HTMLButtonElement>("button")!.click());
  render("library B");
  expect(selections()).toEqual(Array(3).fill(["medium_recall"]));
  render("library A", 1);
  expect(selections()).toEqual(Array(3).fill(["high_recall"]));
});

it.each(["broken", "null", '"high_recall"', '["strict_exam_intent"]', '["unknown"]', JSON.stringify(Array(9).fill("low_recall"))])(
  "safely defaults for invalid saved preferences: %s", raw => {
    localStorage.setItem(key(), raw);
    render();
    expect(selections()).toEqual(Array(3).fill(["medium_recall"]));
  },
);

it("responds to preferences saved by another window", () => {
  render();
  act(() => {
    localStorage.setItem(key(), JSON.stringify([custom]));
    window.dispatchEvent(new StorageEvent("storage", { key: key() }));
  });
  expect(selections()).toEqual(Array(3).fill([custom]));
});

it("keeps selections usable when browser storage rejects writes", () => {
  render("storage-blocked");
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("quota"); });
  act(() => container.querySelector<HTMLButtonElement>("button")!.click());
  expect(selections()).toEqual(Array(3).fill(["high_recall"]));
  render("storage-blocked", 1);
  expect(selections()).toEqual(Array(3).fill(["high_recall"]));
});
