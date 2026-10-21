import { waitForExit } from "./test/waitForExit";
import { act, type RefObject } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { getMarkers, getSubtitles, getVideo, updateMarkerImportance, updateMarkerJumpTime, updateMarkerStatus } from "./api/client";
import type { Marker } from "./types";

vi.mock("./api/client", async (importOriginal) => ({
  ...await importOriginal<typeof import("./api/client")>(),
  getMarkers: vi.fn(),
  getSubtitles: vi.fn(),
  getVideo: vi.fn(),
  updateMarkerJumpTime: vi.fn(),
  updateMarkerImportance: vi.fn(),
  updateMarkerStatus: vi.fn(),
}));

vi.mock("./components/VideoPanel", () => ({
  VideoPanel: ({ videoRef, onTimeUpdate }: { videoRef: RefObject<HTMLVideoElement>; onTimeUpdate: (time: number) => void }) =>
    <video ref={videoRef} onTimeUpdate={(event) => onTimeUpdate(event.currentTarget.currentTime)} />,
}));

const markers: Marker[] = [
  { id: 1, video_id: 1, start_ms: 529100, end_ms: 600000, title: "First point", note: "", source_quote: "", analysis_mode: "medium_recall", review_status: "active" },
  { id: 2, video_id: 1, start_ms: 900000, end_ms: 950000, title: "Second point", note: "", source_quote: "", analysis_mode: "medium_recall", review_status: "active" },
];

let container: HTMLDivElement;
let root: Root;

async function flushUpdates() {
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
}

function click(selector: string, index = 0) {
  const button = container.querySelectorAll<HTMLButtonElement>(selector)[index];
  expect(button).toBeDefined();
  act(() => button.click());
}

async function saveTime(seconds: number, index = 0) {
  click(".marker-edit-time", index);
  act(() => {
    const video = container.querySelector("video")!;
    video.currentTime = seconds;
    video.dispatchEvent(new Event("timeupdate"));
  });
  await act(async () => click(".marker-editor-tools button"));
  await flushUpdates();
}

beforeEach(async () => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.mocked(getVideo).mockResolvedValue({
    value: { id: 1, filename: "lesson.mp4", display_name: "Lesson", video_path: "lesson.mp4", duration_ms: 1800000, status: "review_ready" },
    source: "api",
  });
  vi.mocked(getSubtitles).mockResolvedValue({ value: [], source: "api" });
  vi.mocked(getMarkers).mockResolvedValue({ value: markers.map((marker) => ({ ...marker })), source: "api" });
  vi.mocked(updateMarkerJumpTime).mockImplementation(async (id, time) => ({
    ...markers.find((marker) => marker.id === id)!,
    user_start_ms: time,
    user_start_updated_at: time === null ? null : "2026-10-14T12:00:00",
  }));
  vi.mocked(updateMarkerImportance).mockImplementation(async (id, importance) => ({
    ...markers.find((marker) => marker.id === id)!, importance,
  }));
  vi.mocked(updateMarkerStatus).mockImplementation(async (id, review_status) => ({
    ...markers.find((marker) => marker.id === id)!, review_status,
  }));
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
  window.history.replaceState({}, "", "/review/1");
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => root.render(<App />));
  await vi.waitFor(async () => {
    await flushUpdates();
    expect(container.querySelectorAll(".marker-main")).toHaveLength(2);
  });
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
  vi.clearAllMocks();
});

