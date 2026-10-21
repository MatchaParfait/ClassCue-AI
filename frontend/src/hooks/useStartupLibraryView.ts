import { useCallback, useEffect, useRef, useState } from "react";
import { useDesktopAPI, type StartupLibraryView } from "./useDesktopAPI";

// The native preview is a different document. Keep its handoff above workspace remounts.
export function useStartupLibraryView() {
  const api = useDesktopAPI();
  const [view, setView] = useState<StartupLibraryView | null>(() => window.__classroomStartupView ?? null);
  const handled = useRef(false);
  const interacted = useRef(false);
  const dismiss = useCallback(() => {
    handled.current = true;
    delete window.__classroomStartupView;
    setView(null);
    void api?.clear_startup_view?.().catch(() => {});
  }, [api]);

  useEffect(() => {
    const markInteraction = () => {
      interacted.current = true;
      if (!handled.current) dismiss();
    };
    const events = ["wheel", "pointerdown", "keydown"] as const;
    events.forEach(name => window.addEventListener(name, markInteraction, { passive: true }));
    return () => events.forEach(name => window.removeEventListener(name, markInteraction));
  }, [dismiss]);

  useEffect(() => {
    if (handled.current) {
      void api?.clear_startup_view?.().catch(() => {});
      return;
    }
    if (!api?.get_startup_view) return;
    let active = true;
    void api.get_startup_view().then(value => {
      if (!active || handled.current) return;
      if (interacted.current || window.location.pathname !== "/") { dismiss(); return; }
      if (value && typeof value.library_id === "string" && Number.isFinite(value.scroll_y)
        && Number.isFinite(value.offset) && (value.anchor === null || /^(video|folder)-\d+$/.test(value.anchor))) {
        setView(value);
      }
    }).catch(() => {});
    return () => { active = false; };
  }, [api, dismiss]);

  // Do not let a late bridge response override a user's first action on the live page.
  return { view: interacted.current ? null : view, dismiss };
}
