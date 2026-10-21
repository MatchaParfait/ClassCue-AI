import { RECALL_MODES } from "../processOptions";
import type { RecallMode } from "../types";
import { useId } from "react";
import { useRecallSchemes } from "../hooks/useRecallSchemes";
import { Link } from "react-router-dom";

export function RecallModeControl({ value = ["medium_recall"], onChange, disabled = false, active = true }: {
  value: RecallMode[];
  onChange: (value: RecallMode[]) => void;
  disabled?: boolean;
  active?: boolean;
}) {
  const hintId = useId();
  const schemes = useRecallSchemes(active);
  const custom = (schemes.data ?? []).filter(item => !item.archived);
  const choices: { value: RecallMode; label: string; base?: string }[] = [...RECALL_MODES, ...custom.map(item => ({
    value: item.id, label: item.name,
    base: RECALL_MODES.find(base => base.value === item.base_mode)?.label,
  }))];
  const missing = value.filter(id => id.startsWith("custom_") && !custom.some(item => item.id === id));
  return (
    <fieldset className="recall-mode-control" disabled={disabled} aria-describedby={hintId}>
      <legend>考点召回</legend>
      <div className="recall-mode-options">
        {choices.map((mode) => {
          const checked = value.includes(mode.value);
          return <label key={mode.value} className={`recall-mode-option${checked ? " is-selected" : ""}`}>
            <input type="checkbox" value={mode.value} checked={checked}
              disabled={disabled || (!checked && value.length >= 8)}
              onChange={() => {
                onChange(checked ? value.filter(item => item !== mode.value)
                  : [...new Set([...choices.map(item => item.value).filter(item => item === mode.value || value.includes(item)), ...missing])]);
              }} />
            <span title={mode.label}>{mode.label}{mode.base && <small>基于{mode.base}</small>}</span>
          </label>;
        })}
      </div>
      {missing.length > 0 && <p className="field-error" role="alert">所选自定义方案尚未加载或已停用。
        <button className="text-button" type="button" disabled={disabled} onClick={() => onChange(value.filter(id => !missing.includes(id)))}>移除不可用选择</button></p>}
      {schemes.isError && <p className="field-error" role="status">自定义方案读取失败。
        <button className="text-button" type="button" onClick={() => void schemes.refetch()}>重试</button></p>}
      <p id={hintId} className="field-hint">可多选；每种方案独立调用 AI，多选会使用更多 token。</p>
      <Link className="text-button recall-manage-link" to="/settings?section=recall">管理自定义方案</Link>
    </fieldset>
  );
}
