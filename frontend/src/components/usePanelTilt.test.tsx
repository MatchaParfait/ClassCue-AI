import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { usePanelTilt } from "./usePanelTilt";

let host: HTMLDivElement, root: Root, tick: FrameRequestCallback | undefined;
const request = vi.fn((callback: FrameRequestCallback) => { tick = callback; return 12; });
const cancel = vi.fn();
function Panel({ enabled }: { enabled: boolean }) { return <div {...usePanelTilt(enabled)} />; }
const render = (enabled = true) => act(() => root.render(<Panel enabled={enabled} />));
const panel = () => host.firstElementChild as HTMLDivElement;
function move(x = 200, y = 0, pointerType = "mouse") {
  vi.spyOn(panel(), "getBoundingClientRect").mockReturnValue({ left: 0, top: 0, width: 200, height: 100 } as DOMRect);
  const event = new MouseEvent("pointermove", { bubbles: true, clientX: x, clientY: y });
  Object.defineProperty(event, "pointerType", { value: pointerType });
  act(() => panel().dispatchEvent(event));
}
beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  tick = undefined; request.mockClear(); cancel.mockClear();
  vi.stubGlobal("requestAnimationFrame", request); vi.stubGlobal("cancelAnimationFrame", cancel);
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});
afterEach(() => { act(() => root.unmount()); host.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
it("coalesces pointer events and limits tilt to readable angles", () => {
  render(); move(); move(400, -200);
  expect(request).toHaveBeenCalledTimes(1); act(() => tick?.(0));
  expect(panel().style.getPropertyValue("--panel-rx")).toBe("2.2deg");
  expect(panel().style.getPropertyValue("--panel-ry")).toBe("2.8deg");
  expect(panel().dataset.hovered).toBe("true");
});
it("resets and cancels outstanding work when motion is disabled", () => {
  render(); move(); act(() => tick?.(0)); move(); render(false);
  expect(cancel).toHaveBeenCalledWith(12);
  expect(panel().style.getPropertyValue("--panel-rx")).toBe("");
  expect(panel().dataset.hovered).toBeUndefined();
  request.mockClear(); move(); expect(request).not.toHaveBeenCalled();
});
it("returns to rest on pointer leave and cancels on unmount", () => {
  render(); move(); act(() => tick?.(0));
  act(() => panel().dispatchEvent(new MouseEvent("pointerout", { bubbles: true, relatedTarget: document.body })));
  expect(panel().dataset.hovered).toBeUndefined();
  move(); act(() => root.unmount()); expect(cancel).toHaveBeenCalledWith(12);
});
it("does not tilt on touch input", () => {
  render(); move(20, 20, "touch"); expect(request).not.toHaveBeenCalled();
});
