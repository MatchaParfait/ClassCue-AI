import { useState } from "react";
import { Check, RefreshCw, X } from "lucide-react";
import type { StudySyncTask } from "../api/deviceTransfers";

const states: Record<string, string> = {
  waiting_peer: "等待平板响应", awaiting_confirmation: "待确认修改",
  awaiting_ack: "已保存，等待平板确认", completed: "已同步", rejected: "未接收",
};
const fields: Record<string, string> = {
  title: "标题", note: "备注", removed: "状态", importance: "重要程度", user_start_ms: "跳转位置",
  source_quote: "字幕引用", trigger_phrase: "来源提示", confidence: "置信度", origin: "来源",
  mode: "方案", kind: "类型", scheme_name: "方案名称", scheme_revision: "方案版本", start_ms: "原始开始", end_ms: "原始结束",
};
function display(field: string, value: unknown) {
  if (value == null) return field === "user_start_ms" ? "原始位置" : "未填写";
  if (field === "removed") return value ? "已移除" : "保留";
  if (field === "importance") return ({ standard: "普通", important: "重要", critical: "非常重要" } as Record<string, string>)[String(value)] ?? String(value);
  if (field.endsWith("_ms") && typeof value === "number") {
    const seconds = Math.floor(value / 1000);
    return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
  }
  return String(value) || "未填写";
}

export function DeviceStudySyncTask({ task, busy, execute }: {
  task: StudySyncTask; busy: boolean; execute: (id: string, action: string, choices?: Record<string, string>) => void;
}) {
  const [choices, setChoices] = useState<Record<string, string>>({});
  const conflicts = task.preview?.conflicts ?? [];
  return <article className="device-task">
    <div className="device-task-heading"><strong>{task.name}</strong><span>{states[task.state] ?? task.state}</span></div>
    <p className="device-task-meta">{task.peer_name}{task.preview?.bootstrap ? " · 首次关联" : ""}</p>
    {task.error && <p className="device-note field-error" role="alert">{task.error}</p>}
    {task.state === "awaiting_confirmation" && task.preview && <>
      <p className="device-note">新增 {task.preview.added} · 移除 {task.preview.removed} · 其他修改 {task.preview.edited}
        {conflicts.length > 0 && ` · 冲突 ${conflicts.length}`}</p>
      {conflicts.map(conflict => <fieldset className="device-sync-conflict" key={conflict.id}>
        <legend>{conflict.title}</legend>
        <div className="device-sync-versions">{(["local", "remote"] as const).map(side => <label key={side}>
          <span><input type="radio" name={`sync-${task.id}-${conflict.id}`} checked={choices[conflict.id] === side}
            disabled={busy} onChange={() => setChoices(current => ({ ...current, [conflict.id]: side }))} />
            {side === "local" ? "保留电脑修改" : "采用平板修改"}</span>
          <dl>{conflict.fields.map(field => <div key={field}><dt>{fields[field] ?? "内容"}</dt>
            <dd>{display(field, conflict[side][field])}</dd></div>)}</dl>
        </label>)}</div>
      </fieldset>)}
      <div className="device-task-actions">
        <button className="text-button" disabled={busy} onClick={() => execute(task.id, "refresh")}><RefreshCw size={14} />刷新预览</button>
        {task.direction === "incoming" && <button className="text-button" disabled={busy} onClick={() => execute(task.id, "reject")}><X size={14} />拒绝</button>}
        <button className="primary-button compact" disabled={busy || conflicts.some(conflict => !choices[conflict.id])}
          onClick={() => execute(task.id, "accept", choices)}><Check size={14} />确认同步</button>
      </div>
    </>}
  </article>;
}
