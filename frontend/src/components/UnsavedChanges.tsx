import { createContext, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState, type MutableRefObject, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useBlocker, UNSAFE_DataRouterContext, type Location } from "react-router-dom";
import { useModalFocus } from "../hooks/useModalFocus";
import { useDesktopAPI } from "../hooks/useDesktopAPI";
import "../unsaved.css";

type Draft = { label: string; dirty: boolean; busy?: boolean; save: () => Promise<boolean | void>; discard: () => void };
type Leave = { proceed: () => void | Promise<unknown>; cancel?: () => void; source?: "navigation" };
type Guard = {
  register: (draft: MutableRefObject<Draft>) => () => void;
  notify: () => void;
  isDirty: () => boolean;
  request: (proceed: Leave["proceed"], cancel?: Leave["cancel"], source?: Leave["source"]) => void;
};
const Context = createContext<Guard | null>(null);
const noop = () => {};
const unprotectedRequest = (proceed: Leave["proceed"]) => { void proceed(); };
export function useUnsavedDraft(draft: Draft) {
  const guard = useContext(Context);
  const current = useRef(draft);
  current.current = draft;
  useLayoutEffect(() => guard?.register(current), [guard]);
  useLayoutEffect(() => { guard?.notify(); }, [guard, draft.dirty, draft.busy]);
  return useCallback(() => { current.current.dirty = false; guard?.notify(); }, [guard]);
}
export function useUnsavedLeave() {
  const guard = useContext(Context);
  return guard?.request ?? unprotectedRequest;
}
function NavigationGuard({ guard }: { guard: Guard }) {
  const blocker = useBlocker(useCallback(({ currentLocation, nextLocation }: { currentLocation: Location; nextLocation: Location }) =>
    guard.isDirty() && (currentLocation.pathname !== nextLocation.pathname || currentLocation.search !== nextLocation.search), [guard]));
  const pending = useRef<string | null>(null);
  useEffect(() => {
    if (blocker.state !== "blocked") { pending.current = null; return; }
    if (pending.current === blocker.location.key) return;
    pending.current = blocker.location.key;
    guard.request(() => blocker.proceed(), () => blocker.reset(), "navigation");
  }, [blocker, guard]);
  return null;
}
export function UnsavedChangesProvider({ children }: { children: ReactNode }) {
  const drafts = useRef(new Set<MutableRefObject<Draft>>());
  const [revision, setRevision] = useState(0);
  const [pending, setPending] = useState<Leave | null>(null);
  const pendingRef = useRef<Leave | null>(null);
  const [saving, setSaving] = useState(false);
  const running = useRef(false);
  const [error, setError] = useState("");
  const modal = useRef<HTMLDivElement>(null), cancelButton = useRef<HTMLButtonElement>(null);
  const router = useContext(UNSAFE_DataRouterContext);
  const api = useDesktopAPI();
  const notify = useCallback(() => setRevision(value => value + 1), []);
  const isDirty = useCallback(() => [...drafts.current].some(item => item.current.dirty), []);
  const request = useCallback((proceed: Leave["proceed"], cancel?: Leave["cancel"], source?: Leave["source"]) => {
    if (pendingRef.current) {
      // Router replaces its blocked destination on another back/link action.
      // Retain the prompt but use the latest callbacks instead of resetting it.
      if (source === "navigation" && pendingRef.current.source === source) {
        pendingRef.current = { proceed, cancel, source };
      } else cancel?.();
      return;
    }
    if (!isDirty()) { void Promise.resolve().then(proceed).catch(failure => setError(String(failure))); return; }
    const next = { proceed, cancel, source };
    pendingRef.current = next; setPending(next); setError("");
  }, [isDirty]);
  const guard = useMemo<Guard>(() => ({
    notify, isDirty, request,
    register: draft => { drafts.current.add(draft); notify(); return () => { drafts.current.delete(draft); notify(); }; },
  }), [notify, isDirty, request]);
  const dirty = [...drafts.current].filter(item => item.current.dirty);
  const hasChanges = dirty.length > 0;
  const busy = saving || dirty.some(item => item.current.busy);
  const cancel = () => {
    if (running.current) return;
    pendingRef.current?.cancel?.(); pendingRef.current = null; setPending(null); setError("");
  };
  useModalFocus(Boolean(pending), modal, cancelButton, cancel);
  useEffect(() => {
    if (!hasChanges) return;
    const unload = (event: BeforeUnloadEvent) => {
      if (!isDirty()) return;
      event.preventDefault(); event.returnValue = "";
    };
    window.addEventListener("beforeunload", unload);
    return () => window.removeEventListener("beforeunload", unload);
  }, [hasChanges, isDirty]);
  const nativeSync = useRef(Promise.resolve());
  useEffect(() => {
    if (!api?.set_unsaved_changes) return;
    nativeSync.current = nativeSync.current.catch(noop).then(async () => { await api.set_unsaved_changes!(isDirty()); }).catch(noop);
  }, [api, hasChanges, revision, isDirty]);
  const finish = async (save: boolean) => {
    if (running.current || busy || !pendingRef.current) return;
    running.current = true; setSaving(true); setError("");
    try {
      for (const ref of [...drafts.current]) {
        const item = ref.current;
        if (!item.dirty) continue;
        if (save) {
          const done = await item.save();
          if (done === false) {
            // Directory changes open their existing migration/restart confirmation.
            pendingRef.current?.cancel?.(); pendingRef.current = null; setPending(null); return;
          }
        } else item.discard();
        ref.current.dirty = false;
      }
      notify();
      const leave = pendingRef.current;
      pendingRef.current = null; setPending(null);
      await leave?.proceed();
    } catch (failure) { setError(failure instanceof Error ? failure.message : String(failure)); }
    finally { running.current = false; setSaving(false); }
  };
  return <Context.Provider value={guard}>
    {router && <NavigationGuard guard={guard} />}
    {children}
    {pending && createPortal(<div className="unsaved-backdrop" onClick={cancel}>
      <div ref={modal} className="unsaved-dialog" role="dialog" aria-modal="true" aria-labelledby="unsaved-title" aria-describedby="unsaved-description" tabIndex={-1} onClick={event => event.stopPropagation()}>
        <h2 id="unsaved-title">有未保存的修改</h2>
        <p id="unsaved-description">{dirty.map(item => item.current.label).join("、") || "设置"}尚未保存。是否保存后离开？</p>
        <p className="settings-hint">放弃修改会恢复已保存的设置。{dirty.some(item => ["存储与导入", "应用数据目录"].includes(item.current.label)) && "目录迁移仍需确认迁移范围及重启提示。"}</p>
        {error && <p role="alert" className="field-error">{error}</p>}
        <div className="unsaved-actions">
          <button type="button" className="primary-button" disabled={busy} onClick={() => void finish(true)}>{saving ? "正在处理…" : "保存并离开"}</button>
          <button type="button" className="secondary-button" disabled={busy} onClick={() => void finish(false)}>放弃修改</button>
          <button ref={cancelButton} type="button" className="secondary-button" disabled={saving} onClick={cancel}>继续编辑</button>
        </div>
      </div>
    </div>, document.body)}
  </Context.Provider>;
}
