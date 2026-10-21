import { useEffect, useRef, type RefObject } from "react";
import { lockModalEnvironment } from "./modalEnvironment";

const FOCUSABLE = 'button:not([disabled]), a[href], input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function useModalFocus(
  active: boolean, dialogRef: RefObject<HTMLElement>, initialRef: RefObject<HTMLElement>,
  onClose: () => void, backgroundRef?: RefObject<HTMLElement>, returnFocus?: HTMLElement | null,
) {
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    if (!active) return;
    const background = backgroundRef?.current;
    const opener = returnFocus ?? (document.activeElement instanceof HTMLElement ? document.activeElement : null);
    const release = lockModalEnvironment(background);
    initialRef.current?.focus();
    const topDialog = () => [...document.querySelectorAll<HTMLElement>('[role="dialog"][aria-modal="true"]')]
      .filter((element) => !element.closest("[hidden], [inert]")).at(-1);
    const controls = (scope: HTMLElement) => [...scope.querySelectorAll<HTMLElement>(FOCUSABLE)]
      .filter((element) => !element.closest("[hidden], [inert]")
        && getComputedStyle(element).display !== "none" && getComputedStyle(element).visibility !== "hidden");
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.isComposing) return;
      const scope = topDialog();
      if (!scope || !dialogRef.current?.contains(scope)) return;
      if (event.key === "Escape" && scope === dialogRef.current) {
        event.preventDefault();
        close.current();
      } else if (event.key === "Tab") {
        const items = controls(scope);
        const first = items[0], last = items.at(-1);
        if (!first) { event.preventDefault(); scope.focus(); }
        else if (event.shiftKey && (document.activeElement === first || !scope.contains(document.activeElement))) {
          event.preventDefault(); last?.focus();
        } else if (!event.shiftKey && (document.activeElement === last || !scope.contains(document.activeElement))) {
          event.preventDefault(); first.focus();
        }
      }
    };
    const onFocus = (event: FocusEvent) => {
      const scope = topDialog();
      if (scope && dialogRef.current?.contains(scope) && event.target instanceof Node && !scope.contains(event.target)) {
        (controls(scope)[0] ?? scope).focus();
      }
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("focusin", onFocus);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("focusin", onFocus);
      release();
      if (opener?.isConnected && !opener.closest("[hidden], [inert]")) opener.focus({ preventScroll: true });
    };
  }, [active, backgroundRef, dialogRef, initialRef, returnFocus]);
}
