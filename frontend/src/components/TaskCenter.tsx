import { AlertCircle, CheckCircle2, ChevronRight, Clock3, LoaderCircle } from "lucide-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState, type RefObject } from "react";
import { useNavigate } from "react-router-dom";
import { createPortal } from "react-dom";
import { getRecentJobs } from "../api/client";
import { isTaskTerminal, taskLabel, taskTypeLabel, useTaskStatus } from "../hooks/useTaskStatus";
import type { Job } from "../types";
import { cleanupSummary, isCleanupJob } from "./ModelCleanupProgress";
import { hasActivePreparation, preparationSummary } from "./ModelPreparationProgress";
import { TaskDetails } from "./TaskDetails";
import { WorkbenchPanel } from "./WorkbenchPanel";
import "./TaskCenter.css";

export function useRecentTasks(active = true, poll = true) {
  return useQuery({
    queryKey: ["recent-jobs"], queryFn: getRecentJobs, enabled: active,
    staleTime: 1000, refetchInterval: active && poll ? 5000 : false,
    refetchIntervalInBackground: false,
  });
}

export function taskDestination(job: Job) {
  return job.job_type === "storage_migration" ? "/settings?section=storage"
    : job.job_type === "batch_video_process" ? "/batch-process"
      : job.video_id ? `/process?videoId=${job.video_id}` : "/settings?section=asr";
}

export function TaskIndicator({ job, error, expanded, onClick }: {
  job?: Job; error?: boolean; expanded: boolean; onClick: () => void;
}) {
  const running = Boolean(job && !isTaskTerminal(job));
  const Icon = error || job?.status === "failed" ? AlertCircle
    : running ? LoaderCircle : job?.status === "completed" ? CheckCircle2 : Clock3;
  const text = error ? "状态读取失败" : job?.stage === "download_paused" ? "下载已暂停"
    : job?.stage === "pausing" ? "正在暂停下载" : job && running && isCleanupJob(job)
      ? cleanupSummary(job) : job && hasActivePreparation(job) ? preparationSummary(job)
        : job ? `${taskLabel(job.status)}${running ? ` · ${Math.round(job.progress ?? 0)}%` : ""}` : "暂无任务";
  return <button type="button" className={`task-indicator ${job ? `task-${job.status}` : ""}`} onClick={onClick}
    title="查看任务详情" aria-label="查看任务详情" aria-haspopup="dialog" aria-expanded={expanded}>
    <Icon className={running && !error ? "spin" : undefined} size={20} aria-hidden="true" />
    <span className="task-indicator-label">任务</span>
    <span className="task-indicator-status" role="status">{text}</span>
    <ChevronRight size={15} aria-hidden="true" />
  </button>;
}

export function TaskHistory({ jobs, current, loading, error, onRetryLoad, onOpen, onSettings, onJob, active = true }: {
  jobs: Job[]; current?: Job; loading: boolean; error: unknown; onRetryLoad: () => void;
  onOpen: (job: Job) => void; onSettings: () => void; onJob: (id: string | number) => void; active?: boolean;
}) {
  const [selectedId, setSelectedId] = useState<string | number | null>(null);
  const selected = jobs.find(job => String(job.id) === String(selectedId));
  const detail = useTaskStatus(selected?.id ?? null, active && selected != null && selected.id !== current?.id);
  const visibleJob = selected ? selected.id === current?.id ? current : detail.data ?? selected : current ?? jobs[0];
  const rows = current && !jobs.some(job => String(job.id) === String(current.id)) ? [current, ...jobs] : jobs;
  return <div className="task-history">
    {rows.length > 0 && <section className="task-history-list" aria-label="最近任务">
      <h3>最近任务</h3>
      <ul>{rows.map(row => {
        const job = String(row.id) === String(current?.id) ? current! : row;
        const time = job.finished_at ?? job.started_at ?? job.created_at;
        const timestamp = time?.replace(" ", "T");
        const date = timestamp ? new Date(/(?:Z|[+-]\d{2}:?\d{2})$/i.test(timestamp) ? timestamp : `${timestamp}Z`) : null;
        const running = !isTaskTerminal(job);
        return <li key={String(job.id)}><button type="button"
          className={`task-history-row task-${job.status}`} aria-pressed={String(visibleJob?.id) === String(job.id)}
          onClick={() => setSelectedId(job.id)}>
          <span className="task-history-icon">{running ? <LoaderCircle className="spin" size={16} />
            : job.status === "completed" ? <CheckCircle2 size={16} /> : <Clock3 size={16} />}</span>
          <span className="task-history-name"><strong>{taskTypeLabel(job)}</strong>
            {job.video_name && <span title={job.video_name}>{job.video_name}</span>}
            {date && !Number.isNaN(date.getTime()) && <time dateTime={time!}>{date.toLocaleString("zh-CN", {
              month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
            })}</time>}
          </span>
          <span className="task-history-status">{taskLabel(job.status)}{running ? ` · ${Math.round(job.progress ?? 0)}%` : ""}</span>
          <ChevronRight size={14} aria-hidden="true" />
        </button></li>;
      })}</ul>
    </section>}
    <TaskDetails job={visibleJob} loading={loading} error={detail.error ?? error}
      onRetryLoad={() => { onRetryLoad(); if (selected) void detail.refetch(); }} onOpen={onOpen} onSettings={onSettings}
      onJob={id => { setSelectedId(null); onJob(id); }} />
  </div>;
}

export function ReviewTaskCenter({ demo = false, backgroundRef }: {
  demo?: boolean; backgroundRef?: RefObject<HTMLDivElement>;
}) {
  const [open, setOpen] = useState(false);
  const localBackground = useRef<HTMLDivElement>(null);
  const recent = useRecentTasks(!demo);
  const jobs = recent.data ?? [];
  const current = useTaskStatus(jobs[0]?.id ?? null, !demo);
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const onJob = (id: string | number) => {
    void queryClient.invalidateQueries({ queryKey: ["recent-jobs"] });
    void queryClient.invalidateQueries({ queryKey: ["jobs", id] });
  };
  return <>
    <TaskIndicator job={current.data ?? jobs[0]} error={!demo && (recent.isError || current.isError)} expanded={open}
      onClick={() => { setOpen(true); if (!demo) void recent.refetch(); }} />
    {createPortal(<WorkbenchPanel active={open} title="任务详情" scrollKey="review-tasks"
      backgroundRef={backgroundRef ?? localBackground} onClose={() => setOpen(false)}>
      {open && <TaskHistory jobs={jobs} current={current.data ?? jobs[0]} loading={!demo && recent.isLoading}
        error={recent.error ?? current.error} onRetryLoad={() => { void recent.refetch(); if (jobs[0]) void current.refetch(); }}
        onOpen={job => navigate(taskDestination(job))} onSettings={() => navigate("/settings?section=ai")}
        onJob={onJob} />}
    </WorkbenchPanel>, document.body)}
  </>;
}
