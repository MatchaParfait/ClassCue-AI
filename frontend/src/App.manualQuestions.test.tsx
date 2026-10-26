import { act, type RefObject } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import App from "./App";
import * as api from "./api/client";
import { defaultShortcuts, saveShortcuts } from "./shortcuts";
import type { Marker } from "./types";

vi.mock("./api/client", async (importOriginal) => ({
  ...await importOriginal<typeof import("./api/client")>(),
  getMarkers: vi.fn(), getSubtitles: vi.fn(), getVideo: vi.fn(), getAnalysisRuns: vi.fn(),
  createManualQuestion: vi.fn(), renameManualQuestion: vi.fn(), updateQuestionEmphasis: vi.fn(),
  updateMarkerStatus: vi.fn(), updateMarkerJumpTime: vi.fn(), updateMarkerImportance: vi.fn(), getVideoProcessingOptions: vi.fn(),
}));
vi.mock("./components/VideoPanel", () => ({
  VideoPanel: ({ videoRef }: { videoRef: RefObject<HTMLVideoElement> }) => <video ref={videoRef} />,
}));

let container: HTMLDivElement, root: Root;
let markers: Marker[];
async function flush() {
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)); });
}
async function until(check: () => void) {
  await vi.waitFor(async () => { await flush(); check(); });
}
function key(value: string, options: KeyboardEventInit = {}, target: EventTarget = document) {
  const event = new KeyboardEvent("keydown", { key: value, bubbles: true, cancelable: true, ...options });
  act(() => target.dispatchEvent(event));
  return event;
}
function switchQuestions() {
  act(() => [...container.querySelectorAll<HTMLButtonElement>(".mode-switcher button")]
    .find(button => button.textContent?.includes("题目提取"))!.click());
}
function row(id: number) { return container.querySelector<HTMLElement>(`[data-marker-id="${id}"]`)!; }

beforeEach(async () => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const storage = new Map<string, string>();
  vi.stubGlobal("localStorage", { getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => storage.set(key, value) });
  markers = [
    { id: 1, video_id: 1, start_ms: 100_000, end_ms: 110_000, title: "Earlier example", note: "", source_quote: "",
      analysis_mode: "question_extraction", review_status: "active" },
    { id: 2, video_id: 1, start_ms: 200_000, end_ms: 210_000, title: "Later example", note: "", source_quote: "",
      analysis_mode: "question_extraction", review_status: "active" },
    { id: 3, video_id: 1, start_ms: 1000, end_ms: 2000, title: "Exam point", note: "", source_quote: "",
      analysis_mode: "medium_recall", review_status: "active" },
  ];
  vi.mocked(api.getVideo).mockResolvedValue({
    value: { id: 1, filename: "lesson.mp4", display_name: "Lesson", video_path: "lesson.mp4",
      duration_ms: 600_000, status: "review_ready" }, source: "api",
  });
  vi.mocked(api.getSubtitles).mockResolvedValue({ value: [], source: "api" });
  vi.mocked(api.getMarkers).mockImplementation(async () => ({ value: markers.map(marker => ({ ...marker })), source: "api" }));
  vi.mocked(api.getAnalysisRuns).mockResolvedValue([
    { analysis_mode: "question_extraction", status: "completed", candidate_count: 2 },
    { analysis_mode: "medium_recall", status: "completed", candidate_count: 1 },
  ]);
  vi.mocked(api.createManualQuestion).mockImplementation(async (videoId, timeMs) => {
    const created: Marker = {
      id: Math.max(...markers.map(marker => marker.id)) + 1, video_id: videoId,
      start_ms: timeMs, end_ms: timeMs + 1, title: "手动题目 · 02:30", note: "", source_quote: "",
      trigger_phrase: "MANUAL-question:fixture", analysis_mode: "question_extraction",
      review_status: "active", question_emphasized: false,
    };
    markers.push(created);
    return { ...created };
  });
  vi.mocked(api.renameManualQuestion).mockImplementation(async (id, title) => {
    const marker = markers.find(marker => marker.id === id)!;
    marker.title = title;
    return { ...marker };
  });
  vi.mocked(api.updateQuestionEmphasis).mockImplementation(async (id, emphasized) => ({
    ...markers.find(marker => marker.id === id)!, question_emphasized: emphasized ?? false, question_emphasis_override: emphasized,
  }));
  vi.mocked(api.updateMarkerJumpTime).mockImplementation(async (id, user_start_ms) => ({
    ...markers.find(marker => marker.id === id)!, user_start_ms,
  }));
  vi.mocked(api.updateMarkerImportance).mockImplementation(async (id, importance) => ({
    ...markers.find(marker => marker.id === id)!, importance,
  }));
  vi.mocked(api.updateMarkerStatus).mockImplementation(async (id, review_status) => ({
    ...markers.find(marker => marker.id === id)!, review_status,
  }));
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => undefined);
  window.history.replaceState({}, "", "/review/1");
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => root.render(<App />));
  await until(() => expect(container.querySelector(".mode-switcher")).not.toBeNull());
  container.querySelector("video")!.currentTime = 150.425;
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

