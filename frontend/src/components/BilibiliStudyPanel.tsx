import { useEffect, useRef, useState, type RefObject } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, ExternalLink, LoaderCircle, RotateCcw, Save, Search, ShieldCheck, Square, X } from "lucide-react";
import {
  getBiliAccount, getBiliState, planBiliSearch, resumeBiliReading,
  saveBiliContext, startBiliSearch, continueBiliSearch,
} from "../api/bilibili";
import type { BiliCandidate, BiliContext, BiliQuery, BiliStarted, BiliUnit, BiliTarget } from "../api/bilibili";
import { cancelJob } from "../api/client";
import { useModalFocus } from "../hooks/useModalFocus";
import { isTaskTerminal, useTaskStatus } from "../hooks/useTaskStatus";
import { useUnsavedDraft, useUnsavedLeave } from "./UnsavedChanges";
import { BilibiliLogin } from "./BilibiliLogin";
import type { Marker } from "../types";
import { formatTime } from "../utils";
import "../bilibili.css";

const EMPTY_CONTEXT: BiliContext = { course: "", notes: "" };
const ACTIVE = new Set(["planning", "searching", "screening", "reading", "checking"]);
const VERDICTS = { covers: "覆盖目标考点", partial: "部分相关", unrelated: "不相关", insufficient: "证据不足" };
const STATUSES: Record<string, string> = {
  planning: "生成搜索方案", ready: "等待确认搜索词", searching: "搜索中", screening: "初筛中",
  reading: "读取字幕", checking: "核验字幕", completed: "已完成", failed: "未完成", interrupted: "已停止",
  unverified: "未核验", no_subtitles: "无可用字幕", requires_login: "需要登录后读取",
  read_failed: "字幕读取失败", available: "字幕已取得", checked: "字幕已核验",
  awaiting_approval: "等待确认补充搜索词", budget_exhausted: "预算已用完", needs_clarification: "需要补充课程类别",
};
const STOP: Record<string, string> = {
  success: "已找到满足目标的字幕讲解", partial: "仅找到部分相关讲解", exhausted: "未找到满足全部要求的讲解",
  budget: "本次预算已用完，已保存完成的核验", clarification: "请补充课程类别后重新生成方案",
  approval: "等待确认补充课程搜索词", cancelled: "已停止，完成的证据已保留",
  platform_pause: "平台读取已暂停", service_error: "服务失败，可手动继续",
};

