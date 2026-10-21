import { defaultShortcuts, saveShortcuts, SHORTCUTS_KEY } from "../shortcuts";
import { waitForExit } from "../test/waitForExit";
import { act, createRef } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Subtitle, Video } from "../types";
import { VideoPanel } from "./VideoPanel";
import type { SearchSeek } from "../searchNavigation";
import { ReviewTabsProvider } from "./ReviewTabs";

const video: Video = {
  id: 1, display_name: "Lesson", filename: "lesson.mp4",
  duration_ms: 60000, video_path: "D:\\lesson.mp4", status: "review_ready",
};
const subtitles: Subtitle[] = [
  { id: 1, start_ms: 0, end_ms: 5000, text: "First subtitle" },
  { id: 2, start_ms: 6000, end_ms: 10000, text: "Second subtitle" },
];
let container: HTMLDivElement;
let root: Root;
let videoRef: ReturnType<typeof createRef<HTMLVideoElement>>;
let timeUpdate: ReturnType<typeof vi.fn>;
let durationChange: ReturnType<typeof vi.fn>;
let observerCallback: ResizeObserverCallback;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const storage = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => storage.set(key, String(value)),
    removeItem: (key: string) => storage.delete(key),
    clear: () => storage.clear(),
  });
  vi.stubGlobal("ResizeObserver", class {
    constructor(callback: ResizeObserverCallback) { observerCallback = callback; }
    observe() {}
    disconnect() {}
  });
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(function (this: HTMLElement) {
    return this.classList.contains("video-caption-area") ? 600 : 0;
  });
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockImplementation(function (this: HTMLElement) {
    return this.classList.contains("video-caption-area") ? 240 : 0;
  });
  vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(200);
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(40);
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => undefined);
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  videoRef = createRef<HTMLVideoElement>();
  timeUpdate = vi.fn();
  durationChange = vi.fn();
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function mount(currentTime = 0, items = subtitles, navigationSeek?: SearchSeek, autoPlay = false) {
  act(() => root.render(
    <VideoPanel video={video} subtitles={items} videoId={1} libraryId="test"
      currentTime={currentTime} onTimeUpdate={timeUpdate} onDurationChange={durationChange} videoRef={videoRef} navigationSeek={navigationSeek} autoPlay={autoPlay} />,
  ));
}

it("requests autoplay only after processing completion without resetting saved playback position", () => {
  localStorage.setItem('classroom-study-progress:test:1', '24');
  mount(); expect(videoRef.current!.autoplay).toBe(false);
  mount(0, subtitles, undefined, true);
  expect(videoRef.current!.autoplay).toBe(true);
  loadMetadata();
  expect(videoRef.current!.currentTime).toBe(24);
  expect(button('播放')).not.toBeNull();
});

it("saves the last position and pauses the detached player when leaving a video", () => {
  mount();
  const element = videoRef.current!;
  Object.defineProperty(element, "readyState", { configurable: true, value: 1 });
  element.currentTime = 27.5;
  act(() => root.render(null));
  expect(localStorage.getItem("classroom-study-progress:test:1")).toBe("27.5");
  expect(element.pause).toHaveBeenCalledOnce();
  mount(); loadMetadata();
  expect(videoRef.current!.currentTime).toBe(27.5);
});

it("saves on page hide, but never overwrites an existing position for an unready player", () => {
  localStorage.setItem("classroom-study-progress:test:1", "24");
  mount();
  act(() => window.dispatchEvent(new Event("pagehide")));
  act(() => root.render(null));
  expect(localStorage.getItem("classroom-study-progress:test:1")).toBe("24");
  mount();
  Object.defineProperty(videoRef.current!, "readyState", { configurable: true, value: 1 });
  videoRef.current!.currentTime = 36;
  act(() => window.dispatchEvent(new Event("pagehide")));
  expect(localStorage.getItem("classroom-study-progress:test:1")).toBe("36");
});

