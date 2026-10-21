import { defaultShortcuts, saveShortcuts } from "./shortcuts";
import { waitForExit } from "./test/waitForExit";
import { act, type RefObject } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { useStudyData } from "./hooks/useStudyData";
import type { Marker } from "./types";

const { mutateStatus, mutateTime } = vi.hoisted(() => ({ mutateStatus: vi.fn(), mutateTime: vi.fn() }));

vi.mock("./hooks/useStudyData", () => ({
  useStudyData: vi.fn(),
  useMarkerStatusMutation: () => ({
    mutate: mutateStatus, isPending: false, isError: false,
  }),
  useMarkerJumpTimeMutation: () => ({ mutateAsync: mutateTime, isPending: false }),
  useMarkerImportanceMutation: () => ({ mutateAsync: vi.fn().mockResolvedValue(undefined) }),
}));

vi.mock("./components/VideoPanel", () => ({
  VideoPanel: ({ videoRef, onTimeUpdate }: {
    videoRef: RefObject<HTMLVideoElement>; onTimeUpdate: (time: number) => void;
  }) => <video ref={videoRef} onTimeUpdate={(event) => onTimeUpdate(event.currentTarget.currentTime)} />,
}));

const markers: Marker[] = [
  { id: 1, video_id: 1, start_ms: 1000, end_ms: 2000, title: "First point", note: "", source_quote: "", analysis_mode: "medium_recall", review_status: "active" },
  { id: 2, video_id: 1, start_ms: 3000, end_ms: 4000, title: "Saved mastered point", note: "", source_quote: "", analysis_mode: "medium_recall", review_status: "mastered" },
  { id: 3, video_id: 1, start_ms: 5000, end_ms: 6000, title: "Other mode", note: "", source_quote: "", analysis_mode: "high_recall", review_status: "active" },
  { id: 4, video_id: 1, start_ms: 7000, end_ms: 8000, title: "Removed point", note: "", source_quote: "", analysis_mode: "medium_recall", review_status: "excluded" },
];

let container: HTMLDivElement;
let root: Root;

beforeEach(async () => {
  const values = new Map<string, string>();
  vi.stubGlobal("localStorage", { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => values.set(key, value) });
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  mutateStatus.mockClear();
  mutateTime.mockReset().mockResolvedValue(undefined);
  vi.mocked(useStudyData).mockImplementation((videoId) => ({
    video: {
      id: videoId, filename: "lesson.mp4", display_name: "Lesson",
      video_path: "lesson.mp4", duration_ms: 10000, status: "review_ready",
      library_id: "test",
    },
    subtitles: [], markers: markers.map((marker) => ({ ...marker, video_id: videoId })),
    source: { video: "mock", subtitles: "mock", markers: "mock" },
    isLoading: false, isError: false,
    errors: { video: null, subtitles: null, markers: null }, error: null,
  }));
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
  window.history.replaceState({}, "", "/review/1");
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => root.render(<App />));
  await vi.waitFor(async () => {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)); });
    expect(container.querySelector("video")).not.toBeNull();
  });
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks(); vi.unstubAllGlobals();
});

function click(element: Element | null) {
  expect(element).not.toBeNull();
  act(() => (element as HTMLButtonElement).click());
}

function markerButtons() {
  return container.querySelectorAll<HTMLButtonElement>(".marker-main");
}

function navigate(path: string) {
  act(() => {
    window.history.pushState({}, "", path);
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
}

function press(key: string, options: KeyboardEventInit = {}, target: EventTarget = document) {
  const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...options });
  act(() => target.dispatchEvent(event));
  return event;
}

function scrubTime(seconds: number) {
  act(() => {
    const video = container.querySelector("video")!;
    video.currentTime = seconds;
    video.dispatchEvent(new Event("timeupdate"));
  });
}

