import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CircuitBoard, Download, Presentation, RefreshCw, Square, Trash2 } from "lucide-react";
import { cancelJob } from "../api/client";
import {
  clearSlideResources, getSlideResources, getSlideSelection, prepareSlideAcceleration,
  saveSlideSelection, type SlideBackend,
} from "../api/slides";
import { isTaskTerminal, useTaskStatus } from "../hooks/useTaskStatus";
import { ModelPreparationProgress } from "./ModelPreparationProgress";
import { isCleanupJob, ModelCleanupProgress } from "./ModelCleanupProgress";
import "./SlideSettings.css";

function bytes(value: number | undefined) {
  if (value === undefined) return "正在统计…";
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MB`;
  return `${(value / 1024 ** 3).toFixed(2)} GB`;
}

const deviceOptions: { value: SlideBackend; label: string }[] = [
  { value: "auto", label: "自动" },
  { value: "cpu", label: "CPU" },
  { value: "cuda", label: "GPU" },
];

export function SlideSettings({ active, onJob }: {
  active: boolean; onJob?: (id: string | number) => void;
}) {
  const client = useQueryClient();
  const [deviceDraft, setDeviceDraft] = useState<SlideBackend | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [jobKind, setJobKind] = useState<"slide_prepare" | "slide_cache_cleanup">("slide_prepare");
  const finished = useRef<string | null>(null);
  const resources = useQuery({
    queryKey: ["slides", "resources"], queryFn: () => getSlideResources(), enabled: active,
    staleTime: 30_000, refetchInterval: active ? 5000 : false, retry: false,
  });
  useEffect(() => {
    if (!resources.data?.active_job_id) return;
    setJobId(resources.data.active_job_id);
    if (resources.data.active_job_kind) setJobKind(resources.data.active_job_kind);
  }, [resources.data?.active_job_id, resources.data?.active_job_kind]);
  const selection = useQuery({
    queryKey: ["slides", "selection"], queryFn: getSlideSelection, enabled: active, retry: false,
  });
  const trackedId = resources.data?.active_job_id ?? jobId;
  const trackedKind = resources.data?.active_job_kind ?? jobKind;
  const job = useTaskStatus(trackedId, active);
  const running = Boolean(trackedId && (!job.data || !isTaskTerminal(job.data)));
  const cleaning = trackedKind === "slide_cache_cleanup" || isCleanupJob(job.data);
  const save = useMutation({
    mutationFn: saveSlideSelection,
    onSuccess: result => client.setQueryData(["slides", "selection"], result),
    onSettled: () => setDeviceDraft(null),
  });
  const command = useMutation({
    mutationFn: (action: "prepare" | "all" | "downloads") => action === "prepare"
      ? prepareSlideAcceleration() : clearSlideResources(action),
    onSuccess: (result, action) => {
      setJobId(result.job_id);
      setJobKind(action === "prepare" ? "slide_prepare" : "slide_cache_cleanup");
      onJob?.(result.job_id);
      void client.invalidateQueries({ queryKey: ["slides", "resources"] });
      void client.invalidateQueries({ queryKey: ["recent-jobs"] });
    },
  });
  const stop = useMutation({
    mutationFn: () => cancelJob(trackedId!),
    onSuccess: result => client.setQueryData(["jobs", trackedId], result),
  });
  useEffect(() => {
    if (!trackedId || !job.data || !isTaskTerminal(job.data) || finished.current === trackedId) return;
    finished.current = trackedId;
    void client.invalidateQueries({ queryKey: ["slides", "resources"] });
    void client.invalidateQueries({ queryKey: ["slides", "acceleration"] });
    void client.invalidateQueries({ queryKey: ["recent-jobs"] });
  }, [client, job.data, trackedId]);
  const value = deviceDraft ?? selection.data?.backend ?? "auto";
  const busy = deviceDraft !== null || command.isPending || save.isPending || running || Boolean(resources.data?.busy);
  const unavailable = !resources.data || resources.isError;
  const error = save.error ?? command.error ?? stop.error;
  const refresh = () => {
    void client.fetchQuery({ queryKey: ["slides", "resources"], staleTime: 0,
      queryFn: () => getSlideResources(true) }).catch(() => {});
  };
  return <section className="settings-section slide-settings" aria-label="PPT 提取设置">
    <div className="settings-section-heading">
      <div className="settings-section-title"><Presentation size={20} /><h2>PPT 提取</h2></div>
      <button type="button" className="icon-button" aria-label="刷新 PPT 设备与占用" title="刷新 PPT 设备与占用"
        disabled={resources.isFetching || busy} onClick={refresh}>
        <RefreshCw size={16} className={resources.isFetching ? "spin" : undefined} />
      </button>
    </div>
    <div className="ppt-default-device">
      <fieldset className="firered-device-selector ppt-device-selector" aria-label="PPT 默认处理设备">
        <legend>默认处理设备</legend>
        <div className="firered-device-modes">
          {deviceOptions.map(option => {
            const disabled = busy || !selection.data || selection.isError
              || (option.value === "cuda" && (unavailable || !resources.data?.ready));
            return <label key={option.value}
              className={`${value === option.value ? "selected " : ""}${disabled ? "unavailable" : ""}`}>
              <input type="radio" name="ppt-default-device" value={option.value}
                checked={value === option.value} disabled={disabled}
                onChange={() => { setDeviceDraft(option.value); save.mutate(option.value); }} />
              <span>{option.label}</span>
            </label>;
          })}
        </div>
      </fieldset>
      <span className="settings-hint" role="status">
        {save.isPending ? "正在保存…" : save.isSuccess ? "默认设备已保存" : "后续 PPT 任务沿用此选择"}
      </span>
    </div>
    {value === "auto" && <p className="settings-hint">至少 2 分钟的视频优先使用已准备的 GPU；暂不可用时使用 CPU。</p>}
    <div className="ppt-resources" role="list" aria-label="PPT 运行资源">
      <div className="ppt-resource-row" role="listitem">
        <div className="ppt-resource-name"><CircuitBoard size={18} /><div><strong>GPU（CUDA）</strong>
          <p className="settings-hint">{resources.isPending ? "正在检查 GPU…"
            : resources.data?.ready ? resources.data.gpu_name || "环境已准备"
            : resources.data?.gpu_unavailable_reason || "设备状态暂不可用"}</p>
          <p className="settings-hint">首次下载约 3.3 GB，安装环境约占 5.8 GB。</p>
        </div></div>
        <span className="ppt-resource-size" aria-label="PPT GPU 环境占用">{bytes(resources.data?.runtime_size_bytes)}</span>
        <div className="ppt-resource-actions">
          <button type="button" className="secondary-button compact"
            disabled={busy || unavailable || !resources.data?.gpu_prepare_available || resources.data.ready}
            onClick={() => command.mutate("prepare")}><Download size={15} />
            {resources.data?.ready ? "GPU 已准备" : "准备 GPU 环境"}
          </button>
          <button type="button" className="icon-button danger-icon" aria-label="清理 PPT GPU 环境及缓存"
            title={value === "cuda" ? "先切换到 CPU 或自动，再清理 GPU 环境" : "清理 GPU 环境及 PPT 下载缓存"}
            disabled={busy || unavailable || !selection.data || selection.isError || value === "cuda"
              || !((resources.data?.runtime_size_bytes ?? 0) + (resources.data?.download_cache_size_bytes ?? 0))}
            onClick={() => command.mutate("all")}><Trash2 size={16} /></button>
        </div>
      </div>
      <div className="ppt-resource-row" role="listitem">
        <div className="ppt-resource-name"><Download size={18} /><div><strong>PPT 下载与计算缓存</strong>
          <p className="settings-hint">安装成功后自动清理安装包；此处可清理失败残留和计算缓存。</p>
          {Boolean(resources.data?.shared_download_cache_bytes) && <p className="settings-hint">
            包含共用安装包；以后重新安装相关 GPU 环境时，可能需要重新下载。
          </p>}
        </div></div>
        <span className="ppt-resource-size" aria-label="PPT 下载缓存占用">{bytes(resources.data?.download_cache_size_bytes)}</span>
        <button type="button" className="icon-button danger-icon" title="清理 PPT 下载缓存" aria-label="清理 PPT 下载缓存"
          disabled={busy || unavailable || !resources.data?.download_cache_size_bytes}
          onClick={() => command.mutate("downloads")}><Trash2 size={16} /></button>
      </div>
    </div>
    <p className="settings-hint">占用按文件大小统计，共享文件可能重复计入。清理不影响视频、字幕和已提取的 PPT。</p>
    {value === "cuda" && <p className="settings-hint">先将默认设备切换到 CPU 或自动，才能移除 GPU 环境。</p>}
    {running && <div className="ppt-resource-task">
      {cleaning
        ? <ModelCleanupProgress job={job.data} connectionError={job.isError} label="PPT 资源清理进度" />
        : <ModelPreparationProgress job={job.data} connectionError={job.isError} label="PPT GPU 安装进度" />}
      <button type="button" className="secondary-button compact" disabled={stop.isPending || !trackedId}
        onClick={() => stop.mutate()}><Square size={14} />停止{cleaning ? "清理" : "准备"}</button>
    </div>}
    {!running && job.data && cleaning && <ModelCleanupProgress job={job.data} label="PPT 资源清理进度" />}
    {!running && job.data && <p className={job.data.status === "failed" ? "field-error" : "settings-hint"}
      role={job.data.status === "failed" ? "alert" : "status"}>
      {job.data.error_message || (job.data.status === "interrupted" ? "PPT 资源任务已停止。" : job.data.message)}
    </p>}
    {job.isError && <p className="field-error" role="alert">任务进度读取失败，后台任务仍可能继续。
      <button type="button" className="text-button" onClick={() => void job.refetch()}>重新读取进度</button>
    </p>}
    {resources.isError && <p className="field-error" role="alert">PPT 设备与占用读取失败。
      <button type="button" className="text-button" onClick={refresh}>重试</button>
    </p>}
    {selection.isError && <p className="field-error" role="alert">默认设备读取失败。
      <button type="button" className="text-button" onClick={() => void selection.refetch()}>重新读取默认设备</button>
    </p>}
    {resources.data?.busy && !running && <p className="settings-hint" role="status">后台有任务正在运行，完成或停止后可管理 PPT 环境。</p>}
    {error && <p className="field-error" role="alert">{error.message}</p>}
  </section>;
}
