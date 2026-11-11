import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRightLeft, CircuitBoard, Cpu, Database, Download, LoaderCircle, RefreshCw, Search, Square, Trash2 } from "lucide-react";
import { cancelJob } from "../api/client";
import { buildSemanticIndex, cleanupSemanticSearch, getSemanticCache, getSemanticCourses, getSemanticStatus, prepareSemanticModel, setupSemanticSearch, type SemanticCleanupTarget } from "../api/semantic";
import { isTaskTerminal, taskLabel, useTaskStatus } from "../hooks/useTaskStatus";
import { ModelPreparationProgress } from "./ModelPreparationProgress";
import { ModelCleanupProgress } from "./ModelCleanupProgress";
import "./SemanticSettings.css";

function bytes(value: number) {
  return value >= 1024 ** 3 ? `${(value / 1024 ** 3).toFixed(2)} GB`
    : value >= 1024 ** 2 ? `${(value / 1024 ** 2).toFixed(1)} MB`
      : value >= 1024 ? `${(value / 1024).toFixed(1)} KB` : `${value} B`;
}
const cleanupNames: Record<SemanticCleanupTarget, string> = {
  cuda: "GPU 环境", cpu: "CPU 环境", weights: "共享模型权重", all: "全部 RAG 缓存",
};
type Command = "prepare" | "index" | "setup"
  | { action: "cleanup"; target: SemanticCleanupTarget; keepDevice?: "cpu" | "cuda" };

