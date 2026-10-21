import template from "../../assets/desktop-startup-transition.js?raw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { releaseStartupCover } from "./startupTransition";

let cancel: ReturnType<typeof releaseStartupCover>;
const view = { library_id: "012345abcdef", scroll_y: 600, anchor: "video-1", offset: -30 };

function install(origin = location.origin) {
  const script = template.replace("__STARTUP_TRANSITION_DATA__", JSON.stringify({
    origin, view, html: '<!doctype html><html><body><main>Cached library</main></body></html>',
  }));
  new Function(script)();
  return document.getElementById("classroom-startup-cover")!;
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: false })));
  window.history.replaceState({}, "", "/");
});

afterEach(() => {
  cancel?.();
  cancel = undefined;
  const cover = document.getElementById("classroom-startup-cover");
  cover?.dispatchEvent(new Event("classroom-startup-released"));
  cover?.remove();
  document.body.innerHTML = "";
  delete window.__classroomStartupView;
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("read-only startup transition", () => {
  it("installs before React, carries scroll and isolates preview styling without allowing scripts", () => {
    const cover = install();
    expect(cover.style.position).toBe("fixed");
    expect(cover.getAttribute("aria-hidden")).toBe("true");
    expect(cover.dataset.libraryId).toBe(view.library_id);
    expect(cover.querySelector("iframe")?.getAttribute("sandbox")).toBe("allow-same-origin");
    expect(cover.querySelector("iframe")?.style.pointerEvents).toBe("none");
    expect(window.__classroomStartupView).toEqual(view);
    expect(document.documentElement.style.overflowAnchor).toBe("none");
    expect(cover.dataset.phase).toBeUndefined();
  });

  it("does not install on another origin or a non-library route", () => {
    expect(install("https://unrelated.example")).toBeNull();
    window.history.replaceState({}, "", "/settings");
    expect(install()).toBeNull();
    expect(window.__classroomStartupView).toBeUndefined();
  });

  it("never permanently covers a failed frontend bundle", async () => {
    const cover = install();
    await vi.advanceTimersByTimeAsync(10000);
    expect(cover.isConnected).toBe(false);
    expect(document.documentElement.style.overflowAnchor).toBe("");
  });

  it("releases immediately after ready paints, with no minimum waiting time or motion in reduced mode", async () => {
    vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: true })));
    const cover = install();
    const animate = vi.fn();
    cover.animate = animate;
    cancel = releaseStartupCover(document.body);
    await vi.advanceTimersByTimeAsync(50);
    expect(cover.isConnected).toBe(false);
    expect(animate).not.toHaveBeenCalled();
  });

  it("keeps the preview until a visible cover is decoded, then fades for 160ms", async () => {
    const cover = install();
    document.body.innerHTML = '<div class="video-thumbnail"><img></div>';
    const image = document.querySelector("img")!;
    vi.spyOn(image, "getBoundingClientRect").mockReturnValue({
      top: 100, left: 100, bottom: 250, right: 300, width: 200, height: 150, x: 100, y: 100, toJSON: () => ({}),
    });
    Object.defineProperty(image, "complete", { value: false });
    image.decode = vi.fn(async () => {});
    let finish!: () => void;
    const animation = { finished: new Promise<void>(resolve => { finish = resolve; }) };
    const animate = vi.fn(() => animation as unknown as Animation);
    const align = vi.fn();
    cover.animate = animate;
    cancel = releaseStartupCover(document.body, align);
    await vi.advanceTimersByTimeAsync(100);
    expect(animate).not.toHaveBeenCalled();
    image.dispatchEvent(new Event("load"));
    await vi.advanceTimersByTimeAsync(50);
    expect(image.decode).toHaveBeenCalledOnce();
    expect(align).toHaveBeenCalledOnce();
    expect(align.mock.invocationCallOrder[0]).toBeLessThan(animate.mock.invocationCallOrder[0]);
    expect(animate).toHaveBeenCalledWith([{ opacity: 1 }, { opacity: 0 }],
      { duration: 160, easing: "ease-out", fill: "forwards" });
    expect(cover.isConnected).toBe(true);
    finish();
    await vi.advanceTimersByTimeAsync(0);
    expect(cover.isConnected).toBe(false);
  });

  it("bounds slow image waiting and cancels stale readiness work", async () => {
    const cover = install();
    document.body.innerHTML = '<div class="video-thumbnail"><img></div>';
    const image = document.querySelector("img")!;
    vi.spyOn(image, "getBoundingClientRect").mockReturnValue({
      top: 100, left: 100, bottom: 250, right: 300, width: 200, height: 150, x: 100, y: 100, toJSON: () => ({}),
    });
    Object.defineProperty(image, "complete", { value: false });
    cancel = releaseStartupCover(document.body);
    await vi.advanceTimersByTimeAsync(100);
    cancel?.();
    await vi.advanceTimersByTimeAsync(500);
    expect(cover.isConnected).toBe(true);
    cancel = releaseStartupCover(document.body);
    await vi.advanceTimersByTimeAsync(450);
    expect(cover.isConnected).toBe(false);
  });

  it("ignores unrelated calls after the startup surface is gone", () => {
    expect(releaseStartupCover(document.body)).toBeUndefined();
  });
});
