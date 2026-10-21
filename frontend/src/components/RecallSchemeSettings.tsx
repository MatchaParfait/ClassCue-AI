import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Copy, FileText, LoaderCircle, Minus, Plus, RotateCcw, Save, Trash2, X } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import { archiveRecallScheme, saveRecallScheme } from "../api/client";
import { recallSchemesKey, useRecallSchemes } from "../hooks/useRecallSchemes";
import { RECALL_MODES } from "../processOptions";
import type { RecallScheme, RecallSchemeInput, TeacherRule } from "../types";
import { useUnsavedDraft } from "./UnsavedChanges";
import { RecallPromptEditor } from "./RecallPromptEditor";

const emptyScheme = (): RecallSchemeInput => ({ name: "", base_mode: "medium_recall", rules: [], notes: "" });
const formOf = (scheme: RecallScheme): RecallSchemeInput => ({
  name: scheme.name, base_mode: scheme.base_mode, rules: scheme.rules.map(rule => ({ ...rule })), notes: scheme.notes,
  ...(scheme.advanced_prompt != null ? { advanced_prompt: scheme.advanced_prompt } : {}),
});
const preferenceTemplate = [
  "优先关注：教师明确要求掌握的概念、方法和易错条件。",
  "减少纳入：仅用于当前例题的操作提醒、闲聊和重复表述。",
  "范围限制：只标记有字幕依据的具体知识对象。",
].join("\n");
const ruleGroups: { meaning: TeacherRule["meaning"]; label: string; addLabel: string; placeholder: string }[] = [
  { meaning: "emphasis", label: "正向提示词", addLabel: "添加正向表达", placeholder: "例如：拿笔画下来、这个要记住" },
  { meaning: "deemphasize", label: "负向提示词", addLabel: "添加负向表达", placeholder: "例如：随便了解一下、不用记" },
  { meaning: "ordinary", label: "一般提示", addLabel: "添加一般表达", placeholder: "例如：看一下" },
];