export function BilibiliStudyPanel({ marker, backgroundRef, onClose }: {
  marker: Marker; backgroundRef: RefObject<HTMLElement>; onClose: () => void;
}) {
  const dialog = useRef<HTMLElement>(null), closeButton = useRef<HTMLButtonElement>(null);
  const cache = useQueryClient();
  const key = ["bilibili", marker.id];
  const state = useQuery({
    queryKey: key, queryFn: () => getBiliState(marker.id),
    refetchInterval: query => query.state.data?.runs.some(run => ACTIVE.has(run.status)) ? 1000 : false,
    refetchIntervalInBackground: false,
  });
  const account = useQuery({ queryKey: ["bilibili-account"], queryFn: getBiliAccount });
  const [context, setContext] = useState<BiliContext>(EMPTY_CONTEXT);
  const [savedContext, setSavedContext] = useState<BiliContext>(EMPTY_CONTEXT);
  const initialized = useRef(false);
  const [queries, setQueries] = useState<BiliQuery[]>([]);
  const [courseQueries, setCourseQueries] = useState<BiliQuery[]>([]);
  const [pendingQueries, setPendingQueries] = useState<BiliQuery[]>([]);
  const [target, setTarget] = useState<BiliTarget>();
  const [autoRewrite, setAutoRewrite] = useState(false), [increaseBudget, setIncreaseBudget] = useState(false);
  const [selectedRun, setSelectedRun] = useState("");
  const [submitted, setSubmitted] = useState<BiliStarted | null>(null);
  const run = state.data?.runs.find(item => item.id === selectedRun) ?? state.data?.runs[0];
  const serverQueries = JSON.stringify(run?.queries ?? []);
  const serverPlan = JSON.stringify({ target: run?.target, course: run?.course_queries ?? [], pending: run?.pending_queries ?? [] });
  const [pending, setPending] = useState(false), [error, setError] = useState("");
  const [searchConsent, setSearchConsent] = useState(false);
  const [loginActive, setLoginActive] = useState(false), [riskAcknowledged, setRiskAcknowledged] = useState(false);
  const task = useTaskStatus(submitted?.job_id ?? run?.job_id ?? null);
  const taskBusy = pending || Boolean(submitted && !task.data) || Boolean(task.data && !isTaskTerminal(task.data))
    || Boolean(run && ACTIVE.has(run.status) && !task.data);
  const busy = taskBusy || loginActive;
  const dirty = JSON.stringify(context) !== JSON.stringify(savedContext);
  const operationLock = useRef(false);
  const leave = useUnsavedLeave();
  const requestClose = () => leave(onClose);
  useModalFocus(true, dialog, closeButton, requestClose, backgroundRef);
  useEffect(() => {
    if (state.data && !initialized.current) {
      initialized.current = true;
      setContext(state.data.context);
      setSavedContext(state.data.context);
    }
  }, [state.data]);
  useEffect(() => { setQueries(JSON.parse(serverQueries) as BiliQuery[]); setSearchConsent(false); }, [run?.id, serverQueries]);
  useEffect(() => {
    const plan = JSON.parse(serverPlan) as { target?: BiliTarget; course: BiliQuery[]; pending: BiliQuery[] };
    setTarget(plan.target); setCourseQueries(plan.course); setPendingQueries(plan.pending);
    setAutoRewrite(run?.auto_rewrite ?? false); setIncreaseBudget(false); setSearchConsent(false);
  }, [run?.id, serverPlan]);
  useEffect(() => {
    if (!task.data || !isTaskTerminal(task.data)) return;
    void cache.invalidateQueries({ queryKey: key });
    void cache.invalidateQueries({ queryKey: ["bilibili-account"] });
  }, [task.data?.id, task.data?.status]);

  const save = async () => {
    const value = await saveBiliContext(marker.id, context);
    setContext(value); setSavedContext(value);
    await cache.invalidateQueries({ queryKey: key });
  };
  useUnsavedDraft({
    label: "考点搜索补充信息", dirty, busy,
    save, discard: () => setContext(savedContext),
  });
  const perform = async (operation: () => Promise<void>) => {
    if (operationLock.current) return;
    operationLock.current = true; setPending(true); setError("");
    try { await operation(); }
    catch (failure) { setError(failure instanceof Error ? failure.message : "操作失败，请重试"); }
    finally { operationLock.current = false; setPending(false); }
  };
  const started = async (result: BiliStarted) => {
    setSubmitted(result); setSelectedRun(result.run_id);
    await cache.invalidateQueries({ queryKey: key });
  };
  const progress = Math.max(0, Math.min(100, Math.round(task.data?.progress ?? 0)));
  const recommendations = run?.candidates.filter(item => item.units.some(part =>
    !run?.legacy && part.status === "checked" && part.verdict === "covers")) ?? [];
  const others = run?.candidates.filter(item => !recommendations.includes(item)) ?? [];

  return <div className="modal-backdrop bili-backdrop" onClick={requestClose}>
    <section ref={dialog} className="bili-dialog" role="dialog" aria-modal="true" aria-labelledby="bili-title"
      tabIndex={-1} onClick={event => event.stopPropagation()}>
      <header className="dialog-heading">
        <div><h2 id="bili-title">B 站讲解</h2><p className="bili-marker-title">{marker.title}</p></div>
        <button ref={closeButton} className="icon-button" type="button" onClick={requestClose} title="关闭" aria-label="关闭 B 站讲解"><X size={18} /></button>
      </header>
      {state.isPending ? <p className="bili-feedback" role="status"><LoaderCircle className="spin" size={16} />正在读取考点资料</p>
        : state.isError ? <div className="bili-feedback" role="alert">{state.error.message}
          <button className="text-button" onClick={() => void state.refetch()}><RotateCcw size={15} />重新读取</button></div>
          : <div className="bili-body">
            <section className="bili-section">
              <h3>考点补充信息</h3>
              <blockquote className="bili-original">{marker.source_quote}</blockquote>
              <div className="bili-fields">
                <label className="bili-wide">课程
                  <input value={context.course} maxLength={200} disabled={busy}
                    onChange={event => setContext(current => ({ ...current, course: event.target.value }))} /></label>
                <label className="bili-wide">题目条件或学习需求
                  <textarea rows={3} value={context.notes} maxLength={4000} disabled={busy}
                    onChange={event => setContext(current => ({ ...current, notes: event.target.value }))} /></label>
              </div>
              <div className="bili-actions"><button className="secondary-button compact" disabled={busy || !dirty}
                onClick={() => void perform(save)}><Save size={15} />保存补充信息</button></div>
              <button className="secondary-button" disabled={busy} onClick={() => void perform(async () => {
                if (dirty) await save();
                await started(await planBiliSearch(marker.id, true));
              })}><Search size={16} />{run ? "重新生成搜索词" : "生成搜索词"}</button>
            </section>
            {run && <section className="bili-section">
              <div className="bili-section-heading"><h3>搜索方案</h3>
                {(state.data?.runs.length ?? 0) > 1 && <label>记录
                  <select aria-label="搜索记录" value={run.id} disabled={busy} onChange={event => {
                    setSelectedRun(event.target.value); setSubmitted(null); setSearchConsent(false);
                  }}>{state.data?.runs.map((item, index) => <option key={item.id} value={item.id}>
                    {index === 0 ? "最近" : `历史 ${index}`} · {STATUSES[item.status] ?? item.status}{item.stale ? " · 已过期" : ""}
                  </option>)}</select></label>}
              </div>
              {run.stale && <p className="bili-warning" role="status">考点、字幕或补充信息已变化，以下为历史结果。请重新生成搜索词。</p>}
              {run.legacy && <p className="bili-warning">旧版核验记录，请重新生成目标后使用新版检索。</p>}
              {target && <fieldset className="bili-target" disabled={busy || run.status !== "ready"}>
                <legend>学习目标</legend>
                {target.requirements.map((requirement, index) => <div className="bili-target-row" key={requirement.id}>
                  <label>要求 {index + 1}<input aria-label={`目标要求 ${index + 1}`} value={requirement.text} maxLength={500}
                    onChange={event => { setSearchConsent(false); setTarget({ ...target, requirements: target.requirements.map((item, i) =>
                      i === index ? { ...item, text: event.target.value, sources: ["user.confirmed"] } : item) }); }} /></label>
                  <label>深度<select aria-label={`讲解深度 ${index + 1}`} value={requirement.depth}
                    onChange={event => { setSearchConsent(false); setTarget({ ...target, requirements: target.requirements.map((item, i) =>
                      i === index ? { ...item, depth: event.target.value as typeof item.depth, sources: ["user.confirmed"] } : item) }); }}>
                    <option value="concept">概念解释</option><option value="method">方法讲解</option><option value="example">例题应用</option>
                  </select></label><span>{requirement.required ? "核心要求" : "补充要求"}</span>
                </div>)}
                {!!target.unknowns.length && <p className="bili-muted">待明确：{target.unknowns.join("；")}</p>}
              </fieldset>}
              <h4>精确知识点搜索</h4>
              <div className="bili-queries">{queries.map((query, index) => <label key={index}>
                <span>搜索词 {index + 1}</span><input aria-label={`搜索词 ${index + 1}`} value={query.text} maxLength={160} disabled={busy || run.status !== "ready"}
                  onChange={event => { setSearchConsent(false); setQueries(current => current.map((item, position) => position === index ? { ...item, text: event.target.value } : item)); }} />
                <small>{query.purpose}</small>
              </label>)}</div>
              {!!courseQueries.length && <><h4>未命中时：课程类别搜索</h4>
                <div className="bili-queries">{courseQueries.map((query, index) => <label key={index}>
                  <span>课程搜索词 {index + 1}</span><input aria-label={`课程搜索词 ${index + 1}`} value={query.text} maxLength={160}
                    disabled={busy || run.status !== "ready"} onChange={event => { setSearchConsent(false);
                      setCourseQueries(current => current.map((item, i) => i === index ? { ...item, text: event.target.value } : item)); }} />
                  <small>{query.purpose}</small></label>)}</div></>}
              {run.status === "awaiting_approval" && <div className="bili-queries"><h4>补充课程搜索</h4><p>{run.rewrite_reason}</p>
                {pendingQueries.map((query, index) => <label key={index}>补充搜索词 {index + 1}
                  <input value={query.text} maxLength={160} disabled={busy} onChange={event => { setSearchConsent(false);
                    setPendingQueries(current => current.map((item, i) => i === index ? { ...item, text: event.target.value } : item)); }} /></label>)}
              </div>}
              {queries.length > 0 && <>
                <label className="bili-consent"><input type="checkbox" checked={searchConsent} disabled={busy}
                  onChange={event => setSearchConsent(event.target.checked)} />
                  同意将以上搜索词发送到 B 站，并将课堂证据、补充信息及候选字幕发送至当前 AI 服务核验。</label>
                {run.status === "ready" && <><label className="bili-consent"><input type="checkbox" checked={autoRewrite} disabled={busy}
                  onChange={event => { setAutoRewrite(event.target.checked); setSearchConsent(false); }} />允许在本目标内自动补充课程搜索词</label>
                  <button className="primary-button" disabled={busy || dirty || run.stale || run.legacy || !target || !searchConsent
                    || Boolean(account.data?.pause) || [...queries, ...courseQueries].some(query => !query.text.trim())
                    || target.requirements.some(item => !item.text.trim())}
                    onClick={() => void perform(async () => started(await startBiliSearch(marker.id, run.id, queries, searchConsent,
                      { target, course_queries: courseQueries, auto_rewrite: autoRewrite })))}><Search size={16} />搜索并核验</button></>}
                {run.status === "budget_exhausted" && !run.budget_extended && <label className="bili-consent">
                  <input type="checkbox" checked={increaseBudget} disabled={busy} onChange={event => setIncreaseBudget(event.target.checked)} />
                  追加一份初始预算，继续本任务（最多一次）</label>}
                {["failed", "interrupted", "awaiting_approval", "budget_exhausted"].includes(run.status) && <button className="primary-button"
                  disabled={busy || dirty || run.stale || run.legacy || !searchConsent || Boolean(account.data?.pause)
                    || (run.status === "awaiting_approval" && (!pendingQueries.length || pendingQueries.some(q => !q.text.trim())))
                    || (run.status === "budget_exhausted" && (!increaseBudget || run.budget_extended))}
                  onClick={() => void perform(async () => started(await continueBiliSearch(marker.id, run.id,
                    run.status === "awaiting_approval" ? pendingQueries : undefined, increaseBudget)))}>
                  <RotateCcw size={16} />{run.status === "awaiting_approval" ? "确认并补搜" : "继续搜索与核验"}</button>}
              </>}
              {run.phase && <p className="bili-muted">当前阶段：{run.phase === "exact" ? "精确知识点检索" : "课程类别检索"}</p>}
              {run.stop_reason && <p role="status">{STOP[run.stop_reason] ?? run.stop_reason}</p>}
              {run.budget && run.limits && <p className="bili-muted">剩余：平台请求 {Math.max(0, run.limits.platform - run.budget.platform)} · AI 请求 {Math.max(0, run.limits.llm - run.budget.llm)} · 核验单元 {Math.max(0, run.limits.verified - run.budget.verified)}</p>}
              {!!run.trace?.length && <details><summary>检索过程</summary><ol className="bili-trace">{run.trace.filter(e => ["search", "fallback", "directory", "verified", "stop"].includes(e.action)).map(e =>
                <li key={e.seq}>{({ search: "搜索", fallback: "转入课程检索", directory: "读取课程目录", verified: "完成字幕核验", stop: "停止" } as Record<string, string>)[e.action]}{e.reason ? `：${STOP[e.reason] ?? e.reason}` : ""}</li>)}</ol></details>}
              {taskBusy && <div className="bili-job" role="status">
                <div><LoaderCircle className="spin" size={16} /><span>{task.data?.message || "正在处理"}</span>
                  {(submitted?.job_id ?? run.job_id) && <button className="text-button" disabled={pending}
                    onClick={() => void perform(async () => {
                      await cancelJob(submitted?.job_id ?? run.job_id!);
                      await task.refetch(); await state.refetch();
                    })}><Square size={13} />停止</button>}</div>
                <progress value={progress} max={100} aria-label="B 站检索进度" />
              </div>}
              {task.isError && <p className="field-error" role="alert">任务状态读取失败
                <button className="text-button" onClick={() => void task.refetch()}><RotateCcw size={14} />重试读取</button></p>}
              {run.error && <p className="field-error" role="alert">{run.error}</p>}
            </section>}
            {run && run.candidates.length > 0 && <section className="bili-section">
              <h3>字幕核验结果</h3>
              <p className="bili-muted">以下判断基于已取得的字幕，不代表已确认画面中的题目。</p>
              {run.directories?.filter(d => !d.complete).map(d => <p key={d.id} className="bili-warning">{d.title}：目录未读完，已取得 {d.episodes.length}/{d.total} 个条目。</p>)}
              {recommendations.map(item => <Candidate key={item.bvid} item={item} recommended />)}
              {others.length > 0 && <details className="bili-other" open={recommendations.length === 0}>
                <summary>其他候选 · {others.length}</summary>
                {others.map(item => <Candidate key={item.bvid} item={item} />)}
              </details>}
            </section>}
            {run?.status === "completed" && !run.candidates.length && <p className="bili-feedback">未找到候选视频，可调整搜索词后重试。</p>}
            <section className="bili-section">
              <div className="bili-account">
                <h3><ShieldCheck size={15} />B 站账号</h3>
                {account.isError && <p className="field-error" role="alert">{account.error.message}
                  <button className="text-button" onClick={() => void account.refetch()}>重试读取</button></p>}
                <BilibiliLogin configured={Boolean(account.data?.configured)} disabled={taskBusy} onActiveChange={setLoginActive} />
              </div>
              {account.data?.pause && <div className="bili-warning" role="alert">
                <p>B 站读取已因登录或风控提示暂停（{account.data.pause.reason}）。</p>
                <label className="bili-consent"><input type="checkbox" checked={riskAcknowledged} disabled={busy}
                  onChange={event => setRiskAcknowledged(event.target.checked)} />我已检查账号及平台提示，确认恢复读取。</label>
                <button className="secondary-button compact" disabled={busy || !riskAcknowledged} onClick={() => void perform(async () => {
                  await resumeBiliReading(riskAcknowledged); setRiskAcknowledged(false); await account.refetch();
                })}><RotateCcw size={15} />恢复读取</button>
              </div>}
            </section>
          </div>}
      {error && <p className="bili-feedback field-error" role="alert"><AlertCircle size={16} />{error}</p>}
    </section>
  </div>;
}

