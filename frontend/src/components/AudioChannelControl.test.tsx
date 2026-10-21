import { waitForExit } from "../test/waitForExit";
import { act, createRef, StrictMode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AudioChannelControl } from "./AudioChannelControl";
import { createAudioChannelRouter, type AudioChannelRouter } from "../audio/channelRouter";

vi.mock("../audio/channelRouter", () => ({ createAudioChannelRouter: vi.fn() }));

let root: Root;
let container: HTMLDivElement;
let videoRef: ReturnType<typeof createRef<HTMLVideoElement>>;
let router: AudioChannelRouter;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.stubGlobal("AudioContext", vi.fn());
  vi.stubGlobal("AudioWorkletNode", vi.fn());
  router = { setMode: vi.fn(), setDenoise: vi.fn().mockResolvedValue(undefined), resume: vi.fn().mockResolvedValue(undefined), dispose: vi.fn() };
  vi.mocked(createAudioChannelRouter).mockReset().mockResolvedValue(router);
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  videoRef = createRef<HTMLVideoElement>();
  act(() => root.render(<StrictMode><video ref={videoRef} /><AudioChannelControl videoRef={videoRef} /></StrictMode>));
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

function open() {
  act(() => container.querySelector<HTMLButtonElement>(".audio-channel-button")!.click());
}

async function select(value: string) {
  await act(async () => container.querySelector<HTMLInputElement>(`input[value="${value}"]`)!.click());
}

