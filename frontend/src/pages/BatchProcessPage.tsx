import { CharacterEye } from "../components/CharacterAccent";
import { WarmAtmosphere } from "../components/WarmAtmosphere";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  CheckCircle2,
  FolderOpen,
  LoaderCircle,
  Plus,
  ArrowLeft,
  Ban,
  Sparkles,
  X,
  RotateCcw,
  SkipForward,
  PauseCircle,
  Settings2,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { cancelJob, getJob, openPicker, pollPicker, processBatch, resumeJob, getPausedJobs, getAsrSelection, getAiSettings, previewBatch, getLatestBatch, retryBatch } from "../api/client";
import type { Job } from "../types";
import { buildProcessPayload, RECALL_SELECTION_REQUIRED, type ProcessForm } from "../processOptions";
import { QwenResourceControls } from "../components/QwenResourceControls";
import { FireRedResourceProfile, FireRedResourceStatus } from "../components/FireRedResourceStatus";
import { hasActivePreparation, ModelPreparationProgress } from "../components/ModelPreparationProgress";
import { RecallModeControl } from "../components/RecallModeControl";
import { useAsrStatus } from "../hooks/useAsrStatus";
import { useTaskStatus } from "../hooks/useTaskStatus";
import { useRecallSelection } from "../hooks/useRecallSelection";

const EMPTY_FORM: ProcessForm = {
  displayName: "",
  recallModes: ["medium_recall"],
  keywords: "",
  filterFormula: "",
  filterLowInformation: false,
  forceRetranscribe: false,
};

function localFileHref(root: string, relative: string) {
  const path = `${root.replace(/[\\/]+$/, "")}\\${relative}`.replaceAll("\\", "/");
  return `file:///${encodeURI(path.replace(/^\/+/, ""))}`;
}

