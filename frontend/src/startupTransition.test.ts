import template from "../../assets/desktop-startup-transition.js?raw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { releaseStartupCover } from "./startupTransition";

let cancel: ReturnType<typeof releaseStartupCover>;
const view = { library_id: "012345abcdef", scroll_y: 600, anchor: "video-1", offset: -30 };

function install(origin = location.origin, carriedView: typeof view | null = view) {
  const script = template.replace("__STARTUP_TRANSITION_DATA__", JSON.stringify({
    origin, view: carriedView, background: "#edf5fa",
    html: '<!doctype html><html><body><main>Startup artwork</main></body></html>',
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

  it("keeps the artwork and offers working native recovery when the bundle is slow or missing", async () => {
    const cover = install(location.origin, null);
    const frame = cover.querySelector("iframe")!;
    frame.contentDocument!.body.innerHTML = '<div id="sync"><p id="message"></p></div><div id="actions" hidden>'
      + '<button data-startup-action="retry">Retry</button><button data-startup-action="logs">Logs</button></div>';
    frame.dispatchEvent(new Event("load"));
    const retry = vi.fn(async () => {});
    const logs = vi.fn(async () => {});
    vi.stubGlobal("pywebview", { api: { retry, open_logs: logs } });
    await vi.advanceTimersByTimeAsync(10000);
    expect(cover.isConnected).toBe(true);
    expect(frame.contentDocument!.getElementById("actions")!.hidden).toBe(false);
    expect(frame.contentDocument!.getElementById("sync")!.getAttribute("role")).toBe("alert");
    expect(cover.hasAttribute("aria-hidden")).toBe(false);
    expect(window.__classroomStartupView).toBeUndefined();
    frame.contentDocument!.querySelector<HTMLButtonElement>('[data-startup-action="retry"]')!.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(retry).toHaveBeenCalledOnce();
    frame.contentDocument!.querySelector<HTMLButtonElement>('[data-startup-action="logs"]')!.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(logs).toHaveBeenCalledOnce();
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

  it("waits for slow visible images instead of revealing partial content, and cancels stale work", async () => {
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
    await vi.advanceTimersByTimeAsync(1000);
    expect(cover.isConnected).toBe(true);
    image.dispatchEvent(new Event("error"));
    await vi.advanceTimersByTimeAsync(50);
    expect(cover.isConnected).toBe(false);
  });

  it("blocks hidden application controls and restores their previous state on release", async () => {
    const application = document.createElement("div");
    application.id = "root";
    document.body.append(application);
    const cover = install(location.origin, null);
    expect(application.inert).toBe(true);
    expect(cover.style.background).toBe("rgb(237, 245, 250)");
    cancel = releaseStartupCover(application);
    await vi.advanceTimersByTimeAsync(50);
    expect(cover.isConnected).toBe(false);
    expect(application.inert).toBe(false);
    expect(document.documentElement.style.overflowAnchor).toBe("");
  });

  it("waits for the visible library background so decorative details do not appear after reveal", async () => {
    document.body.innerHTML = '<main class="library-page"><div class="library-scene" '
      + 'style="background-image:url(/art/library-bauhaus.webp)"></div><section id="content"></section></main>';
    const scene = document.querySelector<HTMLElement>(".library-scene")!;
    vi.spyOn(scene, "getBoundingClientRect").mockReturnValue({
      top: 100, left: 300, bottom: 500, right: 700, width: 400, height: 400, x: 300, y: 100, toJSON: () => ({}),
    });
    let finish!: () => void;
    const decode = vi.fn(() => new Promise<void>(resolve => { finish = resolve; }));
    vi.stubGlobal("Image", class {
      complete = true;
      src = "";
      onload = null;
      onerror = null;
      decode = decode;
    });
    const cover = install(location.origin, null);
    cancel = releaseStartupCover(document.getElementById("content"));
    await vi.advanceTimersByTimeAsync(1000);
    expect(cover.isConnected).toBe(true);
    expect(decode).toHaveBeenCalledOnce();
    finish();
    await vi.advanceTimersByTimeAsync(50);
    expect(cover.isConnected).toBe(false);
  });

  it("ignores unrelated calls after the startup surface is gone", () => {
    expect(releaseStartupCover(document.body)).toBeUndefined();
  });
});
