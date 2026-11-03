import { useShortcuts, shortcutTitle, shortcutAria } from "../shortcuts";
import { AnimatePresence, AnimatedListItem, AnimatedQuote, Collapse, FadeRegion, SelectionIndicator, useEntryMotion, useSelectionScope } from "./Motion";
import { ArrowUpRight, Check, ChevronDown, ChevronLeft, ChevronRight, Circle, LoaderCircle, Pencil, Plus, Redo2, Save, Search, Star, Trash2, Undo2, X } from "lucide-react";
import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import type { AnalysisRun, Marker, MarkerImportance, MarkerMode, RecallMode, ReviewFilter, ReviewStatus } from "../types";
import { recallLabel, useRecallSchemes } from "../hooks/useRecallSchemes";
import { formatTime, isManualQuestion, markerJumpTime } from "../utils";
import { RECALL_MODES } from "../processOptions";
import { MarkerTimeEditor } from "./MarkerTimeEditor";
import { MarkerImportanceControl } from "./MarkerImportanceControl";
import { RecallAnalysisPanel } from "./RecallAnalysisPanel";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { assignQuestionImages, getQuestionVisuals } from "../api/questionVisuals";
import { QuestionVisualButton } from "./QuestionVisualButton";
import "./ReviewModeGroups.css";

interface ReviewSidebarProps {
  analysisVideoId?: number;
  analysisRuns?: AnalysisRun[];
  mode: MarkerMode;
  onModeChange: (mode: MarkerMode) => void;
  status: ReviewFilter;
  onStatusChange: (status: ReviewFilter) => void;
  markers: Marker[];
  allMarkers: Marker[];
  excludedMarkers: Marker[];
  selectedMarkerId: number | null;
  createdMarkerId?: number | null;
  creatingQuestion?: boolean;
  onCreateQuestion?: () => void;
  onRenameQuestion?: (marker: Marker, title: string) => Promise<void>;
  viewedMarkerIds: ReadonlySet<number>;
  onSelectMarker: (marker: Marker) => void;
  onChangeStatus: (marker: Marker, status: ReviewStatus) => void;
  onUndo: () => void;
  canUndo: boolean;
  onRedo?: () => void;
  canRedo?: boolean;
  undoLabel?: string;
  undoing?: boolean;
  onRestoreExcluded: (marker: Marker) => void;
  onPrevious: () => void;
  onNext: () => void;
  editingMarkerId: number | null;
  timeSaving: boolean;
  onEditMarker: (markerId: number | null) => void;
  durationMs: number;
  currentTimeMs: number;
  getCurrentTimeMs: () => number;
  onSaveTime: (marker: Marker, timeMs: number | null) => Promise<void>;
  onChangeImportance: (marker: Marker, importance: MarkerImportance) => Promise<void>;
  onChangeQuestionEmphasis?: (marker: Marker, emphasized: boolean) => Promise<void>;
  onSearchBilibili?: (marker: Marker) => void;
  onCaptureQuestion?: (marker: Marker) => Promise<void>;
  onRemoveQuestionImage?: (marker: Marker) => Promise<void>;
  imageSaving?: boolean;
}