describe("S saves the last deliberately chosen marker", () => {
  it("does not save an automatically highlighted point before any deliberate selection", async () => {
    scrubTime(1.5);
    expect(markerButtons()[0].getAttribute("aria-current")).toBe("true");
    expect(press("s").defaultPrevented).toBe(false);
    expect(mutateTime).not.toHaveBeenCalled();
  });

  it.each(["card", "timestamp", "D", "A", "next button"])("captures the live timeline position after selecting with %s", async (source) => {
    if (source === "card") click(markerButtons()[0]);
    if (source === "timestamp") click(container.querySelector(".marker-time-jump"));
    if (source === "D") press("d");
    if (source === "A") { press("d"); press("d"); press("a"); }
    if (source === "next button") click(container.querySelectorAll(".sidebar-nav button")[1]);
    scrubTime(3.9);
    expect(markerButtons()[0].getAttribute("aria-current")).toBe("true");
    const playCalls = vi.mocked(HTMLMediaElement.prototype.play).mock.calls.length;
    // Capture the player itself, even before its next timeupdate reaches React.
    container.querySelector("video")!.currentTime = 3.7;
    await act(async () => { expect(press("S").defaultPrevented).toBe(true); });
    expect(mutateTime).toHaveBeenCalledWith({ marker: expect.objectContaining({ id: 1 }), userStartMs: 3000 });
    expect(container.querySelector("video")!.currentTime).toBe(3.7);
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(playCalls);
    expect(container.querySelectorAll(".marker-item.is-viewed")).toHaveLength(source === "A" ? 2 : 1);
    await waitForExit(container, ".marker-time-editor");
    expect(container.textContent).toContain("跳转时间已保存为 0:03");
    expect(mutateStatus).not.toHaveBeenCalled();
  });

  it("always uses the most recent explicit switch, not the interval reached while scrubbing", async () => {
    click(markerButtons()[0]);
    press("d");
    scrubTime(1.5);
    await act(async () => { press("s"); });
    expect(mutateTime).toHaveBeenLastCalledWith({ marker: expect.objectContaining({ id: 2 }), userStartMs: 1000 });
    click(markerButtons()[0]);
    scrubTime(3.5);
    await act(async () => { press("s"); });
    expect(mutateTime).toHaveBeenLastCalledWith({ marker: expect.objectContaining({ id: 1 }), userStartMs: 3000 });
  });

  it("retains the deliberately chosen target when changing a view filter", async () => {
    click(markerButtons()[0]);
    click(container.querySelectorAll(".status-filter button")[1]);
    expect(markerButtons()).toHaveLength(1);
    scrubTime(3.5);
    await act(async () => { press("s"); });
    expect(mutateTime).toHaveBeenCalledWith({ marker: expect.objectContaining({ id: 1 }), userStartMs: 3000 });
  });

  it.each([[0, 0], [-1, 0], [12, 10000]])("normalizes %s seconds like the capture button", async (seconds, expected) => {
    press("d");
    scrubTime(seconds);
    const slider = document.createElement("input");
    slider.type = "range";
    container.append(slider);
    await act(async () => { press("s", {}, slider); });
    expect(mutateTime).toHaveBeenCalledWith({ marker: expect.objectContaining({ id: 1 }), userStartMs: expected });
  });

  it("reports unavailable playback position without sending an invalid timestamp", () => {
    press("d");
    Object.defineProperty(container.querySelector("video"), "currentTime", { configurable: true, value: NaN });
    press("s");
    expect(mutateTime).not.toHaveBeenCalled();
    expect(container.querySelector("[role=alert]")?.textContent).toContain("无法读取当前播放位置");
  });

  it("blocks same-tick duplicate saves and pending saves, then captures a fresh position on retry", async () => {
    let finish!: () => void;
    mutateTime.mockImplementationOnce(() => new Promise<void>((resolve) => { finish = resolve; }));
    press("d");
    scrubTime(4.5);
    act(() => {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "s", bubbles: true }));
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "s", bubbles: true }));
    });
    press("s");
    expect(mutateTime).toHaveBeenCalledTimes(1);
    await act(async () => { finish(); });
    scrubTime(6.5);
    await act(async () => { press("s"); });
    expect(mutateTime).toHaveBeenCalledTimes(2);
    expect(mutateTime).toHaveBeenLastCalledWith({ marker: expect.objectContaining({ id: 1 }), userStartMs: 6000 });
  });

  it("shows a truthful failure and clears it after retrying at the latest position", async () => {
    mutateTime.mockRejectedValueOnce(new Error("Save failed"));
    press("d");
    scrubTime(4.5);
    await act(async () => { press("s"); });
    expect(container.querySelector("[role=alert]")?.textContent).toContain("Save failed");
    expect(container.textContent).not.toContain("跳转时间已保存");
    scrubTime(6.5);
    await act(async () => { press("s"); });
    await waitForExit(container, "[role=alert]");
    expect(container.textContent).toContain("跳转时间已保存为 0:06");
  });

  it.each(["input", "textarea", "select", "editable", "textbox"])("does not save while typing in %s", (kind) => {
    press("d");
    const element = document.createElement(kind === "editable" || kind === "textbox" ? "div" : kind);
    if (kind === "editable") element.setAttribute("contenteditable", "true");
    if (kind === "textbox") element.setAttribute("role", "textbox");
    container.append(element);
    expect(press("s", {}, element).defaultPrevented).toBe(false);
    expect(mutateTime).not.toHaveBeenCalled();
  });

  it.each([{ ctrlKey: true }, { altKey: true }, { metaKey: true }, { repeat: true }, { isComposing: true }])("ignores modified, repeated and composing S (%j)", (options) => {
    press("d");
    press("s", options);
    expect(mutateTime).not.toHaveBeenCalled();
  });

  it("does not save during an editor, settings dialog or player popover", () => {
    press("d");
    click(container.querySelector(".marker-edit-time"));
    press("s");
    click(container.querySelector(".marker-editor-heading button"));
    click(container.querySelector(".settings-button"));
    press("s");
    click(container.querySelector(".settings-dialog .dialog-heading button"));
    const popover = document.createElement("div");
    popover.className = "audio-channel-popover";
    container.append(popover);
    press("s");
    popover.className = "subtitle-settings-popover";
    press("s");
    expect(mutateTime).not.toHaveBeenCalled();
  });

  it("respects already handled S events", () => {
    press("d");
    const event = new KeyboardEvent("keydown", { key: "s", bubbles: true, cancelable: true });
    event.preventDefault();
    act(() => document.dispatchEvent(event));
    expect(mutateTime).not.toHaveBeenCalled();
  });

  it("does not save a removed marker or carry a target into a different video", () => {
    press("d");
    const study = vi.mocked(useStudyData).mock.results[0].value;
    vi.mocked(useStudyData).mockReturnValue({
      ...study, markers: study.markers.map((marker: Marker) => marker.id === 1 ? { ...marker, review_status: "excluded" } : marker),
    });
    // A router location update re-renders the mocked study observer without remounting the review session.
    navigate("/review/1?fixture=updated");
    press("s");
    expect(mutateTime).not.toHaveBeenCalled();
    navigate("/review/2");
    press("s");
    expect(mutateTime).not.toHaveBeenCalled();
  });
});

