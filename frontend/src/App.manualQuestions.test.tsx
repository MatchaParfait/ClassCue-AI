import { act, type RefObject } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import App from "./App";
import * as api from "./api/client";
import { defaultShortcuts, saveShortcuts } from "./shortcuts";
import type { Marker } from "./types";
import * as questionImages from "./api/questionVisuals";
import * as frameCapture from "./utils/captureVideoFrame";

vi.mock("./api/questionVisuals", async original => ({
  ...await original<typeof import("./api/questionVisuals")>(),
  getQuestionVisuals: vi.fn().mockResolvedValue({ items: [], active: false }),
  assignQuestionImages: vi.fn().mockResolvedValue({ items: [], active: false }),
  captureQuestionImage: vi.fn(), setQuestionImage: vi.fn(),
}));

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
  vi.mocked(questionImages.getQuestionVisuals).mockResolvedValue({ items: [], active: false });
  vi.mocked(questionImages.assignQuestionImages).mockResolvedValue({ items: [], active: false });
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

it("Ctrl+Z undoes a new manual point without seeking, and the point remains recoverable", async () => {
  key("w");
  await until(() => expect(row(4)).not.toBeNull());
  expect(container.querySelector(".undo-banner")?.textContent).toContain("已新增手动题目");
  expect(key("z", { ctrlKey: true }).defaultPrevented).toBe(true);
  await until(() => expect(row(4)).toBeNull());
  expect(api.updateMarkerStatus).toHaveBeenLastCalledWith(4, "excluded");
  expect(container.querySelector(".excluded-section")?.textContent).toContain("手动题目");
  expect(container.querySelector("video")!.currentTime).toBe(150.425);
  expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
  expect(HTMLMediaElement.prototype.pause).not.toHaveBeenCalled();
  expect(key("z", { ctrlKey: true }).defaultPrevented).toBe(false);
  await act(async () => container.querySelector<HTMLButtonElement>(".excluded-row button")!.click());
  await until(() => expect(row(4)).not.toBeNull());
});

it("undoes removals and multiple creations in reverse operation order", async () => {
  key("w");
  await until(() => expect(row(4)).not.toBeNull());
  container.querySelector("video")!.currentTime = 155;
  key("w");
  await until(() => expect(row(5)).not.toBeNull());
  await act(async () => row(1).querySelector<HTMLButtonElement>(".danger-action")!.click());
  await until(() => expect(row(1)).toBeNull());
  key("z", { ctrlKey: true });
  await until(() => expect(row(1)).not.toBeNull());
  expect(api.updateMarkerStatus).toHaveBeenLastCalledWith(1, "active");
  key("z", { ctrlKey: true });
  await until(() => expect(row(5)).toBeNull());
  expect(api.updateMarkerStatus).toHaveBeenLastCalledWith(5, "excluded");
  key("z", { ctrlKey: true });
  await until(() => expect(row(4)).toBeNull());
  expect(api.updateMarkerStatus).toHaveBeenLastCalledWith(4, "excluded");
  expect(row(2)).not.toBeNull();
  expect(container.querySelector(".undo-banner")).toBeNull();
});

it("preserves undo history after a failed undo and retries the same action", async () => {
  key("w");
  await until(() => expect(row(4)).not.toBeNull());
  vi.mocked(api.updateMarkerStatus).mockRejectedValueOnce(new Error("Undo unavailable"));
  key("z", { ctrlKey: true });
  await until(() => expect(container.querySelector("[role=alert]")?.textContent).toContain("撤销失败"));
  expect(row(4)).not.toBeNull();
  expect(container.querySelector<HTMLButtonElement>(".undo-banner button")!.disabled).toBe(false);
  await act(async () => container.querySelector<HTMLButtonElement>(".undo-banner button")!.click());
  await until(() => expect(row(4)).toBeNull());
  expect(vi.mocked(api.updateMarkerStatus).mock.calls).toEqual([[4, "excluded"], [4, "excluded"]]);
});

it("does not replace the last successful action with a failed creation or removal", async () => {
  vi.mocked(api.createManualQuestion).mockRejectedValueOnce(new Error("Create unavailable"));
  key("w");
  await until(() => expect(container.querySelector("[role=alert]")?.textContent).toContain("Create unavailable"));
  expect(key("z", { ctrlKey: true }).defaultPrevented).toBe(false);
  expect(api.updateMarkerStatus).not.toHaveBeenCalled();
  key("w");
  await until(() => expect(row(4)).not.toBeNull());
  vi.mocked(api.updateMarkerStatus).mockRejectedValueOnce(new Error("Remove unavailable"));
  await act(async () => row(1).querySelector<HTMLButtonElement>(".danger-action")!.click());
  await until(() => expect(container.querySelector("[role=alert]")?.textContent).toContain("Remove unavailable"));
  expect(row(1)).not.toBeNull();
  key("z", { ctrlKey: true });
  await until(() => expect(row(4)).toBeNull());
  expect(api.updateMarkerStatus).toHaveBeenLastCalledWith(4, "excluded");
});

