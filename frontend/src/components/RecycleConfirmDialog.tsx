import { FileVideo, Folder, LoaderCircle, RotateCcw, Trash2, X } from "lucide-react";
import { useRef, useState, type RefObject } from "react";
import { createPortal } from "react-dom";
import { useModalFocus } from "../hooks/useModalFocus";
import { AnimatedOverlay, AnimatedPopup } from "./Motion";

export function RecycleConfirmDialog({ kind, name, backgroundRef, onConfirm, onClose }: {
  kind: "video" | "folder";
  name: string;
  backgroundRef: RefObject<HTMLElement>;
  onConfirm: () => Promise<void>;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDivElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const submitting = useRef(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const close = () => { if (!submitting.current) onClose(); };
  useModalFocus(true, dialog, cancel, close, backgroundRef);

  const confirm = async () => {
    if (submitting.current) return;
    submitting.current = true;
    setBusy(true);
    setError("");
    try {
      await onConfirm();
      onClose();
    } catch (reason) {
      setError(`移入回收站失败：${reason instanceof Error ? reason.message : String(reason)}`);
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  };
  const Icon = kind === "folder" ? Folder : FileVideo;

  return createPortal(
    <AnimatedOverlay className="modal-backdrop recycle-confirm-backdrop" duration={.18} onClick={event => {
      if (event.target === event.currentTarget) close();
    }}>
      <AnimatedPopup ref={dialog} className="recycle-confirm-dialog" role="dialog" aria-modal="true"
        aria-labelledby="recycle-confirm-title" aria-describedby="recycle-confirm-description" aria-busy={busy}
        tabIndex={-1} duration={.18}>
        <div className="recycle-confirm-heading">
          <span className="recycle-confirm-symbol"><Trash2 size={21} aria-hidden="true" /></span>
          <h2 id="recycle-confirm-title">移入回收站？</h2>
          <button className="icon-button" type="button" aria-label="关闭删除确认" title="关闭"
            disabled={busy} onClick={close}><X size={18} /></button>
        </div>
        <div className="recycle-confirm-object">
          <Icon size={21} aria-hidden="true" />
          <div><span>{kind === "folder" ? "文件夹" : "视频"}</span><strong>{name}</strong></div>
        </div>
        <p id="recycle-confirm-description">
          {kind === "folder"
            ? "此文件夹及其中的子文件夹、视频和相关资料将保存为一个 ClassCue 文件夹，移入 Windows 回收站，本次不会永久删除。清空 Windows 回收站后将无法恢复。"
            : "此视频和相关资料将保存为一个 ClassCue 文件夹，移入 Windows 回收站，本次不会永久删除。清空 Windows 回收站后将无法恢复。"}
        </p>
        <p className="recycle-confirm-undo"><RotateCcw size={14} aria-hidden="true" /><span>清空前可按 <kbd>Ctrl</kbd> + <kbd>Z</kbd> 或点击提示中的“撤销”。在 Windows 中还原后，仍需回到软件撤销以恢复课程记录。</span></p>
        {error && <p className="field-error recycle-confirm-error" role="alert">{error}</p>}
        <div className="recycle-confirm-actions">
          <button ref={cancel} className="secondary-button" type="button" disabled={busy} onClick={close}>取消</button>
          <button className="primary-button recycle-confirm-submit" type="button" disabled={busy} onClick={() => void confirm()}>
            {busy ? <LoaderCircle size={16} className="spin" aria-hidden="true" /> : <Trash2 size={16} aria-hidden="true" />}
            {busy ? "正在移入…" : error ? "重试移入" : "移入回收站"}
          </button>
        </div>
      </AnimatedPopup>
    </AnimatedOverlay>, document.body,
  );
}
