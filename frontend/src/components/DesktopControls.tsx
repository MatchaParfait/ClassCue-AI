import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { LoaderCircle, LogOut, Minimize2, Monitor, X } from "lucide-react";
import { useModalFocus } from "../hooks/useModalFocus";
import "../desktop.css";
import { useNavigate } from "react-router-dom";
import { useDesktopAPI, type DesktopDialog as Dialog } from "../hooks/useDesktopAPI";
import { useUnsavedLeave } from "./UnsavedChanges";

export function DesktopControls() {
  const api = useDesktopAPI();
  const navigate = useNavigate();
  const requestLeave = useUnsavedLeave();
  const [dialog, setDialog] = useState<Dialog | null>(null);
  const [remember, setRemember] = useState(false);
  const [progress, setProgress] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [offline, setOffline] = useState(false);
  const modal = useRef<HTMLDivElement>(null);
  const cancelButton = useRef<HTMLButtonElement>(null);
  const close = () => {
    if (saving) return;
    setDialog(null); setError("");
    void api?.respond("cancel").catch(() => undefined);
  };
  useModalFocus(Boolean(dialog), modal, cancelButton, close);
  useEffect(() => {
    if (dialog && !saving) cancelButton.current?.focus();
  }, [dialog?.kind, saving]);
  useEffect(() => {
    let disposed = false;
    if (!api) return;
    const showDialog = (value: Dialog | null) => {
      setRemember(false); setProgress(""); setError("");
      if (value?.kind === "unsaved-close") {
        setDialog(null);
        requestLeave(() => api.respond("continue-close").catch(reason => setError(String(reason))),
          () => { void api.respond("cancel").catch(reason => setError(String(reason))); });
      } else if (value?.kind === "hotkey") {
        setDialog(null);
        void api.respond("cancel").catch(reason => setError(String(reason)));
        navigate("/settings?section=shortcuts");
      } else setDialog(value);
    };
    void api.describe().then(state => { if (!disposed) showDialog(state.dialog); })
      .catch(() => { if (!disposed) setError("无法连接桌面控制，请重试。"); });
    const onDialog = (event: Event) => showDialog((event as CustomEvent<Dialog>).detail);
    window.__classroomDesktopProgress = message => { setDialog(null); setProgress(message); };
    window.addEventListener("classroom-desktop-dialog", onDialog);
    return () => {
      disposed = true;
      delete window.__classroomDesktopProgress;
      window.removeEventListener("classroom-desktop-dialog", onDialog);
    };
  }, [api, navigate, requestLeave]);
  useEffect(() => {
    if (!api) return;
    let disposed = false, failures = 0;
    let controller: AbortController | null = null;
    const check = async () => {
      if (controller) return;
      controller = new AbortController();
      const timeout = window.setTimeout(() => controller?.abort(), 3000);
      try {
        const result = await fetch("/api/health", { signal: controller.signal, cache: "no-store" });
        if (!result.ok) throw new Error("offline");
        if (!disposed) { failures = 0; setOffline(false); }
      } catch {
        if (!disposed && ++failures >= 2) setOffline(true);
      } finally { clearTimeout(timeout); controller = null; }
    };
    const timer = window.setInterval(() => { void check(); }, 2000);
    return () => { disposed = true; controller?.abort(); clearInterval(timer); };
  }, [api]);
  if (!api) return null;
  const respond = async (action: string) => {
    if (saving) return;
    setSaving(true); setError("");
    const current = dialog;
    try {
      if (current?.kind === "close-choice") await api.respond(action, remember);
      else await api.respond(action);
      // Native response may already have opened the active-task confirmation.
      setDialog(value => value === current ? null : value);
    } catch (reason) { setError(String(reason)); }
    finally { setSaving(false); }
  };
  return <>
    {offline && <div className="desktop-offline" role="status">服务暂时离线，正在等待重连…
      <button onClick={() => { void api.retry().catch(reason => setError(String(reason))); }}>重试启动</button>
    </div>}
    {error && !dialog && <div className="desktop-offline" role="alert">{error}
      <button onClick={() => setError("")} aria-label="关闭桌面提示"><X size={14} /></button></div>}
    {createPortal(<>
      {progress && <div className="desktop-backdrop desktop-progress" role="status" aria-live="polite">
        <div><LoaderCircle className="spin" size={24} /><strong>{progress}</strong><p>请稍候，应用会完成清理后退出或恢复。</p></div>
      </div>}
      {dialog && <div className="desktop-backdrop" onClick={() => { if (!saving) close(); }}>
        <div ref={modal} className="desktop-dialog" role="dialog" aria-modal="true" aria-labelledby="desktop-title"
          tabIndex={-1} onClick={event => event.stopPropagation()}>
          <div className="desktop-dialog-heading"><Monitor size={20} /><h2 id="desktop-title">
            {dialog.kind === "exit-task" ? "停止任务并退出？" : "关闭窗口"}
          </h2></div>
          <p>{dialog.message}</p>
          {dialog.kind === "service-error" && <p className="settings-hint">
            无法确认后台状态时，也可以仅关闭窗口。后台服务和可能仍在运行的任务不会被停止，下次启动可重新连接。
          </p>}
          {dialog.kind === "close-choice" && <>
            <label className="desktop-remember"><input type="checkbox" checked={remember} disabled={saving}
              onChange={event => setRemember(event.target.checked)} />下次不再询问，记住本次选择</label>
            <p className="settings-hint">可在“设置 → 桌面设置”中修改。有处理任务时，退出仍需确认。</p>
            {!dialog.tray && <p className="settings-hint">当前托盘不可用，请选择退出或取消关闭。</p>}
          </>}
          {error && <p className="field-error" role="alert">{error}</p>}
          <div className={`desktop-dialog-actions${dialog.kind === "close-choice" ? " desktop-close-actions" : ""}`}>
            {dialog.kind === "close-task" && dialog.tray && <button className="primary-button" disabled={saving} onClick={() => void respond("background")}>后台继续</button>}
            {((dialog.kind === "close-task" && !dialog.tray) || dialog.kind === "exit-task") &&
              <button className="primary-button" disabled={saving} onClick={() => void respond("stop-exit")}>停止任务并退出</button>}
            {dialog.kind === "service-error" && <button className="secondary-button" disabled={saving} onClick={() => void respond("retry-close")}>重新检查并退出</button>}
            {dialog.kind === "service-error" && <button className="secondary-button" disabled={saving} onClick={() => void respond("close-window")}>仅关闭窗口</button>}
            {dialog.kind === "close-choice" && <div className="desktop-close-choices" role="group" aria-label="关闭方式">
              <button className="secondary-button" disabled={!dialog.tray || saving} onClick={() => void respond("background")}><Minimize2 size={17} aria-hidden="true" />最小化到托盘</button>
              <button className="secondary-button" disabled={saving} onClick={() => void respond("exit")}><LogOut size={17} aria-hidden="true" />退出应用</button>
            </div>}
            <button ref={cancelButton} className={`secondary-button${dialog.kind === "close-choice" ? " desktop-close-cancel" : ""}`} disabled={saving} onClick={close}>
              {["close-choice", "close-task"].includes(dialog.kind) ? "取消关闭" : "取消"}
            </button>
          </div>
        </div>
      </div>}
    </>, document.body)}
  </>;
}