it("does not double-submit undo or allow a new creation while undo is pending", async () => {
  key("w");
  await until(() => expect(row(4)).not.toBeNull());
  key("w");
  await until(() => expect(row(5)).not.toBeNull());
  let finish!: (marker: Marker) => void;
  vi.mocked(api.updateMarkerStatus).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  key("z", { ctrlKey: true });
  key("z", { ctrlKey: true });
  key("z", { ctrlKey: true, repeat: true });
  key("w");
  await until(() => expect(api.updateMarkerStatus).toHaveBeenCalledTimes(1));
  expect(api.createManualQuestion).toHaveBeenCalledTimes(2);
  expect(container.querySelector<HTMLButtonElement>(".undo-banner button")!.disabled).toBe(true);
  await act(async () => finish({ ...markers.find(marker => marker.id === 5)!, review_status: "excluded" }));
  await until(() => expect(row(5)).toBeNull());
  expect(row(4)).not.toBeNull();
  key("z", { ctrlKey: true });
  await until(() => expect(row(4)).toBeNull());
});

it("does not undo an older action while a new manual point is still saving", async () => {
  key("w");
  await until(() => expect(row(4)).not.toBeNull());
  let finish!: (marker: Marker) => void;
  const create = vi.mocked(api.createManualQuestion).getMockImplementation()!;
  vi.mocked(api.createManualQuestion).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  key("w");
  await until(() => expect(api.createManualQuestion).toHaveBeenCalledTimes(2));
  key("z", { ctrlKey: true });
  expect(api.updateMarkerStatus).not.toHaveBeenCalled();
  await act(async () => finish(await create(1, 150_425)));
  await until(() => expect(row(5)).not.toBeNull());
  key("z", { ctrlKey: true });
  await until(() => expect(row(5)).toBeNull());
  expect(row(4)).not.toBeNull();
});

it("preserves native text undo and ignores redo, extra modifiers, held keys, and modals", async () => {
  key("w");
  await until(() => expect(row(4)).not.toBeNull());
  for (const options of [
    { ctrlKey: true, shiftKey: true }, { ctrlKey: true, altKey: true },
    { ctrlKey: true, repeat: true }, { ctrlKey: true, isComposing: true },
  ]) expect(key("z", options).defaultPrevented).toBe(false);
  act(() => row(4).querySelector<HTMLButtonElement>(".marker-main")!.click());
  const input = row(4).querySelector<HTMLInputElement>("input")!;
  expect(key("z", { ctrlKey: true }, input).defaultPrevented).toBe(false);
  key("Escape", {}, input);
  await until(() => expect(row(4).querySelector("input")).toBeNull());
  const editable = document.createElement("div");
  editable.setAttribute("contenteditable", "true");
  editable.innerHTML = "<span>Editable text</span>";
  container.append(editable);
  expect(key("z", { ctrlKey: true }, editable.firstChild!).defaultPrevented).toBe(false);
  editable.remove();
  const dialog = document.createElement("div");
  dialog.setAttribute("role", "dialog");
  dialog.setAttribute("aria-modal", "true");
  container.append(dialog);
  expect(key("z", { ctrlKey: true }).defaultPrevented).toBe(false);
  dialog.remove();
  expect(api.updateMarkerStatus).not.toHaveBeenCalled();
  expect(key("z", { metaKey: true }).defaultPrevented).toBe(true);
  await until(() => expect(row(4)).toBeNull());
});