it("W creates an independent point without selecting another item or interrupting playback", async () => {
  key("w");
  await until(() => expect(row(4)).not.toBeNull());
  expect(api.createManualQuestion).toHaveBeenCalledWith(1, 150_425);
  expect([...container.querySelectorAll<HTMLElement>(".marker-item")].map(item => item.dataset.markerId)).toEqual(["1", "4", "2"]);
  expect(row(4).classList.contains("is-new-question")).toBe(true);
  expect(row(4).classList.contains("is-selected")).toBe(true);
  expect(container.querySelectorAll(".marker-item.is-viewed")).toHaveLength(0);
  expect(container.querySelector("video")!.currentTime).toBe(150.425);
  expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
  expect(HTMLMediaElement.prototype.pause).not.toHaveBeenCalled();
});

it("the plus button also works from an empty viewed filter and reveals the new point", async () => {
  switchQuestions();
  await until(() => expect(container.querySelector(".add-manual-question")).not.toBeNull());
  act(() => [...container.querySelectorAll<HTMLButtonElement>(".status-filter button")]
    .find(button => button.textContent === "已看")!.click());
  await until(() => expect(container.querySelectorAll(".marker-item")).toHaveLength(0));
  await act(async () => container.querySelector<HTMLButtonElement>(".add-manual-question")!.click());
  await until(() => expect(row(4)).not.toBeNull());
  expect(container.querySelector(".status-filter [aria-selected=true]")?.textContent).toBe("全部");
});

it("ignores held keys and duplicate submissions while a save is pending", async () => {
  let resolve!: (marker: Marker) => void;
  const create = vi.mocked(api.createManualQuestion).getMockImplementation()!;
  vi.mocked(api.createManualQuestion).mockImplementation(() => new Promise(done => { resolve = done; }));
  key("w", { repeat: true });
  expect(api.createManualQuestion).not.toHaveBeenCalled();
  key("w");
  key("w");
  await until(() => expect(api.createManualQuestion).toHaveBeenCalledTimes(1));
  const created = await create(1, 150_425);
  await act(async () => resolve(created));
  await until(() => expect(row(4)).not.toBeNull());
});

it("keeps failed creates out of the list and permits retry", async () => {
  vi.mocked(api.createManualQuestion).mockRejectedValueOnce(new Error("Save unavailable"));
  key("w");
  await until(() => expect(container.querySelector('[role=alert]')?.textContent).toContain("Save unavailable"));
  expect(row(4)).toBeNull();
  expect(markers).toHaveLength(3);
  key("w");
  await until(() => expect(row(4)).not.toBeNull());
});

