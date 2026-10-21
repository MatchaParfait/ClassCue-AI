import { useEffect, useState } from "react";
import type { JobStatus } from "../types";

export function hasActivePreparation(job?: JobStatus & { job_type?: string }) {
  return Boolean(job && ["queued", "running"].includes(job.status)
    && (job.job_type === "asr_prepare" || job.preparation?.active === true));
}

export function preparationSummary(job: JobStatus) {
  const detail = job.preparation;
  if (!detail) return "模型准备中";
  const fraction = detail.phase_progress;
  const percent = typeof fraction === "number" && ["downloading", "verifying"].includes(detail.phase)
    ? ` · ${Math.floor(Math.max(0, Math.min(1, fraction)) * 100)}%` : "";
  return detail.label + percent;
}

function bytes(value: number) {
  if (value < 1024) return `${Math.round(value)} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MB`;
  return `${(value / 1024 ** 3).toFixed(2)} GB`;
}

export function remainingTime(seconds: number) {
  const rounded = Math.max(1, Math.ceil(seconds));
  if (rounded < 60) return `${rounded} 秒`;
  if (rounded < 3600) return `${Math.floor(rounded / 60)} 分 ${rounded % 60} 秒`;
  const minutes = Math.ceil(rounded / 60);
  return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分钟`;
}

export function ModelPreparationProgress({ job, connectionError = false }: {
  job?: JobStatus; connectionError?: boolean;
}) {
  const [now, setNow] = useState(Date.now);
  const detail = job?.preparation;
  const running = !job || ["queued", "running"].includes(job.status);
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running]);
  if (!running) return null;
  const stale = Boolean(detail && now / 1000 - detail.updated_at > 8);
  const downloading = detail?.phase === "downloading";
  const fraction = detail?.phase_progress;
  const known = typeof fraction === "number" && Number.isFinite(fraction);
  const percent = known ? Math.max(0, Math.min(100, fraction * 100)) : undefined;
  const waiting = detail?.waiting_for_network;
  const pausing = job?.stage === "pausing";
  const eta = !pausing && !stale && !connectionError && !waiting && downloading ? detail?.eta_seconds : null;
  const speed = !pausing && !stale && !connectionError && downloading ? detail?.speed_bytes_per_second : null;
  const components: Record<string, string> = {
    recognizer: "语音识别权重", aligner: "时间戳对齐权重", vad: "语音活动检测权重",
  };
  const label = detail?.label ?? "等待准备任务启动";
  return <section className="model-preparation" aria-label="模型准备进度">
    <div className="model-preparation-heading" role="status">
      <strong>{label}{detail?.step && detail.steps ? ` · 步骤 ${detail.step}/${detail.steps}` : ""}</strong>
      <span>{percent === undefined ? "进行中" : `${Math.floor(percent)}%`}</span>
    </div>
    <div className={`model-preparation-track${known ? "" : " indeterminate"}`}
      role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100}
      aria-valuenow={percent === undefined ? undefined : Math.floor(percent)}
      aria-valuetext={percent === undefined ? `${label}，暂时无法估算进度` : `${label} ${Math.floor(percent)}%`}>
      <span style={percent === undefined ? undefined : { width: `${percent}%` }} />
    </div>
    {detail?.component && <p className="model-preparation-component">{components[detail.component] ?? detail.component}
      {detail.source ? ` · ${detail.source}` : ""}</p>}
    {detail?.current_file ? <p className="model-preparation-file" title={detail.current_file}>{detail.current_file}</p>
      : <p className="model-preparation-file">{detail?.message ?? job?.message ?? "任务已提交，正在连接准备进程…"}</p>}
    {detail?.current_package && <p>当前依赖：{detail.current_package}</p>}
    {detail?.phase === "installing" && detail.elapsed_seconds != null && <p>
      本步骤已用时 {remainingTime(detail.elapsed_seconds + Math.max(0, now / 1000 - detail.updated_at))}
    </p>}
    {downloading && detail?.bytes_total !== undefined && <div className="model-preparation-metrics">
      <span>{bytes(detail.bytes_completed ?? 0)} / {bytes(detail.bytes_total)}</span>
      {speed != null && speed > 0 && <span>{bytes(speed)}/s</span>}
    </div>}
    {detail?.phase === "verifying" && detail.check_total_bytes != null && <p>
      已校验 {bytes(detail.checked_bytes ?? 0)} / {bytes(detail.check_total_bytes)}
    </p>}
    <p className="model-preparation-estimate" role="status">
      {pausing ? "正在停止传输并保存续传记录，完成后可继续下载。"
        : connectionError ? "进度连接暂时中断，正在重连；后台任务继续运行。"
        : stale ? "等待后台更新，暂时无法估算剩余时间。"
          : waiting ? "正在等待网络传输，剩余时间暂时无法估算。"
            : eta != null && eta > 0 ? `权重下载预计还需约 ${remainingTime(eta)}，随后进行校验。`
              : downloading ? "正在测量下载速度，随后显示预计剩余时间。"
                : detail?.phase === "installing" ? "依赖安装耗时取决于网络和设备，暂时无法可靠估算。"
                  : detail?.phase === "verifying" ? "下载文件正在校验，校验完成后继续准备。"
                    : "准备仍在进行，完成当前阶段后自动继续。"}
    </p>
  </section>;
}
