import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, Ban, CheckCircle2, ChevronLeft, ChevronRight, Clock3, Download, Film, Image as ImageIcon, LoaderCircle, MapPin, Presentation, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { createPortal } from "react-dom";
import { cancelJob, downloadSlidePdf, extractVideoSlides, getJob, getSlideImageUrl, getStreamUrl, getVideoSlides } from "../api/client";
import type { Job, Video, VideoSlide } from "../types";
import { canOpenReview, formatTime } from "../utils";
import { AnimatedOverlay, motion, useMotionTransition, useReducedMotion } from "./Motion";
import { useModalFocus } from "../hooks/useModalFocus";

export function PptViewer({
  video,
  libraryId,
  active,
  backgroundRef,
  onClose,
}: {
  video: Video | null;
  libraryId?: string;
  active: boolean;
  backgroundRef?: React.RefObject<HTMLElement>;
  onClose: () => void;
}) {
  const open = active && video !== null;
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const inlineVideoRef = useRef<HTMLVideoElement>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [inlineVideoTime, setInlineVideoTime] = useState(0);
  const [jobId, setJobId] = useState<string | number | null>(null);
  const [extractError, setExtractError] = useState<string | null>(null);
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const reduced = useReducedMotion();
  const transition = useMotionTransition(.18);
  const [viewMode, setViewMode] = useState<"timeline_states" | "preferred_pages">("preferred_pages");
  const [exportOpen, setExportOpen] = useState(false);
  const [exportMode, setExportMode] = useState<"timeline_states" | "preferred_pages">("preferred_pages");
  const slides = useQuery({
    queryKey: ["slides", video?.id, "viewer"],
    queryFn: () => getVideoSlides(video!.id, { include_auxiliary: true }),
    enabled: open,
    staleTime: 30_000,
    retry: false,
  });
  const extraction = useMutation({
    mutationFn: () => extractVideoSlides(video!.id),
    onSuccess: (result) => {
      const id = result.job_id ?? result.id;
      setJobId(id ?? null);
      setExtractError(null);
      if (id !== undefined && id !== null) queryClient.setQueryData(["jobs", id], result);
    },
    onError: (reason) => setExtractError(reason instanceof Error ? reason.message : "PPT 提取失败"),
  });
  const extractionJob = useQuery({
    queryKey: ["jobs", jobId],
    queryFn: () => getJob(jobId!),
    enabled: open && jobId !== null,
    refetchInterval: (query) => isTerminal(query.state.data?.status) ? false : 1000,
    retry: false,
  });
  const stopExtraction = useMutation({
    mutationFn: () => cancelJob(jobId!),
    onSuccess: (result) => queryClient.setQueryData(["jobs", jobId], result),
    onError: (reason) => setExtractError(reason instanceof Error ? reason.message : "停止提取失败"),
  });
  const exportPdf = useMutation({
    mutationFn: () => downloadSlidePdf(video!, exportMode),
    onSuccess: () => setExportOpen(false),
  });

  useModalFocus(open, dialogRef, closeRef, onClose, backgroundRef);

  const allItems = useMemo(() => [...(slides.data ?? [])].sort((a, b) => a.start_ms - b.start_ms), [slides.data]);
  const preferredItems = useMemo(() => allItems.filter((slide) => slide.is_llm_preferred), [allItems]);
  const items = viewMode === "preferred_pages" ? preferredItems : allItems;
  const selected = items.find((slide) => slide.id === selectedId) ?? items[0] ?? null;
  const selectedIndex = selected ? items.findIndex((slide) => slide.id === selected.id) : -1;
  const timelineDuration = Math.max(1, video?.duration_ms ?? 0, items.at(-1)?.end_ms ?? 0);
  const timeTicks = [0, 25, 50, 75, 100];
  const currentJob = extractionJob.data;
  const isExtracting = extraction.isPending || Boolean(currentJob && !isTerminal(currentJob.status));

  useEffect(() => {
    if (!currentJob || currentJob.status !== "completed") return;
    void queryClient.invalidateQueries({ queryKey: ["slides", video?.id] });
    void queryClient.invalidateQueries({ queryKey: ["videos"] });
    setJobId(null);
  }, [currentJob, queryClient, video?.id]);

  useEffect(() => {
    if (!open) {
      setSelectedId(null);
      setViewMode("preferred_pages");
      setExportOpen(false);
      return;
    }
    if (items.length && !items.some((slide) => slide.id === selectedId)) setSelectedId(items[0].id);
  }, [items, open, selectedId]);

  useEffect(() => {
    setViewMode("preferred_pages");
    setExportMode("preferred_pages");
    setExportOpen(false);
    setSelectedId(null);
  }, [video?.id]);

  const selectRelative = (offset: number) => {
    if (selectedIndex < 0 || items.length === 0) return;
    const next = Math.max(0, Math.min(items.length - 1, selectedIndex + offset));
    setSelectedId(items[next].id);
  };

  const seekInlineVideo = (slide: VideoSlide | null) => {
    if (!slide) return;
    const target = Math.max(0, slide.representative_ms / 1000);
    const element = inlineVideoRef.current;
    if (element) {
      const duration = Number.isFinite(element.duration) && element.duration > 0
        ? element.duration
        : (video?.duration_ms ?? 0) / 1000;
      element.currentTime = duration > 0 ? Math.min(target, duration) : target;
    }
    setInlineVideoTime(target);
  };

  useEffect(() => {
    if (!open) {
      inlineVideoRef.current?.pause();
      setSelectedId(null);
      setInlineVideoTime(0);
      return;
    }
    if (selected) seekInlineVideo(selected);
  }, [open, selected?.id]);

  const jumpToVideo = (slide: VideoSlide) => {
    if (!video || !canOpenReview(video.status, Boolean(video.has_transcript))) return;
    const query = new URLSearchParams({
      start_ms: String(slide.representative_ms),
      ...(libraryId ? { library_id: libraryId } : {}),
    });
    onClose();
    navigate(`/review/${video.id}?${query.toString()}`);
  };

  if (!open) return null;

  return createPortal((
    <AnimatedOverlay className="modal-backdrop ppt-viewer-backdrop" onClick={onClose}>
      <motion.div
        ref={dialogRef}
        className="ppt-viewer-dialog motion-surface"
        role="dialog"
        aria-modal="true"
        aria-labelledby="ppt-viewer-title"
        initial={{ opacity: 0, y: reduced ? 0 : 8, scale: reduced ? 1 : .99 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        exit={{ opacity: 0, y: reduced ? 0 : 8, scale: reduced ? 1 : .99 }}
        transition={transition}
        onClick={(event) => event.stopPropagation()}
      >
        <header className="ppt-viewer-header">
          <div className="ppt-viewer-heading">
            <Presentation size={19} aria-hidden="true" />
            <div>
              <h2 id="ppt-viewer-title">PPT 页面</h2>
              <p title={video.display_name}>{video.display_name}</p>
            </div>
          </div>
          <div className="ppt-viewer-header-meta">
            <span>{viewMode === "preferred_pages" ? `${items.length} 个精选页面` : `${items.length} 个时间状态`}</span>
            <div className="ppt-viewer-export">
              <button
                className="secondary-button compact"
                type="button"
                disabled={allItems.length === 0 || slides.isPending || slides.isError || exportPdf.isPending}
                aria-expanded={exportOpen}
                aria-controls="ppt-export-options"
                onClick={() => {
                  setExportOpen(!exportOpen);
                  setExportMode("preferred_pages");
                  exportPdf.reset();
                }}
              ><Download size={15} />导出 PDF</button>
              {exportOpen && (
                <div className="ppt-viewer-export-options" id="ppt-export-options">
                  <fieldset disabled={exportPdf.isPending}>
                    <legend>导出范围</legend>
                    <label><input type="radio" name="ppt-export-mode" value="preferred_pages"
                      checked={exportMode === "preferred_pages"}
                      onChange={() => setExportMode("preferred_pages")} />
                      PPT 页面精选 <span>{preferredItems.length} 页</span>
                    </label>
                    <label><input type="radio" name="ppt-export-mode" value="timeline_states"
                      checked={exportMode === "timeline_states"}
                      onChange={() => setExportMode("timeline_states")} />
                      PPT 时间轴状态 <span>{allItems.length} 页</span>
                    </label>
                  </fieldset>
                  {exportPdf.isError && <p role="alert">{exportPdf.error instanceof Error ? exportPdf.error.message : "PDF 导出失败，请重试"}</p>}
                  <button className="primary-button compact" type="button"
                    disabled={exportPdf.isPending || (exportMode === "preferred_pages" ? preferredItems.length : allItems.length) === 0}
                    onClick={() => exportPdf.mutate()}>
                    {exportPdf.isPending ? <LoaderCircle className="spin" size={15} /> : <Download size={15} />}
                    {exportPdf.isPending ? "正在导出…" : "下载 PDF"}
                  </button>
                </div>
              )}
            </div>
            <button ref={closeRef} className="icon-button" type="button" onClick={onClose} aria-label="关闭 PPT 查看器" title="关闭">
              <X size={19} />
            </button>
          </div>
        </header>

        {!slides.isPending && !slides.isError && items.length > 0 && (
          <section className="ppt-viewer-time-axis" aria-label={viewMode === "preferred_pages" ? "PPT 页面精选时间轴" : "PPT 时间轴状态"}>
            <div className="ppt-viewer-axis-labels" aria-hidden="true">
              {timeTicks.map((percent) => <span key={percent} style={{ left: `${percent}%` }}>{formatTime(timelineDuration * percent / 100)}</span>)}
            </div>
            <div className="ppt-viewer-axis-track">
              {items.map((slide, index) => {
                const left = Math.max(0, Math.min(100, slide.start_ms / timelineDuration * 100));
                const width = Math.max(.35, Math.min(100 - left, (slide.end_ms - slide.start_ms) / timelineDuration * 100));
                return <button
                  key={slide.id}
                  type="button"
                  className={`ppt-viewer-axis-segment ppt-retention-${slide.retention_class}${slide.id === selected?.id ? " is-selected" : ""}`}
                  style={{ left: `${left}%`, width: `${width}%` }}
                  aria-label={`页面 ${index + 1}，${formatTime(slide.start_ms)} 到 ${formatTime(slide.end_ms)}，${retentionLabel(slide.retention_class)}`}
                  aria-pressed={slide.id === selected?.id}
                  title={`页面 ${index + 1} · ${formatTime(slide.start_ms)} - ${formatTime(slide.end_ms)}`}
                  onClick={() => setSelectedId(slide.id)}
                ><span>{index + 1}</span></button>;
              })}
            </div>
            <div className="ppt-viewer-axis-caption"><span>视频开始</span><span>{formatTime(timelineDuration)} · 视频结束</span></div>
          </section>
        )}

        {slides.isPending && (
          <div className="ppt-viewer-state" role="status"><LoaderCircle className="spin" size={22} />正在读取 PPT 图片…</div>
        )}
        {slides.isError && (
          <div className="ppt-viewer-state is-error" role="alert">
            <AlertCircle size={22} />
            <div><strong>PPT 图片读取失败</strong><p>{slides.error instanceof Error ? slides.error.message : "请稍后重试。"}</p></div>
          </div>
        )}
        {!slides.isPending && !slides.isError && allItems.length > 0 && items.length === 0 && (
          <div className="ppt-viewer-state">
            <ImageIcon size={28} />
            <div>
              <strong>暂无 PPT 页面精选</strong>
              <p>当前提取结果还没有可供视觉分析的精选页面。</p>
              <button className="secondary-button compact" type="button"
                onClick={() => setViewMode("timeline_states")}>查看 PPT 时间轴状态</button>
            </div>
          </div>
        )}
        {!slides.isPending && !slides.isError && allItems.length === 0 && (
          <div className="ppt-viewer-state ppt-viewer-empty-state">
            <ImageIcon size={28} />
            <div>
              <strong>还没有提取出的 PPT 页面</strong>
              <p>可以直接从这里开始提取，完成后会自动显示 PPT 时间轴状态和页面精选。</p>
              <button className="primary-button compact" type="button" onClick={() => extraction.mutate()} disabled={isExtracting}>
                {isExtracting ? <LoaderCircle className="spin" size={15} /> : <Presentation size={15} />}
                {isExtracting ? "正在提取 PPT…" : "提取 PPT 页面"}
              </button>
              {extractError && <p className="field-error" role="alert">{extractError}</p>}
              {currentJob && <div className={`ppt-viewer-extract-job job-${currentJob.status}`}>
                <div><span>{jobStatusLabel(currentJob.status)}</span><b>{Math.round(Number(currentJob.progress ?? 0))}%</b></div>
                <div className="job-progress" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Number(currentJob.progress ?? 0)}><span style={{ width: `${Math.max(0, Math.min(100, Number(currentJob.progress ?? 0)))}%` }} /></div>
                {currentJob.message && <small>{currentJob.message}</small>}
                {!isTerminal(currentJob.status) && <button className="stop-job-button" type="button" onClick={() => stopExtraction.mutate()} disabled={stopExtraction.isPending}><Ban size={14} />{stopExtraction.isPending ? "正在停止…" : "停止提取"}</button>}
                {currentJob.status === "failed" && <button className="text-button" type="button" onClick={() => extraction.mutate()} disabled={isExtracting}><CheckCircle2 size={14} />重试提取</button>}
              </div>}
            </div>
          </div>
        )}
        {!slides.isPending && !slides.isError && selected && (
          <div className="ppt-viewer-body">
            <aside className="ppt-viewer-timeline" aria-label={viewMode === "preferred_pages" ? "PPT 页面精选" : "PPT 时间轴状态"}>
              <div className="ppt-viewer-timeline-heading">
                <div className="ppt-viewer-mode-switch" role="tablist" aria-label="PPT 查看模式">
                  <button
                    className={viewMode === "preferred_pages" ? "is-active" : ""}
                    type="button"
                    role="tab"
                    aria-selected={viewMode === "preferred_pages"}
                    onClick={() => setViewMode("preferred_pages")}
                  >页面精选</button>
                  <button
                    className={viewMode === "timeline_states" ? "is-active" : ""}
                    type="button"
                    role="tab"
                    aria-selected={viewMode === "timeline_states"}
                    onClick={() => setViewMode("timeline_states")}
                  >时间轴状态</button>
                </div>
                <span>{selectedIndex + 1} / {items.length}</span>
              </div>
              <div className="ppt-viewer-list">
                {items.map((slide, index) => (
                  <button
                    key={slide.id}
                    className={`ppt-viewer-item${slide.id === selected.id ? " is-selected" : ""}`}
                    type="button"
                    aria-pressed={slide.id === selected.id}
                    onClick={() => setSelectedId(slide.id)}
                  >
                    <span className="ppt-viewer-item-thumb">
                      <img src={getSlideImageUrl(slide)} alt="" loading={index < 3 ? "eager" : "lazy"} />
                    </span>
                    <span className="ppt-viewer-item-copy">
                      <strong>页面 {index + 1}</strong>
                      <span>{formatTime(slide.start_ms)} - {formatTime(slide.end_ms)}</span>
                      <small className={`ppt-retention-${slide.retention_class}`}>{retentionLabel(slide.retention_class)}</small>
                    </span>
                  </button>
                ))}
              </div>
            </aside>

            <section className="ppt-viewer-preview" aria-label={`页面 ${selectedIndex + 1} 预览`}>
              <div className="ppt-viewer-media-grid">
                <div className="ppt-viewer-video-panel">
                  <div className="ppt-viewer-media-heading">
                    <span><Film size={15} aria-hidden="true" />视频画面</span>
                    <time>{formatTime(inlineVideoTime * 1000)}</time>
                  </div>
                  <video
                    ref={inlineVideoRef}
                    className="ppt-viewer-video"
                    src={getStreamUrl(video.id)}
                    controls
                    playsInline
                    preload="metadata"
                    aria-label="当前 PPT 对应的视频画面"
                    onLoadedMetadata={() => seekInlineVideo(selected)}
                    onTimeUpdate={(event) => setInlineVideoTime(event.currentTarget.currentTime)}
                  >
                    你的浏览器不支持视频播放。
                  </video>
                </div>
                <div className="ppt-viewer-image-wrap">
                  <div className="ppt-viewer-media-heading">
                    <span><Presentation size={15} aria-hidden="true" />PPT 截图</span>
                    <time>{formatTime(selected.representative_ms)}</time>
                  </div>
                  <img src={getSlideImageUrl(selected)} alt={`PPT 页面 ${selectedIndex + 1}`} />
                </div>
              </div>
              <div className="ppt-viewer-details">
                <div className="ppt-viewer-page-title">
                  <div><span className={`ppt-retention-badge ppt-retention-${selected.retention_class}`}>{retentionLabel(selected.retention_class)}</span><strong>页面 {selectedIndex + 1}</strong></div>
                  <span className="ppt-viewer-confidence">置信度 {Math.round(selected.confidence * 100)}%</span>
                </div>
                <div className="ppt-viewer-time-grid">
                  <span><Clock3 size={14} />开始 <b>{formatTime(selected.start_ms)}</b></span>
                  <span><Clock3 size={14} />结束 <b>{formatTime(selected.end_ms)}</b></span>
                  <span><Clock3 size={14} />展示 <b>{formatTime(selected.duration_ms)}</b></span>
                  <span><Clock3 size={14} />截图时刻 <b>{formatTime(selected.representative_ms)}</b></span>
                  <span><Clock3 size={14} />完整状态 <b>{formatTime(selected.visible_from_ms)} - {formatTime(selected.visible_until_ms)}</b></span>
                </div>
                <div className="ppt-viewer-actions">
                  <div className="ppt-viewer-nav">
                    <button className="secondary-button compact" type="button" onClick={() => selectRelative(-1)} disabled={selectedIndex <= 0}><ChevronLeft size={15} />上一页</button>
                    <button className="secondary-button compact" type="button" onClick={() => selectRelative(1)} disabled={selectedIndex < 0 || selectedIndex >= items.length - 1}>下一页<ChevronRight size={15} /></button>
                  </div>
                  <button className="primary-button compact" type="button" onClick={() => jumpToVideo(selected)} disabled={!canOpenReview(video.status, Boolean(video.has_transcript))} title={canOpenReview(video.status, Boolean(video.has_transcript)) ? "跳转到视频对应时间" : "视频转写完成后可定位"}>
                    <MapPin size={15} />定位到视频
                  </button>
                </div>
              </div>
            </section>
          </div>
        )}
      </motion.div>
    </AnimatedOverlay>
  ), document.body);
}

function retentionLabel(value: VideoSlide["retention_class"]) {
  return value === "primary" ? "主要页面" : value === "auxiliary" ? "辅助页面" : "动画状态";
}

function isTerminal(status?: string) {
  return status ? ["completed", "failed", "interrupted", "cancelled", "canceled"].includes(status) : false;
}

function jobStatusLabel(status: string) {
  return ({ queued: "排队中", running: "提取中", completed: "提取完成", failed: "提取失败", interrupted: "已停止", cancelled: "已取消", canceled: "已取消" } as Record<string, string>)[status] ?? status;
}