it.each([.5, 59.5])("restores exact in-window tab positions near the start or end: %s", (seconds) => {
  const show = (visible: boolean) => act(() => root.render(
    <ReviewTabsProvider>{visible && <VideoPanel video={video} subtitles={[]} videoId={1} libraryId="test"
      currentTime={0} onTimeUpdate={timeUpdate} onDurationChange={durationChange} videoRef={videoRef} />}</ReviewTabsProvider>,
  ));
  show(true); loadMetadata();
  Object.defineProperty(videoRef.current!, "readyState", { configurable: true, value: 1 });
  videoRef.current!.currentTime = seconds;
  show(false);
  show(true); loadMetadata();
  expect(videoRef.current!.currentTime).toBe(seconds);
  expect(videoRef.current!.autoplay).toBe(false);
});

function button(label: string) {
  return container.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`)!;
}

function inputValue(input: HTMLInputElement, value: string) {
  act(() => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

function loadMetadata() {
  Object.defineProperty(videoRef.current!, "duration", { configurable: true, value: 60 });
  act(() => videoRef.current!.dispatchEvent(new Event("loadedmetadata")));
}

function pointer(element: HTMLElement, type: string, x: number, y: number) {
  const event = new MouseEvent(type, { bubbles: true, button: 0, clientX: x, clientY: y });
  Object.defineProperty(event, "pointerId", { value: 1 });
  act(() => element.dispatchEvent(event));
}

describe("video subtitles and player controls", () => {
  it("waits for metadata and gives search timestamps priority over saved playback", () => {
    localStorage.setItem("classroom-study-progress:test:1", "30");
    mount(0, subtitles, { token: "search-1", seconds: 8 });
    expect(videoRef.current!.currentTime).toBe(0);
    loadMetadata();
    expect(videoRef.current!.currentTime).toBe(8);
    videoRef.current!.currentTime = 9;
    loadMetadata();
    expect(videoRef.current!.currentTime).toBe(9);
    mount(9, subtitles, { token: "search-2", seconds: 8 });
    expect(videoRef.current!.currentTime).toBe(8);
    mount(8, subtitles, { token: "search-3", seconds: 0 });
    expect(videoRef.current!.currentTime).toBe(0);
    expect(videoRef.current!.play).toHaveBeenCalledTimes(3);
  });

  it("clamps a stale subtitle timestamp to the available media duration", () => {
    mount(0, subtitles, { token: "search", seconds: 120 });
    loadMetadata();
    expect(videoRef.current!.currentTime).toBe(60);
  });
  it("shows the active subtitle inside the video and hides it during gaps", () => {
    mount();
    expect(container.querySelector(".video-frame .video-caption")?.textContent).toBe("First subtitle");
    mount(5);
    expect(container.querySelector<HTMLElement>(".video-caption")?.style.visibility).toBe("hidden");
    mount(6);
    expect(container.querySelector(".video-caption")?.textContent).toBe("Second subtitle");
    mount(10);
    expect(container.querySelector<HTMLElement>(".video-caption")?.style.visibility).toBe("hidden");
  });

  it("toggles subtitles without changing playback and remembers the preference", () => {
    mount();
    act(() => button("字幕").click());
    expect(button("字幕").getAttribute("aria-pressed")).toBe("false");
    expect(container.querySelector<HTMLElement>(".video-caption")?.style.visibility).toBe("hidden");
    expect(JSON.parse(localStorage.getItem("classroom-player-subtitles")!).enabled).toBe(false);
    expect(videoRef.current?.play).not.toHaveBeenCalled();
    act(() => button("字幕").click());
    expect(container.querySelector(".video-caption")?.textContent).toBe("First subtitle");
  });

  it("adjusts subtitle size, restores saved settings, and closes settings with Escape", async () => {
    localStorage.setItem("classroom-player-subtitles", JSON.stringify({ enabled: true, fontSize: 30 }));
    mount();
    expect(container.querySelector<HTMLElement>(".video-caption")?.style.fontSize).toBe("30px");
    act(() => button("字幕大小").click());
    inputValue(container.querySelector<HTMLInputElement>("#subtitle-font-size")!, "36");
    expect(container.querySelector<HTMLElement>(".video-caption")?.style.fontSize).toBe("36px");
    expect(JSON.parse(localStorage.getItem("classroom-player-subtitles")!).fontSize).toBe(36);
    act(() => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    await waitForExit(container, ".subtitle-settings-popover");
    expect(document.activeElement).toBe(button("字幕大小"));
  });

  it("closes subtitle settings on an outside pointer press", () => {
    mount();
    act(() => button("字幕大小").click());
    act(() => document.body.dispatchEvent(new Event("pointerdown", { bubbles: true })));
    expect(button("字幕大小").getAttribute("aria-expanded")).toBe("false");
  });

  it("recovers from malformed preferences and disables subtitle controls when unavailable", () => {
    localStorage.setItem("classroom-player-subtitles", "{broken");
    mount(0, []);
    expect(button("字幕").disabled).toBe(true);
    expect(button("字幕大小").disabled).toBe(true);
    expect(container.querySelector(".video-caption")).toBeNull();
  });

  it("drags subtitles inside their bounds, preserves position across toggles, and resets", () => {
    mount();
    const caption = container.querySelector<HTMLElement>(".video-caption")!;
    caption.setPointerCapture = vi.fn();
    expect(caption.style.left).toBe("200px");
    expect(caption.style.top).toBe("200px");
    pointer(caption, "pointerdown", 200, 200);
    pointer(caption, "pointermove", -1000, -1000);
    expect(caption.style.left).toBe("0px");
    expect(caption.style.top).toBe("0px");
    pointer(caption, "pointermove", 2000, 2000);
    expect(caption.style.left).toBe("400px");
    expect(caption.style.top).toBe("200px");
    pointer(caption, "pointerup", 2000, 2000);
    expect(caption.classList.contains("is-dragging")).toBe(false);
    act(() => button("字幕").click());
    act(() => button("字幕").click());
    expect(caption.style.left).toBe("400px");
    act(() => caption.dispatchEvent(new MouseEvent("dblclick", { bubbles: true })));
    expect(caption.style.left).toBe("200px");
    expect(caption.style.top).toBe("200px");
    expect(videoRef.current?.play).not.toHaveBeenCalled();
  });

  it("ignores retired subtitle movement keys and preserves drag/reset controls", () => {
    localStorage.setItem(SHORTCUTS_KEY, JSON.stringify({
      ...defaultShortcuts(), captionLeft: ["H"], captionRight: ["J"], captionUp: ["U"], captionDown: ["N"],
    }));
    mount();
    const caption = container.querySelector<HTMLElement>(".video-caption")!;
    const original = { left: caption.style.left, top: caption.style.top };
    timeUpdate.mockClear();
    for (const key of ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "h", "j", "u", "n"]) {
      const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true });
      act(() => caption.dispatchEvent(event));
      expect(event.defaultPrevented).toBe(false);
      expect({ left: caption.style.left, top: caption.style.top }).toEqual(original);
    }
    expect(timeUpdate).not.toHaveBeenCalled();
    expect(caption.title).toBe("拖动字幕；双击恢复底部居中。");
    caption.setPointerCapture = vi.fn();
    pointer(caption, "pointerdown", 200, 200);
    pointer(caption, "pointermove", 200, 190);
    pointer(caption, "pointerup", 200, 190);
    expect(caption.style.top).toBe("190px");
    act(() => button("字幕大小").click());
    const reset = container.querySelector<HTMLButtonElement>(".subtitle-settings-popover button")!;
    act(() => reset.click());
    expect(container.querySelector<HTMLElement>(".video-caption")?.style.top).toBe("200px");
  });

  it("recalculates subtitle placement after a resize", () => {
    mount();
    vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(400);
    vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(100);
    act(() => observerCallback([], {} as ResizeObserver));
    expect(container.querySelector<HTMLElement>(".video-caption")?.style.left).toBe("100px");
    expect(container.querySelector<HTMLElement>(".video-caption")?.style.top).toBe("140px");
  });

  it("restores progress and seeks with the custom timeline", () => {
    localStorage.setItem("classroom-study-progress:test:1", "12");
    mount();
    loadMetadata();
    expect(videoRef.current?.currentTime).toBe(12);
    expect(durationChange).toHaveBeenCalledWith(60);
    expect(timeUpdate).toHaveBeenCalledWith(12);
    inputValue(container.querySelector<HTMLInputElement>(".player-progress")!, "25");
    expect(videoRef.current?.currentTime).toBe(25);
    expect(timeUpdate).toHaveBeenCalledWith(25);
    act(() => videoRef.current!.dispatchEvent(new Event("pause")));
    expect(localStorage.getItem("classroom-study-progress:test:1")).toBe("25");
  });

  it("syncs playback state, volume and playback speed with the media element", async () => {
    mount();
    await act(async () => button("播放").click());
    expect(videoRef.current?.play).toHaveBeenCalled();
    Object.defineProperty(videoRef.current!, "paused", { configurable: true, value: false });
    act(() => videoRef.current!.dispatchEvent(new Event("play")));
    act(() => button("暂停").click());
    expect(videoRef.current?.pause).toHaveBeenCalled();
    inputValue(container.querySelector<HTMLInputElement>(".player-volume")!, "0");
    expect(videoRef.current?.muted).toBe(true);
    act(() => button("取消静音").click());
    expect(videoRef.current?.muted).toBe(false);
    expect(videoRef.current?.volume).toBe(0.5);
    const speed = container.querySelector<HTMLSelectElement>(".player-speed")!;
    act(() => {
      speed.value = "1.5";
      speed.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(videoRef.current?.playbackRate).toBe(1.5);
  });

  it("fullscreen targets the whole player including the subtitles and controls", async () => {
    mount();
    const frame = container.querySelector<HTMLDivElement>(".video-frame")!;
    frame.requestFullscreen = vi.fn().mockResolvedValue(undefined);
    await act(async () => button("全屏").click());
    expect(frame.requestFullscreen).toHaveBeenCalled();
    Object.defineProperty(document, "fullscreenElement", { configurable: true, value: frame });
    act(() => document.dispatchEvent(new Event("fullscreenchange")));
    expect(button("退出全屏")).toBeTruthy();
    document.exitFullscreen = vi.fn().mockResolvedValue(undefined);
    await act(async () => button("退出全屏").click());
    expect(document.exitFullscreen).toHaveBeenCalled();
    Object.defineProperty(document, "fullscreenElement", { configurable: true, value: null });
  });

  it("shows playback errors without losing the player controls", async () => {
    mount();
    vi.mocked(videoRef.current!.play).mockRejectedValueOnce(new Error("Unavailable"));
    await act(async () => button("播放").click());
    expect(container.querySelector(".video-playback-error")?.textContent).toContain("视频无法播放");
    expect(button("字幕")).toBeTruthy();
  });
});


it("updates player shortcuts immediately without firing while editing controls or focusing subtitles", () => {
  mount();
  const map = defaultShortcuts(); map.playPause = ["P"]; map.mute = ["Ctrl+J"];
  act(() => saveShortcuts(map));
  const frame = container.querySelector(".video-frame")!;
  const press = (target: Element, key: string, options: KeyboardEventInit = {}) => act(() => {
    target.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...options }));
  });
  vi.mocked(HTMLMediaElement.prototype.play).mockClear();
  press(frame, "k"); press(frame, " ");
  expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
  press(frame, "p"); expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(1);
  press(frame, "j", { ctrlKey: true }); expect(videoRef.current!.muted).toBe(true);
  press(frame, "m"); expect(videoRef.current!.muted).toBe(true);
  press(frame, "p", { isComposing: true }); expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(1);
  press(container.querySelector('input[type="range"]')!, "p"); expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(1);
  const caption = container.querySelector<HTMLElement>(".video-caption")!;
  const before = caption.style.top;
  press(caption, "ArrowUp"); expect(caption.style.top).toBe(before);
  press(caption, "u"); expect(caption.style.top).toBe(before);
  press(caption, "p"); expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(1);
  expect(container.querySelector('[aria-label="播放"]')?.getAttribute("title")).toContain("P");
});