export function ReviewSidebar({
  analysisVideoId,
  analysisRuns = [],
  mode,
  onModeChange,
  status,
  onStatusChange,
  markers,
  allMarkers,
  excludedMarkers,
  selectedMarkerId,
  createdMarkerId,
  creatingQuestion = false,
  onCreateQuestion,
  onRenameQuestion,
  viewedMarkerIds,
  onSelectMarker,
  onChangeStatus,
  onUndo,
  canUndo,
  onRedo,
  canRedo = false,
  undoLabel = "重点已移除",
  undoing = false,
  onRestoreExcluded,
  onPrevious,
  onNext,
  editingMarkerId,
  timeSaving,
  onEditMarker,
  durationMs,
  currentTimeMs,
  getCurrentTimeMs,
  onSaveTime,
  onChangeImportance,
  onChangeQuestionEmphasis,
  onSearchBilibili,
  onCaptureQuestion,
  onRemoveQuestionImage,
  imageSaving = false,
}: ReviewSidebarProps) {
  const shortcuts = useShortcuts();
  const schemes = useRecallSchemes(Boolean(analysisVideoId));
  const queryClient = useQueryClient();
  const visuals = useQuery({
    queryKey: ["question-visuals", analysisVideoId], queryFn: () => getQuestionVisuals(analysisVideoId!),
    enabled: Boolean(analysisVideoId) && mode === "question_extraction",
    retry: false, refetchInterval: query => query.state.data?.active ? 1500 : 10000,
    refetchIntervalInBackground: false,
  });
  const [imageError, setImageError] = useState<string | null>(null);
  const questionIds = allMarkers.filter(marker => marker.analysis_mode === "question_extraction")
    .map(marker => marker.id).join(",");
  useEffect(() => {
    if (!analysisVideoId || mode !== "question_extraction" || visuals.data?.active !== false) return;
    let current = true;
    setImageError(null);
    void assignQuestionImages(analysisVideoId).then(value => {
      if (current) queryClient.setQueryData(["question-visuals", analysisVideoId], value);
    }).catch((error: unknown) => {
      if (current) setImageError(error instanceof Error ? error.message : "自动配图失败");
    });
    return () => { current = false; };
  }, [analysisVideoId, mode, visuals.data?.active, questionIds, queryClient]);
  const customIds = [...new Set([
    ...(schemes.data ?? []).filter(item => !item.archived).map(item => item.id),
    ...allMarkers.filter(marker => marker.analysis_mode.startsWith("custom_")).map(marker => marker.analysis_mode as RecallMode),
    ...analysisRuns.filter(run => run.analysis_mode.startsWith("custom_")).map(run => run.analysis_mode as RecallMode),
  ])];
  const label = (id: MarkerMode) => recallLabel(id, schemes.data)
    === "自定义方案" ? analysisRuns.find(run => run.analysis_mode === id)?.scheme_name ?? "自定义方案" : recallLabel(id, schemes.data);
  const countForMode = (value: MarkerMode) => allMarkers.filter(marker =>
    marker.analysis_mode === value && marker.review_status !== "excluded").length;
  const builtinSelected = RECALL_MODES.some(item => item.value === mode);
  const customSelected = customIds.includes(mode as RecallMode);
  const modeButton = (value: MarkerMode, name: string) => (
    <button key={value} role="tab" title={name} aria-selected={mode === value}
      className={mode === value ? "is-selected" : ""} type="button" onClick={() => onModeChange(value)}
      onKeyDown={navigateModes}>
      <span>{name}</span><small>{countForMode(value)}</small>
    </button>
  );
  const currentRun = analysisRuns.find(run => run.analysis_mode === mode);
  const outdated = mode.startsWith("custom_") && Boolean(currentRun && (currentRun.latest_scheme_revision ?? 0) > (currentRun.scheme_revision ?? 0));
  const sidebarRef = useRef<HTMLElement>(null);
  useEntryMotion(sidebarRef, { duration: .14 });
  const selectionScope = useSelectionScope();
  const [expandedQuotes, setExpandedQuotes] = useState<ReadonlySet<number>>(new Set());
  const [editingTitleId, setEditingTitleId] = useState<number | null>(null);
  const markerListRef = useRef<HTMLDivElement>(null);
  const scrolledCreatedId = useRef<number | null>(null);
  const onlyManualQuestions = mode === "question_extraction"
    && !allMarkers.some(marker => marker.analysis_mode === mode && !isManualQuestion(marker));
  const currentIndex = markers.findIndex((marker) => marker.id === selectedMarkerId);
  const modeMarkers = allMarkers.filter((marker) => marker.analysis_mode === mode && marker.review_status !== "excluded");
  const modeCount = modeMarkers.length;
  const viewedCount = modeMarkers.filter((marker) => viewedMarkerIds.has(marker.id)).length;
  useEffect(() => {
    if (editingMarkerId !== null && !markers.some((marker) => marker.id === editingMarkerId)) onEditMarker(null);
  }, [editingMarkerId, markers, onEditMarker]);
  useEffect(() => {
    if (editingTitleId !== null && !markers.some(marker => marker.id === editingTitleId)) setEditingTitleId(null);
  }, [editingTitleId, markers]);
  useEffect(() => {
    if (createdMarkerId == null || scrolledCreatedId.current === createdMarkerId) return;
    const list = markerListRef.current;
    const row = list?.querySelector<HTMLElement>(`[data-marker-id="${createdMarkerId}"]`);
    if (!list || !row) return;
    scrolledCreatedId.current = createdMarkerId;
    const bounds = list.getBoundingClientRect(), rowBounds = row.getBoundingClientRect();
    if (rowBounds.top < bounds.top || rowBounds.bottom > bounds.bottom) {
      list.scrollTop += rowBounds.top - bounds.top - 12;
    }
  }, [createdMarkerId, markers]);

  const closeTitleEditor = (id: number) => {
    setEditingTitleId(null);
    window.requestAnimationFrame(() => sidebarRef.current
      ?.querySelector<HTMLButtonElement>(`[data-marker-id="${id}"] .marker-main`)?.focus());
  };

  return (
    <aside ref={sidebarRef} className="review-sidebar">
      <div className="sidebar-heading">
        <div>
          <h2>{mode === "question_extraction" ? "题目讲解" : "考点"}</h2>
        </div>
        <div className="sidebar-heading-actions">
          <span className="count-pill">{modeCount}</span>
          {mode === "question_extraction" && onCreateQuestion && <button className="icon-button add-manual-question"
            type="button" title={shortcutTitle("新增手动题目定位", shortcuts, "createQuestion")}
            aria-label="新增手动题目定位" aria-keyshortcuts={shortcutAria(shortcuts, "createQuestion")}
            disabled={creatingQuestion || editingTitleId !== null} onClick={onCreateQuestion}>
            {creatingQuestion ? <LoaderCircle className="spin" size={16} /> : <Plus size={16} />}
          </button>}
        </div>
      </div>

      <div className="mode-switcher mode-groups" role="group" aria-label="重点模式">
        <details className={`recall-mode-drawer${builtinSelected ? " is-current" : ""}`}>
          <summary>
            <span className="recall-mode-group-label">默认召回</span>
            <span className="recall-mode-current">{builtinSelected ? label(mode) : "3 种"}</span>
            <ChevronDown size={15} aria-hidden="true" />
          </summary>
          <div className="recall-mode-options" role="tablist" aria-label="默认召回">
            {RECALL_MODES.map(item => modeButton(item.value, item.label))}
          </div>
        </details>
        {customIds.length > 1 ? <details className={`recall-mode-drawer${customSelected ? " is-current" : ""}`}>
          <summary>
            <span className="recall-mode-group-label">自定义</span>
            <span className="recall-mode-current">{customSelected ? label(mode) : `${customIds.length} 个方案`}</span>
            <ChevronDown size={15} aria-hidden="true" />
          </summary>
          <div className="recall-mode-options" role="tablist" aria-label="自定义召回">
            {customIds.map(value => modeButton(value, label(value)))}
          </div>
        </details> : customIds.length === 1 && <div className="recall-mode-single" role="tablist" aria-label="自定义召回">
          {modeButton(customIds[0], label(customIds[0]))}
        </div>}
        <div className="recall-mode-single question-mode" role="tablist" aria-label="题目模式">
          {modeButton("question_extraction", "题目提取")}
        </div>
        {allMarkers.some(marker => marker.analysis_mode === "strict_exam_intent") &&
          <div className="recall-mode-single" role="tablist" aria-label="旧版考点">
            {modeButton("strict_exam_intent", "旧版考点")}
          </div>}
      </div>

      <div className="status-filter" role="tablist" aria-label="本次查看状态">
        {([
          ["all", "全部"],
          ["unviewed", "未看"],
          ["viewed", "已看"],
        ] as const).map(([key, label]) => (
          <button key={key} role="tab" aria-selected={status === key} className={status === key ? "is-selected" : ""} type="button" onClick={() => onStatusChange(key)}>
            {status === key && <SelectionIndicator id={`${selectionScope}-status`} line />}
            {label}
          </button>
        ))}
      </div>

      <div className="sidebar-summary">
        <span><Circle size={12} /> {modeCount - viewedCount} 个未看</span>
        <span><Check size={13} /> {viewedCount} 个已看</span>
      </div>
      {mode === "question_extraction" && (imageError || visuals.error) && <p className="field-error" role="status">
        {imageError ?? visuals.error?.message}
      </p>}

      {canUndo && (
        <div className="undo-banner">
          <span>{undoLabel}</span>
          <button type="button" onClick={onUndo} disabled={undoing} title="撤销（Ctrl+Z）"
            aria-keyshortcuts="Control+Z Meta+Z"><Undo2 size={14} />{undoing ? "正在撤销…" : "撤销"}</button>
        </div>
      )}
      {canRedo && <div className="redo-banner">
        <span>可重做上次撤销的操作</span>
        <button type="button" onClick={onRedo} disabled={undoing} title="重做（Ctrl+Y / Ctrl+Shift+Z）"
          aria-keyshortcuts="Control+Y Control+Shift+Z Meta+Y Meta+Shift+Z"><Redo2 size={14} />重做</button>
      </div>}

      <div ref={markerListRef} className="marker-list" aria-label="重点列表">
      {analysisVideoId && markers.length > 0 && mode !== "strict_exam_intent"
        && (outdated || (onlyManualQuestions && currentRun?.status !== "completed"))
        && <RecallAnalysisPanel videoId={analysisVideoId} mode={mode} label={label(mode)} />}
      <FadeRegion changeKey={`${mode}:${status}`}><AnimatePresence initial={false} key={`${mode}:${status}`}>
        {markers.length === 0 ? (
          analysisVideoId && mode !== "strict_exam_intent" && !allMarkers.some(marker => marker.analysis_mode === mode) ?
          <RecallAnalysisPanel videoId={analysisVideoId} mode={mode} label={label(mode)} /> : <div className="empty-state">
            <Check size={20} />
            <strong>这里暂时没有重点</strong>
            <span>切换筛选条件，或返回复习其他模式。</span>
          </div>
        ) : markers.map((marker) => (
          <AnimatedListItem data-marker-id={marker.id} className={`marker-item${selectedMarkerId === marker.id ? " is-selected" : ""}${viewedMarkerIds.has(marker.id) ? " is-viewed" : ""}${marker.analysis_mode === "question_extraction" && marker.question_emphasized ? " is-question-emphasized" : ""}${createdMarkerId === marker.id ? " is-new-question" : ""}`} key={marker.id}
            onClick={(event) => {
              if (!(event.target instanceof Element)
                || event.target.closest("button, a, input, label, select, textarea, summary, [role='button'], .marker-time-editor, .marker-importance-control, .manual-question-title-editor")
                || window.getSelection()?.isCollapsed === false) return;
              onSelectMarker(marker);
            }}>
            <div className="marker-time-row">
              <button className="marker-time marker-time-jump" type="button" title="跳转到这个重点" onClick={() => onSelectMarker(marker)}>{formatTime(markerJumpTime(marker))}</button>
              {analysisVideoId && marker.analysis_mode === "question_extraction" && <QuestionVisualButton marker={marker}
                state={visuals.data?.items.find(item => item.marker_id === marker.id)}
                busy={imageSaving || (visuals.data?.active ?? false)}
                onCapture={onCaptureQuestion ? () => onCaptureQuestion(marker) : undefined}
                onRemove={onRemoveQuestionImage ? () => onRemoveQuestionImage(marker) : undefined} />}
              <button className="icon-button marker-edit-time" type="button" title={shortcutTitle("调整跳转时间；快捷键直接保存最近选中考点的播放位置", shortcuts, "captureTime")} aria-label={`调整跳转时间：${marker.title}`}
                disabled={timeSaving} aria-expanded={editingMarkerId === marker.id} onClick={() => onEditMarker(editingMarkerId === marker.id ? null : marker.id)}><Pencil size={14} /></button>
              {marker.user_start_ms != null && <span className="marker-time-adjusted">已调整 · 原始定位 {formatTime(marker.start_ms)}</span>}
              {marker.analysis_mode === "question_extraction" && onChangeQuestionEmphasis
                && <QuestionEmphasisControl marker={marker} onChange={onChangeQuestionEmphasis} />}
            </div>
            {editingTitleId === marker.id && onRenameQuestion ? <ManualQuestionTitleEditor marker={marker}
              onSave={title => onRenameQuestion(marker, title)} onClose={() => closeTitleEditor(marker.id)} /> :
            <button className="marker-main" type="button" title={isManualQuestion(marker) && onRenameQuestion ? "修改题目名称" : `跳转到 ${formatTime(markerJumpTime(marker))}`}
              aria-current={selectedMarkerId === marker.id ? "true" : undefined}
              onClick={() => {
                if (isManualQuestion(marker) && onRenameQuestion) setEditingTitleId(marker.id);
                else onSelectMarker(marker);
              }}>
              <strong>{marker.title}</strong>
              <span className="marker-jump-indicator" aria-hidden="true">{isManualQuestion(marker) && onRenameQuestion ? <Pencil size={14} /> : <ArrowUpRight size={16} />}</span>
              <span className="marker-note">{marker.note}</span>
            </button>}
            {(!isManualQuestion(marker) || marker.source_quote) && <div className={`marker-evidence${expandedQuotes.has(marker.id) ? " is-expanded" : ""}`}>
              <AnimatedQuote id={`marker-quote-${marker.id}`} expanded={expandedQuotes.has(marker.id)}>“{marker.source_quote}”</AnimatedQuote>
              {marker.source_quote.length > 90 && <button className="quote-toggle" type="button" title={expandedQuotes.has(marker.id) ? "收起原文" : "展开原文"}
                aria-label={`${expandedQuotes.has(marker.id) ? "收起" : "展开"}原文：${marker.title}`} aria-expanded={expandedQuotes.has(marker.id)} aria-controls={`marker-quote-${marker.id}`}
                onClick={() => setExpandedQuotes((current) => { const next = new Set(current); if (next.has(marker.id)) next.delete(marker.id); else next.add(marker.id); return next; })}>{expandedQuotes.has(marker.id) ? "收起原文" : "展开原文"}<ChevronDown size={14} /></button>}
            </div>}
            <div className="marker-actions">
              {onSearchBilibili && <button className="text-button compact" type="button"
                title={`查找 B 站讲解：${marker.title}`} onClick={() => onSearchBilibili(marker)}>
                <Search size={14} /><span>B 站讲解</span>
              </button>}
              <button className="danger-action" type="button" disabled={timeSaving} onClick={() => onChangeStatus(marker, "excluded")} title="移除这个重点">
                <Trash2 size={14} />
                <span>移除</span>
              </button>
              <MarkerImportanceControl marker={marker} onChange={onChangeImportance} />
            </div>
            <Collapse open={editingMarkerId === marker.id}><MarkerTimeEditor key={marker.id} marker={marker} durationMs={durationMs} currentTimeMs={currentTimeMs}
              getCurrentTimeMs={getCurrentTimeMs}
              onSave={(timeMs) => onSaveTime(marker, timeMs)} onClose={() => onEditMarker(null)} /></Collapse>
          </AnimatedListItem>
        ))}
      </AnimatePresence></FadeRegion></div>

      <div className="sidebar-nav">
        <button type="button" onClick={onPrevious} disabled={currentIndex <= 0} title={shortcutTitle("上一个重点", shortcuts, "previousMarker")} aria-keyshortcuts={shortcutAria(shortcuts, "previousMarker")}>
          <ChevronLeft size={16} /> 上一个重点
        </button>
        <button type="button" onClick={onNext} disabled={markers.length === 0 || currentIndex >= markers.length - 1} title={shortcutTitle("下一个重点", shortcuts, "nextMarker")} aria-keyshortcuts={shortcutAria(shortcuts, "nextMarker")}>
          下一个重点 <ChevronRight size={16} />
        </button>
      </div>

      {excludedMarkers.length > 0 && (
        <details className="excluded-section">
          <summary>已移除重点 <span>{excludedMarkers.length}</span></summary>
          <div className="excluded-list">
            {excludedMarkers.map((marker) => (
              <div className="excluded-row" key={marker.id}>
                <span>{formatTime(markerJumpTime(marker))} · {marker.title}</span>
                <button type="button" onClick={() => onRestoreExcluded(marker)} title="恢复重点"><Undo2 size={14} /></button>
              </div>
            ))}
          </div>
        </details>
      )}
    </aside>
  );
}

