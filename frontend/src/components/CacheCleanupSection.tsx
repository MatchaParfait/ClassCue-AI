import { useEffect, useId, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Clock3, LoaderCircle, RefreshCw, Save, Square, Trash2 } from "lucide-react";
import { cancelJob, getJobStatus } from "../api/client";
import {
  automaticCategories, getCleanupStatus, previewCleanup, saveCleanupPreferences, startCleanup,
  type CacheCategory, type CleanupMode, type CleanupPreferences,
} from "../api/cacheCleanup";
import { ModelCleanupProgress } from "./ModelCleanupProgress";
import { useUnsavedDraft } from "./UnsavedChanges";
import "./CacheCleanupSection.css";

const explanations: Record<CacheCategory, string> = {
  transcription: "仅清理字幕已保存的任务；失败和待续跑的转写缓存保留。",
  previews: "可从原视频重新生成，不包含 PPT 和题图。",
  logs: "清理过期的历史日志，保留当前进程日志。",
  updates: "保留当前已下载更新和正在使用的安装文件。",
  downloads: "手动清理依赖安装包；下次准备环境可能重新下载。",
  ai: "只清理重复分析用的分块缓存，保留已保存的考点和题目；再次分析可能产生费用。",
  backups: "仅清理有完成记录的旧备份，保留最近两份完整备份；未知及失败备份保留。",
};
function bytes(value: number) {
  return value >= 1024 ** 3 ? `${(value / 1024 ** 3).toFixed(2)} GB`
    : value >= 1024 ** 2 ? `${(value / 1024 ** 2).toFixed(1)} MB`
      : value >= 1024 ? `${(value / 1024).toFixed(1)} KB` : `${value} B`;
}
function errorText(error: unknown) {
  return error instanceof Error ? error.message : "操作未完成，请重试。";
}

