import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import { AlertCircle, CheckCircle2, LoaderCircle, X } from "lucide-react";
import { useShortcuts, matchesShortcut } from "../shortcuts";
import { getApiBaseUrl, getAnalysisRuns } from "../api/client";
import { TopBar } from "../components/TopBar";
import { useReviewTabs } from "../components/ReviewTabs";
import { parseSearchSeek } from "../searchNavigation";
import { ReviewSidebar } from "../components/ReviewSidebar";
import { VideoPanel } from "../components/VideoPanel";
import { useModalFocus } from "../hooks/useModalFocus";
import { useMarkerImportanceMutation, useMarkerJumpTimeMutation, useMarkerStatusMutation, useStudyData } from "../hooks/useStudyData";
import type { Marker, MarkerMode, ReviewFilter, ReviewStatus } from "../types";
import { canOpenReview, captureJumpTime, filterMarkers, formatTime, markerJumpTime } from "../utils";
import { AnimatePresence, AnimatedOverlay, AnimatedPopup, motion, useMotionTransition, useReducedMotion } from "../components/Motion";

export function ReviewPage({ demo = false }: { demo?: boolean }) {
  const params = useParams();
  const videoId = demo ? 1 : Number(params.videoId);
  return <ReviewSession key={`${demo ? "demo" : "api"}:${videoId}`} videoId={videoId} demo={demo} />;
}

