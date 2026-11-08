import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useLocation, useParams } from "react-router-dom";
import { AlertCircle, Bookmark, BookmarkPlus, CheckCircle2, LoaderCircle, RotateCcw, X } from "lucide-react";
import { useShortcuts, matchesShortcut, isShortcutInput, isUndoShortcut, isRedoShortcut } from "../shortcuts";
import { getAnalysisRuns } from "../api/client";
import { TopBar } from "../components/TopBar";
import { useReviewTabs } from "../components/ReviewTabs";
import { parseSearchSeek } from "../searchNavigation";
import { ReviewSidebar } from "../components/ReviewSidebar";
import { BilibiliStudyPanel } from "../components/BilibiliStudyPanel";
import { VideoPanel, type PlayerTimeline } from "../components/VideoPanel";
import { captureVideoFrame } from "../utils/captureVideoFrame";
import { captureQuestionImage, getQuestionVisual, setQuestionImage, type ImageSelection,
  type QuestionVisual } from "../api/questionVisuals";
import { useCreateManualQuestionMutation, useMarkerImportanceMutation, useMarkerJumpTimeMutation, useMarkerStatusMutation, useQuestionEmphasisMutation, useRenameManualQuestionMutation, useStudyData } from "../hooks/useStudyData";
import type { Marker, MarkerImportance, MarkerMode, ReviewFilter, ReviewStatus } from "../types";
import { canOpenReview, captureJumpTime, filterMarkers, formatTime, markerJumpTime } from "../utils";
import { AnimatePresence, AnimatedPopup } from "../components/Motion";
import { useStudyProgress } from "../hooks/useStudyProgress";
import { StudyBookmarksDialog, type BookmarkScope } from "../components/StudyBookmarks";
import { CodexVideoClip } from "../components/CodexVideoClip";

type ReviewChange =
  | { kind: "time"; previous: number | null; next: number | null }
  | { kind: "title"; previous: string; next: string }
  | { kind: "importance"; previous: MarkerImportance; next: MarkerImportance }
  | { kind: "emphasis"; previous: boolean | null; next: boolean | null }
  | { kind: "image"; previous: ImageSelection; next: ImageSelection };
type ReviewUndoAction = { sequence: number; marker: Marker } & (
  | { kind: "create"; previous: ReviewStatus }
  | { kind: "remove"; previous: ReviewStatus }
  | ReviewChange
);
const undoLabels: Record<ReviewUndoAction["kind"], string> = {
  create: "已新增手动题目", remove: "重点已移除", time: "已调整跳转时间",
  title: "已修改题目名称", importance: "已修改重要程度", emphasis: "已修改题目强调",
  image: "已修改题图",
};

export function ReviewPage({ demo = false }: { demo?: boolean }) {
  const params = useParams();
  const videoId = demo ? 1 : Number(params.videoId);
  return <ReviewSession key={`${demo ? "demo" : "api"}:${videoId}`} videoId={videoId} demo={demo} />;
}

