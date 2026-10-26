import { act, type RefObject } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import App from "./App";
import * as api from "./api/client";
import type { Marker } from "./types";

vi.mock("./api/client", async (importOriginal) => ({
  ...await importOriginal<typeof import("./api/client")>(),
  getMarkers: vi.fn(), getSubtitles: vi.fn(), getVideo: vi.fn(), getAnalysisRuns: vi.fn(),
  updateQuestionEmphasis: vi.fn(), updateMarkerStatus: vi.fn(),
}));
vi.mock("./components/VideoPanel", () => ({
  VideoPanel: ({ videoRef }: { videoRef: RefObject<HTMLVideoElement> }) => <video ref={videoRef} />,
}));

let container: HTMLDivElement;
let root: Root;
let markers: Marker[];

async function flush() {
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)); });
}

beforeEach(async () => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  markers = [
    { id: 1, video_id: 1, start_ms: 720_000, end_ms: 900_000, title: "First example", note: "", source_quote: "",
      analysis_mode: "question_extraction", review_status: "active", question_emphasized: true },
    { id: 2, video_id: 1, start_ms: 1_440_000, end_ms: 1_620_000, title: "Second example", note: "", source_quote: "",
      analysis_mode: "question_extraction", review_status: "active", question_emphasized: false },
    { id: 3, video_id: 1, start_ms: 660_000, end_ms: 661_000, title: "Exam point", note: "", source_quote: "",
      analysis_mode: "medium_recall", review_status: "active" },
  ];
  vi.mocked(api.getVideo).mockResolvedValue({
    value: { id: 1, filename: "lesson.mp4", display_name: "Lesson", video_path: "lesson.mp4", duration_ms: 3_600_000, status: "review_ready" },
    source: "api",
  });
  vi.mocked(api.getSubtitles).mockResolvedValue({ value: [], source: "api" });
  vi.mocked(api.getMarkers).mockImplementation(async () => ({ value: markers.map(marker => ({ ...marker })), source: "api" }));
  vi.mocked(api.getAnalysisRuns).mockResolvedValue([]);
  vi.mocked(api.updateQuestionEmphasis).mockImplementation(async (id, emphasized) => ({
    ...markers.find(marker => marker.id === id)!, question_emphasized: emphasized ?? id === 1, question_emphasis_override: emphasized,
  }));
  vi.mocked(api.updateMarkerStatus).mockImplementation(async (id, review_status) => ({
    ...markers.find(marker => marker.id === id)!, review_status,
  }));
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
  window.history.replaceState({}, "", "/review/1");
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => root.render(<App />));
  await vi.waitFor(async () => {
    await flush();
    expect(container.querySelector(".mode-switcher")).not.toBeNull();
  });
  act(() => [...container.querySelectorAll<HTMLButtonElement>(".mode-switcher button")]
    .find(button => button.textContent?.includes("题目提取"))!.click());
  await vi.waitFor(async () => {
    await flush();
    expect(container.querySelectorAll(".question-emphasis-toggle")).toHaveLength(2);
  });
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
  vi.clearAllMocks();
});

function toggle(index = 0) {
  return container.querySelectorAll<HTMLButtonElement>(".question-emphasis-toggle")[index];
}

it("shows automatic emphasis and saves manual choices without changing time order or playback", async () => {
  expect(toggle().getAttribute("aria-pressed")).toBe("true");
  expect(container.querySelectorAll(".is-question-emphasized")).toHaveLength(1);
  await act(async () => toggle().click());
  await vi.waitFor(async () => {
    await flush();
    expect(toggle().getAttribute("aria-pressed")).toBe("false");
    expect(container.querySelectorAll(".is-question-emphasized")).toHaveLength(0);
  });
  expect(api.updateQuestionEmphasis).toHaveBeenCalledWith(1, false);
  await act(async () => toggle(1).click());
  await vi.waitFor(async () => {
    await flush();
    expect(toggle(1).getAttribute("aria-pressed")).toBe("true");
    expect(container.querySelectorAll(".is-question-emphasized")).toHaveLength(1);
  });
  expect(api.updateQuestionEmphasis).toHaveBeenCalledWith(2, true);
  expect([...container.querySelectorAll(".marker-main strong")].map(node => node.textContent))
    .toEqual(["First example", "Second example"]);
  expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
  expect(container.querySelectorAll(".marker-item.is-viewed")).toHaveLength(0);
});

it("keeps the previous emphasis on save failure and allows retry", async () => {
  vi.mocked(api.updateQuestionEmphasis).mockRejectedValueOnce(new Error("offline"));
  await act(async () => toggle().click());
  await vi.waitFor(async () => {
    await flush();
    expect(container.querySelector(".question-emphasis-control [role=alert]")?.textContent).toContain("保存失败");
  });
  expect(toggle().getAttribute("aria-pressed")).toBe("true");
  expect(toggle().disabled).toBe(false);
  await act(async () => toggle().click());
  await vi.waitFor(async () => {
    await flush();
    expect(toggle().getAttribute("aria-pressed")).toBe("false");
  });
  expect(container.querySelector(".question-emphasis-control [role=alert]")).toBeNull();
});

it("removes an emphasized example and restores it through undo", async () => {
  await act(async () => container.querySelector<HTMLButtonElement>(".danger-action")!.click());
  await vi.waitFor(async () => {
    await flush();
    expect(container.querySelectorAll(".question-emphasis-toggle")).toHaveLength(1);
  });
  expect(api.updateMarkerStatus).toHaveBeenCalledWith(1, "excluded");
  await act(async () => container.querySelector<HTMLButtonElement>(".undo-banner button")!.click());
  await vi.waitFor(async () => {
    await flush();
    expect(container.querySelectorAll(".question-emphasis-toggle")).toHaveLength(2);
  });
  expect(toggle().getAttribute("aria-pressed")).toBe("true");
});

it("refreshes automatic emphasis after a nearby exam point is removed", async () => {
  vi.mocked(api.getMarkers).mockImplementation(async () => ({
    value: markers.map(marker => marker.id === 1 ? { ...marker, question_emphasized: false }
      : marker.id === 3 ? { ...marker, review_status: "excluded" as const } : { ...marker }),
    source: "api",
  }));
  act(() => [...container.querySelectorAll<HTMLButtonElement>(".mode-switcher button")]
    .find(button => button.textContent?.includes("中召回"))!.click());
  await vi.waitFor(async () => {
    await flush();
    expect(container.querySelector(".marker-main strong")?.textContent).toBe("Exam point");
  });
  await act(async () => container.querySelector<HTMLButtonElement>(".danger-action")!.click());
  await vi.waitFor(async () => {
    await flush();
    expect(api.getMarkers).toHaveBeenCalledTimes(2);
  });
  act(() => [...container.querySelectorAll<HTMLButtonElement>(".mode-switcher button")]
    .find(button => button.textContent?.includes("题目提取"))!.click());
  await vi.waitFor(async () => {
    await flush();
    expect(toggle().getAttribute("aria-pressed")).toBe("false");
  });
});
