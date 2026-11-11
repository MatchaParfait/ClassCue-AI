import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, LoaderCircle, RefreshCw, Square } from "lucide-react";
import { cancelJob } from "../api/client";
import { getSlideAcceleration, prepareSlideAcceleration, type SlideBackend } from "../api/slides";
import { isTaskTerminal, useTaskStatus } from "../hooks/useTaskStatus";

export function SlideDeviceControl({ active, busy, value, onChange, onBusy }: {
  active: boolean; busy: boolean; value: SlideBackend;
  onChange: (value: SlideBackend) => void; onBusy: (value: boolean) => void;
}) {
  const client = useQueryClient();
  const [jobId, setJobId] = useState<string | null>(null);
  const finished = useRef<string | null>(null);
  const status = useQuery({ queryKey: ["slides", "acceleration"], queryFn: getSlideAcceleration,
    enabled: active, staleTime: 30_000 });
  const job = useTaskStatus(jobId, active);
  const prepare = useMutation({
    mutationFn: prepareSlideAcceleration,
    onSuccess: result => setJobId(result.job_id),
  });
  const stop = useMutation({ mutationFn: () => cancelJob(jobId!) });
  const preparing = prepare.isPending || Boolean(jobId && (!job.data || !isTaskTerminal(job.data)));
  useEffect(() => {
    onBusy(preparing);
    return () => onBusy(false);
  }, [onBusy, preparing]);
  useEffect(() => {
    if (!jobId || !job.data || !isTaskTerminal(job.data) || finished.current === jobId) return;
    finished.current = jobId;
    void client.invalidateQueries({ queryKey: ["slides", "acceleration"] });
  }, [client, job.data, jobId]);
  const error = prepare.error ?? stop.error;
  return (
    <div className="slide-device-control">
      <div className="slide-extraction-actions">
        <label className="field-label slide-device-label">
          处理设备
          <select aria-label="PPT 处理设备" value={value} disabled={busy || preparing}
            onChange={event => onChange(event.target.value as SlideBackend)}>
            <option value="auto">自动</option>
            <option value="cpu">CPU</option>
            <option value="cuda" disabled={!status.data?.ready}>CUDA</option>
          </select>
        </label>
        {status.data && !status.data.ready && !preparing && (
          <button type="button" className="secondary-button compact" disabled={busy}
            onClick={() => prepare.mutate()}>
            <Download size={15} />准备 CUDA
          </button>
        )}
        {status.data?.ready && <span className="slide-roi-note">{status.data.gpu_name ?? "CUDA 已准备"}</span>}
        {status.isError && <button type="button" className="text-button" disabled={busy || preparing}
          onClick={() => void status.refetch()}>重试读取设备状态</button>}
      </div>
      {value === "auto" && <p className="slide-roi-note">长视频优先使用已准备的 CUDA，暂不可用时使用 CPU。</p>}
      {value === "cuda" && status.data && !status.data.ready && <p className="slide-roi-note">请先准备 CUDA 环境。</p>}
      {preparing && <div className="slide-roi-note" role="status">
        <LoaderCircle size={15} className="spin" />{job.data?.message || "正在准备 CUDA…"}
        <button type="button" className="secondary-button compact" disabled={!jobId || stop.isPending}
          onClick={() => stop.mutate()}><Square size={14} />停止准备</button>
      </div>}
      {job.isError && <div className="field-error" role="alert">
        无法读取 CUDA 准备进度，请重新读取或停止准备。
        <button type="button" className="secondary-button compact" disabled={job.isFetching}
          onClick={() => void job.refetch()}><RefreshCw size={14} />重新读取准备进度</button>
      </div>}
      {job.data?.status === "failed" && <p className="field-error" role="alert">{job.data.error_message || "CUDA 准备失败，请重试。"}</p>}
      {job.data?.status === "interrupted" && <p className="slide-roi-note" role="status">CUDA 准备已停止。</p>}
      {error && <p className="field-error" role="alert">{error instanceof Error ? error.message : "设备操作失败，请重试。"}</p>}
    </div>
  );
}
