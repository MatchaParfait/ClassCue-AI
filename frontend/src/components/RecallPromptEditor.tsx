import { useEffect, useId, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, ChevronDown, Code2, LockKeyhole, LoaderCircle, RotateCcw } from "lucide-react";
import { previewRecallPrompt } from "../api/client";
import type { RecallSchemeInput } from "../types";

export function RecallPromptEditor({ form, active, busy, onChange }: {
  form: RecallSchemeInput;
  active: boolean;
  busy: boolean;
  onChange: (prompt: string | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const [confirmReset, setConfirmReset] = useState(false);
  const id = useId();
  const advanced = form.advanced_prompt != null;
  const source = JSON.stringify({
    base_mode: form.base_mode, rules: form.rules,
    notes: form.notes, advanced_prompt: form.advanced_prompt ?? null,
  });
  const [settledSource, setSettledSource] = useState(source);
  useEffect(() => {
    if (!open || !active) return;
    const timer = window.setTimeout(() => setSettledSource(source), 350);
    return () => window.clearTimeout(timer);
  }, [source, open, active]);
  const preview = useQuery({
    queryKey: ["recall-prompt-preview", settledSource],
    queryFn: () => previewRecallPrompt(JSON.parse(settledSource)),
    enabled: active && open && settledSource === source && form.advanced_prompt !== "",
    gcTime: 0, staleTime: 30_000, retry: false,
  });
  const current = settledSource === source;
  const ready = current && Boolean(preview.data) && !preview.isError;
  const pending = form.advanced_prompt !== "" && (!current || preview.isPending);
  const content = advanced ? form.advanced_prompt! : ready ? preview.data!.prompt : "";
  return <section className="recall-advanced" aria-label="高级提示词">
    <button className="recall-advanced-toggle" type="button" aria-expanded={open} aria-controls={`${id}-body`}
      disabled={busy} onClick={() => setOpen(value => !value)}>
      <Code2 size={17} /><strong>高级：完整提示词编辑</strong>
      <span>{advanced ? "已启用" : "未启用"}</span><ChevronDown className={open ? "is-open" : ""} size={17} />
    </button>
    {open && <div id={`${id}-body`} className="recall-advanced-body">
      <p className="recall-advanced-warning"><AlertTriangle size={16} />
        <span>修改可能增加漏检、误检和 token 消耗。建议复制方案后修改；输出格式与字幕证据校验由系统维护。</span>
      </p>
      {!advanced && <button className="secondary-button compact" type="button" disabled={busy || !ready}
        onClick={() => { if (ready) onChange(preview.data!.prompt); }}><Code2 size={15} />启用完整编辑</button>}
      {pending && <p className="settings-hint" role="status"><LoaderCircle className="spin" size={15} />正在组装提示词…</p>}
      {current && preview.isError && <p className="field-error" role="alert">提示词读取失败：{preview.error.message}
        <button className="text-button" type="button" disabled={busy} onClick={() => void preview.refetch()}><RotateCcw size={14} />重试</button>
      </p>}
      {(advanced || ready) && <label className="field-group">
        <span className="field-label">完整业务提示词 <small>{advanced ? `${content.length}/60000` : "只读预览"}</small></span>
        <textarea className="recall-prompt-text" rows={18} maxLength={60000} value={content}
          readOnly={!advanced} disabled={busy} spellCheck={false}
          aria-describedby={`${id}-mode`} onChange={event => onChange(event.target.value)} />
      </label>}
      <p id={`${id}-mode`} className="settings-hint">{advanced
        ? "当前使用这份完整提示词；教师表达和补充偏好已包含在内，不再重复追加。收起后仍然生效。"
        : "启用后可修改任务说明、判断规则和示例。当前教师表达与补充偏好会一起复制。"}
      </p>
      {ready && <>
        <details className="recall-prompt-readonly"><summary><LockKeyhole size={15} />固定输出与字幕引用协议 · 只读</summary>
          <pre>{preview.data!.protocol}</pre>
        </details>
        <details className="recall-prompt-readonly"><summary>当前完整请求预览</summary>
          {preview.data!.messages.map((message, index) => <div key={index}>
            <h4>{message.role === "system" ? "系统消息" : "用户消息"}</h4><pre>{message.content}</pre>
          </div>)}
        </details>
        <details className="recall-prompt-readonly"><summary>异常修正指令 · 只读</summary>
          <h4>格式修正</h4><pre>{preview.data!.repair}</pre>
          <h4>证据引用修正</h4><pre>{preview.data!.evidence_repair}</pre>
        </details>
      </>}
      {advanced && <div className="recall-prompt-reset">
        <button className="secondary-button compact" type="button" disabled={busy}
          onClick={() => setConfirmReset(true)}><RotateCcw size={15} />恢复基础编辑</button>
        {confirmReset && <div role="group" aria-label="确认恢复基础编辑">
          <p>恢复后将使用原基础方案、教师表达和补充偏好，替换当前完整提示词。保存后才生效。</p>
          <button className="secondary-button compact" type="button" disabled={busy}
            onClick={() => { onChange(null); setConfirmReset(false); }}>确认恢复</button>
          <button className="text-button" type="button" disabled={busy} onClick={() => setConfirmReset(false)}>取消</button>
        </div>}
      </div>}
    </div>}
  </section>;
}