function navigateModes(event: KeyboardEvent<HTMLButtonElement>) {
  const buttons = [...(event.currentTarget.closest('[role="tablist"]')
    ?.querySelectorAll<HTMLButtonElement>('[role="tab"]') ?? [])];
  const index = buttons.indexOf(event.currentTarget);
  const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1
    : ["ArrowDown", "ArrowRight"].includes(event.key) ? (index + 1) % buttons.length
    : ["ArrowUp", "ArrowLeft"].includes(event.key) ? (index - 1 + buttons.length) % buttons.length : null;
  if (next === null || !buttons[next]) return;
  event.preventDefault();
  buttons[next].focus();
  buttons[next].click();
}

function ManualQuestionTitleEditor({ marker, onSave, onClose }: {
  marker: Marker;
  onSave: (title: string) => Promise<void>;
  onClose: () => void;
}) {
  const [title, setTitle] = useState(marker.title);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const savingRef = useRef(false);
  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => { inputRef.current?.focus(); inputRef.current?.select(); }, []);
  const save = async () => {
    if (savingRef.current || !title.trim()) return;
    if (title.trim() === marker.title) { onClose(); return; }
    savingRef.current = true;
    setSaving(true);
    setError("");
    try {
      await onSave(title.trim());
      onClose();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "保存失败，请重试");
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  };
  return <form className="manual-question-title-editor" onSubmit={event => { event.preventDefault(); void save(); }}
    onKeyDown={event => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        if (!savingRef.current) onClose();
      }
    }}>
    <input ref={inputRef} aria-label="题目名称" value={title} maxLength={200} disabled={saving}
      onChange={event => setTitle(event.target.value)} />
    <button className="icon-button" type="submit" title="保存题目名称" aria-label="保存题目名称"
      disabled={saving || !title.trim()}>{saving ? <LoaderCircle className="spin" size={14} /> : <Save size={14} />}</button>
    <button className="icon-button" type="button" title="取消修改名称" aria-label="取消修改名称"
      disabled={saving} onClick={onClose}><X size={14} /></button>
    {error && <span className="field-error" role="alert">{error}</span>}
  </form>;
}

function QuestionEmphasisControl({ marker, onChange }: {
  marker: Marker;
  onChange: (marker: Marker, emphasized: boolean) => Promise<void>;
}) {
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(false);
  const savingRef = useRef(false);
  const emphasized = Boolean(marker.question_emphasized);
  const label = emphasized ? "取消强调题目" : "强调题目";
  const save = async () => {
    if (savingRef.current) return;
    savingRef.current = true;
    setSaving(true);
    setError(false);
    try {
      await onChange(marker, !emphasized);
    } catch {
      setError(true);
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  };
  return <span className="question-emphasis-control">
    <button className="icon-button question-emphasis-toggle" type="button"
      title={label} aria-label={`${label}：${marker.title}`} aria-pressed={emphasized}
      disabled={saving} onClick={() => void save()}>
      {saving ? <LoaderCircle className="spin" size={16} /> : <Star size={16} fill={emphasized ? "currentColor" : "none"} />}
    </button>
    {error && <span className="field-error" role="alert">保存失败，请重试</span>}
  </span>;
}