it("renames inline and suppresses W while typing without jumping", async () => {
  key("w");
  await until(() => expect(row(4)).not.toBeNull());
  act(() => row(4).querySelector<HTMLButtonElement>(".marker-main")!.click());
  const input = row(4).querySelector<HTMLInputElement>('input[aria-label="题目名称"]')!;
  key("w", {}, input);
  expect(api.createManualQuestion).toHaveBeenCalledTimes(1);
  const setValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
  act(() => {
    setValue.call(input, "  Added example  ");
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await act(async () => row(4).querySelector<HTMLButtonElement>('button[type=submit]')!.click());
  await until(() => expect(row(4).querySelector(".marker-main strong")?.textContent).toBe("Added example"));
  expect(api.renameManualQuestion).toHaveBeenCalledWith(4, "Added example");
  expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
});

it("keeps the title editor and draft on failure and supports Escape cancellation", async () => {
  key("w");
  await until(() => expect(row(4)).not.toBeNull());
  vi.mocked(api.renameManualQuestion).mockRejectedValueOnce(new Error("Rename unavailable"));
  act(() => row(4).querySelector<HTMLButtonElement>(".marker-main")!.click());
  const input = row(4).querySelector<HTMLInputElement>("input")!;
  act(() => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "Changed");
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await act(async () => row(4).querySelector<HTMLButtonElement>('button[type=submit]')!.click());
  await until(() => expect(row(4).querySelector("[role=alert]")?.textContent).toContain("Rename unavailable"));
  expect(input.value).toBe("Changed");
  key("Escape", {}, input);
  await until(() => expect(row(4).querySelector("input")).toBeNull());
  expect(row(4).querySelector(".marker-main strong")?.textContent).toBe("手动题目 · 02:30");
});

it("respects customized shortcuts and ignores typing, composition and dialogs", async () => {
  const map = defaultShortcuts();
  map.createQuestion = ["Q"];
  act(() => saveShortcuts(map));
  key("w");
  key("q", { isComposing: true });
  const input = document.createElement("input");
  container.append(input);
  key("q", {}, input);
  const dialog = document.createElement("div");
  dialog.setAttribute("role", "dialog");
  dialog.setAttribute("aria-modal", "true");
  container.append(dialog);
  key("q");
  expect(api.createManualQuestion).not.toHaveBeenCalled();
  dialog.remove();
  input.remove();
  key("q");
  await until(() => expect(row(4)).not.toBeNull());
});

it("keeps the AI extraction entry available when the list only contains manual questions", async () => {
  await act(async () => root.render(null));
  markers = markers.filter(marker => marker.analysis_mode !== "question_extraction");
  vi.mocked(api.getAnalysisRuns).mockResolvedValue([
    { analysis_mode: "medium_recall", status: "completed", candidate_count: 1 },
  ]);
  vi.mocked(api.getVideoProcessingOptions).mockResolvedValue(
    {} as Awaited<ReturnType<typeof api.getVideoProcessingOptions>>,
  );
  await act(async () => root.render(<App />));
  await until(() => expect(container.querySelector(".mode-switcher")).not.toBeNull());
  container.querySelector("video")!.currentTime = 150.425;
  key("w");
  await until(() => expect(container.querySelector(".recall-analysis-state")?.textContent).toContain("题目提取尚未识别"));
  expect(row(4)).not.toBeNull();
  expect(container.querySelector(".recall-analysis-state button")?.textContent).toContain("识别题目提取");
});

it("supports emphasis, removal, undo and timestamp jumps for a newly created point", async () => {
  key("w");
  await until(() => expect(row(4)).not.toBeNull());
  await act(async () => row(4).querySelector<HTMLButtonElement>(".question-emphasis-toggle")!.click());
  await until(() => expect(row(4).classList.contains("is-question-emphasized")).toBe(true));
  await act(async () => row(4).querySelector<HTMLButtonElement>(".danger-action")!.click());
  await until(() => expect(row(4)).toBeNull());
  await act(async () => container.querySelector<HTMLButtonElement>(".undo-banner button")!.click());
  await until(() => expect(row(4)).not.toBeNull());
  expect(row(4).classList.contains("is-question-emphasized")).toBe(true);
  container.querySelector("video")!.currentTime = 20;
  act(() => row(4).querySelector<HTMLButtonElement>(".marker-time-jump")!.click());
  expect(container.querySelector("video")!.currentTime).toBe(150.425);
  expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(1);
});