function ReviewSession({ videoId, demo }: { videoId: number; demo: boolean }) {
  const shortcuts = useShortcuts();
  const location = useLocation();
  const mockMode = demo || import.meta.env.VITE_USE_MOCK === "true";
  const navigate = useNavigate();
  const study = useStudyData(videoId || 0, mockMode);
  const { video, subtitles, markers: allMarkers, errors, isLoading } = study;
  const runs = useQuery({
    queryKey: ["study", "analysis-runs", videoId], queryFn: () => getAnalysisRuns(videoId),
    enabled: !mockMode && Boolean(video), staleTime: 30_000,
  });
  const reviewTabs = useReviewTabs();
  useEffect(() => {
    if (!mockMode && !isLoading && video && canOpenReview(video.status, subtitles.length > 0)) reviewTabs?.register(video);
  }, [mockMode, isLoading, video, subtitles.length, reviewTabs?.register]);
  const markerMutation = useMarkerStatusMutation(videoId || 0, mockMode);
  const jumpTimeMutation = useMarkerJumpTimeMutation(videoId || 0, mockMode);
  const importanceMutation = useMarkerImportanceMutation(videoId || 0, mockMode);
  const videoRef = useRef<HTMLVideoElement>(null);
  const [mode, setMode] = useState<MarkerMode>("medium_recall");
  const initializedModeVideo = useRef<number | null>(null);
  const [status, setStatus] = useState<ReviewFilter>("all");
  const [viewedMarkerIds, setViewedMarkerIds] = useState<ReadonlySet<number>>(() => new Set());
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(video ? video.duration_ms / 1000 : 0);
  const [selectedMarkerId, setSelectedMarkerId] = useState<number | null>(null);
  const lastChosenMarkerId = useRef<number | null>(null);
  const jumpTimeSavingRef = useRef(false);
  const [editingMarkerId, setEditingMarkerId] = useState<number | null>(null);
  const [lastRemoved, setLastRemoved] = useState<{ marker: Marker; previous: ReviewStatus } | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const reviewBackground = useRef<HTMLDivElement>(null);
  const reviewDialog = useRef<HTMLElement>(null);
  const reviewDialogClose = useRef<HTMLButtonElement>(null);
  const reviewDialogMotion = useMotionTransition(.18);
  const reduceReviewMotion = useReducedMotion();
  useModalFocus(settingsOpen, reviewDialog, reviewDialogClose, () => setSettingsOpen(false), reviewBackground);
  const [notice, setNotice] = useState<string | null>(null);
  const [jumpTimeError, setJumpTimeError] = useState<string | null>(null);

  const visibleMarkers = useMemo(
    () => filterMarkers(allMarkers, mode, status, viewedMarkerIds),
    [allMarkers, mode, status, viewedMarkerIds],
  );
  useEffect(() => {
    if (isLoading || (!mockMode && runs.isPending) || initializedModeVideo.current === videoId) return;
    const initial = (["medium_recall", "low_recall", "high_recall", "strict_exam_intent"] as const)
      .find((candidate) => allMarkers.some((marker) => marker.analysis_mode === candidate));
    setMode(initial ?? allMarkers.find(marker => marker.analysis_mode.startsWith("custom_"))?.analysis_mode
      ?? runs.data?.find(run => run.analysis_mode.startsWith("custom_"))?.analysis_mode ?? "medium_recall");
    initializedModeVideo.current = videoId;
  }, [allMarkers, isLoading, videoId, runs.isPending, runs.data, mockMode]);
  const excludedMarkers = useMemo(
    () => allMarkers.filter((marker) => marker.review_status === "excluded").sort((a, b) => a.start_ms - b.start_ms),
    [allMarkers],
  );

  const seekTo = useCallback((seconds: number, markerId?: number) => {
    if (markerId !== undefined) {
      lastChosenMarkerId.current = markerId;
      setSelectedMarkerId(markerId);
      setViewedMarkerIds((previous) => previous.has(markerId) ? previous : new Set([...previous, markerId]));
    }
    if (!videoRef.current) return;
    videoRef.current.currentTime = seconds;
    setCurrentTime(seconds);
    videoRef.current.play().catch(() => undefined);
  }, []);

  const updateStatus = (marker: Marker, nextStatus: ReviewStatus) => {
    if (nextStatus === "excluded") {
      setLastRemoved({ marker, previous: marker.review_status });
      setNotice(`已移除“${marker.title}”`);
    } else {
      setNotice("已恢复重点");
    }
    setSelectedMarkerId(marker.id);
    markerMutation.mutate({ marker, status: nextStatus });
  };

  const undoRemove = () => {
    if (!lastRemoved) return;
    markerMutation.mutate({ marker: lastRemoved.marker, status: lastRemoved.previous });
    setNotice("已撤销移除");
    setLastRemoved(null);
  };

  const moveToMarker = useCallback((direction: -1 | 1) => {
    const index = visibleMarkers.findIndex((marker) => marker.id === selectedMarkerId);
    const nextIndex = index < 0 ? (direction === 1 ? 0 : -1) : index + direction;
    const marker = visibleMarkers[nextIndex];
    if (!marker) return false;
    seekTo(markerJumpTime(marker) / 1000, marker.id);
    return true;
  }, [visibleMarkers, selectedMarkerId, seekTo]);

  const saveJumpTime = useCallback(async (marker: Marker, userStartMs: number | null) => {
    if (jumpTimeSavingRef.current) throw new Error("正在保存，请稍后重试");
    jumpTimeSavingRef.current = true;
    setJumpTimeError(null);
    try {
      await jumpTimeMutation.mutateAsync({ marker, userStartMs });
      setNotice(userStartMs === null ? "已恢复原始跳转时间" : `跳转时间已保存为 ${formatTime(userStartMs)}`);
    } finally {
      jumpTimeSavingRef.current = false;
    }
  }, [jumpTimeMutation.mutateAsync]);

  useEffect(() => {
    if (isLoading || !video || (!mockMode && !canOpenReview(video.status, subtitles.length > 0))) return;
    const onMarkerShortcut = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.repeat || event.isComposing
        || settingsOpen || editingMarkerId !== null || jumpTimeMutation.isPending || jumpTimeSavingRef.current) return;
      const action = (["previousMarker", "nextMarker", "captureTime"] as const).find(id => matchesShortcut(event, shortcuts, id));
      if (!action) return;
      const target = event.target instanceof Element ? event.target : null;
      if (target?.closest('input[type="range"]') && ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown"].includes(event.key)) return;
      if (target?.closest('input:not([type="range"]), [data-shortcut-recorder], .video-caption, .review-tabs, textarea, select, [contenteditable]:not([contenteditable="false"]), [role="textbox"], [role="combobox"], [role="listbox"], [role="menu"], [role="dialog"]')
        || [...document.querySelectorAll('.audio-channel-popover, .subtitle-settings-popover, [role="dialog"][aria-modal="true"]')]
          .some((element) => !element.closest('[aria-hidden="true"], [inert], [hidden]'))) return;
      if (action === "captureTime") {
        const marker = allMarkers.find((item) => item.id === lastChosenMarkerId.current && item.review_status !== "excluded");
        if (!marker) return;
        event.preventDefault();
        setNotice(null);
        const time = videoRef.current?.currentTime;
        if (time === undefined || !Number.isFinite(time)) {
          setJumpTimeError("无法读取当前播放位置，请稍后重试");
          return;
        }
        const durationMs = (duration > 0 ? duration : video.duration_ms / 1000) * 1000;
        void saveJumpTime(marker, captureJumpTime(time * 1000, durationMs)).catch((failure: unknown) => {
          setJumpTimeError(failure instanceof Error ? failure.message : "保存失败，请重试");
        });
        return;
      }
      if (moveToMarker(action === "previousMarker" ? -1 : 1)) event.preventDefault();
    };
    document.addEventListener("keydown", onMarkerShortcut);
    return () => document.removeEventListener("keydown", onMarkerShortcut);
  }, [isLoading, video, mockMode, subtitles.length, settingsOpen, editingMarkerId, jumpTimeMutation.isPending, moveToMarker, allMarkers, duration, saveJumpTime, shortcuts]);

  if (isLoading) {
    return <div className="app-shell"><TopBar /><div className="loading-screen"><LoaderCircle className="spin" size={24} /><span>正在准备复习工作台…</span></div></div>;
  }

  if (!video) {
    return (
      <div className="app-shell"><TopBar /><main className="error-page">
        <div className="error-panel" role="alert">
          <AlertCircle size={22} />
          <div>
            <strong>复习页面无法加载</strong>
            <p>{errorMessage(errors.video ?? study.error)}</p>
            <span>真实 API 错误不会自动替换为 mock 数据。</span>
          </div>
        </div>
        <div className="error-actions"><Link className="secondary-button" to="/">返回视频库</Link></div>
      </main></div>
    );
  }

  if (!mockMode && !canOpenReview(video.status, subtitles.length > 0)) {
    return (
      <div className="app-shell"><TopBar /><main className="error-page">
        <div className="error-panel" role="alert">
          <AlertCircle size={22} />
          <div><strong>该视频尚未完成转写</strong><p>字幕未就绪或正在重新转写。</p></div>
        </div>
        <div className="error-actions"><Link className="secondary-button" to="/">返回视频库</Link></div>
      </main></div>
    );
  }

  const libraryId = video.library_id ?? (mockMode ? "demo-library" : (import.meta.env.VITE_LIBRARY_ID ?? "default-library"));
  const searchSeek = mockMode ? undefined : parseSearchSeek(location.search, location.key, libraryId);

  return (
    <div className="app-shell">
      <div ref={reviewBackground}>
      <TopBar video={video} onOpenSettings={() => setSettingsOpen(true)} />
      <main className="study-page">
        <div className="study-grid">
          <div className="study-main">
            {!mockMode && video.status !== "review_ready" && <div className="local-error" role="status"><AlertCircle size={15} /><span>字幕已保存，AI 重点尚未完成。</span><Link to={`/process?videoId=${video.id}`}>继续处理</Link></div>}
            {errors.video && <LocalError label="视频详情" error={errors.video} />}
            <VideoPanel
              key={`${libraryId}:${videoId}`}
              video={video}
              subtitles={subtitles}
              videoId={videoId}
              libraryId={libraryId}
              navigationSeek={searchSeek}
              autoPlay={location.state?.completedVideoId === videoId && Boolean(location.state?.completedJobId)}
              demo={mockMode}
              currentTime={currentTime}
              onTimeUpdate={(time) => {
                setCurrentTime(time);
                // Timeline movement must not replace the marker chosen by clicking or navigation.
                if (searchSeek || editingMarkerId !== null || lastChosenMarkerId.current !== null) return;
                const selected = visibleMarkers.find((marker) => marker.id === selectedMarkerId);
                if (selected?.user_start_ms != null) {
                  const end = Math.max(selected.end_ms, markerJumpTime(selected) + selected.end_ms - selected.start_ms);
                  if (time * 1000 >= markerJumpTime(selected) && time * 1000 < end) return;
                }
                const current = visibleMarkers.find((marker) => time * 1000 >= marker.start_ms && time * 1000 < marker.end_ms);
                if (current) setSelectedMarkerId(current.id);
              }}
              onDurationChange={setDuration}
              videoRef={videoRef}
            />
            {errors.subtitles && <LocalError label="字幕" error={errors.subtitles} />}
          </div>
          <div>
            {errors.markers && <LocalError label="重点" error={errors.markers} />}
            <ReviewSidebar
              analysisRuns={runs.data}
              analysisVideoId={mockMode ? undefined : videoId}
              mode={mode}
              onModeChange={value => { initializedModeVideo.current = videoId; setMode(value); }}
              status={status}
              onStatusChange={setStatus}
              markers={visibleMarkers}
              allMarkers={allMarkers}
              excludedMarkers={excludedMarkers}
              selectedMarkerId={selectedMarkerId}
              viewedMarkerIds={viewedMarkerIds}
              onSelectMarker={(marker) => seekTo(markerJumpTime(marker) / 1000, marker.id)}
              editingMarkerId={editingMarkerId}
              timeSaving={jumpTimeMutation.isPending}
              onEditMarker={setEditingMarkerId}
              durationMs={(duration > 0 ? duration : video.duration_ms / 1000) * 1000}
              currentTimeMs={currentTime * 1000}
              getCurrentTimeMs={() => (videoRef.current?.currentTime ?? currentTime) * 1000}
              onSaveTime={saveJumpTime}
              onChangeImportance={async (marker, importance) => { await importanceMutation.mutateAsync({ marker, importance }); }}
              onChangeStatus={updateStatus}
              onUndo={undoRemove}
              canUndo={Boolean(lastRemoved)}
              onRestoreExcluded={(marker) => updateStatus(marker, "active")}
              onPrevious={() => moveToMarker(-1)}
              onNext={() => moveToMarker(1)}
            />
          </div>
        </div>
      </main>
      </div>
      <AnimatePresence>{notice && (
        <AnimatedPopup key="notice" duration={.18} className="toast" role="status">
          <CheckCircle2 size={17} /><span>{notice}</span>
          <button type="button" onClick={() => setNotice(null)} aria-label="关闭提示"><X size={15} /></button>
        </AnimatedPopup>
      )}</AnimatePresence>
      <AnimatePresence>{jumpTimeError && (
        <AnimatedPopup key="time-error" duration={.18} className="toast" role="alert">
          <AlertCircle size={17} /><span>{jumpTimeError}</span>
          <button type="button" onClick={() => setJumpTimeError(null)} aria-label="关闭错误提示"><X size={15} /></button>
        </AnimatedPopup>
      )}</AnimatePresence>
      <AnimatePresence>{settingsOpen && (
        <AnimatedOverlay className="modal-backdrop" role="presentation" onClick={() => setSettingsOpen(false)}>
          <motion.section ref={reviewDialog} initial={{ y: reduceReviewMotion ? 0 : 4 }} animate={{ y: 0 }} exit={{ y: reduceReviewMotion ? 0 : 4 }} transition={reviewDialogMotion}
            className="settings-dialog motion-surface" role="dialog" aria-modal="true" aria-labelledby="settings-title" onClick={(event) => event.stopPropagation()}>
            <div className="dialog-heading">
              <div><p className="eyebrow">复习设置</p><h2 id="settings-title">复习偏好</h2></div>
              <button ref={reviewDialogClose} className="icon-button" type="button" onClick={() => setSettingsOpen(false)} aria-label="关闭设置"><X size={18} /></button>
            </div>
            <div className="settings-list">
              <div><span>视频编号</span><strong>{video.id}</strong></div>
              <div><span>视频状态</span><strong>{video.status}</strong></div>
              <div><span>数据来源</span><strong>{mockMode ? "显式 /demo mock" : getApiBaseUrl()}</strong></div>
              <div><span>播放记忆</span><strong>已启用</strong></div>
            </div>
            <button className="secondary-button" type="button" onClick={() => navigate("/")}>返回视频库</button>
          </motion.section>
        </AnimatedOverlay>
      )}</AnimatePresence>
      {markerMutation.isPending && <div className="saving-indicator"><LoaderCircle className="spin" size={14} /> 正在保存</div>}
      {markerMutation.isError && <div className="inline-error"><AlertCircle size={14} /> 状态保存失败，请重试</div>}
    </div>
  );
}

function LocalError({ label, error }: { label: string; error: unknown }) {
  return <div className="local-error" role="status"><AlertCircle size={15} /><span>{label}加载失败：{errorMessage(error)}</span></div>;
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "请求失败，请检查本地服务。";
}