export function CacheCleanupSection({ active, onJob }: { active: boolean; onJob?: (id: string | number) => void }) {
  const client = useQueryClient();
  const draftId = useId();
  const [draft, setDraft] = useState<CleanupPreferences | null>(null);
  const [selected, setSelected] = useState<CacheCategory[]>(automaticCategories);
  const [mode, setMode] = useState<CleanupMode>("expired");
  const [jobId, setJobId] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const handled = useRef("");
  const job = useQuery({ queryKey: ["job", jobId], queryFn: () => getJobStatus(jobId!),
    enabled: active && Boolean(jobId), refetchInterval: query =>
      active && ["queued", "running"].includes(query.state.data?.status ?? "queued") ? 750 : false });
  const running = Boolean(jobId && (!job.data || ["queued", "running"].includes(job.data.status)));
  const status = useQuery({ queryKey: ["cache-cleanup"], queryFn: getCleanupStatus,
    enabled: active, staleTime: 30_000, refetchInterval: query =>
      active && (running || query.state.data?.busy) ? 3000 : false });
  const inventory = useQuery({ queryKey: ["cache-cleanup-inventory", mode], queryFn: () => previewCleanup(mode),
    enabled: false, staleTime: 0 });
  const busy = running || Boolean(status.data?.busy);
  const preferences = draft ?? status.data?.preferences;
  const save = useMutation({ mutationFn: saveCleanupPreferences,
    onSuccess: value => { client.setQueryData(["cache-cleanup"], value); setDraft(null); setNotice("自动清理设置已保存。"); setError(""); },
    onError: value => setError(errorText(value)) });
  useUnsavedDraft({ id: draftId, label: "缓存自动清理", dirty: Boolean(draft), busy: save.isPending,
    save: async () => { if (draft) await save.mutateAsync(draft); return true; }, discard: () => setDraft(null) });
  const start = useMutation({ mutationFn: () => startCleanup(selected, mode),
    onSuccess: value => { setJobId(value.job_id); setNotice(""); setError(""); onJob?.(value.job_id); },
    onError: value => setError(errorText(value)) });
  const stop = useMutation({ mutationFn: () => cancelJob(jobId!), onError: value => setError(errorText(value)) });
  useEffect(() => {
    if (!jobId || !job.data || ["queued", "running"].includes(job.data.status) || handled.current === jobId) return;
    handled.current = jobId;
    void client.invalidateQueries({ queryKey: ["cache-cleanup"] });
    void inventory.refetch();
    if (job.data.status === "failed") setError(job.data.error_message || "缓存清理未完成，请重试。");
  }, [jobId, job.data, client, inventory.refetch]);
  const change = (value: Partial<CleanupPreferences>) => {
    if (preferences) { setDraft({ ...preferences, ...value }); setNotice(""); }
  };
  const eligible = inventory.data?.categories.filter(item => selected.includes(item.id))
    .reduce((total, item) => total + item.eligible_bytes, 0) ?? 0;
  return <section className="settings-section cache-cleanup-section" aria-labelledby="cache-cleanup-title">
    <div className="settings-section-heading">
      <div className="settings-section-title"><Trash2 size={18} /><h2 id="cache-cleanup-title">缓存清理</h2></div>
      <button type="button" className="secondary-button compact" disabled={!active || inventory.isFetching || busy}
        onClick={() => { setError(""); void inventory.refetch(); }}>
        {inventory.isFetching ? <LoaderCircle className="spin" size={15} /> : <RefreshCw size={15} />}检查占用
      </button>
    </div>
    <p className="settings-hint">字幕保存成功后自动清理临时音频。原视频、字幕、笔记、PPT、题图和删除恢复文件保留。</p>
    {status.isLoading && <p role="status" className="settings-loading">正在读取清理设置…</p>}
    {status.isError && <p role="alert" className="field-error">清理设置读取失败。
      <button type="button" className="text-button" onClick={() => void status.refetch()}>重试</button></p>}
    {preferences && <fieldset className="cache-cleanup-preferences" disabled={save.isPending || busy}>
      <label className="cache-cleanup-toggle"><input type="checkbox" checked={preferences.enabled}
        onChange={event => change({ enabled: event.target.checked })} /><span>空闲时自动清理</span></label>
      <div className="settings-form-grid">
        <label className="settings-field"><span>执行频率</span><select aria-label="自动清理频率" value={preferences.interval_days}
          onChange={event => change({ interval_days: Number(event.target.value) })}>
          <option value={1}>每天</option><option value={7}>每周</option><option value={30}>每 30 天</option>
        </select></label>
        <label className="settings-field"><span>缓存保留天数</span><input aria-label="缓存保留天数" type="number" min={1} max={365}
          value={preferences.retention_days} onChange={event => change({ retention_days: Number(event.target.value) })} /></label>
        <label className="settings-field"><span>预览图片上限（MB）</span><input aria-label="预览图片容量上限" type="number" min={64} max={10240} step={64}
          value={preferences.preview_limit_mb} onChange={event => change({ preview_limit_mb: Number(event.target.value) })} /></label>
      </div>
      <div className="cache-cleanup-auto-categories" role="group" aria-label="自动清理项目">
        {automaticCategories.map(key => <label key={key}><input type="checkbox" checked={preferences.categories.includes(key)}
          onChange={event => change({ categories: event.target.checked ? [...preferences.categories, key]
            : preferences.categories.filter(item => item !== key) })} />
          {{ transcription: "已完成转写", previews: "封面与预览", logs: "历史日志", updates: "旧更新文件" }[key]}</label>)}
      </div>
      <button className="secondary-button compact" type="button" disabled={!draft || save.isPending}
        onClick={() => draft && save.mutate(draft)}>
        {save.isPending ? <LoaderCircle className="spin" size={15} /> : <Save size={15} />}保存清理设置
      </button>
    </fieldset>}
    <p className="settings-hint cache-cleanup-schedule"><Clock3 size={14} />软件运行期间检查，到期后等待空闲再执行；暂停任务存在时延后。</p>
    <div className="cache-cleanup-manual-heading"><h3>手动清理</h3>
      <label className="settings-field"><span>清理范围</span><select aria-label="手动清理范围" disabled={busy}
        value={mode} onChange={event => setMode(event.target.value as CleanupMode)}>
        <option value="expired">过期缓存</option><option value="all">全部可清理缓存</option>
      </select></label>
    </div>
    {inventory.isError && <p role="alert" className="field-error">占用检查失败，请重试检查。</p>}
    {!inventory.data && !inventory.isFetching && <p className="settings-hint">尚未统计缓存占用。</p>}
    {inventory.data && <div className="cache-cleanup-inventory" role="group" aria-label="选择清理内容">
      <div className="cache-cleanup-table-heading"><span>内容</span><span>占用</span><span>可清理</span></div>
      {inventory.data.categories.map(item => <label key={item.id} className="cache-cleanup-item">
        <span className="cache-cleanup-item-name"><input type="checkbox" disabled={busy || start.isPending}
          checked={selected.includes(item.id)} aria-label={`清理${item.label}`}
          onChange={event => setSelected(event.target.checked ? [...selected, item.id] : selected.filter(key => key !== item.id))} />
          <span><strong>{item.label}</strong><small>{explanations[item.id]}</small></span></span>
        <span className="cache-cleanup-size">{bytes(item.bytes)}</span><span className="cache-cleanup-size">{bytes(item.eligible_bytes)}</span>
      </label>)}
    </div>}
    {selected.includes("ai") && <p className="field-error">清理 AI 缓存后，重复分析可能重新请求 AI 服务并产生费用。数据库空间将由后续写入复用。</p>}
    {selected.includes("backups") && <p className="field-error">较旧备份中的程序和数据库副本将删除；最近两份完整备份、当前恢复所需备份及未知或失败备份保留。</p>}
    {selected.includes("downloads") && <p className="settings-hint">依赖下载缓存仅支持整库手动清理，不按过期时间筛选。模型权重与已安装环境保留。</p>}
    <div className="settings-action-row">
      <button className="primary-button" type="button" disabled={busy || start.isPending || !inventory.data || !selected.length || eligible === 0}
        onClick={() => start.mutate()}>{start.isPending ? <LoaderCircle className="spin" size={16} /> : <Trash2 size={16} />}
        清理所选{inventory.data ? `（${bytes(eligible)}）` : ""}
      </button>
      {running && <button className="secondary-button" type="button" disabled={stop.isPending} onClick={() => stop.mutate()}>
        <Square size={14} />停止清理</button>}
      <Link className="text-button" to="/settings?section=asr">管理模型与未完成下载</Link>
    </div>
    {busy && !running && <p role="status" className="settings-hint">有运行或暂停中的任务，暂不可清理。</p>}
    {jobId && <ModelCleanupProgress job={job.data} connectionError={job.isError} label="缓存清理进度" />}
    {status.data?.last_run && <p className="cache-cleanup-last" role="status">
      上次清理：{new Date(status.data.last_run.finished_at * 1000).toLocaleString()} · 释放 {bytes(status.data.last_run.released_bytes)}
      {" · "}跳过 {status.data.last_run.skipped} 个文件
      {status.data.last_run.warnings.map(value => <span key={value} className="field-error">{value}</span>)}
    </p>}
    {notice && <p role="status">{notice}</p>}
    {error && <p role="alert" className="field-error">{error}</p>}
  </section>;
}
