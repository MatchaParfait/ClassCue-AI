import { useShortcuts, shortcutTitle, shortcutAria } from "../shortcuts";
import { AnimatePresence, AnimatedListItem, AnimatedQuote, Collapse, FadeRegion, SelectionIndicator, useEntryMotion, useSelectionScope } from "./Motion";
import { ArrowUpRight, Check, ChevronDown, ChevronLeft, ChevronRight, Circle, Pencil, Trash2, Undo2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { AnalysisRun, Marker, MarkerImportance, MarkerMode, RecallMode, ReviewFilter, ReviewStatus } from "../types";
import { recallLabel, useRecallSchemes } from "../hooks/useRecallSchemes";
import { formatTime, markerJumpTime } from "../utils";
import { RECALL_MODES } from "../processOptions";
import { MarkerTimeEditor } from "./MarkerTimeEditor";
import { MarkerImportanceControl } from "./MarkerImportanceControl";
import { RecallAnalysisPanel } from "./RecallAnalysisPanel";

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
  viewedMarkerIds: ReadonlySet<number>;
  onSelectMarker: (marker: Marker) => void;
  onChangeStatus: (marker: Marker, status: ReviewStatus) => void;
  onUndo: () => void;
  canUndo: boolean;
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
  viewedMarkerIds,
  onSelectMarker,
  onChangeStatus,
  onUndo,
  canUndo,
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
}: ReviewSidebarProps) {
  const shortcuts = useShortcuts();
  const schemes = useRecallSchemes(Boolean(analysisVideoId));
  const customIds = [...new Set([
    ...(schemes.data ?? []).filter(item => !item.archived).map(item => item.id),
    ...allMarkers.filter(marker => marker.analysis_mode.startsWith("custom_")).map(marker => marker.analysis_mode as RecallMode),
    ...analysisRuns.filter(run => run.analysis_mode.startsWith("custom_")).map(run => run.analysis_mode as RecallMode),
  ])];
  const label = (id: MarkerMode) => recallLabel(id, schemes.data)
    === "自定义方案" ? analysisRuns.find(run => run.analysis_mode === id)?.scheme_name ?? "自定义方案" : recallLabel(id, schemes.data);
  const currentRun = analysisRuns.find(run => run.analysis_mode === mode);
  const outdated = mode.startsWith("custom_") && Boolean(currentRun && (currentRun.latest_scheme_revision ?? 0) > (currentRun.scheme_revision ?? 0));
  const sidebarRef = useRef<HTMLElement>(null);
  useEntryMotion(sidebarRef, { duration: .14 });
  const selectionScope = useSelectionScope();
  const [expandedQuotes, setExpandedQuotes] = useState<ReadonlySet<number>>(new Set());
  const currentIndex = markers.findIndex((marker) => marker.id === selectedMarkerId);
  const modeMarkers = allMarkers.filter((marker) => marker.analysis_mode === mode && marker.review_status !== "excluded");
  const modeCount = modeMarkers.length;
  const viewedCount = modeMarkers.filter((marker) => viewedMarkerIds.has(marker.id)).length;
  useEffect(() => {
    if (editingMarkerId !== null && !markers.some((marker) => marker.id === editingMarkerId)) onEditMarker(null);
  }, [editingMarkerId, markers, onEditMarker]);

  return (
    <aside ref={sidebarRef} className="review-sidebar">
      <div className="sidebar-heading">
        <div>
          <h2>考点</h2>
        </div>
        <span className="count-pill">{modeCount}</span>
      </div>

      <div className="mode-switcher" role="tablist" aria-label="重点模式">
        {[
          ...RECALL_MODES,
          ...customIds.map(value => ({ value, label: label(value) })),
          ...(allMarkers.some((marker) => marker.analysis_mode === "strict_exam_intent")
            ? [{ value: "strict_exam_intent" as const, label: "旧版考点" }] : []),
        ].map((item) => (
          <button key={item.value} role="tab" title={item.label} aria-selected={mode === item.value} className={mode === item.value ? "is-selected" : ""} type="button" onClick={() => onModeChange(item.value)}>
            {mode === item.value && <SelectionIndicator id={`${selectionScope}-mode`} />}
            <span>{item.label}</span>
            <small>{allMarkers.filter((marker) => marker.analysis_mode === item.value && marker.review_status !== "excluded").length}</small>
          </button>
        ))}
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

      {canUndo && (
        <div className="undo-banner">
          <span>重点已移除</span>
          <button type="button" onClick={onUndo}><Undo2 size={14} /> 撤销</button>
        </div>
      )}

      <div className="marker-list" aria-label="重点列表">
      {analysisVideoId && outdated && markers.length > 0 && mode !== "strict_exam_intent"
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
          <AnimatedListItem className={`marker-item${selectedMarkerId === marker.id ? " is-selected" : ""}${viewedMarkerIds.has(marker.id) ? " is-viewed" : ""}`} key={marker.id}
            onClick={(event) => {
              if (!(event.target instanceof Element)
                || event.target.closest("button, a, input, label, select, textarea, summary, [role='button'], .marker-time-editor, .marker-importance-control")
                || window.getSelection()?.isCollapsed === false) return;
              onSelectMarker(marker);
            }}>
            <div className="marker-time-row">
              <button className="marker-time marker-time-jump" type="button" title="跳转到这个重点" onClick={() => onSelectMarker(marker)}>{formatTime(markerJumpTime(marker))}</button>
              <button className="icon-button marker-edit-time" type="button" title={shortcutTitle("调整跳转时间；快捷键直接保存最近选中考点的播放位置", shortcuts, "captureTime")} aria-label={`调整跳转时间：${marker.title}`}
                disabled={timeSaving} aria-expanded={editingMarkerId === marker.id} onClick={() => onEditMarker(editingMarkerId === marker.id ? null : marker.id)}><Pencil size={14} /></button>
              {marker.user_start_ms != null && <span className="marker-time-adjusted">已调整 · 原始定位 {formatTime(marker.start_ms)}</span>}
            </div>
            <button className="marker-main" type="button" title={`跳转到 ${formatTime(markerJumpTime(marker))}`}
              aria-current={selectedMarkerId === marker.id ? "true" : undefined} onClick={() => onSelectMarker(marker)}>
              <strong>{marker.title}</strong>
              <span className="marker-jump-indicator" aria-hidden="true"><ArrowUpRight size={16} /></span>
              <span className="marker-note">{marker.note}</span>
            </button>
            <div className={`marker-evidence${expandedQuotes.has(marker.id) ? " is-expanded" : ""}`}>
              <AnimatedQuote id={`marker-quote-${marker.id}`} expanded={expandedQuotes.has(marker.id)}>“{marker.source_quote}”</AnimatedQuote>
              {marker.source_quote.length > 90 && <button className="quote-toggle" type="button" title={expandedQuotes.has(marker.id) ? "收起原文" : "展开原文"}
                aria-label={`${expandedQuotes.has(marker.id) ? "收起" : "展开"}原文：${marker.title}`} aria-expanded={expandedQuotes.has(marker.id)} aria-controls={`marker-quote-${marker.id}`}
                onClick={() => setExpandedQuotes((current) => { const next = new Set(current); if (next.has(marker.id)) next.delete(marker.id); else next.add(marker.id); return next; })}>{expandedQuotes.has(marker.id) ? "收起原文" : "展开原文"}<ChevronDown size={14} /></button>}
            </div>
            <div className="marker-actions">
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