function ReviewSession({ videoId, demo }: { videoId: number; demo: boolean }) {
  const shortcuts = useShortcuts();
  const location = useLocation();
  const mockMode = demo || import.meta.env.VITE_USE_MOCK === "true";
  const study = useStudyData(videoId || 0, mockMode);
  const queryClient = useQueryClient();
  const { video, subtitles, markers: allMarkers, errors, isLoading } = study;
  const savedStudy = useStudyProgress(video?.library_id, videoId, !mockMode && !isLoading && Boolean(video && canOpenReview(video.status, subtitles.length > 0)));
  const [bookmarkScope, setBookmarkScope] = useState<BookmarkScope | null>(null);
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
  const questionEmphasisMutation = useQuestionEmphasisMutation(videoId || 0, mockMode);
  const createQuestionMutation = useCreateManualQuestionMutation(videoId || 0, mockMode);
  const renameQuestionMutation = useRenameManualQuestionMutation(videoId || 0, mockMode);
  const videoRef = useRef<HTMLVideoElement>(null);
  const timelineRef = useRef<PlayerTimeline>(null);
  const getPlayerTime = useCallback(() => timelineRef.current?.getCurrentTime() ?? videoRef.current?.currentTime, []);
  const captureFrame = useCallback(async () => {
    if (timelineRef.current && !timelineRef.current.isReady()) throw new Error("正在切换视角，请等待画面加载后再截图");
    const time = getPlayerTime();
    const frame = await captureVideoFrame(videoRef.current);
    return { ...frame, captured_at_ms: time === undefined ? frame.captured_at_ms : Math.round(time * 1000) };
  }, [getPlayerTime]);
  const [mode, setMode] = useState<MarkerMode>("medium_recall");
  const initializedModeVideo = useRef<number | null>(null);
  const [status, setStatus] = useState<ReviewFilter>("all");
  const [viewedMarkerIds, setViewedMarkerIds] = useState<ReadonlySet<number>>(() => new Set());
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(video ? video.duration_ms / 1000 : 0);
  const [selectedMarkerId, setSelectedMarkerId] = useState<number | null>(null);
  const lastChosenMarkerId = useRef<number | null>(null);
  const jumpTimeSavingRef = useRef(false);
  const creatingQuestionRef = useRef(false);
  const changingStatusRef = useRef(false);
  const imageSavingRef = useRef(false);
  const [imageSaving, setImageSaving] = useState(false);
  const undoingRef = useRef(false);
  const nextUndoSequence = useRef(0);
  const pendingEditsRef = useRef(0);
  const [pendingEdits, setPendingEdits] = useState(0);
  const undoHistoryRef = useRef<ReviewUndoAction[]>([]);
  const [undoHistory, setUndoHistory] = useState<ReviewUndoAction[]>([]);
  const redoHistoryRef = useRef<ReviewUndoAction[]>([]);
  const [redoHistory, setRedoHistory] = useState<ReviewUndoAction[]>([]);
  const [undoing, setUndoing] = useState(false);
  const [createdMarkerId, setCreatedMarkerId] = useState<number | null>(null);
  const [editingMarkerId, setEditingMarkerId] = useState<number | null>(null);
  const [bilibiliMarkerId, setBilibiliMarkerId] = useState<number | null>(null);
  const reviewBackground = useRef<HTMLDivElement>(null);
  const [noticeState, setNoticeState] = useState<{ text: string | null }>({ text: null });
  const notice = noticeState.text;
  const setNotice = useCallback((text: string | null) => setNoticeState({ text }), []);
  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(null), 3000);
    return () => window.clearTimeout(timer);
  }, [noticeState, setNotice]);
  const [jumpTimeError, setJumpTimeError] = useState<string | null>(null);

  const visibleMarkers = useMemo(
    () => filterMarkers(allMarkers, mode, status, viewedMarkerIds),
    [allMarkers, mode, status, viewedMarkerIds],
  );
  useEffect(() => {
    if (isLoading || (!mockMode && runs.isPending) || initializedModeVideo.current === videoId) return;
    const initial = (["medium_recall", "low_recall", "high_recall", "question_extraction", "strict_exam_intent"] as const)
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
    if (timelineRef.current) {
      timelineRef.current.seekTo(seconds, true);
      return;
    }
    videoRef.current.currentTime = seconds;
    setCurrentTime(seconds);
    videoRef.current.play().catch(() => undefined);
  }, []);

  const replaceRedoHistory = useCallback((history: ReviewUndoAction[]) => {
    redoHistoryRef.current = history;
    setRedoHistory(history);
  }, []);

  const replaceUndoHistory = useCallback((history: ReviewUndoAction[], preserveRedo = false) => {
    undoHistoryRef.current = [...history].sort((a, b) => a.sequence - b.sequence).slice(-50);
    setUndoHistory(undoHistoryRef.current);
    if (!preserveRedo) replaceRedoHistory([]);
  }, [replaceRedoHistory]);

  const updateStatus = useCallback(async (marker: Marker, nextStatus: ReviewStatus) => {
    if (changingStatusRef.current || creatingQuestionRef.current || undoingRef.current
      || pendingEditsRef.current || marker.review_status === nextStatus) return;
    const sequence = ++nextUndoSequence.current;
    changingStatusRef.current = true;
    setJumpTimeError(null);
    try {
      await markerMutation.mutateAsync({ marker, status: nextStatus });
      if (nextStatus === "excluded") {
        replaceUndoHistory([...undoHistoryRef.current, { sequence, kind: "remove", marker, previous: marker.review_status }]);
        setNotice(`已移除“${marker.title}”`);
      } else {
        // Explicit restoration already reverses that marker's removal.
        replaceUndoHistory(undoHistoryRef.current.filter(action => action.kind !== "remove" || action.marker.id !== marker.id));
        setNotice("已恢复重点");
      }
      setSelectedMarkerId(marker.id);
    } catch (failure) {
      setJumpTimeError(`状态保存失败：${errorMessage(failure)}`);
    } finally {
      changingStatusRef.current = false;
    }
  }, [markerMutation.mutateAsync, replaceUndoHistory]);

  const publishQuestionImage = useCallback((marker: Marker, value: QuestionVisual) => {
    queryClient.setQueryData(["question-visual", marker.id], value);
    for (const queryKey of [["question-visuals", marker.video_id], ["question", marker.id], ["bilibili", marker.id]])
      void queryClient.invalidateQueries({ queryKey });
  }, [queryClient]);

  const applyChange = useCallback(async (marker: Marker, change: ReviewChange, undo = false) => {
    switch (change.kind) {
      case "time":
        await jumpTimeMutation.mutateAsync({ marker, userStartMs: undo ? change.previous : change.next });
        break;
      case "title":
        await renameQuestionMutation.mutateAsync({ marker, title: undo ? change.previous : change.next });
        break;
      case "importance":
        await importanceMutation.mutateAsync({ marker, importance: undo ? change.previous : change.next });
        break;
      case "emphasis":
        await questionEmphasisMutation.mutateAsync({ marker, emphasized: undo ? change.previous : change.next });
        break;
      case "image":
        publishQuestionImage(marker, await setQuestionImage(marker.id, undo ? change.previous : change.next));
        break;
    }
  }, [jumpTimeMutation.mutateAsync, renameQuestionMutation.mutateAsync,
    importanceMutation.mutateAsync, questionEmphasisMutation.mutateAsync, publishQuestionImage]);

  const saveChange = useCallback(async (marker: Marker, change: ReviewChange) => {
    if (undoingRef.current || creatingQuestionRef.current || changingStatusRef.current) throw new Error("正在保存，请稍后重试");
    if (change.previous === change.next) return;
    const sequence = ++nextUndoSequence.current;
    pendingEditsRef.current += 1;
    setPendingEdits(pendingEditsRef.current);
    setJumpTimeError(null);
    try {
      await applyChange(marker, change);
      replaceUndoHistory([...undoHistoryRef.current, { sequence, marker, ...change }]);
    } finally {
      pendingEditsRef.current -= 1;
      setPendingEdits(pendingEditsRef.current);
    }
  }, [applyChange, replaceUndoHistory]);

  const captureQuestion = useCallback(async (marker: Marker) => {
    if (imageSavingRef.current || undoingRef.current || creatingQuestionRef.current || changingStatusRef.current) return;
    const sequence = ++nextUndoSequence.current;
    imageSavingRef.current = true;
    setImageSaving(true);
    pendingEditsRef.current += 1;
    setPendingEdits(pendingEditsRef.current);
    setJumpTimeError(null);
    try {
      const frame = await captureFrame();
      const result = await captureQuestionImage(marker.id, frame);
      publishQuestionImage(marker, result.visual);
      replaceUndoHistory([...undoHistoryRef.current, { sequence, marker, kind: "image",
        previous: result.previous_image,
        next: { capture_id: result.visual.binding!.capture_id, binding_mode: "manual" } }]);
      setNotice(`题图已保存 · ${formatTime(frame.captured_at_ms)}`);
    } catch (failure) {
      setJumpTimeError(`截图保存失败：${errorMessage(failure)}`);
    } finally {
      imageSavingRef.current = false;
      setImageSaving(false);
      pendingEditsRef.current -= 1;
      setPendingEdits(pendingEditsRef.current);
    }
  }, [publishQuestionImage, replaceUndoHistory, captureFrame]);

  const removeQuestionImage = useCallback(async (marker: Marker) => {
    if (imageSavingRef.current || undoingRef.current || creatingQuestionRef.current || changingStatusRef.current) return;
    const sequence = ++nextUndoSequence.current;
    imageSavingRef.current = true;
    setImageSaving(true);
    pendingEditsRef.current += 1;
    setPendingEdits(pendingEditsRef.current);
    try {
      const current = await getQuestionVisual(marker.id);
      const next: ImageSelection = { capture_id: null, binding_mode: "manual" };
      const saved = await setQuestionImage(marker.id, next);
      publishQuestionImage(marker, saved);
      replaceUndoHistory([...undoHistoryRef.current, { sequence, marker, kind: "image",
        previous: { capture_id: current.binding?.capture_id ?? null, binding_mode: current.binding_mode ?? "auto" }, next }]);
      setNotice("已取消题图");
    } catch (failure) {
      setJumpTimeError(`取消题图失败：${errorMessage(failure)}`);
    } finally {
      imageSavingRef.current = false;
      setImageSaving(false);
      pendingEditsRef.current -= 1;
      setPendingEdits(pendingEditsRef.current);
    }
  }, [publishQuestionImage, replaceUndoHistory]);

  const undoReviewAction = useCallback(async (redo = false) => {
    const action = (redo ? redoHistoryRef : undoHistoryRef).current.at(-1);
    if (!action || undoingRef.current || changingStatusRef.current || creatingQuestionRef.current
      || pendingEditsRef.current || jumpTimeSavingRef.current) return;
    undoingRef.current = true;
    setUndoing(true);
    setJumpTimeError(null);
    const marker = allMarkers.find(item => item.id === action.marker.id) ?? action.marker;
    try {
      if (action.kind === "create" || action.kind === "remove") {
        await markerMutation.mutateAsync({ marker, status: redo
          ? action.kind === "create" ? action.marker.review_status : "excluded"
          : action.kind === "create" ? "excluded" : action.previous });
      } else {
        await applyChange(marker, action, !redo);
      }
      if (redo) {
        replaceRedoHistory(redoHistoryRef.current.slice(0, -1));
        replaceUndoHistory([...undoHistoryRef.current, action], true);
        setMode(marker.analysis_mode);
        setStatus("all");
        setSelectedMarkerId(marker.id);
        if (action.kind === "create") lastChosenMarkerId.current = marker.id;
        setNotice("已重做" + (action.kind === "create" ? "新增手动题目" : action.kind === "remove" ? "移除" : undoLabels[action.kind].slice(1)));
      } else {
        replaceUndoHistory(undoHistoryRef.current.slice(0, -1), true);
        replaceRedoHistory([...redoHistoryRef.current, action]);
      }
      if (!redo && action.kind === "create") {
        setCreatedMarkerId(current => current === marker.id ? null : current);
        setSelectedMarkerId(current => current === marker.id ? null : current);
        if (lastChosenMarkerId.current === marker.id) lastChosenMarkerId.current = null;
        setNotice("已撤销新增手动题目");
      } else if (!redo) {
        setMode(marker.analysis_mode);
        setStatus("all");
        setSelectedMarkerId(marker.id);
        setNotice(action.kind === "remove" ? "已撤销移除" : `已撤销${undoLabels[action.kind].slice(1)}`);
      }
    } catch (failure) {
      setJumpTimeError(`${redo ? "重做" : "撤销"}失败：${errorMessage(failure)}，可再次重试`);
    } finally {
      undoingRef.current = false;
      setUndoing(false);
    }
  }, [allMarkers, markerMutation.mutateAsync, applyChange, replaceUndoHistory, replaceRedoHistory]);

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
      await saveChange(marker, { kind: "time", previous: marker.user_start_ms ?? null, next: userStartMs });
      setNotice(userStartMs === null ? "已恢复原始跳转时间" : `跳转时间已保存为 ${formatTime(userStartMs)}`);
    } finally {
      jumpTimeSavingRef.current = false;
    }
  }, [saveChange]);

  useEffect(() => {
    if (createdMarkerId === null) return;
    const timer = window.setTimeout(() => setCreatedMarkerId(null), 1800);
    return () => window.clearTimeout(timer);
  }, [createdMarkerId]);

  const createQuestion = useCallback(async () => {
    if (creatingQuestionRef.current || changingStatusRef.current || undoingRef.current || pendingEditsRef.current) return;
    const sequence = ++nextUndoSequence.current;
    const time = getPlayerTime();
    if (time === undefined || !Number.isFinite(time) || time < 0) {
      setJumpTimeError("无法读取当前播放位置，请稍后重试");
      return;
    }
    const durationMs = Math.floor((duration > 0 ? duration : (video?.duration_ms ?? 0) / 1000) * 1000);
    const timeMs = Math.max(0, Math.min(Math.round(time * 1000), durationMs > 0 ? durationMs - 1 : Infinity));
    creatingQuestionRef.current = true;
    setJumpTimeError(null);
    try {
      const created = await createQuestionMutation.mutateAsync(timeMs);
      replaceUndoHistory([...undoHistoryRef.current, { sequence, kind: "create", marker: created, previous: "excluded" }]);
      initializedModeVideo.current = videoId;
      setMode("question_extraction");
      setStatus("all");
      setSelectedMarkerId(created.id);
      lastChosenMarkerId.current = created.id;
      setCreatedMarkerId(created.id);
      setNotice(`已新增手动题目 · ${formatTime(created.start_ms)}`);
    } catch (failure) {
      setJumpTimeError(failure instanceof Error ? failure.message : "新增失败，请重试");
    } finally {
      creatingQuestionRef.current = false;
    }
  }, [createQuestionMutation.mutateAsync, duration, video?.duration_ms, videoId, replaceUndoHistory, getPlayerTime]);

  useEffect(() => {
    if (isLoading || !video || (!mockMode && !canOpenReview(video.status, subtitles.length > 0))) return;
    const onMarkerShortcut = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.repeat || event.isComposing
        || editingMarkerId !== null || jumpTimeMutation.isPending || jumpTimeSavingRef.current) return;
      const action = isUndoShortcut(event) ? "undoReview"
        : isRedoShortcut(event) ? "redoReview"
        : (["previousMarker", "nextMarker", "captureTime", "createQuestion"] as const).find(id => matchesShortcut(event, shortcuts, id));
      if (!action) return;
      const target = event.target instanceof Element ? event.target : null;
      if (target?.closest('input[type="range"]') && ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown"].includes(event.key)) return;
      if (target?.closest('input:not([type="range"]), [data-shortcut-recorder], .video-caption, .review-tabs, textarea, select, [contenteditable]:not([contenteditable="false"]), [role="textbox"], [role="combobox"], [role="listbox"], [role="menu"], [role="dialog"]')
        || [...document.querySelectorAll('.audio-channel-popover, .subtitle-settings-popover, .secondary-view-popover, .player-speed-menu, [role="dialog"][aria-modal="true"]')]
          .some((element) => !element.closest('[aria-hidden="true"], [inert], [hidden]'))) return;
      if (action === "undoReview" || action === "redoReview") {
        if (isShortcutInput(event.target) || !(action === "redoReview" ? redoHistoryRef : undoHistoryRef).current.length || undoingRef.current
          || pendingEditsRef.current || changingStatusRef.current || creatingQuestionRef.current || renameQuestionMutation.isPending
          || importanceMutation.isPending || questionEmphasisMutation.isPending) return;
        event.preventDefault();
        void undoReviewAction(action === "redoReview");
        return;
      }
      if (action === "createQuestion") {
        event.preventDefault();
        void createQuestion();
        return;
      }
      if (action === "captureTime") {
        const marker = allMarkers.find((item) => item.id === lastChosenMarkerId.current && item.review_status !== "excluded");
        if (!marker) return;
        event.preventDefault();
        setNotice(null);
        const time = getPlayerTime();
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
  }, [isLoading, video, mockMode, subtitles.length, editingMarkerId, jumpTimeMutation.isPending,
    renameQuestionMutation.isPending, importanceMutation.isPending, questionEmphasisMutation.isPending,
    moveToMarker, allMarkers, duration, saveJumpTime, createQuestion, undoReviewAction, shortcuts, getPlayerTime]);

  if (isLoading) {
    return <div className="app-shell"><TopBar demo={mockMode} /><div className="loading-screen"><LoaderCircle className="spin" size={24} /><span>正在准备复习工作台…</span></div></div>;
  }

  if (!video) {
    return (
      <div className="app-shell"><TopBar demo={mockMode} /><main className="error-page">
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
      <div className="app-shell"><TopBar demo={mockMode} /><main className="error-page">
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
      <TopBar video={video} demo={mockMode} backgroundRef={reviewBackground} />
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
              timelineRef={timelineRef}
              studyProgressReady={savedStudy.ready}
              studyPositionMs={savedStudy.position}
              studyObservedAt={savedStudy.observedAt}
              onStudyProgress={savedStudy.save}
            />
            {!mockMode && video.library_id && <div className="review-study-tools">
              <CodexVideoClip videoId={videoId} libraryId={libraryId} title={video.display_name}
                durationMs={video.duration_ms} getCurrentTimeMs={() => (getPlayerTime() ?? currentTime) * 1000} />
              <button type="button" className="secondary-button compact" aria-haspopup="dialog" onClick={() => {
                const time = getPlayerTime();
                if (!timelineRef.current?.isReady() || time === undefined || !Number.isFinite(time)) {
                  setJumpTimeError("无法读取当前播放位置，请等待视频加载后重试"); return;
                }
                setBookmarkScope({ title: video.display_name, videoId, positionMs: Math.round(time * 1000), create: true });
              }}><BookmarkPlus size={15} />添加书签</button>
              <button type="button" className="text-button compact" aria-haspopup="dialog" onClick={() => setBookmarkScope({
                title: video.display_name, videoId,
              })}><Bookmark size={15} />本视频书签</button>
              {savedStudy.error && <span className="study-progress-error" role="status">续看记录同步失败
                <button type="button" className="icon-button" title="重试同步续看记录" aria-label="重试同步续看记录" onClick={savedStudy.retry}><RotateCcw size={14} /></button>
              </span>}
            </div>}
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
              createdMarkerId={createdMarkerId}
              creatingQuestion={Boolean(createQuestionMutation.isPending || markerMutation.isPending || undoing || pendingEdits)}
              onCreateQuestion={() => void createQuestion()}
              onRenameQuestion={(marker, title) => saveChange(marker, { kind: "title", previous: marker.title, next: title })}
              viewedMarkerIds={viewedMarkerIds}
              onSelectMarker={(marker) => seekTo(markerJumpTime(marker) / 1000, marker.id)}
              editingMarkerId={editingMarkerId}
              timeSaving={Boolean(jumpTimeMutation.isPending || markerMutation.isPending || createQuestionMutation.isPending || undoing)}
              onEditMarker={setEditingMarkerId}
              durationMs={(duration > 0 ? duration : video.duration_ms / 1000) * 1000}
              currentTimeMs={currentTime * 1000}
              getCurrentTimeMs={() => (getPlayerTime() ?? currentTime) * 1000}
              onSaveTime={saveJumpTime}
              onChangeImportance={(marker, importance) => saveChange(marker, { kind: "importance", previous: marker.importance ?? "standard", next: importance })}
              onChangeQuestionEmphasis={(marker, emphasized) => saveChange(marker, { kind: "emphasis", previous: marker.question_emphasis_override ?? null, next: emphasized })}
              onSearchBilibili={mockMode ? undefined : marker => setBilibiliMarkerId(marker.id)}
              onCaptureQuestion={mockMode ? undefined : captureQuestion}
              onRemoveQuestionImage={mockMode ? undefined : removeQuestionImage}
              imageSaving={imageSaving || undoing}
              onChangeStatus={(marker, nextStatus) => void updateStatus(marker, nextStatus)}
              onUndo={() => void undoReviewAction()}
              canUndo={undoHistory.length > 0}
              onRedo={() => void undoReviewAction(true)}
              canRedo={redoHistory.length > 0}
              historyVersion={`${undoHistory.length}:${undoHistory.at(-1)?.sequence ?? ""}:${redoHistory.length}:${redoHistory.at(-1)?.sequence ?? ""}`}
              undoLabel={undoLabels[undoHistory.at(-1)?.kind ?? "remove"]}
              undoing={Boolean(undoing || pendingEdits || markerMutation.isPending || createQuestionMutation.isPending)}
              onRestoreExcluded={(marker) => void updateStatus(marker, "active")}
              onPrevious={() => moveToMarker(-1)}
              onNext={() => moveToMarker(1)}
            />
          </div>
        </div>
      </main>
      </div>
      {!mockMode && allMarkers.some(marker => marker.id === bilibiliMarkerId) && <BilibiliStudyPanel
        key={bilibiliMarkerId} marker={allMarkers.find(marker => marker.id === bilibiliMarkerId)!}
        onCaptureFrame={captureFrame}
        backgroundRef={reviewBackground} onClose={() => setBilibiliMarkerId(null)} />}
      <AnimatePresence>{bookmarkScope && video.library_id && <StudyBookmarksDialog key={`${video.library_id}:${videoId}`}
        libraryId={video.library_id} scope={bookmarkScope}
        onClose={() => setBookmarkScope(null)} onSeek={position => seekTo(position / 1000)} />}</AnimatePresence>
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
