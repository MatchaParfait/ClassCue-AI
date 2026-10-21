import { useEffect, useState } from "react";

export type CloseBehavior = "ask" | "background" | "exit";
export type DesktopDialog = { kind: string; message: string; tray?: boolean };
export type StartupLibraryView = { library_id: string; scroll_y: number; anchor: string | null; offset: number };
export interface DesktopAPI {
  describe(): Promise<{ name: string; tray: boolean; dialog: DesktopDialog | null; hotkey: string; close_behavior?: CloseBehavior }>;
  respond(action: string, remember?: boolean): Promise<unknown>;
  retry(): Promise<unknown>;
  set_hotkey(value: string): Promise<{ hotkey: string }>;
  set_close_behavior(value: CloseBehavior): Promise<{ close_behavior: CloseBehavior }>;
  set_unsaved_changes?(value: boolean): Promise<unknown>;
  get_startup_view?(): Promise<StartupLibraryView | null>;
  clear_startup_view?(): Promise<unknown>;
  hide_to_tray(): Promise<unknown>;
  exit_application(): Promise<unknown>;
  open_logs(): Promise<unknown>;
  install_update?(): Promise<unknown>;
}
declare global {
  interface Window {
    pywebview?: { api: DesktopAPI };
    __classroomDesktopProgress?: (message: string, error?: boolean) => void;
    __classroomStartupView?: StartupLibraryView;
  }
}

export function useDesktopAPI() {
  const [api, setApi] = useState<DesktopAPI | null>(null);
  useEffect(() => {
    const connect = () => {
      const bridge = window.pywebview?.api;
      const required = ["describe", "respond", "retry", "set_hotkey", "set_close_behavior"] as const;
      if (bridge && required.every(name => typeof bridge[name] === "function")) setApi(bridge);
    };
    connect();
    window.addEventListener("pywebviewready", connect);
    return () => window.removeEventListener("pywebviewready", connect);
  }, []);
  return api;
}