describe("A/D marker navigation", () => {
  it("starts at the first point with D and shares navigation with the sidebar buttons", () => {
    expect(press("d").defaultPrevented).toBe(true);
    expect(container.querySelector("video")!.currentTime).toBe(1);
    expect(markerButtons()[0].getAttribute("aria-current")).toBe("true");
    expect(container.querySelectorAll(".marker-item.is-viewed")).toHaveLength(1);
    click(container.querySelectorAll(".sidebar-nav button")[1]);
    expect(container.querySelector("video")!.currentTime).toBe(3);
    press("A");
    expect(container.querySelector("video")!.currentTime).toBe(1);
    press("D");
    expect(container.querySelector("video")!.currentTime).toBe(3);
    expect(container.querySelectorAll(".marker-item.is-viewed")).toHaveLength(2);
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(4);
    expect(mutateStatus).not.toHaveBeenCalled();
  });

  it("allows the next button to start at the first point before any selection", () => {
    const next = container.querySelectorAll<HTMLButtonElement>(".sidebar-nav button")[1];
    expect(next.disabled).toBe(false);
    click(next);
    expect(container.querySelector("video")!.currentTime).toBe(1);
  });

  it("does not wrap around or play when there is no previous/next point", () => {
    expect(press("a").defaultPrevented).toBe(false);
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
    press("d");
    press("a");
    expect(container.querySelector("video")!.currentTime).toBe(1);
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(1);
    press("d");
    press("d");
    expect(container.querySelector("video")!.currentTime).toBe(3);
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(2);
  });

  it("uses saved times including zero without changing the point order", () => {
    const study = vi.mocked(useStudyData).mock.results[0].value;
    vi.mocked(useStudyData).mockReturnValue({
      ...study, markers: study.markers.map((marker: Marker) => ({
        ...marker, user_start_ms: marker.id === 1 ? 2500 : marker.id === 2 ? 0 : null,
      })),
    });
    // A router location update re-renders the mocked study observer without remounting the review session.
    navigate("/review/1?fixture=updated");
    press("d");
    expect(container.querySelector("video")!.currentTime).toBe(2.5);
    press("d");
    expect(container.querySelector("video")!.currentTime).toBe(0);
    press("a");
    expect(container.querySelector("video")!.currentTime).toBe(2.5);
  });

  it("uses the current mode and filtered list and does nothing for an empty list", () => {
    click(container.querySelectorAll(".status-filter button")[2]);
    press("d");
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
    click(container.querySelectorAll(".status-filter button")[0]);
    click(container.querySelectorAll(".mode-switcher button")[0]);
    press("d");
    expect(container.querySelector("video")!.currentTime).toBe(5);
    expect(markerButtons()[0].getAttribute("aria-current")).toBe("true");
    press("d");
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(1);
  });

  it.each(["input", "textarea", "select", "editable", "textbox"])("does not hijack keys from %s", (kind) => {
    const element = document.createElement(kind === "editable" || kind === "textbox" ? "div" : kind);
    if (kind === "editable") element.setAttribute("contenteditable", "true");
    if (kind === "textbox") element.setAttribute("role", "textbox");
    container.append(element);
    expect(press("d", {}, element).defaultPrevented).toBe(false);
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
  });

  it("keeps A/D available after using a timeline or volume slider", () => {
    const slider = document.createElement("input");
    slider.type = "range";
    container.append(slider);
    press("d", {}, slider);
    expect(container.querySelector("video")!.currentTime).toBe(1);
    press("d", {}, slider);
    expect(container.querySelector("video")!.currentTime).toBe(3);
    press("a", {}, slider);
    expect(container.querySelector("video")!.currentTime).toBe(1);
  });

  it.each([{ ctrlKey: true }, { altKey: true }, { metaKey: true }, { repeat: true }, { isComposing: true }])("ignores modified, repeated or composing key presses (%j)", (options) => {
    press("d", options);
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
  });

  it("respects events already handled by another control", () => {
    const event = new KeyboardEvent("keydown", { key: "d", bubbles: true, cancelable: true });
    event.preventDefault();
    act(() => document.dispatchEvent(event));
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
  });

  it("blocks navigation while adjusting a time or using the settings dialog", () => {
    click(container.querySelector(".marker-edit-time"));
    press("d");
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
    click(container.querySelector(".marker-editor-heading button"));
    click(container.querySelector(".settings-button"));
    press("d");
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
    click(container.querySelector(".settings-dialog .dialog-heading button"));
    press("d");
    expect(container.querySelector("video")!.currentTime).toBe(1);
  });

  it("does not interfere with open player popovers even when focus stays on their toggle", () => {
    const popover = document.createElement("div");
    popover.className = "audio-channel-popover";
    container.append(popover);
    press("d");
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
    popover.className = "subtitle-settings-popover";
    press("d");
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
    popover.remove();
    press("d");
    expect(container.querySelector("video")!.currentTime).toBe(1);
  });

  it("removes the old shortcut listener when switching videos", () => {
    press("d");
    navigate("/review/2");
    vi.mocked(HTMLMediaElement.prototype.play).mockClear();
    press("d");
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(1);
    expect(container.querySelector("video")!.currentTime).toBe(1);
    expect(container.querySelectorAll(".marker-item.is-viewed")).toHaveLength(1);
  });
});