function Candidate({ item, recommended = false }: { item: BiliCandidate; recommended?: boolean }) {
  return <article className="bili-candidate">
    <div className="bili-candidate-heading"><a href={item.url} target="_blank" rel="noreferrer noopener">{item.title}<ExternalLink size={14} /></a>
      <span className="bili-muted">{item.author}</span></div>
    {item.collection_title && <p className="bili-muted">所属课程：{item.collection_title}</p>}
    {item.selection_reason && <p className="bili-muted">取证理由：{item.selection_reason}</p>}
    {item.directory_unread && <p className="bili-muted">关联合集尚未展开。</p>}
    {!recommended && !item.units.length && <p className="bili-muted">{item.error || STATUSES[item.status] || "未核验"}</p>}
    {item.units.map(part => <Unit key={part.id} unit={part} />)}
    {!!item.unchecked_pages && <p className="bili-muted">另有 {item.unchecked_pages} 个分 P 未核验。</p>}
  </article>;
}

function Unit({ unit }: { unit: BiliUnit }) {
  return <div className="bili-unit">
    <div className="bili-unit-heading"><a href={unit.url} target="_blank" rel="noreferrer noopener">{unit.multipart === false ? "" : `P${unit.page} · `}{unit.part || "视频讲解"}</a>
      <strong className={unit.verdict === "covers" ? "bili-positive" : ""}>{unit.verdict ? VERDICTS[unit.verdict] : STATUSES[unit.status] ?? "未核验"}</strong></div>
    {unit.source?.language && <p className="bili-muted">{unit.source.language} · {unit.source.automatic ? "自动字幕" : "人工字幕轨"}</p>}
    {unit.reason && <p>{unit.reason}</p>}
    {!!unit.assessments?.length && <ul>{unit.assessments.map(item => <li key={item.requirement_id}>{item.reason}</li>)}</ul>}
    {unit.error && <p className="field-error">{unit.error}</p>}
    {unit.coverage?.partial && <p className="bili-warning">仅核验了预算内的部分字幕（{unit.coverage.selected_segments}/{unit.coverage.total_segments} 条）。</p>}
    {!!unit.evidence?.length && <details><summary>字幕依据 · {unit.evidence.length}</summary>
      {unit.evidence.map(evidence => <blockquote key={evidence.id}>
        <a href={evidence.url} target="_blank" rel="noreferrer noopener">{formatTime(evidence.start_ms)}<ExternalLink size={12} /></a>
        <span>{evidence.text}</span>
      </blockquote>)}</details>}
  </div>;
}