export function RecallSchemeSettings({ active, onSaved }: { active: boolean; onSaved?: () => void }) {
  const queryClient = useQueryClient();
  const schemes = useRecallSchemes(active);
  const [selected, setSelected] = useState<RecallScheme | null>(null);
  const [form, setForm] = useState<RecallSchemeInput | null>(null);
  const [original, setOriginal] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [confirmArchive, setConfirmArchive] = useState<string | null>(null);
  useEffect(() => {
    if (!notice || !active) return;
    const timer = window.setTimeout(() => setNotice(""), 4000);
    return () => window.clearTimeout(timer);
  }, [notice, active]);
  const submitting = useRef(false);
  const nameInput = useRef<HTMLInputElement>(null);
  const ruleInputs = useRef(new Map<number, HTMLInputElement>());
  const notesId = useId();
  const dirty = form !== null && JSON.stringify(form) !== original;
  const advanced = form?.advanced_prompt != null;
  const mutation = useMutation({
    mutationFn: ({ value, id }: { value: RecallSchemeInput; id?: string }) => saveRecallScheme(value, id),
    onSuccess: saved => {
      queryClient.setQueryData<RecallScheme[]>(recallSchemesKey, current =>
        current?.some(item => item.id === saved.id) ? current.map(item => item.id === saved.id ? saved : item) : [...(current ?? []), saved]);
      void queryClient.invalidateQueries({ queryKey: ["study", "analysis-runs"] });
      void queryClient.invalidateQueries({ queryKey: ["process", "options"] });
      setSelected(saved); setForm(formOf(saved)); setOriginal(JSON.stringify(formOf(saved)));
      setNotice("方案已保存"); setError("");
    },
  });
  const archive = useMutation({
    mutationFn: archiveRecallScheme,
    onSuccess: (_value, id) => {
      queryClient.setQueryData<RecallScheme[]>(recallSchemesKey, current => current?.map(item => item.id === id ? { ...item, archived: true } : item));
      setConfirmArchive(null);
      if (selected?.id === id) { setSelected(null); setForm(null); setOriginal(""); }
      setNotice("方案已停用，历史结果保留"); setError("");
    },
    onError: reason => setError(reason.message),
  });
  const busy = mutation.isPending || archive.isPending;
  const save = async () => {
    if (!form || submitting.current) return false;
    const value = { ...form, name: form.name.trim(), notes: form.notes.trim(),
      rules: form.rules.map(rule => ({ ...rule, phrase: rule.phrase.trim() })) };
    if (!value.name) { setError("请填写方案名称"); nameInput.current?.focus(); return false; }
    if (value.advanced_prompt != null && !value.advanced_prompt.trim()) {
      setError("完整提示词不能为空；请填写内容或恢复基础编辑"); return false;
    }
    if (value.rules.some(rule => !rule.phrase)) { setError("请填写教师表达，或删除空白规则"); return false; }
    if (new Set(value.rules.map(rule => rule.phrase.toLocaleLowerCase())).size !== value.rules.length) {
      setError("同一方案内的教师表达不能重复"); return false;
    }
    submitting.current = true;
    try {
      await mutation.mutateAsync({ value, id: selected?.id });
      return true;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason)); return false;
    } finally { submitting.current = false; }
  };
  useUnsavedDraft({ label: "自定义召回方案", dirty, busy, save, discard: () => {
    setForm(original ? JSON.parse(original) as RecallSchemeInput : null); setError("");
  } });

  const choose = (scheme: RecallScheme | null, duplicate = false) => {
    const value = scheme ? formOf(scheme) : emptyScheme();
    if (duplicate) value.name = `${value.name.slice(0, 50)}（副本）`;
    setSelected(duplicate ? null : scheme); setForm(value); setOriginal(JSON.stringify(duplicate ? emptyScheme() : value));
    setError(""); setNotice(""); setConfirmArchive(null);
    requestAnimationFrame(() => nameInput.current?.focus());
  };
  const addRule = (meaning: TeacherRule["meaning"]) => {
    if (!form || busy || form.rules.length >= 20) return;
    const index = form.rules.length;
    setForm({ ...form, rules: [...form.rules, { phrase: "", meaning }] });
    requestAnimationFrame(() => ruleInputs.current.get(index)?.focus());
  };
  const templatedNotes = form?.notes.trim()
    ? `${form.notes.trimEnd()}\n\n${preferenceTemplate}` : preferenceTemplate;
  const entries = schemes.data ?? [];
  return <section className="settings-section recall-settings">
    <div className="settings-section-heading"><div><p className="eyebrow">考点提取</p><h2>自定义召回方案</h2></div>
      <button className="secondary-button" type="button" disabled={busy || dirty || entries.filter(item => !item.archived).length >= 20}
        onClick={() => choose(null)}><Plus size={16} />新建方案</button></div>
    {notice && <p className="recall-settings-notice" role="status">{notice}</p>}
    {schemes.isPending && active && <p role="status"><LoaderCircle className="spin" size={16} />正在读取方案…</p>}
    {schemes.isError && <p className="field-error" role="alert">方案读取失败：{schemes.error.message}
      <button className="text-button" type="button" onClick={() => void schemes.refetch()}><RotateCcw size={15} />重试</button></p>}
    <div className="recall-settings-layout">
      <div className="recall-scheme-list" aria-label="自定义召回方案列表">
        {entries.filter(item => !item.archived).map(scheme => <div key={scheme.id} className="recall-scheme-row">
          <button type="button" className={`recall-scheme-entry${selected?.id === scheme.id ? " is-selected" : ""}`}
            aria-pressed={selected?.id === scheme.id} disabled={busy || (dirty && selected?.id !== scheme.id)}
            onClick={() => { if (!dirty) choose(scheme); }} title={scheme.name}>
            <strong>{scheme.name}</strong><span>基于{RECALL_MODES.find(item => item.value === scheme.base_mode)?.label} · 版本 {scheme.revision}</span>
          </button>
          <button className="icon-button" type="button" title={`复制方案“${scheme.name}”`} aria-label={`复制方案“${scheme.name}”`}
            disabled={busy || dirty} onClick={() => choose(scheme, true)}><Copy size={15} /></button>
          <button className="icon-button danger-icon" type="button" title={`停用方案“${scheme.name}”`} aria-label={`停用方案“${scheme.name}”`}
            disabled={busy || dirty} onClick={() => setConfirmArchive(scheme.id)}><Trash2 size={15} /></button>
          {confirmArchive === scheme.id && <div className="recall-scheme-archive" role="group" aria-label={`确认停用“${scheme.name}”`}>
            <span>停用后保留历史结果</span><button className="secondary-button compact" type="button" disabled={busy}
              onClick={() => archive.mutate(scheme.id)}>确认停用</button>
            <button className="text-button" type="button" disabled={busy} onClick={() => setConfirmArchive(null)}>取消</button>
          </div>}
        </div>)}
        {!entries.some(item => !item.archived) && !schemes.isPending && <p className="settings-hint">暂无自定义方案</p>}
        {entries.some(item => item.archived) && <details className="recall-archived"><summary>已停用方案</summary>
          {entries.filter(item => item.archived).map(item => <p key={item.id}>{item.name}</p>)}</details>}
      </div>
      {form && <form className="recall-scheme-form" aria-label="编辑召回方案" onSubmit={event => { event.preventDefault(); void save().then(saved => { if (saved) onSaved?.(); }); }}>
        <label className="field-group"><span className="field-label">方案名称</span>
          <input ref={nameInput} required maxLength={60} value={form.name} disabled={busy} onChange={event => setForm({ ...form, name: event.target.value })} /></label>
        <fieldset className="recall-basic-fields" disabled={busy || advanced}>
        <label className="field-group"><span className="field-label">提示词基座</span>
          <select value={form.base_mode} onChange={event => setForm({ ...form, base_mode: event.target.value as RecallSchemeInput["base_mode"] })}>
            {RECALL_MODES.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label>
        <section className="recall-expressions" aria-label="教师表达">
          <div className="recall-rule-heading"><h3>教师表达</h3><span>{form.rules.length} / 20 条</span></div>
          <div className="recall-rule-groups">
            {ruleGroups.filter(group => group.meaning !== "ordinary" || form.rules.some(rule => rule.meaning === "ordinary")).map(group => {
              const rules = form.rules.map((rule, index) => ({ ...rule, index })).filter(rule => rule.meaning === group.meaning);
              return <fieldset className={`recall-rule-group is-${group.meaning}`} key={group.meaning} disabled={busy}>
                <legend>{group.meaning === "deemphasize" ? <Minus size={16} /> : group.meaning === "emphasis" ? <Plus size={16} /> : <FileText size={16} />}
                  <span>{group.label}</span><small>{rules.length}</small></legend>
                <div className="recall-rule-fields">
                  {rules.map(rule => <div className="recall-rule-row" key={rule.index}>
                    <input ref={node => { if (node) ruleInputs.current.set(rule.index, node); else ruleInputs.current.delete(rule.index); }}
                      required aria-label={`教师表达 ${rule.index + 1}`} maxLength={60} value={rule.phrase} disabled={busy} placeholder={group.placeholder}
                      onChange={event => setForm({ ...form, rules: form.rules.map((item, at) => at === rule.index ? { ...item, phrase: event.target.value } : item) })} />
                    <button className="icon-button" type="button" aria-label={`删除教师表达 ${rule.index + 1}`} title="删除表达" disabled={busy}
                      onClick={() => setForm({ ...form, rules: form.rules.filter((_item, at) => at !== rule.index) })}><X size={15} /></button>
                  </div>)}
                </div>
                {!rules.length && <p className="recall-rule-example">{group.placeholder}</p>}
                <button className="secondary-button recall-rule-add" type="button" disabled={busy || form.rules.length >= 20}
                  onClick={() => addRule(group.meaning)}><Plus size={16} />{group.addLabel}</button>
              </fieldset>;
            })}
          </div>
        </section>
        <div className="field-group recall-notes">
          <div className="recall-notes-heading">
            <label className="field-label" htmlFor={notesId}>补充偏好 <small>可选</small></label>
            <button className="secondary-button compact" type="button" title={form.notes.trim() ? "追加模板，保留已填写的内容" : "填入可编辑的参考模板"}
              disabled={busy || form.notes.includes(preferenceTemplate) || templatedNotes.length > 500}
              onClick={() => setForm({ ...form, notes: templatedNotes })}><FileText size={15} />插入模板</button>
          </div>
          <textarea id={notesId} aria-describedby={`${notesId}-count`} maxLength={500} rows={5} placeholder={preferenceTemplate}
            value={form.notes} disabled={busy} onChange={event => setForm({ ...form, notes: event.target.value })} />
          <small id={`${notesId}-count`} className="recall-notes-count">{form.notes.length}/500</small>
        </div>
        </fieldset>
        <RecallPromptEditor key={selected?.id ?? "new"} form={form} active={active} busy={busy}
          onChange={prompt => setForm({ ...form, advanced_prompt: prompt })} />
        <p className="settings-hint">字幕证据、时间定位与输出格式使用固定校验。保存不发起 AI 请求。</p>
        <div className="recall-scheme-actions"><button className="primary-button" type="submit" disabled={busy || !dirty || schemes.isError}>
          {mutation.isPending ? <LoaderCircle className="spin" size={16} /> : <Save size={16} />}{mutation.isPending ? "正在保存…" : "保存方案"}</button>
          <button className="secondary-button" type="button" disabled={busy} onClick={() => { setForm(original ? JSON.parse(original) : null); setError(""); }}>撤销修改</button>
        </div>
      </form>}
    </div>
    {error && <p className="field-error" role="alert">{error}</p>}
  </section>;
}