export function SemanticSettings({ active, onJob }: {
  active: boolean; onJob?: (id: string | number) => void;
}) {
  const client = useQueryClient();
  const [course, setCourse] = useState("");
  const [device, setDevice] = useState<"cpu" | "cuda" | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const finished = useRef<string | null>(null);
  const operation = useRef<{ id: string; action: "prepare" | "index" | "setup" | "cleanup";
    target?: SemanticCleanupTarget; device?: "cpu" | "cuda" } | null>(null);
  const courses = useQuery({ queryKey: ["semantic", "courses"], queryFn: getSemanticCourses,
    enabled: active, staleTime: 30_000 });
  const status = useQuery({ queryKey: ["semantic", "status", course],
    queryFn: () => getSemanticStatus(course || undefined), enabled: active,
    staleTime: 5000, refetchInterval: active ? 5000 : false, retry: false });
  const cache = useQuery({ queryKey: ["semantic", "cache"], queryFn: getSemanticCache,
    enabled: active, staleTime: 15_000, retry: false });
  useEffect(() => {
    if (status.data?.active_job_id) setJobId(status.data.active_job_id);
  }, [status.data?.active_job_id]);
  const trackedId = status.data?.active_job_id ?? jobId;
  const job = useTaskStatus(trackedId, active);
  const selectedDevice = device ?? status.data?.device ?? "cpu";
  const deviceName = selectedDevice === "cuda" ? "GPU" : "CPU";
  const weightsReady = status.data?.weights_ready ?? Boolean(status.data?.ready);
  const runtimeReady = status.data?.runtimes?.[selectedDevice]?.ready
    ?? Boolean(status.data?.ready && selectedDevice === status.data.device);
  const gpuUnavailable = selectedDevice === "cuda" && status.data?.gpu_detected === false;
  const needsPreparation = !weightsReady || !runtimeReady;
  const needsActivation = selectedDevice !== status.data?.device;
  const estimates = status.data?.download_estimates;
  const cachePresent = status.data?.cache_present ?? (weightsReady
    || Boolean(status.data?.runtimes?.cpu.ready || status.data?.runtimes?.cuda.ready));
  const command = useMutation({
    mutationFn: (action: Command) => typeof action === "object"
      ? action.keepDevice ? cleanupSemanticSearch(action.target, action.keepDevice)
        : cleanupSemanticSearch(action.target) : action === "prepare"
      ? prepareSemanticModel(selectedDevice) : action === "setup"
      ? setupSemanticSearch(course, selectedDevice) : buildSemanticIndex(course),
    onSuccess: (result, action) => {
      setJobId(result.job_id);
      operation.current = { id: result.job_id, action: typeof action === "object" ? "cleanup" : action,
        target: typeof action === "object" ? action.target : undefined,
        device: typeof action === "object" ? action.keepDevice : selectedDevice };
      onJob?.(result.job_id);
      void client.invalidateQueries({ queryKey: ["semantic", "status"] });
      void client.invalidateQueries({ queryKey: ["recent-jobs"] });
    },
  });
  const stop = useMutation({
    mutationFn: (id: string) => cancelJob(id),
    onSuccess: (result, id) => client.setQueryData(["jobs", id], result),
  });
  useEffect(() => {
    if (!trackedId || !job.data || !isTaskTerminal(job.data) || finished.current === trackedId) return;
    finished.current = trackedId;
    if (job.data.status === "completed" && operation.current?.id === trackedId) {
      if (operation.current.action !== "index") setDevice(null);
      operation.current = null;
    }
    void client.invalidateQueries({ queryKey: ["semantic", "status"] });
    void client.invalidateQueries({ queryKey: ["semantic", "cache"] });
    void client.invalidateQueries({ queryKey: ["recent-jobs"] });
  }, [client, job.data, trackedId]);
  const running = Boolean(trackedId && (!job.data || !isTaskTerminal(job.data)));
  const busy = command.isPending || running || Boolean(status.data?.busy);
  const currentAction = operation.current?.id === trackedId ? operation.current.action : null;
  const cleaning = job.data?.job_type === "semantic_cleanup" || currentAction === "cleanup";
  const cleanupTarget = job.data?.cleanup_target
    ?? (currentAction === "cleanup" ? operation.current?.target : undefined);
  const cleanupLabel = cleanupTarget ? cleanupNames[cleanupTarget] : "RAG 缓存";
  const stopping = stop.isPending || (running && stop.isSuccess && stop.variables === trackedId)
    || job.data?.stage === "stopping";
  const preparing = running && !cleaning && job.data?.stage !== "indexing"
    && (["semantic_prepare", "semantic_setup"].includes(job.data?.job_type ?? "")
      || currentAction === "prepare" || currentAction === "setup");
  const preparingDevice = preparing ? job.data?.semantic_device
    ?? (operation.current?.id === trackedId ? operation.current.device : undefined) : undefined;
  const preparingName = preparingDevice ? preparingDevice === "cuda" ? "GPU" : "CPU" : "检索模型";
  useEffect(() => {
    if (job.data?.stage === "indexing") void client.invalidateQueries({ queryKey: ["semantic", "cache"] });
  }, [client, trackedId, job.data?.stage]);
  const error = command.error ?? stop.error;
  const coverage = status.data?.coverage;
  return <section className="settings-section semantic-settings" aria-label="课程检索">
    <div className="settings-section-heading"><div className="settings-section-title">
      <Search size={20} /><h2>课程检索</h2>
    </div></div>
    <div className="semantic-model-row">
      <div><strong>Qwen3-Embedding-0.6B</strong>
        <span className="settings-hint">{status.isPending ? "正在读取模型状态…"
          : running && cleaning ? `正在清理 ${cleanupLabel}`
          : preparing ? `正在准备 ${preparingName}`
          : status.data?.ready ? `当前使用 · ${status.data.device === "cuda" ? "GPU" : "CPU"}`
          : weightsReady ? "权重已下载 · 运行环境未准备" : "尚未准备"}</span>
      </div>
      <fieldset className="firered-device-selector semantic-device-selector" disabled={busy || !status.data}>
        <legend>处理设备</legend>
        <div className="firered-device-modes">
          {(["cuda", "cpu"] as const).map(value => {
            const unavailable = value === "cuda" && status.data?.gpu_detected === false;
            const Icon = value === "cuda" ? CircuitBoard : Cpu;
            const name = value === "cuda" ? "GPU" : "CPU";
            return <label key={value}
              className={`${status.data && selectedDevice === value ? "selected " : ""}${unavailable ? "unavailable" : ""}`}
              title={unavailable ? status.data?.gpu_unavailable_reason : undefined}>
              <input type="radio" name="semantic-device" value={value} aria-label={name}
                checked={Boolean(status.data) && selectedDevice === value} disabled={unavailable}
                onChange={() => { setDevice(value); command.reset(); }} />
              <Icon size={16} aria-hidden="true" /><span>{name}</span>
              <small>{unavailable ? "不可用" : status.data?.runtimes?.[value]?.ready ? "已准备" : ""}</small>
            </label>;
          })}
        </div>
      </fieldset>
      {(needsPreparation || needsActivation || preparing) && !(running && cleaning) && <button type="button" className="secondary-button compact"
        disabled={busy || !status.data || gpuUnavailable} onClick={() => command.mutate("prepare")}>
        {preparing ? <LoaderCircle size={15} className="spin" /> : needsPreparation ? <Download size={15} /> : <ArrowRightLeft size={15} />}
        {preparing ? `正在准备 ${preparingName}` : needsPreparation ? `下载并准备 ${deviceName}` : `切换到 ${deviceName}`}
      </button>}
    </div>
    {status.data && <p className="settings-hint semantic-device-status" role="status">
      {running && cleaning ? `正在清理 ${cleanupLabel}，完成后更新资源状态`
        : preparing ? job.data?.preparation?.label || `正在准备 ${preparingName}`
        : gpuUnavailable ? status.data.gpu_unavailable_reason || "未检测到可用的 NVIDIA 显卡或驱动"
        : `${deviceName} 环境${runtimeReady ? "已准备" : "尚未准备"} · ${weightsReady ? "权重已下载" : "权重尚未下载"}`}
    </p>}
    {status.data?.gpu_detected === false && !gpuUnavailable && <p className="settings-hint">
      {status.data.gpu_unavailable_reason || "未检测到可用的 NVIDIA 显卡及驱动"}
    </p>}
    {estimates && !(running && cleaning) && <div className="semantic-download-estimate" aria-label="预计下载大小">
      <strong>{estimates.total_bytes[selectedDevice] > 0
        ? `${preparing ? "本次准备预计下载约" : "预计还需下载约"} ${bytes(estimates.total_bytes[selectedDevice])}` : "所选设备无需下载"}</strong>
      <span>模型权重：{estimates.weights_bytes > 0 ? `约 ${bytes(estimates.weights_bytes)}` : "已下载"}</span>
      <span>{deviceName} 运行依赖：{estimates.runtimes[selectedDevice] > 0
        ? `约 ${bytes(estimates.runtimes[selectedDevice])}` : "已准备"}</span>
    </div>}
    {running && <div className="semantic-task" role="status">
      <div><LoaderCircle size={16} className="spin" /><span>{job.data?.message || "任务已提交，正在读取进度…"}</span>
        {!cleaning && <button type="button" className="secondary-button compact" disabled={stopping || !trackedId}
          onClick={() => stop.mutate(trackedId!)}><Square size={14} />
          {stopping ? "正在停止…" : "停止任务"}</button>}</div>
      {cleaning ? <ModelCleanupProgress job={job.data} connectionError={job.isError} label="RAG 清理进度" />
        : preparing ? <ModelPreparationProgress job={job.data} connectionError={job.isError} indeterminateLabel="进度暂不可估算" />
          : <progress max={100} value={job.data?.progress ?? 0} aria-label="课程检索任务进度" />}
    </div>}
    {!running && cleaning && job.data?.cleanup && <ModelCleanupProgress job={job.data} label="RAG 清理结果" />}
    {status.data?.busy && !running && <p className="settings-hint" role="status">
      后台有任务正在运行，请完成或停止后继续。
    </p>}
    {job.data && isTaskTerminal(job.data) && <p role={job.data.status === "failed" ? "alert" : "status"}
      className={job.data.status === "failed" ? "field-error" : "settings-hint"}>
      {job.data.error_message || `${taskLabel(job.data.status)} · ${job.data.message || ""}`}
    </p>}
    {job.isError && <p className="field-error" role="alert">无法读取任务进度
      <button className="text-button" type="button" onClick={() => void job.refetch()}>重新读取</button>
    </p>}
    {(status.isError || courses.isError) && <p className="field-error" role="alert">课程检索状态读取失败
      <button className="text-button" type="button" onClick={() => {
        void status.refetch(); void courses.refetch();
      }}><RefreshCw size={14} />重试</button>
    </p>}
    {error && <p className="field-error" role="alert">{error.message}</p>}
    <div className="semantic-cleanup-section">
      <div className="semantic-cache-heading"><div><h3>检索资源</h3>
        <p className="settings-hint">清理保留课程字幕、PPT 和已建索引。</p>
      </div><button type="button" className="secondary-button compact" disabled={busy || cache.isFetching}
        onClick={() => void cache.refetch()}><RefreshCw size={14} />刷新占用</button></div>
      <div className="semantic-cache-list" aria-label="RAG 缓存占用">
        {(["cuda", "cpu", "weights"] as const).map(target => {
          const item = target === "weights" ? cache.data?.weights : cache.data?.runtimes[target];
          const clearing = running && cleaning && (cleanupTarget === target || cleanupTarget === "all");
          const installing = preparing && preparingDevice === target;
          return <div className="semantic-cache-row" key={target} aria-label={`${cleanupNames[target]}缓存`}>
            <strong>{cleanupNames[target]}</strong>
            <div className="semantic-cache-state" role="status">
              {clearing ? <><span>正在清理</span><small>完成后更新剩余占用</small></>
                : installing ? <><span>正在准备</span><small>{job.data?.preparation?.label || "等待任务启动"}</small></>
                : item ? item.present ? <><span>{item.ready ? "已准备" : "未准备 · 有缓存"}</span>
                  <small>{bytes(item.bytes)} · {item.files.toLocaleString()} 个文件</small></> : <span>无</span>
                  : <span>{cache.isError ? "无法读取" : "正在统计…"}</span>}
            </div>
          </div>;
        })}
      </div>
      {cache.isError && <p className="field-error" role="alert">缓存占用读取失败
        <button type="button" className="text-button" onClick={() => void cache.refetch()}>重新读取</button></p>}
      <div className="semantic-resource-actions">
        {(["cuda", "cpu"] as const).map(keepDevice => {
          const name = keepDevice === "cuda" ? "GPU" : "CPU";
          const target = keepDevice === "cuda" ? "cpu" : "cuda";
          const other = keepDevice === "cuda" ? "CPU" : "GPU";
          const ready = weightsReady && Boolean(status.data?.runtimes?.[keepDevice]?.ready)
            && (keepDevice !== "cuda" || status.data?.gpu_detected === true);
          const available = ready && (cache.data?.runtimes[target].present || status.data?.device !== keepDevice);
          const Icon = keepDevice === "cuda" ? CircuitBoard : Cpu;
          return <div className="semantic-resource-action" key={keepDevice}>
            <button type="button" className="secondary-button compact" disabled={busy || !available}
              onClick={() => command.mutate({ action: "cleanup", target, keepDevice })}>
              <Icon size={15} /><span>仅使用 {name} 处理，清理 {other} 缓存</span></button>
            <p className="settings-hint">{!ready ? `未启用 ${name}`
              : !available ? `当前已仅使用 ${name}。` : `保留 ${name} 环境和共享模型文件。`}</p>
          </div>;
        })}
        <div className="semantic-resource-action">
          <button type="button" className="secondary-button compact" disabled={busy || !status.data || !cachePresent}
            onClick={() => command.mutate({ action: "cleanup", target: "all" })}>
            <Trash2 size={15} /><span>卸载 RAG（AI 检索功能能力可能下降）</span></button>
          <p className="settings-hint">清除 CPU、GPU 环境与模型文件，原文关键词检索仍可使用。</p>
        </div>
      </div>
    </div>
    <div className="semantic-index-section">
      <label className="field-label">课程范围
        <select aria-label="建立索引的课程" value={course} disabled={busy || courses.isPending}
          onChange={event => setCourse(event.target.value)}>
          <option value="">{courses.data?.length === 0 ? "暂无课程" : "选择课程"}</option>
          {courses.data?.map(item => <option value={item.course_id} key={item.course_id}>
            {item.path || item.name}（{item.with_subtitles}/{item.video_count} 个视频有字幕）
          </option>)}
        </select>
      </label>
      {!course && coverage && <p className="settings-hint">全库索引概况</p>}
      {coverage && <dl className="semantic-index-summary">
        <div><dt>已索引视频</dt><dd>{coverage.indexed_videos} / {coverage.with_subtitles}</dd></div>
        <div><dt>字幕片段</dt><dd>{coverage.chunk_count}</dd></div>
        <div><dt>待建索引</dt><dd>{coverage.pending_videos}</dd></div>
      </dl>}
      {course && coverage?.missing_subtitles ? <p className="settings-hint">
        {coverage.missing_subtitles} 个视频暂无字幕
      </p> : null}
      <button type="button" className="primary-button compact"
        disabled={busy || !status.data || !course || gpuUnavailable || !coverage?.with_subtitles}
        onClick={() => command.mutate(needsPreparation || needsActivation ? "setup" : "index")}><Database size={15} />
        {needsPreparation ? "准备并建立课程索引" : needsActivation ? "切换设备并检查索引"
          : coverage?.pending_videos ? "建立课程索引" : "检查并更新索引"}
      </button>
    </div>
  </section>;
}