describe("saved jump time with the real query cache", () => {
  it("updates importance immediately without jumping, viewing or reordering points", async () => {
    const media = container.querySelector("video")!;
    media.currentTime = 100;
    click(".marker-importance-swatch.importance-critical");
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector(".marker-importance-label")?.textContent).toBe("很重要");
    });
    expect(updateMarkerImportance).toHaveBeenCalledWith(1, "critical");
    expect(media.currentTime).toBe(100);
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
    expect(container.querySelectorAll(".marker-item.is-viewed")).toHaveLength(0);
    expect([...container.querySelectorAll(".marker-main strong")].map((node) => node.textContent)).toEqual(["First point", "Second point"]);
    expect(container.querySelectorAll(".marker-importance-label")[1].textContent).toBe("标准");
  });

  it("preserves a jump-time update when an earlier importance response arrives late", async () => {
    let resolve!: (marker: Marker) => void;
    vi.mocked(updateMarkerImportance).mockImplementation(() => new Promise((done) => { resolve = done; }));
    click(".marker-importance-swatch.importance-important");
    await saveTime(450);
    expect(container.querySelector(".marker-time-jump")?.textContent).toBe("7:30");
    await act(async () => resolve({ ...markers[0], importance: "important" }));
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector(".marker-importance-label")?.textContent).toBe("重要");
    });
    expect(container.querySelector(".marker-time-jump")?.textContent).toBe("7:30");
  });

  it("keeps importance and adjusted time after remove and undo responses", async () => {
    click(".marker-importance-swatch.importance-critical");
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelector(".marker-importance-label")?.textContent).toBe("很重要");
    });
    await saveTime(450);
    click(".danger-action");
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelectorAll(".marker-main")).toHaveLength(1);
    });
    click(".undo-banner button");
    await vi.waitFor(async () => {
      await flushUpdates();
      expect(container.querySelectorAll(".marker-main")).toHaveLength(2);
    });
    expect(container.querySelector(".marker-importance-label")?.textContent).toBe("很重要");
    expect(container.querySelector(".marker-time-jump")?.textContent).toBe("7:30");
  });

  it("S saves the last clicked marker across another interval and immediately updates future jumps", async () => {
    const video = container.querySelector("video")!;
    click(".marker-main");
    act(() => {
      video.currentTime = 910.9;
      video.dispatchEvent(new Event("timeupdate"));
    });
    const playCalls = vi.mocked(HTMLMediaElement.prototype.play).mock.calls.length;
    await act(async () => { document.dispatchEvent(new KeyboardEvent("keydown", { key: "s", bubbles: true })); });
    await flushUpdates();
    expect(updateMarkerJumpTime).toHaveBeenCalledWith(1, 910000);
    expect(container.querySelector(".marker-time-jump")?.textContent).toBe("15:10");
    expect(container.querySelectorAll(".marker-time-jump")[1].textContent).toBe("15:00");
    expect(container.querySelector(".marker-time-adjusted")?.textContent).toContain("8:49");
    expect(video.currentTime).toBe(910.9);
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(playCalls);
    await waitForExit(container, ".marker-time-editor");
    click(".sidebar-nav button", 1);
    expect(video.currentTime).toBe(900);
    act(() => { document.dispatchEvent(new KeyboardEvent("keydown", { key: "a", bubbles: true })); });
    expect(video.currentTime).toBe(910);
    click(".marker-main");
    expect(video.currentTime).toBe(910);
    expect(getMarkers).toHaveBeenCalledTimes(1);
  });

  it("S preserves the old cached time on failure and retries with the latest player position", async () => {
    vi.mocked(updateMarkerJumpTime).mockRejectedValueOnce(new Error("Save failed"));
    click(".marker-main");
    const video = container.querySelector("video")!;
    video.currentTime = 450;
    await act(async () => { document.dispatchEvent(new KeyboardEvent("keydown", { key: "s", bubbles: true })); });
    await flushUpdates();
    expect(container.querySelector("[role=alert]")?.textContent).toContain("Save failed");
    expect(container.querySelector(".marker-time-jump")?.textContent).toBe("8:49");
    video.currentTime = 451;
    await act(async () => { document.dispatchEvent(new KeyboardEvent("keydown", { key: "S", bubbles: true })); });
    await flushUpdates();
    expect(updateMarkerJumpTime).toHaveBeenLastCalledWith(1, 451000);
    await waitForExit(container, "[role=alert]");
    click(".marker-main");
    expect(video.currentTime).toBe(451);
  });

  it("immediately updates timestamp, card, and previous/next jumps without refetching or remounting the player", async () => {
    const video = container.querySelector("video")!;
    await saveTime(450);
    await waitForExit(container, ".marker-time-editor");
    expect(container.querySelector(".marker-time-jump")?.textContent).toBe("7:30");
    expect(container.querySelector("video")).toBe(video);
    expect(video.currentTime).toBe(450);
    expect(container.querySelectorAll(".is-viewed")).toHaveLength(0);
    act(() => { video.currentTime = 100; });
    click(".marker-time-jump");
    expect(video.currentTime).toBe(450);
    click(".marker-main");
    expect(video.currentTime).toBe(450);
    click(".sidebar-nav button", 1);
    expect(video.currentTime).toBe(900);
    click(".sidebar-nav button");
    expect(video.currentTime).toBe(450);
    expect(getMarkers).toHaveBeenCalledTimes(1);
  });

  it("uses zero captured from the player immediately after one-click saving", async () => {
    const video = container.querySelector("video")!;
    click(".marker-edit-time");
    await act(async () => click(".marker-editor-tools button"));
    await flushUpdates();
    expect(updateMarkerJumpTime).toHaveBeenCalledWith(1, 0);
    expect(container.querySelector(".marker-time-jump")?.textContent).toBe("0:00");
    act(() => { video.currentTime = 200; });
    click(".marker-main");
    expect(video.currentTime).toBe(0);
  });

  it("immediately restores the original jump and preserves the saved cache across app rerenders", async () => {
    await saveTime(450);
    act(() => root.render(<App />));
    expect(container.querySelector(".marker-time-jump")?.textContent).toBe("7:30");
    click(".marker-edit-time");
    await act(async () => click(".marker-reset-time"));
    await flushUpdates();
    expect(container.querySelector(".marker-time-adjusted")).toBeNull();
    click(".marker-main");
    expect(container.querySelector("video")!.currentTime).toBe(529.1);
    expect(getMarkers).toHaveBeenCalledTimes(1);
  });

  it("keeps the previous jump time when saving fails and makes the retry effective immediately", async () => {
    vi.mocked(updateMarkerJumpTime).mockRejectedValueOnce(new Error("Save failed"));
    await saveTime(450);
    expect(container.querySelector("[role=alert]")?.textContent).toBe("Save failed");
    expect(container.querySelector(".marker-time-jump")?.textContent).toBe("8:49");
    await act(async () => click(".marker-editor-tools button"));
    await flushUpdates();
    click(".marker-main");
    expect(container.querySelector("video")!.currentTime).toBe(450);
  });
});