export function BatchProcessPage({ embedded = false, active = true, onClose, onJob }: {
  embedded?: boolean; active?: boolean; onClose?: () => void; onJob?: (id: string | number) => void;
}) {
  const queryClient = useQueryClient();
  const [folders, setFolders] = useState<string[]>([""]);
  const [formOptions, setForm] = useState<ProcessForm>(EMPTY_FORM);
  const [recallModes, setRecallModes] = useRecallSelection();
  const form = { ...formOptions, recallModes };
  const [pickerIndex, setPickerIndex] = useState<number | null>(null);
  const [jobId, setJobId] = useState<string | number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [stopping, setStopping] = useState(false);
  const pausedJobs = useQuery({ queryKey: ["paused-jobs"], queryFn: getPausedJobs, enabled: active, staleTime: 15_000 });
  const latest = useQuery({ queryKey: ["batch-latest"], queryFn: getLatestBatch, enabled: active, staleTime: 15_000 });
  const selection = useQuery({ queryKey: ["settings", "asr", "selection"], queryFn: getAsrSelection, enabled: active, staleTime: 30_000 });
  const ai = useQuery({ queryKey: ["settings", "ai"], queryFn: getAiSettings, enabled: active, staleTime: 30_000 });
  const asrStatus = useAsrStatus(active ? selection.data : undefined);
  useEffect(() => {
    if (jobId !== null) return;
    if (!latest.isSuccess) return;
    const previous = latest.data ?? pausedJobs.data?.find((item) => item.job_type === "batch_video_process");
    if (previous) setJobId(previous.job_id ?? previous.id);
  }, [pausedJobs.data, latest.data, latest.isSuccess, jobId]);

  const job = useTaskStatus(jobId, active);
  const terminalSeen = useRef("");

  useEffect(() => {
    const current = job.data;
    if (!current || !isTerminal(current)) return;
    const identity = `${current.id}:${current.status}`;
    if (terminalSeen.current === identity) return;
    terminalSeen.current = identity;
    setStopping(false);
    void queryClient.invalidateQueries({ queryKey: ["videos"] });
    void queryClient.invalidateQueries({ queryKey: ["library"] });
    void queryClient.invalidateQueries({ queryKey: ["batch-latest"] });
    if (current.status === "completed") setNotice(current.message || "批处理结束");
    if (current.status === "awaiting_api") setNotice(null);
  }, [job.data, queryClient]);

  const mutation = useMutation({
    mutationFn: (importOnly: boolean) => {
      const selected = folders.map((folder) => folder.trim()).filter(Boolean);
      if (!selected.length) throw new Error("请至少选择一个视频文件夹");
      return processBatch(selected, {
        ...buildProcessPayload(form), asr_model_id: selection.data?.id, import_only: importOnly,
      });
    },
    onSuccess: (result, importOnly) => {
      setJobId(result.job_id);
      onJob?.(result.job_id);
      setFolders([""]);
      setError(result.errors?.length ? result.errors.join("；") : null);
      setNotice(importOnly
        ? `已导入 ${result.total ?? result.video_ids?.length ?? 0} 个视频，尚未开始处理`
        : `已加入批处理队列，共 ${result.total ?? result.video_ids?.length ?? 0} 个视频`);
      void queryClient.invalidateQueries({ queryKey: ["batch-latest"] });
    },
    onError: (reason) => setError(reason instanceof Error ? reason.message : String(reason)),
  });
  const resumeMutation = useMutation({
    mutationFn: (skip: boolean) => resumeJob(jobId!, skip),
    onSuccess: (result) => {
      setJobId(result.job_id ?? result.id);
      onJob?.(result.job_id ?? result.id);
      setStopping(false);
      setError(null);
    },
    onError: (reason) => setError(reason instanceof Error ? reason.message : String(reason)),
  });

  const retryMutation = useMutation({
    mutationFn: () => retryBatch(jobId!),
    onSuccess: (result) => {
      const id = result.job_id ?? result.id;
      queryClient.setQueryData(["jobs", id], result);
      setJobId(id);
      onJob?.(id);
      setStopping(false);
      setError(null);
      setNotice("已提交未完成视频，已有字幕将直接复用");
      void queryClient.invalidateQueries({ queryKey: ["batch-latest"] });
    },
    onError: (reason) => setError(reason instanceof Error ? reason.message : String(reason)),
  });

  const isBusy = mutation.isPending || resumeMutation.isPending || retryMutation.isPending || Boolean(job.data && !isTerminal(job.data));
  const validFolders = useMemo(() => folders.map((folder) => folder.trim()).filter(Boolean), [folders]);
  const [checkedFolders, setCheckedFolders] = useState<string[]>([]);
  useEffect(() => {
    const timeout = window.setTimeout(() => setCheckedFolders(validFolders), 350);
    return () => window.clearTimeout(timeout);
  }, [validFolders]);
  const preview = useQuery({
    queryKey: ["batch-preview", checkedFolders],
    queryFn: () => previewBatch(checkedFolders),
    enabled: active && checkedFolders.length > 0 && !isBusy,
    retry: false,
    refetchOnMount: "always",
    refetchOnWindowFocus: true,
  });
  const previewCurrent = JSON.stringify(validFolders) === JSON.stringify(checkedFolders);
  const canImport = validFolders.length > 0 && previewCurrent && preview.isSuccess && !preview.isFetching && preview.data.valid;

  const chooseFolder = async (index: number) => {
    setPickerIndex(index);
    setError(null);
    try {
      const started = await openPicker("directory");
      const path = await pollPicker(String(started.picker_id ?? started.id ?? ""));
      if (path) {
        setFolders((current) => current.map((value, currentIndex) => currentIndex === index ? path : value));
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setPickerIndex(null);
    }
  };

  const start = (importOnly = false) => {
    setError(null);
    if (!canImport) {
      setError("请先选择通过检查的视频文件夹");
      return;
    }
    mutation.mutate(importOnly);
  };

  const currentJob = job.data;
  const summary = currentJob?.batch_summary;
  const retryableCount = summary
    ? (summary.retryable ?? summary.failed + summary.awaiting_ai + summary.pending_asr)
    : 0;
  const retryable = currentJob && isTerminal(currentJob) && currentJob.status !== "resource_paused"
    && !currentJob.import_only
    && (summary ? retryableCount > 0
      : ["failed", "interrupted"].includes(currentJob.status));
  useEffect(() => {
    if (!notice || !active) return;
    const timer = window.setTimeout(() => setNotice(null), 4000);
    return () => window.clearTimeout(timer);
  }, [notice, active]);
  return (
    <main className={`process-page batch-process-page has-warm-atmosphere${embedded ? " embedded-page" : ""}`}>
      <WarmAtmosphere active={active} />
      <div className="process-inner">
        <div className="process-header">
          <div>
            <p className="eyebrow">批量处理</p>
            <h1 tabIndex={-1}>导入文件夹</h1>
            <p>每个课程目录分别归档，已有字幕在重试时保留。</p>
          </div>
          {embedded ? <button className="secondary-button panel-return" onClick={onClose}>返回视频库</button>
            : <Link className="secondary-button" to="/"><ArrowLeft size={16} /> 返回视频库</Link>}
        </div>

        <section className="process-section process-video-section">
          <div className="section-heading compact">
            <div><p className="eyebrow">步骤 1</p><h2><CharacterEye heading />选择视频文件夹</h2></div>
            <FolderOpen size={20} />
          </div>
          <div className="batch-folder-list">
            {folders.map((folder, index) => (
              <div className="batch-folder-row" key={index}>
                <input
                  aria-label={`视频文件夹 ${index + 1}`}
                  value={folder}
                  disabled={isBusy}
                  onChange={(event) => setFolders((current) => current.map((value, currentIndex) => currentIndex === index ? event.target.value : value))}
                  placeholder="例如：D:\\课程\\第一章"
                />
                <button className="secondary-button" type="button" disabled={isBusy || pickerIndex !== null} onClick={() => void chooseFolder(index)}>
                  {pickerIndex === index ? <LoaderCircle className="spin" size={15} /> : <FolderOpen size={15} />} 选择文件夹
                </button>
                {folders.length > 1 && <button className="icon-button" type="button" disabled={isBusy} aria-label="移除文件夹" onClick={() => setFolders((current) => current.filter((_value, currentIndex) => currentIndex !== index))}><X size={16} /></button>}
              </div>
            ))}
          </div>
          <button className="secondary-button compact" type="button" disabled={isBusy} onClick={() => setFolders((current) => [...current, ""])}><Plus size={15} /> 添加另一个文件夹</button>
          <p className="field-hint">支持包含子文件夹的目录，导入后会在视频库保留相同层级（含空子文件夹）。任意层包含非视频文件都会拦截本次导入。是否保留原文件取决于设置中的“导入后删除原文件”；重复视频保留来源副本。</p>
          {validFolders.length > 0 && (!previewCurrent || preview.isFetching) && <p className="field-hint" role="status"><LoaderCircle className="spin" size={14} /> 正在检查文件夹…</p>}
          {previewCurrent && preview.isError && <p className="field-error" role="alert"><AlertCircle size={14} />{preview.error.message}<button className="icon-button" type="button" title="重新检查文件夹" aria-label="重新检查文件夹" onClick={() => void preview.refetch()}><RotateCcw size={14} /></button></p>}
          {previewCurrent && preview.data && validFolders.length > 0 && <div className="batch-directory-checks">
            {preview.data.folders.map((folder) => <div className={`batch-directory-check ${folder.valid ? "is-valid" : "is-invalid"}`} key={folder.path} role={folder.valid ? "status" : "alert"}>
              {folder.valid ? <CheckCircle2 size={16} /> : <AlertCircle size={16} />}
              <div><strong>{folder.path}</strong><span>{folder.video_count} 个视频 · {folder.non_video_count} 个非视频文件 · {folder.subfolder_count} 个子文件夹</span>
                {folder.error && <p>{folder.error}</p>}
                {folder.examples.length > 0 && <small className="batch-directory-examples">
                  {folder.examples.length < 5
                    ? folder.examples.map((example) => <a key={example} href={localFileHref(folder.path, example)} target="_blank" rel="noreferrer" title={`打开问题文件：${example}`}>{example}</a>)
                    : folder.examples.join("、")}
                </small>}
                {!folder.valid && <button className="text-button batch-directory-recheck" type="button" disabled={preview.isFetching}
                  onClick={() => void preview.refetch()}><RotateCcw size={14} />重新检查</button>}
              </div>
            </div>)}
          </div>}
        </section>

        <section className="process-section process-options-section">
          <div className="section-heading compact">
            <div><p className="eyebrow">步骤 2</p><h2><CharacterEye heading />处理选项</h2></div>
            <Sparkles size={20} />
          </div>
          <div className="readonly-config-grid">
          <div className="readonly-config">
            <span>当前 ASR 模型</span><strong>{selection.data?.name || "正在读取配置"}</strong>
            {asrStatus.isFetching && <small role="status">检查中</small>}
            {asrStatus.isError && <small role="status">状态检查失败</small>}
            <Link to="/settings?section=asr"><Settings2 size={14} /> 修改</Link>
          </div>
          <div className="readonly-config">
            <span>当前 AI 服务</span><strong>{ai.isLoading ? "正在读取配置" : ai.data?.model || "未配置"}</strong>
            {ai.isError && <small role="status">配置读取失败</small>}
            <Link to="/settings?section=ai"><Settings2 size={14} /> 修改</Link>
          </div>
          </div>
          {selection.data?.engine === "firered" ? <FireRedResourceProfile modelId={selection.data.id} active={active} />
            : selection.data?.engine === "qwen" ? <QwenResourceControls form={form} onChange={setForm} disabled={isBusy} /> : null}
          <div className="option-grid">
            <RecallModeControl value={form.recallModes} active={active} disabled={isBusy} onChange={(recallModes) => {
              setRecallModes(recallModes);
              setForm((current) => ({ ...current, recallModes }));
              setError((current) => current === RECALL_SELECTION_REQUIRED ? null : current);
            }} />
          </div>
          <div className="process-primary-actions">
          {currentJob?.status === "resource_paused" && <div className="process-actions">
            <button className="primary-button" disabled={isBusy} onClick={() => resumeMutation.mutate(false)}><RotateCcw size={16} />继续队列</button>
            <button className="secondary-button" disabled={isBusy} onClick={() => resumeMutation.mutate(true)}><SkipForward size={16} />跳过当前文件</button>
          </div>}
          {retryable && <div className="process-actions">
            <button className="primary-button" type="button" disabled={isBusy} onClick={() => retryMutation.mutate()}><RotateCcw size={16} />{retryMutation.isPending ? "正在提交…" : "重试未完成视频"}</button>
            {currentJob?.status === "awaiting_api" && <Link className="secondary-button" to="/settings?section=ai"><Settings2 size={15} /> AI 服务设置</Link>}
          </div>}
          <div className="process-actions batch-import-actions">
            <button className={retryable ? "secondary-button" : "primary-button"} type="button" onClick={() => start(false)} disabled={isBusy || !canImport}>
              <Sparkles size={16} /> {mutation.isPending ? "正在排队…" : isBusy ? "批处理中…" : currentJob ? "导入并开始新批次" : "导入并开始批处理"}
            </button>
            <button className="secondary-button" type="button" title="只导入到视频库，不启动 ASR 或 AI" onClick={() => start(true)} disabled={isBusy || !canImport}>
              <FolderOpen size={16} /> 仅导入
            </button>
          </div>
          {latest.isError && <p className="field-error">无法恢复最近批次：{latest.error.message}</p>}
          {error && <p className="field-error"><AlertCircle size={14} />{error}</p>}
          </div>
        </section>

        <section className="process-section process-status-section">
          <div className="section-heading compact"><div><h2><CharacterEye heading />队列状态</h2></div><span className="count-pill">{validFolders.length || currentJob?.folders?.length || 0} 个文件夹</span></div>
          {currentJob ? <BatchJobStatus job={currentJob} onCancel={() => {
            setStopping(true);
            void cancelJob(currentJob.job_id ?? currentJob.id).then((result) => queryClient.setQueryData(["jobs", jobId], result)).catch((reason) => { setStopping(false); setError(reason instanceof Error ? reason.message : String(reason)); });
          }} stopping={stopping} /> : <div className="job-empty"><FolderOpen size={18} /><span>暂无批处理任务</span></div>}
        </section>
      </div>
      {notice && <div className="toast" role="status"><CheckCircle2 size={17} /><span>{notice}</span><button type="button" onClick={() => setNotice(null)} aria-label="关闭提示"><X size={15} /></button></div>}
    </main>
  );
}

function BatchJobStatus({ job, onCancel, stopping }: { job: Job; onCancel: () => void; stopping: boolean }) {
  const progress = Math.max(0, Math.min(100, Number(job.progress ?? 0)));
  return (
    <div className={`job-status job-${job.status}`}>
      <div className="job-status-heading"><div className="job-status-title">{job.status === "completed" ? <CheckCircle2 size={20} /> : ["resource_paused", "awaiting_api", "interrupted"].includes(job.status) ? <PauseCircle size={20} /> : job.status === "failed" ? <AlertCircle size={20} /> : <LoaderCircle className="spin" size={20} />}<strong>{jobStatusLabel(job.status)}</strong></div><span>{Math.round(progress)}%</span></div>
      <div className="job-progress"><span style={{ width: `${progress}%` }} /></div>
      {hasActivePreparation(job) && <ModelPreparationProgress job={job} />}
      <div className="job-stage-row"><span>当前阶段：{({ importing: "导入视频", transcribing: "保存字幕", analyzing: "AI 筛选", awaiting_api: "等待 API 恢复", completed: "批次结束", interrupted: "已停止", resource_paused: "资源暂停" } as Record<string, string>)[job.stage ?? ""] || "等待处理"}</span><span>{job.message || "队列已创建"}</span></div>
      {job.import_only && <p className="job-import-only-note">本批次仅完成导入，视频已进入视频库；请在视频库选择视频后开始处理。</p>}
      {job.batch_summary && <dl className="batch-summary">
        <div><dt>成功</dt><dd>{job.batch_summary.completed + job.batch_summary.skipped}</dd></div>
        <div><dt>失败</dt><dd>{job.batch_summary.failed}</dd></div>
        <div><dt>待 AI</dt><dd>{job.batch_summary.awaiting_ai}</dd></div>
        <div><dt>尚未转写</dt><dd>{job.batch_summary.pending_asr}</dd></div>
      </dl>}
      {job.batch_entries?.some((entry) => entry.error || entry.warning) && <ul className="batch-entry-notices">
        {job.batch_entries.filter((entry) => entry.error || entry.warning).map((entry, index) => <li key={index}>{entry.label || entry.source_path?.split(/[\\/]/).pop() || `视频 ${entry.video_id}`}：{entry.error || entry.warning}</li>)}
      </ul>}
      <FireRedResourceStatus resource={job.resource} />
      {job.error_message && <p className="job-error"><AlertCircle size={14} />{job.error_message}</p>}
      {(!isTerminal(job) || job.status === "resource_paused") && <button className="stop-job-button" type="button" onClick={onCancel} disabled={stopping}><Ban size={15} /> {stopping ? "正在停止…" : "停止批处理"}</button>}
    </div>
  );
}

function isTerminal(job: Job) {
  return ["completed", "failed", "interrupted", "resource_paused", "awaiting_api"].includes(String(job.status).toLowerCase());
}

function jobStatusLabel(status: string) {
  return ({ queued: "排队中", running: "处理中", completed: "批次结束", failed: "失败", interrupted: "已停止", resource_paused: "资源暂停", awaiting_api: "等待 AI 恢复" } as Record<string, string>)[status] ?? status;
}