it("does not carry the current video's undo history to another review session", async () => {
  key("w");
  await until(() => expect(row(4)).not.toBeNull());
  act(() => {
    window.history.pushState({}, "", "/review/2");
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
  await until(() => expect(api.getVideo).toHaveBeenCalledWith(2));
  expect(key("z", { ctrlKey: true }).defaultPrevented).toBe(false);
  expect(api.updateMarkerStatus).not.toHaveBeenCalled();
});

async function rename(id: number, title: string) {
  act(() => row(id).querySelector<HTMLButtonElement>(".marker-main")!.click());
  const input = row(id).querySelector<HTMLInputElement>('input[aria-label="题目名称"]')!;
  act(() => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, title);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await act(async () => row(id).querySelector<HTMLButtonElement>('button[type=submit]')!.click());
  await until(() => expect(row(id).querySelector("input")).toBeNull());
}

it("undoes time, importance, emphasis, title and creation together in reverse order", async () => {
  key("w");
  await until(() => expect(row(4)).not.toBeNull());
  await rename(4, "Named question");
  await act(async () => row(4).querySelector<HTMLButtonElement>(".question-emphasis-toggle")!.click());
  await until(() => expect(row(4).classList.contains("is-question-emphasized")).toBe(true));
  await act(async () => row(4).querySelector<HTMLButtonElement>(".importance-critical.marker-importance-swatch")!.click());
  await until(() => expect(row(4).querySelector(".marker-importance-label")?.textContent).toBe("很重要"));
  container.querySelector("video")!.currentTime = 180;
  key("s");
  await until(() => expect(row(4).querySelector(".marker-time-jump")?.textContent).toBe("3:00"));
  key("z", { ctrlKey: true });
  await until(() => expect(api.updateMarkerJumpTime).toHaveBeenLastCalledWith(4, null));
  await until(() => expect(row(4).querySelector(".marker-time-adjusted")).toBeNull());
  key("z", { ctrlKey: true });
  await until(() => expect(row(4).querySelector(".marker-importance-label")?.textContent).toBe("标准"));
  key("z", { ctrlKey: true });
  await until(() => expect(api.updateQuestionEmphasis).toHaveBeenLastCalledWith(4, null));
  await until(() => expect(row(4).classList.contains("is-question-emphasized")).toBe(false));
  key("z", { ctrlKey: true });
  await until(() => expect(row(4).querySelector(".marker-main strong")?.textContent).toBe("手动题目 · 02:30"));
  key("z", { ctrlKey: true });
  await until(() => expect(row(4)).toBeNull());
  expect(key("z", { ctrlKey: true }).defaultPrevented).toBe(false);
  expect(container.querySelector("video")!.currentTime).toBe(180);
  expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
});

it("restores a previous adjusted time when undoing a reset to the original position", async () => {
  key("w");
  await until(() => expect(row(4)).not.toBeNull());
  container.querySelector("video")!.currentTime = 180;
  key("s");
  await until(() => expect(row(4).querySelector(".marker-time-adjusted")).not.toBeNull());
  act(() => row(4).querySelector<HTMLButtonElement>(".marker-edit-time")!.click());
  await act(async () => row(4).querySelector<HTMLButtonElement>(".marker-reset-time")!.click());
  await until(() => expect(row(4).querySelector(".marker-time-editor")).toBeNull());
  expect(row(4).querySelector(".marker-time-adjusted")).toBeNull();
  key("z", { ctrlKey: true });
  await until(() => expect(api.updateMarkerJumpTime).toHaveBeenLastCalledWith(4, 180_000));
  await until(() => expect(row(4).querySelector(".marker-time-jump")?.textContent).toBe("3:00"));
});

it("preserves manual false emphasis rather than reverting to automatic judgment", async () => {
  await act(async () => root.render(null));
  markers[0].question_emphasis_override = false;
  await act(async () => root.render(<App />));
  await until(() => expect(container.querySelector(".mode-switcher")).not.toBeNull());
  switchQuestions();
  await until(() => expect(row(1)).not.toBeNull());
  await act(async () => row(1).querySelector<HTMLButtonElement>(".question-emphasis-toggle")!.click());
  await until(() => expect(row(1).classList.contains("is-question-emphasized")).toBe(true));
  key("z", { ctrlKey: true });
  await until(() => expect(api.updateQuestionEmphasis).toHaveBeenLastCalledWith(1, false));
  await until(() => expect(container.querySelector(".undo-banner")).toBeNull());
});

it("orders concurrent field edits by the user's action order and waits for all saves", async () => {
  key("w");
  await until(() => expect(row(4)).not.toBeNull());
  let finish!: (marker: Marker) => void;
  vi.mocked(api.updateMarkerImportance).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  act(() => row(4).querySelector<HTMLButtonElement>(".importance-critical.marker-importance-swatch")!.click());
  await until(() => expect(api.updateMarkerImportance).toHaveBeenCalledTimes(1));
  container.querySelector("video")!.currentTime = 180;
  key("s");
  await until(() => expect(row(4).querySelector(".marker-time-jump")?.textContent).toBe("3:00"));
  expect(key("z", { ctrlKey: true }).defaultPrevented).toBe(false);
  expect(container.querySelector<HTMLButtonElement>(".undo-banner button")!.disabled).toBe(true);
  await act(async () => finish({ ...markers.find(marker => marker.id === 4)!, importance: "critical" }));
  await until(() => expect(container.querySelector<HTMLButtonElement>(".undo-banner button")!.disabled).toBe(false));
  key("z", { ctrlKey: true });
  await until(() => expect(row(4).querySelector(".marker-time-adjusted")).toBeNull());
  expect(row(4).querySelector(".marker-importance-label")?.textContent).toBe("很重要");
  key("z", { ctrlKey: true });
  await until(() => expect(row(4).querySelector(".marker-importance-label")?.textContent).toBe("标准"));
});

it("keeps edit history retryable and serializes repeated undo for a title change", async () => {
  key("w");
  await until(() => expect(row(4)).not.toBeNull());
  await rename(4, "New name");
  vi.mocked(api.renameManualQuestion).mockRejectedValueOnce(new Error("Rename undo unavailable"));
  key("z", { ctrlKey: true });
  await until(() => expect(container.querySelector("[role=alert]")?.textContent).toContain("撤销失败"));
  expect(row(4).querySelector(".marker-main strong")?.textContent).toBe("New name");
  let finish!: (marker: Marker) => void;
  vi.mocked(api.renameManualQuestion).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  key("z", { ctrlKey: true });
  key("z", { ctrlKey: true });
  key("w");
  await until(() => expect(api.renameManualQuestion).toHaveBeenCalledTimes(3));
  expect(api.createManualQuestion).toHaveBeenCalledTimes(1);
  expect(api.updateMarkerStatus).not.toHaveBeenCalled();
  await act(async () => finish({ ...markers.find(marker => marker.id === 4)!, title: "手动题目 · 02:30" }));
  await until(() => expect(row(4).querySelector(".marker-main strong")?.textContent).toBe("手动题目 · 02:30"));
  expect(container.querySelector(".undo-banner")?.textContent).toContain("已新增手动题目");
});

it("does not add unchanged or failed field saves to undo history", async () => {
  key("w");
  await until(() => expect(row(4)).not.toBeNull());
  container.querySelector("video")!.currentTime = 180;
  key("s");
  await until(() => expect(row(4).querySelector(".marker-time-jump")?.textContent).toBe("3:00"));
  key("s");
  await flush();
  expect(api.updateMarkerJumpTime).toHaveBeenCalledTimes(1);
  vi.mocked(api.updateMarkerImportance).mockRejectedValueOnce(new Error("Importance unavailable"));
  await act(async () => row(4).querySelector<HTMLButtonElement>(".importance-critical.marker-importance-swatch")!.click());
  await until(() => expect(row(4).querySelector("[role=alert]")?.textContent).toContain("保存失败"));
  key("z", { ctrlKey: true });
  await until(() => expect(api.updateMarkerJumpTime).toHaveBeenLastCalledWith(4, null));
  expect(container.querySelector(".undo-banner")?.textContent).toContain("已新增手动题目");
});

it("captures the current frame for the clicked question and Ctrl+Z restores its old image", async () => {
  switchQuestions();
  await until(() => expect(row(1)?.querySelector(".question-capture-current")).not.toBeNull());
  const frame = { blob: new Blob(["synthetic"], { type: "image/png" }), captured_at_ms: 150425 };
  const screenshot = vi.spyOn(frameCapture, "captureVideoFrame").mockResolvedValue(frame);
  const original = { capture_id: "old", binding_mode: "auto" as const };
  const visual = { marker_id: 1, question_id: "stable", status: "completed" as const,
    verdict: "uncertain" as const, reason: "", binding_mode: "manual" as const,
    algorithm: "question-image-time-v1",
    binding: { capture_id: "new", slide_id: null, source: "capture" as const, representative_ms: 150425 } };
  vi.mocked(questionImages.captureQuestionImage).mockResolvedValue({ visual, previous_image: original });
  vi.mocked(questionImages.setQuestionImage).mockResolvedValue({
    ...visual, binding_mode: "auto", binding: { ...visual.binding, capture_id: "old" },
  });
  await act(async () => row(1).querySelector<HTMLButtonElement>(".question-capture-current")!.click());
  await until(() => expect(questionImages.captureQuestionImage).toHaveBeenCalledWith(1, frame));
  expect(screenshot).toHaveBeenCalledWith(container.querySelector("video"));
  expect(container.querySelector("video")!.currentTime).toBe(150.425);
  expect(api.updateMarkerJumpTime).not.toHaveBeenCalled();
  expect(container.querySelector('[role="dialog"]')).toBeNull();
  key("z", { ctrlKey: true });
  await until(() => expect(questionImages.setQuestionImage).toHaveBeenCalledWith(1, original));
});

it("a failed current-frame capture leaves the image and undo history unchanged", async () => {
  switchQuestions();
  await until(() => expect(row(1)?.querySelector(".question-capture-current")).not.toBeNull());
  vi.spyOn(frameCapture, "captureVideoFrame").mockRejectedValue(new Error("Frame unavailable"));
  await act(async () => row(1).querySelector<HTMLButtonElement>(".question-capture-current")!.click());
  await until(() => expect(container.querySelector("[role=alert]")?.textContent).toContain("Frame unavailable"));
  expect(questionImages.captureQuestionImage).not.toHaveBeenCalled();
  expect(key("z", { ctrlKey: true }).defaultPrevented).toBe(false);
});
