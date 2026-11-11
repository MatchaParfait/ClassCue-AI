import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Ban, CheckCircle2, Crop, Image as ImageIcon, LoaderCircle, Presentation, RotateCcw, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type PointerEvent } from "react";
import { cancelJob, extractVideoSlides, getVideoSlides, getStreamUrl, getJob } from "../api/client";
import type { Job, Video } from "../types";
import { SlideDeviceControl } from "./SlideDeviceControl";

export function SlideExtractionControls({
  video,
  active,
  busy,
  showSettingsLink = true,
}: {
  video: Video;
  active: boolean;
  busy: boolean;
  showSettingsLink?: boolean;
}) {
  const queryClient = useQueryClient();
  const [roi, setRoi] = useState<PixelRegion | null>(null);
  const [deviceState, setDeviceState] = useState({ blocked: true, preparing: false });
  const [ignoreRegions, setIgnoreRegions] = useState<PixelRegion[]>([]);
  const [replaceConfirmed, setReplaceConfirmed] = useState(false);
  const loadedVideo = useRef<number | null>(null);
  const [videoSize, setVideoSize] = useState({ width: 0, height: 0 });
  const [selecting, setSelecting] = useState<"roi" | "ignore" | null>(null);
  const [selectionStart, setSelectionStart] = useState<Point | null>(null);
  const [selectionCurrent, setSelectionCurrent] = useState<Point | null>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const [jobId, setJobId] = useState<string | number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const slides = useQuery({
    queryKey: ["slides", video.id],
    queryFn: () => getVideoSlides(video.id, { include_auxiliary: true }),
    enabled: active,
    staleTime: 30_000,
    retry: false,
  });
  const job = useQuery({
    queryKey: ["jobs", jobId],
    queryFn: () => getJob(jobId!),
    enabled: active && jobId !== null,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status && isTerminal(status) ? false : 1000;
    },
    retry: false,
  });
  const extraction = useMutation({
    mutationFn: (force: boolean) => extractVideoSlides(video.id, {
      force,
      ignore_regions: ignoreRegions,
      ...(roi ? { roi } : {}),
    }),
    onSuccess: (result) => {
      const id = result.job_id ?? result.id;
      setJobId(id ?? null);
      setError(null);
      queryClient.setQueryData(["jobs", id], result);
    },
    onError: (reason) => setError(reason instanceof Error ? reason.message : "PPT 提取失败"),
  });
  const cancel = useMutation({
    mutationFn: () => cancelJob(jobId!),
    onSuccess: (result) => queryClient.setQueryData(["jobs", jobId], result),
    onError: (reason) => setError(reason instanceof Error ? reason.message : "停止任务失败"),
  });
  const currentJob = job.data;
  const terminal = Boolean(currentJob && isTerminal(currentJob.status));
  const isRunning = deviceState.preparing || extraction.isPending || Boolean(currentJob && !terminal);
  const slideCount = slides.data?.length ?? 0;
  const primaryCount = slides.data?.filter((slide) => slide.retention_class === "primary").length ?? 0;
  useEffect(() => {
    if (loadedVideo.current === video.id || !slides.data) return;
    loadedVideo.current = video.id;
    const saved = slides.data[0];
    setRoi(saved?.roi ?? null);
    setIgnoreRegions(saved?.ignore_regions ?? []);
    setReplaceConfirmed(false);
  }, [slides.data, video.id]);
  useEffect(() => {
    if (currentJob?.status !== "completed") return;
    void queryClient.invalidateQueries({ queryKey: ["slides", video.id] });
    void queryClient.invalidateQueries({ queryKey: ["videos"] });
    setReplaceConfirmed(false);
  }, [currentJob?.status, queryClient, video.id]);
  const selectionBox = useMemo(() => {
    if (!selectionStart || !selectionCurrent) return null;
    return {
      left: Math.min(selectionStart.x, selectionCurrent.x),
      top: Math.min(selectionStart.y, selectionCurrent.y),
      width: Math.abs(selectionStart.x - selectionCurrent.x),
      height: Math.abs(selectionStart.y - selectionCurrent.y),
    };
  }, [selectionCurrent, selectionStart]);

  const startSelection = (event: PointerEvent<HTMLDivElement>) => {
    if (!selecting || busy || isRunning) return;
    const point = relativePoint(event.currentTarget, event);
    event.currentTarget.setPointerCapture(event.pointerId);
    setSelectionStart(point);
    setSelectionCurrent(point);
  };
  const moveSelection = (event: PointerEvent<HTMLDivElement>) => {
    if (!selectionStart) return;
    setSelectionCurrent(relativePoint(event.currentTarget, event));
  };
  const finishSelection = (event: PointerEvent<HTMLDivElement>) => {
    if (!selectionStart) return;
    const point = relativePoint(event.currentTarget, event);
    const left = Math.min(selectionStart.x, point.x);
    const top = Math.min(selectionStart.y, point.y);
    const width = Math.abs(selectionStart.x - point.x);
    const height = Math.abs(selectionStart.y - point.y);
    setSelectionStart(null);
    setSelectionCurrent(null);
    const mode = selecting;
    setSelecting(null);
    if (width < 0.03 || height < 0.03) {
      return;
    }
    if (!videoSize.width || !videoSize.height) return;
    const widthPixels = videoSize.width;
    const heightPixels = videoSize.height;
    const region = {
      x: Math.round(left * widthPixels),
      y: Math.round(top * heightPixels),
      width: Math.max(1, Math.round(width * widthPixels)),
      height: Math.max(1, Math.round(height * heightPixels)),
    };
    if (mode === "ignore") setIgnoreRegions((current) => [...current, region].slice(0, 16));
    else setRoi(region);
  };

  const start = (force = false) => {
    if (isRunning || busy || deviceState.blocked || slides.isPending || slides.isError || (slideCount > 0 && !replaceConfirmed)) return;
    setError(null);
    extraction.mutate(force);
  };

  return (
    <section className="slide-extraction-panel" aria-label="PPT 页面提取">
      <div className="section-heading compact">
        <div>
          <p className="eyebrow">视觉证据</p>
          <h2><Presentation size={18} />提取 PPT 页面</h2>
        </div>
        <span className="process-section-step"><ImageIcon size={15} /></span>
      </div>
      <p className="settings-hint">
        将录屏中的稳定 PPT 画面保存为带时间范围的本地图片，供后续考点视觉分析使用。
      </p>
      <div className="slide-extraction-summary">
        <span><strong>{slideCount}</strong> 个稳定画面</span>
        <span><strong>{primaryCount}</strong> 个主要页面</span>
        {roi && <span>已设置 PPT 区域</span>}
      </div>
      <SlideDeviceControl active={active} busy={busy || extraction.isPending || Boolean(currentJob && !terminal)}
        onStateChange={setDeviceState} showSettingsLink={showSettingsLink} />
      <div className="slide-extraction-actions">
        <button className="secondary-button compact" type="button" disabled={busy || isRunning}
          onClick={() => setSelecting((value) => value === "roi" ? null : "roi")}>
          <Crop size={15} />{selecting === "roi" ? "取消框选" : "选择 PPT 区域"}
        </button>
        <button className="secondary-button compact" type="button" disabled={busy || isRunning || ignoreRegions.length >= 16}
          onClick={() => setSelecting((value) => value === "ignore" ? null : "ignore")}>
          <Ban size={15} />{selecting === "ignore" ? "取消排除区域" : "排除干扰区域"}
        </button>
        <button className="primary-button compact" type="button" disabled={busy || isRunning || deviceState.blocked || slides.isPending || slides.isError || (slideCount > 0 && !replaceConfirmed)}
          onClick={() => start(slideCount > 0)}>
          {isRunning ? <LoaderCircle className="spin" size={15} /> : <Presentation size={15} />}
          {deviceState.preparing ? "正在准备…" : isRunning ? "正在提取…" : slideCount ? "重新提取 PPT" : "提取 PPT 页面"}
        </button>
      </div>
      {slides.isError && <p className="field-error" role="alert">
        PPT 结果读取失败
        <button type="button" className="text-button" onClick={() => void slides.refetch()}>重试</button>
      </p>}
      {slideCount > 0 && <label className="slide-roi-note">
        <input type="checkbox" checked={replaceConfirmed} disabled={busy || isRunning}
          onChange={(event) => setReplaceConfirmed(event.target.checked)} />
        确认替换已有 PPT 截图
      </label>}
      {selecting && <div
        className="slide-region-selector"
        style={videoSize.width ? { aspectRatio: `${videoSize.width} / ${videoSize.height}` } : undefined}
        onPointerDown={startSelection}
        onPointerMove={moveSelection}
        onPointerUp={finishSelection}
        onPointerCancel={() => { setSelectionStart(null); setSelectionCurrent(null); }}
      >
        <video ref={videoRef} src={getStreamUrl(video.id)} preload="metadata" muted controls playsInline
          aria-label="选择 PPT 区域" onLoadedMetadata={(event) => {
            setVideoSize({ width: event.currentTarget.videoWidth, height: event.currentTarget.videoHeight });
          }} />
        <div className="slide-region-selector-hint">{selecting === "ignore" ? "排除干扰区域" : "PPT 区域"}</div>
        {selectionBox && <div className="slide-region-selection" style={{
          left: `${selectionBox.left * 100}%`,
          top: `${selectionBox.top * 100}%`,
          width: `${selectionBox.width * 100}%`,
          height: `${selectionBox.height * 100}%`,
        }} />}
      </div>}
      {roi && <div className="slide-roi-note">
        已选择区域：{roi.width} × {roi.height} 像素
        <button type="button" className="text-button" disabled={busy || isRunning} onClick={() => setRoi(null)}>恢复整幅画面</button>
      </div>}
      {ignoreRegions.map((region, index) => <div className="slide-roi-note" key={index}>
        排除区域 {index + 1}：{region.width} × {region.height} 像素
        <button type="button" className="icon-button" disabled={busy || isRunning}
          aria-label={`删除排除区域 ${index + 1}`} title="删除排除区域"
          onClick={() => setIgnoreRegions((current) => current.filter((_, at) => at !== index))}><X size={14} /></button>
      </div>)}
      {currentJob && <div className={`slide-extraction-job job-${currentJob.status}`}>
        <div className="job-status-heading">
          <div className="job-status-title">
            {currentJob.status === "completed" ? <CheckCircle2 size={18} /> : currentJob.status === "failed" ? <RotateCcw size={18} /> : <LoaderCircle className="spin" size={18} />}
            <strong>{jobStatusLabel(currentJob.status)}</strong>
          </div>
          <span>{Math.round(Number(currentJob.progress ?? 0))}%</span>
        </div>
        <div className="job-progress" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Number(currentJob.progress ?? 0)}>
          <span style={{ width: `${Math.max(0, Math.min(100, Number(currentJob.progress ?? 0)))}%` }} />
        </div>
        <div className="job-stage-row"><span>{stageLabel(currentJob.stage)}</span><span>{currentJob.message || "等待后台更新"}</span></div>
        {currentJob.error_message && <p className="field-error" role="alert">{currentJob.error_message}</p>}
        {!terminal && <button className="stop-job-button" type="button" onClick={() => cancel.mutate()} disabled={cancel.isPending}>
          <Ban size={14} />{cancel.isPending ? "正在停止…" : "停止提取"}
        </button>}
        {terminal && currentJob.status === "completed" && <button className="text-button" type="button" onClick={() => {
          void queryClient.invalidateQueries({ queryKey: ["slides", video.id] });
        }}>重新读取结果</button>}
      </div>}
      {error && <p className="field-error" role="alert">{error}</p>}
    </section>
  );
}

interface Point { x: number; y: number }
interface PixelRegion extends Point { width: number; height: number }

function relativePoint(element: HTMLElement, event: PointerEvent<HTMLElement>): Point {
  const rect = element.getBoundingClientRect();
  return {
    x: Math.max(0, Math.min(1, (event.clientX - rect.left) / Math.max(rect.width, 1))),
    y: Math.max(0, Math.min(1, (event.clientY - rect.top) / Math.max(rect.height, 1))),
  };
}

function isTerminal(status: string) {
  return ["completed", "failed", "interrupted", "cancelled", "canceled"].includes(status);
}

function jobStatusLabel(status: string) {
  return ({ queued: "排队中", running: "提取中", completed: "提取完成", failed: "提取失败", interrupted: "已停止", cancelled: "已取消", canceled: "已取消" } as Record<string, string>)[status] ?? status;
}

function stageLabel(stage?: string) {
  return ({ reading: "读取视频", detecting: "检测 PPT 变化", selecting: "精选页面", encoding: "保存截图", saving: "保存数据库", completed: "已完成" } as Record<string, string>)[stage || ""] || "等待处理";
}
