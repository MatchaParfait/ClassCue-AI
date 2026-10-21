import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, Ban, CheckCircle2, Clock3, LoaderCircle, PauseCircle, RotateCcw, Settings2 } from "lucide-react";
import { cancelJob, resumeJob, retryBatch } from "../api/client";
import { isTaskTerminal, taskLabel } from "../hooks/useTaskStatus";
import { FireRedResourceStatus } from "./FireRedResourceStatus";
import type { Job } from "../types";
import { hasActivePreparation, ModelPreparationProgress } from "./ModelPreparationProgress";
import { TaskModelDownloadControls } from "./ModelDownloadControls";
import { isCleanupJob, ModelCleanupProgress } from "./ModelCleanupProgress";

export function TaskDetails({ job, loading, error, onRetryLoad, onOpen, onSettings, onJob }: {
  job?: Job; loading: boolean; error: unknown; onRetryLoad: () => void;
  onOpen: (job: Job) => void; onSettings: () => void;
  onJob: (id: string | number) => void;
}) {
  const client = useQueryClient();
  const command = useMutation({
    mutationFn: async (action: "stop" | "resume" | "retry") => {
      const id = job!.job_id ?? job!.id;
      return action === "stop" ? cancelJob(id) : action === "resume" ? resumeJob(id) : retryBatch(id);
    },
    onSuccess: (saved) => {
      client.setQueryData(["jobs", saved.job_id ?? saved.id], saved);
      onJob(saved.job_id ?? saved.id);
      void client.invalidateQueries({ queryKey: ["library-status"] });
      void client.invalidateQueries({ queryKey: ["batch-latest"] });
      void client.invalidateQueries({ queryKey: ["paused-jobs"] });
      void client.invalidateQueries({ queryKey: ["videos"] });
      void client.invalidateQueries({ queryKey: ["library"] });
    },
  });
  if (error) return <div className="task-load-error" role="alert"><AlertCircle size={18} /><span>任务状态读取失败</span>
    <button className="secondary-button compact" onClick={onRetryLoad}><RotateCcw size={15} />重新读取</button></div>;
  if (!job) return <div className="task-empty"><Clock3 size={28} /><strong>{loading ? "正在读取任务" : "暂无处理任务"}</strong></div>;
  const running = !isTaskTerminal(job);
  const cleaning = isCleanupJob(job);
  const migrating = job.job_type === "storage_migration";
  const downloadPaused = job.stage === "download_paused";
  const waiting = ["resource_paused", "awaiting_api"].includes(job.status);
  const modelFraction = job.job_type === "asr_prepare" ? job.preparation?.phase_progress : null;
  const progress = Math.max(0, Math.min(100, modelFraction != null ? modelFraction * 100 : job.progress ?? 0));
  const summary = job.batch_summary;
  const retryableBatch = job.job_type === "batch_video_process"
    && !job.import_only
    && (!summary || (summary.retryable ?? ((summary.failed ?? 0) + (summary.awaiting_ai ?? 0) + (summary.pending_asr ?? 0))) > 0);
  const commandIcon = command.isPending ? <LoaderCircle className="spin" size={16} /> : <RotateCcw size={16} />;
  return <div className={`task-details task-${job.status}`}>
    <div className="task-details-title">{running ? <LoaderCircle className="spin" size={22} /> : waiting || downloadPaused ? <PauseCircle size={22} />
      : job.status === "completed" ? <CheckCircle2 size={22} /> : <AlertCircle size={22} />}
      <div><h3>{job.stage === "download_paused" ? "下载已暂停" : job.stage === "pausing" ? "正在暂停下载" : taskLabel(job.status)}</h3><span>{migrating ? "目录迁移" : cleaning ? "模型清理" : job.job_type === "batch_video_process" ? "批量处理" : job.video_id ? "视频处理" : "模型任务"}</span></div>
      {!cleaning && !(running && job.job_type === "asr_prepare") && <output>{Math.round(progress)}%</output>}</div>
    {cleaning ? <ModelCleanupProgress job={job} /> : running && job.job_type === "asr_prepare" ? <ModelPreparationProgress job={job} />
      : <div className="job-progress"><span style={{ width: `${progress}%` }} /></div>}
    <p className="task-message">{job.message}</p>
    {job.job_type !== "asr_prepare" && hasActivePreparation(job) && <ModelPreparationProgress job={job} />}
    {summary && <dl className="task-summary">{[
      ["已完成", summary.completed + summary.skipped], ["失败", summary.failed],
      ["待 AI", summary.awaiting_ai], ["尚未转写", summary.pending_asr],
    ].map(([label, count]) => <div key={label}><dt>{label}</dt><dd>{count}</dd></div>)}</dl>}
    <FireRedResourceStatus resource={job.resource} />
    {job.error_message && <p className="field-error" role="alert">{job.error_message}</p>}
    {command.isError && <p className="field-error" role="alert">{command.error.message}</p>}
    {["asr_prepare", "asr_download_cleanup"].includes(job.job_type ?? "") && <TaskModelDownloadControls job={job} onJob={onJob} />}
    <div className="task-commands">
      {job.status === "resource_paused" ? <button className="primary-button" aria-busy={command.isPending} disabled={command.isPending} onClick={() => command.mutate("resume")}>{commandIcon}继续任务</button>
        : job.job_type === "batch_video_process" && ["awaiting_api", "failed", "interrupted"].includes(job.status) && retryableBatch
          ? <button className="primary-button" aria-busy={command.isPending} disabled={command.isPending} onClick={() => command.mutate("retry")}>{commandIcon}重试未完成视频</button>
          : <button className="secondary-button" onClick={() => onOpen(job)}>{migrating ? "打开存储设置" : "打开处理详情"}</button>}
      {job.status === "awaiting_api" && <button className="secondary-button" onClick={onSettings}><Settings2 size={16} />AI 服务设置</button>}
    </div>
    {!migrating && (running || job.status === "resource_paused") && <div className="task-danger-zone"><button className="stop-job-button"
      disabled={command.isPending || job.stage === "stopping"} onClick={() => command.mutate("stop")}><Ban size={15} />
      {command.isPending || job.stage === "stopping" ? "正在请求停止…" : "停止任务"}</button></div>}
    {job.batch_entries && <ul className="task-entry-list">{job.batch_entries.map((entry, index) =>
      <li key={`${entry.video_id}:${index}`}><div><strong>{entry.label || `视频 ${entry.video_id ?? index + 1}`}</strong>
        <span>{({ completed: "已完成", skipped: "已复用", failed: "失败", pending_ai: "待 AI", awaiting_api: "待 AI", pending_asr: "尚未转写" } as Record<string, string>)[entry.status] ?? entry.status}</span></div>
        {entry.error && <p className="field-error">{entry.error}</p>}</li>)}</ul>}
  </div>;
}
