import { useEffect, useState } from "react";
import type { Job, JobStatus } from "../types";
import { remainingTime } from "./ModelPreparationProgress";

export function isCleanupJob(job?: Job) {
  return Boolean(job && ["asr_delete", "asr_runtime_delete", "asr_download_cleanup"].includes(job.job_type ?? ""));
}

export function cleanupSummary(job: JobStatus) {
  const detail = job.cleanup;
  if (!detail) return "正在清理";
  if (detail.phase === "scanning") return `检查文件 · ${detail.files_scanned.toLocaleString()} 个`;
  if (detail.phase === "deleting") return `删除文件 · ${detail.files_deleted.toLocaleString()}/${detail.files_total?.toLocaleString() ?? "—"}`;
  return detail.phase === "completed" ? "清理完成" : "释放占用并检查目录";
}

function bytes(value: number) {
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MB`;
  return `${(value / 1024 ** 3).toFixed(2)} GB`;
}

export function ModelCleanupProgress({ job, connectionError = false }: { job?: JobStatus; connectionError?: boolean }) {
  const [now, setNow] = useState(Date.now);
  const running = !job || ["queued", "running"].includes(job.status);
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running]);
  const detail = job?.cleanup;
  const stale = Boolean(running && detail && now / 1000 - detail.updated_at > 8);
  const ended = job?.status === "completed";
  const fraction = detail?.phase_progress;
  const known = ended || (typeof fraction === "number" && Number.isFinite(fraction));
  const percent = ended ? 100 : typeof fraction === "number" && Number.isFinite(fraction)
    ? Math.max(0, Math.min(100, fraction * 100)) : undefined;
  const label = ended ? "清理完成" : !running ? "清理未完成" : {
    checking: "释放占用并检查清理范围", scanning: "检查目录并统计文件", deleting: "删除文件", completed: "完成清理",
  }[detail?.phase ?? "checking"];
  const start = job?.started_at ? Date.parse(job.started_at) : NaN;
  const finish = job?.finished_at ? Date.parse(job.finished_at) : NaN;
  const reference = running ? now : Number.isFinite(finish) ? finish : (detail?.updated_at ?? now / 1000) * 1000;
  const elapsed = Number.isFinite(start) ? Math.max(0, (reference - start) / 1000)
    : (detail?.elapsed_seconds ?? 0) + (running && detail ? Math.max(0, now / 1000 - detail.updated_at) : 0);
  return <section className="model-preparation model-cleanup" aria-label="模型清理进度">
    <div className="model-preparation-heading" role="status">
      <strong>{label}</strong><span>{percent == null ? running ? "进行中" : "已停止" : `${Math.floor(percent)}%`}</span>
    </div>
    <div className={`model-preparation-track${!known && running ? " indeterminate" : ""}`}
      role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100}
      aria-valuenow={percent == null ? undefined : Math.floor(percent)}>
      <span style={{ width: percent == null ? running ? undefined : "0%" : `${percent}%` }} />
    </div>
    {detail?.scope && <p className="model-preparation-component">{detail.scope}
      {detail.current_target ? ` · ${detail.current_target}` : ""}</p>}
    <p className="model-preparation-file" title={detail?.current_path ?? undefined}>
      {detail?.current_path ?? detail?.message ?? job?.message ?? "正在等待清理任务启动…"}</p>
    {detail?.files_total != null ? <>
      <p>已删除 {detail.files_deleted.toLocaleString()} / {detail.files_total.toLocaleString()} 个文件
        {" · "}{detail.directories_deleted.toLocaleString()} / {detail.directories_total?.toLocaleString() ?? "0"} 个目录</p>
      <p>已释放 {bytes(detail.bytes_deleted)}{detail.bytes_total != null ? ` / ${bytes(detail.bytes_total)}` : ""}</p>
    </> : detail?.phase === "scanning" && <p>已检查 {detail.files_scanned.toLocaleString()} 个文件
      {" · "}{detail.directories_scanned.toLocaleString()} 个目录，总数量尚在统计</p>}
    <p>已用时 {remainingTime(elapsed)}</p>
    <p className="model-preparation-estimate" role="status">
      {connectionError ? "清理进度连接暂时中断，正在重连；后台任务继续运行。"
        : stale ? "等待系统文件操作返回，暂时无法估算剩余时间。"
          : !running ? ended ? "清理已完成。" : "清理已停止，已完成的删除不会回滚，可重新发起清理。"
            : detail?.phase === "scanning" ? "正在一次性检查目录并统计容量，统计完成后显示删除进度。"
              : detail?.eta_seconds != null && detail.eta_seconds > 0
                ? `按近期删除速度估计，还需约 ${remainingTime(detail.eta_seconds)}。`
                : detail?.phase === "deleting" ? "正在删除文件，速度取决于磁盘和文件占用情况。"
                  : "正在准备清理，操作完成后自动更新状态。"}
    </p>
  </section>;
}
