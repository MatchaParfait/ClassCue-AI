import { AlertTriangle } from "lucide-react";
import type { ProcessForm } from "../processOptions";

export function QwenHardwareWarning() {
  return <p className="qwen-hardware-warning" role="note">
    <AlertTriangle size={16} aria-hidden="true" />
    <span>Qwen 要求 GPU 原生显存至少 6GB；低于 6GB 请勿使用此模型。共享显存不计入。</span>
  </p>;
}

export function QwenResourceControls({
  form, onChange, disabled,
}: { form: ProcessForm; onChange: (form: ProcessForm) => void; disabled: boolean }) {
  const mode = form.qwenMode ?? "auto";
  return (
    <div className="field-group qwen-resource-controls">
      <QwenHardwareWarning />
      <label className="field-label">Qwen 资源模式
        <select value={mode} disabled={disabled} onChange={(event) => onChange({
          ...form, qwenMode: event.target.value as ProcessForm["qwenMode"],
        })}>
          <option value="auto">自动</option>
          <option value="low-resource">节省资源</option>
          <option value="manual">手动</option>
        </select>
      </label>
      {mode === "manual" && <>
        <div className="option-grid qwen-numeric-controls">
          <label className="field-group"><span className="field-label">ASR BS</span>
            <input type="number" min={1} max={8} disabled={disabled} value={form.qwenBatchSize ?? 2}
              onChange={(event) => onChange({ ...form, qwenBatchSize: Number(event.target.value) })} />
          </label>
          <label className="field-group"><span className="field-label">目标分块（秒）</span>
            <input type="number" min={30} max={1200} disabled={disabled} value={form.qwenChunkSeconds ?? 90}
              onChange={(event) => onChange({ ...form, qwenChunkSeconds: Number(event.target.value) })} />
          </label>
          <label className="field-group"><span className="field-label">对齐 BS</span>
            <input type="number" min={1} max={8} disabled={disabled} value={form.qwenAlignBatchSize ?? 2}
              onChange={(event) => onChange({ ...form, qwenAlignBatchSize: Number(event.target.value) })} />
          </label>
        </div>
        <label className="check-option"><input type="checkbox" disabled={disabled}
          checked={Boolean(form.qwenLowMemory)}
          onChange={(event) => onChange({ ...form, qwenLowMemory: event.target.checked })} />
          <span>低内存加载</span></label>
        <label className="check-option"><input type="checkbox" disabled={disabled}
          checked={Boolean(form.qwenAutoRecovery)}
          onChange={(event) => onChange({ ...form, qwenAutoRecovery: event.target.checked })} />
          <span>自动资源降级</span></label>
      </>}
    </div>
  );
}
