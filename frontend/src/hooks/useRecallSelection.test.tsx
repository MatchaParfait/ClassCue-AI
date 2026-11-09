import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { RecallSelectionScope, RecallSelectionSync, RECALL_SELECTION_KEY, useRecallSelection } from "./useRecallSelection";
import { getRecallSelection, saveRecallSelection, type RecallSelection } from "../api/client";

vi.mock("../api/client", () => ({ getRecallSelection: vi.fn(), saveRecallSelection: vi.fn() }));

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

async function syncRender(scope: string) {
  await act(async () => {
    root.render(<RecallSelectionScope.Provider value={scope}>
      <RecallSelectionSync libraryId={scope} /><Form name="single" />
    </RecallSelectionScope.Provider>);
  });
}

function remoteValue(scope: string, modes: RecallSelection["analysis_modes"], revision = 1): RecallSelection {
  return { library_id: scope, analysis_modes: modes, revision, initialized: revision > 0 };
}

it("migrates the old library-scoped selection once and uses the saved server choice afterward", async () => {
  const scope = "migration-library";
  localStorage.setItem(key(scope), JSON.stringify(["high_recall", "question_extraction"]));
  vi.mocked(getRecallSelection).mockResolvedValue(remoteValue(scope, ["medium_recall"], 0));
  vi.mocked(saveRecallSelection).mockImplementation(async (_id, modes) => remoteValue(scope, modes));
  await syncRender(scope);
  expect(saveRecallSelection).toHaveBeenLastCalledWith(scope, ["high_recall", "question_extraction"], 0);
  expect(selections()).toEqual([["high_recall", "question_extraction"]]);
  vi.mocked(getRecallSelection).mockResolvedValue(remoteValue(scope, ["low_recall"], 2));
  await act(async () => window.dispatchEvent(new Event("focus")));
  expect(selections()).toEqual([["low_recall"]]);
});

it("existing server choices win over stale local storage without a migration write", async () => {
  const scope = "server-wins-library";
  localStorage.setItem(key(scope), JSON.stringify(["high_recall"]));
  vi.mocked(getRecallSelection).mockResolvedValue(remoteValue(scope, ["question_extraction"]));
  vi.mocked(saveRecallSelection).mockClear();
  await syncRender(scope);
  expect(saveRecallSelection).not.toHaveBeenCalled();
  expect(selections()).toEqual([["question_extraction"]]);
});

it("persists manual edits with a revision and displays save failures", async () => {
  const scope = "save-failure-library";
  vi.mocked(getRecallSelection).mockResolvedValue(remoteValue(scope, ["medium_recall"]));
  await syncRender(scope);
  vi.mocked(saveRecallSelection).mockRejectedValue(new Error("其他窗口已更新"));
  await act(async () => container.querySelector<HTMLButtonElement>("button")!.click());
  expect(saveRecallSelection).toHaveBeenLastCalledWith(scope, ["high_recall"], 1);
  expect(container.querySelector('[role="alert"]')?.textContent).toContain("其他窗口已更新");
  vi.mocked(getRecallSelection).mockResolvedValue(remoteValue(scope, ["low_recall"], 2));
  await act(async () => container.querySelector<HTMLButtonElement>('[role="alert"] button')!.click());
  expect(selections()).toEqual([["low_recall"]]);
});

it("does not expose a default as saved while loading and rejects another library identity", async () => {
  const scope = "mismatched-library";
  let finish!: (value: RecallSelection) => void;
  vi.mocked(getRecallSelection).mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  await syncRender(scope);
  expect(selections()).toEqual([[]]);
  await act(async () => finish(remoteValue("other-library", ["high_recall"])));
  expect(container.querySelector('[role="alert"]')?.textContent).toContain("视频库身份");
  expect(selections()).toEqual([[]]);
});

it("serializes fast edits without losing the last selection", async () => {
  const scope = "serialized-library";
  vi.mocked(getRecallSelection).mockResolvedValue(remoteValue(scope, ["medium_recall"]));
  await syncRender(scope);
  let finish!: (value: RecallSelection) => void;
  vi.mocked(saveRecallSelection).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }))
    .mockImplementationOnce(async (_id, modes) => remoteValue(scope, modes, 3));
  await act(async () => container.querySelectorAll<HTMLButtonElement>("button")[0].click());
  await act(async () => container.querySelectorAll<HTMLButtonElement>("button")[1].click());
  await act(async () => finish(remoteValue(scope, ["high_recall"], 2)));
  expect(saveRecallSelection).toHaveBeenLastCalledWith(scope, ["low_recall", custom], 2);
  expect(selections()).toEqual([["low_recall", custom]]);
});

it("does not let a delayed refresh overwrite a newer successful save", async () => {
  const scope = "late-refresh-library";
  vi.mocked(getRecallSelection).mockResolvedValue(remoteValue(scope, ["medium_recall"]));
  await syncRender(scope);
  let finish!: (value: RecallSelection) => void;
  vi.mocked(getRecallSelection).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  act(() => window.dispatchEvent(new Event("focus")));
  vi.mocked(saveRecallSelection).mockImplementationOnce(async (_id, modes) => remoteValue(scope, modes, 2));
  await act(async () => container.querySelector<HTMLButtonElement>("button")!.click());
  await act(async () => finish(remoteValue(scope, ["medium_recall"])));
  expect(selections()).toEqual([["high_recall"]]);
});

it("accepts a competing first migration without overwriting its saved choice", async () => {
  const scope = "competing-migration-library";
  localStorage.setItem(key(scope), JSON.stringify(["high_recall"]));
  vi.mocked(getRecallSelection).mockResolvedValueOnce(remoteValue(scope, ["medium_recall"], 0))
    .mockResolvedValueOnce(remoteValue(scope, ["question_extraction"]));
  vi.mocked(saveRecallSelection).mockRejectedValueOnce(new Error("version conflict"));
  await syncRender(scope);
  expect(selections()).toEqual([["question_extraction"]]);
  expect(container.querySelector('[role="alert"]')).toBeNull();
});

it.each(["during", "after"])("ignores an old refresh failure %s a successful save", async timing => {
  const scope = `late-refresh-error-${timing}`;
  vi.mocked(getRecallSelection).mockResolvedValue(remoteValue(scope, ["medium_recall"]));
  await syncRender(scope);
  let fail!: (reason: Error) => void;
  vi.mocked(getRecallSelection).mockImplementationOnce(() => new Promise((_resolve, reject) => { fail = reject; }));
  act(() => window.dispatchEvent(new Event("focus")));
  let finish!: (value: RecallSelection) => void;
  vi.mocked(saveRecallSelection).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  await act(async () => container.querySelector<HTMLButtonElement>("button")!.click());
  if (timing === "during") await act(async () => fail(new Error("old refresh failed")));
  await act(async () => finish(remoteValue(scope, ["high_recall"], 2)));
  if (timing === "after") await act(async () => fail(new Error("old refresh failed")));
  expect(container.querySelector('[role="alert"]')).toBeNull();
  expect(selections()).toEqual([["high_recall"]]);
  vi.mocked(getRecallSelection).mockResolvedValue(remoteValue(scope, ["low_recall"], 3));
  await act(async () => window.dispatchEvent(new Event("focus")));
  expect(selections()).toEqual([["low_recall"]]);
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
