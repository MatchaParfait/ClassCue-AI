import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef } from "react";
import { LoaderCircle, Pause, Play, Trash2 } from "lucide-react";
import { cleanupAsrDownload, getAsrModelStatus, pauseAsrDownload, resumeAsrDownload } from "../api/client";
import type { AsrModel, Job, JobStatus } from "../types";

function bytes(value: number) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MB`;
  return `${(value / 1024 ** 3).toFixed(2)} GB`;
}

export function ModelDownloadControls({ model, job, preparing = false, busy = false, onJob }: {
  model: Pick<AsrModel, "id" | "download_state">;
  job?: JobStatus;
  preparing?: boolean;
  busy?: boolean;
  onJob: (id: string | number, kind: "preparing" | "cleaning_partial") => void;
}) {
  const client = useQueryClient();
  const inFlight = useRef(false);
  const action = useMutation({
    mutationFn: async (kind: "pause" | "resume" | "cleanup") => {
      if (kind === "pause") {
        await pauseAsrDownload(job!.id);
        return { pausedJobId: job!.id };
      }
      return kind === "resume" ? resumeAsrDownload(model.id) : cleanupAsrDownload(model.id);
    },
    onSuccess: (saved, kind) => {
      if ("job_id" in saved) onJob(saved.job_id, kind === "resume" ? "preparing" : "cleaning_partial");
      void client.invalidateQueries({ queryKey: ["jobs", job?.id] });
      void client.invalidateQueries({ queryKey: ["settings", "asr"] });
      void client.invalidateQueries({ queryKey: ["asr-status", model.id] });
    },
  });
  const recovery = model.download_state;
  const working = preparing && (!job || ["queued", "running"].includes(job.status));
  const pausing = job?.stage === "pausing"
    || (action.isSuccess && "pausedJobId" in action.data && action.data.pausedJobId === job?.id && working);
  const canPause = Boolean(job && !["installing", "finalizing"].includes(job.preparation?.phase ?? ""));
  const run = (kind: "pause" | "resume" | "cleanup") => {
    if (inFlight.current || (kind === "pause" && pausing)) return;
    inFlight.current = true;
    action.mutate(kind, { onSettled: () => { inFlight.current = false; } });
  };
  if (!working && !recovery) return null;
  return <div className="model-download-controls">
    {working ? <>
      <button className="secondary-button compact" type="button" disabled={!canPause || pausing || action.isPending}
        onClick={() => run("pause")}>
        {pausing || action.isPending ? <LoaderCircle className="spin" size={14} /> : <Pause size={14} />}
        {pausing ? "正在暂停…" : "暂停下载"}
      </button>
      <p className="settings-hint" role="status">{pausing ? "正在停止传输并保存续传记录，请稍候。"
        : canPause ? "暂停后保留已下载文件，可稍后继续。" : "安装环境与完成准备阶段暂不可暂停；进入权重下载后可暂停。"}</p>
    </> : <>
      <p className="model-download-state" role="status">
        <strong>{recovery!.resumable === false ? "模型已就绪，可清理遗留下载文件"
          : recovery!.status === "paused" ? "下载已暂停" : "发现未完成的模型准备"}</strong>
        <span>{recovery!.retained_bytes_estimated ? "确认保留至少" : "已保留"} {bytes(recovery!.retained_bytes)}，其中已完成文件 {bytes(recovery!.completed_bytes)}。</span>
      </p>
      {recovery!.current_file && <p className="model-preparation-file" title={recovery!.current_file}>{recovery!.current_file}</p>}
      <div className="asr-model-actions">
        {recovery!.resumable !== false && <button className="secondary-button compact" type="button" disabled={busy || action.isPending} onClick={() => run("resume")}>
          {action.isPending && action.variables === "resume" ? <LoaderCircle className="spin" size={14} /> : <Play size={14} />}继续下载
        </button>}
        <button className="secondary-button compact" type="button" disabled={busy || action.isPending || !recovery!.can_cleanup}
          onClick={() => run("cleanup")}>
          {action.isPending && action.variables === "cleanup" ? <LoaderCircle className="spin" size={14} /> : <Trash2 size={14} />}清理未完成下载
        </button>
      </div>
      <p className="settings-hint">仅清理下载临时文件，保留已完成权重与运行环境。继续时会重新校验已有文件。</p>
    </>}
    {action.isError && <p className="field-error" role="alert">{action.error.message}</p>}
  </div>;
}

export function TaskModelDownloadControls({ job, onJob }: { job: Job; onJob: (id: string | number) => void }) {
  const working = ["queued", "running"].includes(job.status);
  const query = useQuery({
    queryKey: ["asr-status", job.model_id, "download-recovery"],
    queryFn: () => getAsrModelStatus(job.model_id!, { full: false }),
    enabled: Boolean(job.model_id) && !working,
    staleTime: 0,
  });
  if (!job.model_id) return null;
  return <>
    {query.isError && <p className="field-error" role="alert">下载恢复信息读取失败。<button className="secondary-button compact" onClick={() => void query.refetch()}>重新读取</button></p>}
    <ModelDownloadControls model={{ id: job.model_id, download_state: query.data?.download_state }}
      job={job} preparing={working && job.job_type === "asr_prepare"} busy={working} onJob={onJob} />
  </>;
}