describe("review visit state", () => {
  it("starts with every visible point unviewed, regardless of saved mastery", () => {
    expect(markerButtons()).toHaveLength(2);
    expect(container.querySelectorAll(".is-viewed")).toHaveLength(0);
    expect(container.querySelector(".sidebar-summary")?.textContent).toContain("0 个已看");
    expect(container.querySelector(".status-action")).toBeNull();
    expect(container.textContent).not.toContain("重新复习");
    expect(container.textContent).not.toContain("已掌握");
  });

  it("dims clicked points without hiding them or saving a mastery change", () => {
    click(markerButtons()[0]);
    expect(container.querySelectorAll(".marker-item.is-viewed")).toHaveLength(1);
    expect(markerButtons()).toHaveLength(2);
    expect(container.querySelector("video")?.currentTime).toBe(1);
    click(markerButtons()[0]);
    expect(container.querySelectorAll(".marker-item.is-viewed")).toHaveLength(1);
    click(markerButtons()[1]);
    expect(container.querySelectorAll(".marker-item.is-viewed")).toHaveLength(2);
    expect(container.querySelector(".sidebar-summary")?.textContent).toContain("2 个已看");
    expect(mutateStatus).not.toHaveBeenCalled();
  });

  it("marks previous and next navigation as viewed", () => {
    click(markerButtons()[0]);
    click(container.querySelectorAll(".sidebar-nav button")[1]);
    expect(container.querySelectorAll(".marker-item.is-viewed")).toHaveLength(2);
    expect(container.querySelector("video")?.currentTime).toBe(3);
    click(container.querySelectorAll(".sidebar-nav button")[0]);
    expect(container.querySelector("video")?.currentTime).toBe(1);
    expect(mutateStatus).not.toHaveBeenCalled();
  });

  it("uses saved jump times for cards and previous/next without changing the original ordering", () => {
    const study = vi.mocked(useStudyData).mock.results[0].value;
    vi.mocked(useStudyData).mockReturnValue({
      ...study, markers: study.markers.map((marker: Marker) => ({
        ...marker, user_start_ms: marker.id === 1 ? 2500 : marker.id === 2 ? 0 : null,
      })),
    });
    // A router location update re-renders the mocked study observer without remounting the review session.
    navigate("/review/1?fixture=updated");
    expect(markerButtons()[0].textContent).toContain("First point");
    expect(container.querySelector(".marker-item")?.textContent).toContain("原始定位 0:01");
    click(markerButtons()[0]);
    expect(container.querySelector("video")?.currentTime).toBe(2.5);
    click(container.querySelectorAll(".sidebar-nav button")[1]);
    expect(container.querySelector("video")?.currentTime).toBe(0);
    click(container.querySelectorAll(".sidebar-nav button")[0]);
    expect(container.querySelector("video")?.currentTime).toBe(2.5);
  });

  it("saves the current player position with one click without playing or marking a point viewed", async () => {
    const video = container.querySelector("video")!;
    act(() => { video.currentTime = 0; });
    click(container.querySelector(".marker-edit-time"));
    const capture = [...container.querySelectorAll(".marker-editor-tools button")].find((button) => button.textContent?.includes("保存当前播放位置"))!;
    act(() => { video.currentTime = 5; });
    await act(async () => click(capture));
    expect(mutateTime).toHaveBeenCalledWith({ marker: expect.objectContaining({ id: 1 }), userStartMs: 5000 });
    expect(video.currentTime).toBe(5);
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
    expect(container.querySelectorAll(".is-viewed")).toHaveLength(0);
    expect(mutateStatus).not.toHaveBeenCalled();
    await waitForExit(container, ".marker-time-editor");
    expect(container.textContent).toContain("跳转时间已保存为 0:05");
  });

  it("shows a disabled preview following playback and saves only on capture", async () => {
    const video = container.querySelector("video")!;
    act(() => { video.currentTime = 5; });
    click(container.querySelector(".marker-edit-time"));
    act(() => {
      video.currentTime = 3.9;
      video.dispatchEvent(new Event("timeupdate"));
    });
    const input = container.querySelector<HTMLInputElement>(".marker-time-input")!;
    expect(input.value).toBe("0:03");
    expect(input.disabled).toBe(true);
    expect(input.readOnly).toBe(true);
    expect(mutateTime).not.toHaveBeenCalled();
    await act(async () => click(container.querySelector(".marker-editor-tools button")));
    expect(mutateTime).toHaveBeenCalledWith({ marker: expect.objectContaining({ id: 1 }), userStartMs: 3000 });
    expect(video.currentTime).toBe(3.9);
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
    await waitForExit(container, ".marker-time-editor");
    expect(container.querySelectorAll(".is-viewed")).toHaveLength(0);
    expect(container.textContent).toContain("跳转时间已保存为 0:03");
  });

  it("does not mark a point viewed just because playback enters its time range", () => {
    const video = container.querySelector("video")!;
    act(() => {
      video.currentTime = 1.5;
      video.dispatchEvent(new Event("timeupdate"));
    });
    expect(container.querySelector(".marker-item.is-selected")).not.toBeNull();
    expect(container.querySelectorAll(".marker-item.is-viewed")).toHaveLength(0);
    expect(mutateStatus).not.toHaveBeenCalled();
  });

  it("filters this visit's viewed points and retains them when switching modes", () => {
    click(markerButtons()[0]);
    const filters = container.querySelectorAll(".status-filter button");
    click(filters[2]);
    expect(markerButtons()).toHaveLength(1);
    expect(markerButtons()[0].textContent).toContain("First point");
    click(filters[1]);
    expect(markerButtons()).toHaveLength(1);
    expect(markerButtons()[0].textContent).toContain("Saved mastered point");
    click(filters[0]);
    const modes = container.querySelectorAll(".mode-switcher button");
    click(modes[0]);
    expect(markerButtons()).toHaveLength(1);
    expect(container.querySelectorAll(".marker-item.is-viewed")).toHaveLength(0);
    click(modes[1]);
    expect(container.querySelectorAll(".marker-item.is-viewed")).toHaveLength(1);
    expect(mutateStatus).not.toHaveBeenCalled();
  });

  it("clears viewed points on video changes, including reopening the first video", () => {
    click(markerButtons()[0]);
    navigate("/review/2");
    expect(container.querySelectorAll(".marker-item.is-viewed")).toHaveLength(0);
    click(markerButtons()[1]);
    navigate("/review/1");
    expect(container.querySelectorAll(".marker-item.is-viewed")).toHaveLength(0);
    expect(container.querySelector(".status-filter [aria-selected=true]")?.textContent).toBe("全部");
  });

  it("clears viewed points when the review page is remounted", () => {
    click(markerButtons()[0]);
    act(() => root.unmount());
    root = createRoot(container);
    act(() => root.render(<App />));
    expect(container.querySelectorAll(".marker-item.is-viewed")).toHaveLength(0);
  });

  it("keeps removal and restoring excluded points on the existing saved-status path", () => {
    click(container.querySelector(".danger-action"));
    expect(mutateStatus).toHaveBeenLastCalledWith({
      marker: expect.objectContaining({ id: 1 }), status: "excluded",
    });
    click(container.querySelector(".undo-banner button"));
    expect(mutateStatus).toHaveBeenLastCalledWith({
      marker: expect.objectContaining({ id: 1 }), status: "active",
    });
    click(container.querySelector(".excluded-row button"));
    expect(mutateStatus).toHaveBeenLastCalledWith({
      marker: expect.objectContaining({ id: 4 }), status: "active",
    });
    expect(container.querySelectorAll(".marker-item.is-viewed")).toHaveLength(0);
  });
});


it("applies custom review navigation and capture keys, including modifier combinations", async () => {
  const map = defaultShortcuts(); map.nextMarker = ["J"]; map.previousMarker = ["H"]; map.captureTime = ["Ctrl+U"];
  act(() => saveShortcuts(map));
  const video = container.querySelector("video")!;
  press("d"); expect(video.currentTime).toBe(0);
  press("j"); expect(video.currentTime).toBe(1);
  press("j"); expect(video.currentTime).toBe(3);
  press("h"); expect(video.currentTime).toBe(1);
  expect(container.querySelectorAll(".sidebar-nav button")[1].getAttribute("title")).toContain("J");
  video.currentTime = 3.7;
  press("s"); expect(mutateTime).not.toHaveBeenCalled();
  await act(async () => { press("u", { ctrlKey: true }); });
  expect(mutateTime).toHaveBeenCalledWith({ marker: expect.objectContaining({ id: 1 }), userStartMs: 3000 });
  const input = document.createElement("input"); container.append(input);
  press("j", {}, input); expect(video.currentTime).toBe(3.7);
});
