import { Clock3, LoaderCircle, RotateCcw, X } from "lucide-react";
import { useId, useRef, useState } from "react";
import type { Marker } from "../types";
import { captureJumpTime, formatTime } from "../utils";

interface MarkerTimeEditorProps {
  marker: Marker;
  durationMs: number;
  currentTimeMs: number;
  getCurrentTimeMs: () => number;
  onSave: (timeMs: number | null) => Promise<void>;
  onClose: () => void;
}

export function MarkerTimeEditor({ marker, durationMs, currentTimeMs, getCurrentTimeMs, onSave, onClose }: MarkerTimeEditorProps) {
  const inputId = useId();
  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const captureTime = (milliseconds: number) => captureJumpTime(milliseconds, durationMs);

  const save = async (nextTime: number | null) => {
    if (savingRef.current) return;
    savingRef.current = true;
    setSaving(true);
    setSaveError(null);
    try {
      await onSave(nextTime);
      onClose();
    } catch (failure) {
      setSaveError(failure instanceof Error ? failure.message : "保存失败，请重试");
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  };

  const saveCurrentPosition = () => {
    if (savingRef.current) return;
    const currentTime = getCurrentTimeMs();
    if (!Number.isFinite(currentTime)) {
      setSaveError("无法读取当前播放位置，请稍后重试");
      return;
    }
    void save(captureTime(currentTime));
  };

  return (
    <section className="marker-time-editor" aria-label={`调整跳转时间：${marker.title}`} aria-busy={saving}
      onKeyDown={(event) => {
        if (event.key === "Escape" && !saving) { event.preventDefault(); onClose(); }
      }}>
      <fieldset disabled={saving}>
        <div className="marker-editor-heading">
          <label htmlFor={inputId}>将保存的跳转时间</label>
          <button className="icon-button marker-editor-icon" type="button" onClick={onClose} title="取消编辑" aria-label="取消编辑"><X size={15} /></button>
        </div>
        <input id={inputId} className="marker-time-input" type="text" readOnly disabled
          value={Number.isFinite(currentTimeMs) ? formatTime(captureTime(currentTimeMs)) : "--:--"} />
        <div className="marker-original-time">原始定位 {formatTime(marker.start_ms)}</div>
        <div className="marker-editor-tools">
          <button className="secondary-button compact" type="button" autoFocus onClick={saveCurrentPosition}>
            {saving ? <LoaderCircle className="spin" size={14} /> : <Clock3 size={14} />}
            {saving ? "保存中…" : "保存当前播放位置"}
          </button>
        </div>
        {saveError && <p className="field-error" role="alert">{saveError}</p>}
        <div className="marker-editor-footer">
          <button className="secondary-button compact" type="button" onClick={onClose}>取消</button>
          {marker.user_start_ms != null && <button className="icon-button marker-editor-icon marker-reset-time" type="button"
            title="恢复原始时间" aria-label="恢复原始时间" onClick={() => void save(null)}><RotateCcw size={15} /></button>}
        </div>
      </fieldset>
    </section>
  );
}