describe("audio channel control", () => {
  it("does not touch native audio until a mapping is selected", () => {
    open();
    expect(container.querySelectorAll("input[type=radio]")).toHaveLength(3);
    expect(container.querySelector<HTMLInputElement>('input[value="original"]')!.checked).toBe(true);
    expect(createAudioChannelRouter).not.toHaveBeenCalled();
  });

  it("switches in real time, restores original channels, and reuses the source", async () => {
    const element = videoRef.current!;
    element.currentTime = 12;
    element.volume = 0.4;
    element.muted = true;
    element.playbackRate = 1.5;
    open();
    await select("left");
    expect(router.setMode).toHaveBeenLastCalledWith("left");
    expect(container.querySelector(".audio-channel-badge")?.textContent).toBe("L");
    open();
    await select("right");
    expect(router.setMode).toHaveBeenLastCalledWith("right");
    expect(container.querySelector(".audio-channel-badge")?.textContent).toBe("R");
    open();
    await select("original");
    expect(router.setMode).toHaveBeenLastCalledWith("original");
    expect(container.querySelector(".audio-channel-badge")).toBeNull();
    expect(createAudioChannelRouter).toHaveBeenCalledTimes(1);
    expect(videoRef.current).toBe(element);
    expect([element.currentTime, element.volume, element.muted, element.playbackRate]).toEqual([12, 0.4, true, 1.5]);
  });

  it("closes on Escape or outside press, and returns focus after selection", async () => {
    open();
    act(() => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" })));
    await waitForExit(container, "fieldset");
    expect(document.activeElement).toBe(container.querySelector("button"));
    open();
    act(() => document.body.dispatchEvent(new Event("pointerdown", { bubbles: true })));
    await waitForExit(container, "fieldset");
    open();
    await select("left");
    await waitForExit(container, "fieldset");
    expect(document.activeElement).toBe(container.querySelector("button"));
  });

  it("shows an error and allows retry without falsely showing an active mapping", async () => {
    vi.mocked(createAudioChannelRouter).mockRejectedValueOnce(new Error("Denied"));
    open();
    await select("left");
    expect(container.querySelector("[role=alert]")?.textContent).toContain("请重试");
    expect(container.querySelector<HTMLInputElement>('input[value="original"]')!.checked).toBe(true);
    expect(container.querySelector(".audio-channel-badge")).toBeNull();
    await select("left");
    expect(router.setMode).toHaveBeenCalledWith("left");
    expect(container.querySelector("[role=alert]")).toBeNull();
  });

  it("blocks concurrent source creation and releases audio on unmount", async () => {
    let resolve!: (value: AudioChannelRouter) => void;
    vi.mocked(createAudioChannelRouter).mockReturnValueOnce(new Promise((done) => { resolve = done; }));
    open();
    await select("left");
    expect(container.querySelector<HTMLFieldSetElement>("fieldset")!.disabled).toBe(true);
    await select("right");
    expect(createAudioChannelRouter).toHaveBeenCalledTimes(1);
    await act(async () => resolve(router));
    await act(async () => root.render(<div />));
    expect(router.dispose).toHaveBeenCalledTimes(1);
  });

  it("disposes a pending source if the player is removed before activation completes", async () => {
    let resolve!: (value: AudioChannelRouter) => void;
    vi.mocked(createAudioChannelRouter).mockReturnValueOnce(new Promise((done) => { resolve = done; }));
    open();
    await select("left");
    await act(async () => root.render(<div />));
    await act(async () => resolve(router));
    expect(router.dispose).toHaveBeenCalledTimes(1);
    expect(router.setMode).not.toHaveBeenCalled();
  });

  it("resumes the same audio context when another control starts playback", async () => {
    open();
    await select("left");
    await act(async () => videoRef.current!.dispatchEvent(new Event("play")));
    expect(router.resume).toHaveBeenCalledTimes(1);
  });

  it("toggles GTCRN without pausing or replacing the media element and shares the channel router", async () => {
    const element = videoRef.current!;
    const pause = vi.spyOn(element, "pause");
    const denoise = container.querySelector<HTMLButtonElement>('button[aria-label="实时降噪"]')!;
    await act(async () => denoise.click());
    expect(denoise.getAttribute("aria-pressed")).toBe("true");
    expect(router.setDenoise).toHaveBeenLastCalledWith(true);
    open();
    await select("right");
    await act(async () => denoise.click());
    expect(router.setDenoise).toHaveBeenLastCalledWith(false);
    expect(denoise.getAttribute("aria-pressed")).toBe("false");
    expect(createAudioChannelRouter).toHaveBeenCalledTimes(1);
    expect(videoRef.current).toBe(element);
    expect(pause).not.toHaveBeenCalled();
    pause.mockRestore();
  });

  it("retains original audio on initialization failure and allows retry", async () => {
    vi.mocked(router.setDenoise).mockRejectedValueOnce(new Error("Missing model"));
    const denoise = container.querySelector<HTMLButtonElement>('button[aria-label="实时降噪"]')!;
    await act(async () => denoise.click());
    expect(denoise.getAttribute("aria-pressed")).toBe("false");
    expect(container.querySelector(".player-denoise-error")?.textContent).toContain("原声");
    await act(async () => denoise.click());
    expect(denoise.getAttribute("aria-pressed")).toBe("true");
    expect(container.querySelector(".player-denoise-error")).toBeNull();
  });

  it("reflects a runtime fallback and disables only denoise when AudioWorklet is unavailable", async () => {
    const denoise = container.querySelector<HTMLButtonElement>('button[aria-label="实时降噪"]')!;
    await act(async () => denoise.click());
    const callback = vi.mocked(createAudioChannelRouter).mock.calls[0][1]!;
    act(() => callback("已恢复原声"));
    expect(denoise.getAttribute("aria-pressed")).toBe("false");
    expect(container.querySelector(".player-denoise-error")?.textContent).toBe("已恢复原声");
    vi.stubGlobal("AudioWorkletNode", undefined);
    act(() => root.render(<StrictMode><video ref={videoRef} /><AudioChannelControl videoRef={videoRef} /></StrictMode>));
    expect(denoise.disabled).toBe(true);
    expect(container.querySelector<HTMLButtonElement>(".audio-channel-button")!.disabled).toBe(false);
  });

  it("disables the control when Web Audio is unavailable", () => {
    vi.stubGlobal("AudioContext", undefined);
    act(() => root.render(<StrictMode><video ref={videoRef} /><AudioChannelControl videoRef={videoRef} /></StrictMode>));
    expect(container.querySelector<HTMLButtonElement>("button")!.disabled).toBe(true);
  });
});
