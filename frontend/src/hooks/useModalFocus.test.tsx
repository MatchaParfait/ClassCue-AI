import { act, StrictMode, useRef } from "react";
import { createPortal } from "react-dom";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useModalFocus } from "./useModalFocus";

let container: HTMLDivElement, root: Root;
beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
});
function Dialog({ active, name, close }: { active: boolean; name: string; close: () => void }) {
  const dialog = useRef<HTMLDivElement>(null);
  const initial = useRef<HTMLButtonElement>(null);
  useModalFocus(active, dialog, initial, close);
  return <div ref={dialog} role={active ? "dialog" : undefined} aria-modal={active || undefined} hidden={!active}>
    <button ref={initial}>{name}</button>
  </div>;
}
function Fixture({ lower, upper, lowerClose, upperClose }: {
  lower: boolean; upper: boolean; lowerClose: () => void; upperClose: () => void;
}) {
  return <StrictMode>
    <Dialog active={upper} name="upper" close={upperClose} />
    {createPortal(<Dialog active={lower} name="lower" close={lowerClose} />, document.body)}
  </StrictMode>;
}
it("removing a lower dialog never steals focus or leaves a stale trap above the remaining dialog", () => {
  const lowerClose = vi.fn(), upperClose = vi.fn();
  const render = (lower: boolean, upper: boolean) => act(() => root.render(
    <Fixture lower={lower} upper={upper} lowerClose={lowerClose} upperClose={upperClose} />));
  render(true, false);
  const lowerButton = [...document.querySelectorAll("button")].find(item => item.textContent === "lower")!;
  act(() => lowerButton.focus());
  render(true, true);
  const upperButton = container.querySelector("button")!;
  expect(document.activeElement).toBe(upperButton);
  render(false, true);
  expect(document.activeElement).toBe(upperButton);
  expect(document.body.style.overflow).toBe("hidden");
  act(() => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true })));
  expect(upperClose).toHaveBeenCalledOnce();
  expect(lowerClose).not.toHaveBeenCalled();
  render(false, false);
  expect(document.body.style.overflow).not.toBe("hidden");
});
