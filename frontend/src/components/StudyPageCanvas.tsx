import { useEffect, useRef, useState, type PointerEvent, type KeyboardEvent } from "react";
import { LoaderCircle, RotateCcw } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { getStudyTextBounds, studyPageUrl, studyRegionUrl, studySourceLabel, type StudyBBox, type StudyBlock, type StudyRegion } from "../api/studyMaterials";
import type { PageSelectionResize } from "../hooks/useStudyPageBoxSelection";

interface SelectedRegion extends StudyRegion { index: number }
type Point = { x: number; y: number };
const clamp = (n: number, low = 0, high = 1) => Math.max(low, Math.min(high, n));
export function rectangle(a: Point, b: Point): StudyBBox {
  return [Math.min(a.x, b.x), Math.min(a.y, b.y), Math.max(a.x, b.x), Math.max(a.y, b.y)];
}
export function translateBox(box: StudyBBox, dx: number, dy: number): StudyBBox {
  const x = clamp(dx, -box[0], 1 - box[2]), y = clamp(dy, -box[1], 1 - box[3]);
  return [box[0] + x, box[1] + y, box[2] + x, box[3] + y];
}
function boxStyle(box: StudyBBox) {
  return { left: `${box[0] * 100}%`, top: `${box[1] * 100}%`,
    width: `${(box[2] - box[0]) * 100}%`, height: `${(box[3] - box[1]) * 100}%` };
}

export function StudyPageCanvas({ documentId, block, zoom, selecting, regions, savedRegions, selectedIndex, editableIndex = -1,
  editableIndices = [editableIndex], scope = "default", snap = false, drawing, onDrawStart,
  allowNewRegion = true, questionLabel, onOpenRegion, onAdd, onChange, onSelect, onCommit, onError }: {
  documentId: string; block: StudyBlock; zoom: number; selecting: boolean;
  regions: SelectedRegion[]; savedRegions: (StudyRegion & { questionKey?: string; label?: string })[]; selectedIndex: number; editableIndex?: number;
  editableIndices?: number[]; scope?: string; snap?: boolean; drawing?: StudyBBox;
  allowNewRegion?: boolean;
  questionLabel?: string; onOpenRegion?: (key: string) => void;
  onDrawStart?: (event: PointerEvent<HTMLDivElement>, block: string, resize?: PageSelectionResize) => void;
  onAdd: (box: StudyBBox) => void; onChange: (index: number, box: StudyBBox) => void;
  onSelect: (index: number) => void; onCommit: () => void; onError: (message: string) => void;
}) {
  const surface = useRef<HTMLDivElement>(null);
  const [loaded, setLoaded] = useState(false), [failed, setFailed] = useState(false), [attempt, setAttempt] = useState(0);
  const [near, setNear] = useState(false);
  const bounds = useQuery({ queryKey: ["study", scope, documentId, block.id, "text-bounds"],
    queryFn: () => getStudyTextBounds(documentId, block.id),
    enabled: near && selecting && block.kind === "pdf_page", staleTime: Infinity, retry: false });
  const textBounds = bounds.data?.status === "ready" ? bounds.data.bbox : null;
  const textLeft = textBounds ? Math.max(0, textBounds[0] - 6 / (block.width || 600)) : undefined;
  const textRight = textBounds ? Math.min(1, textBounds[2] + 6 / (block.width || 600)) : undefined;
  const [temporary, setTemporary] = useState<StudyBBox | null>(null);
  const drag = useRef<{ start: Point; mode: string; index: number; original?: StudyBBox } | null>(null);
  const label = studySourceLabel(block);
  useEffect(() => {
    const node = surface.current;
    if (!node) return;
    if (!globalThis.IntersectionObserver) { setNear(true); return; }
    const observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) { setNear(true); observer.disconnect(); }
    }, { rootMargin: "900px" });
    observer.observe(node);
    return () => observer.disconnect();
  }, []);
  function point(event: PointerEvent): Point {
    const bounds = surface.current!.getBoundingClientRect();
    return { x: clamp((event.clientX - bounds.left) / bounds.width), y: clamp((event.clientY - bounds.top) / bounds.height) };
  }
  function start(event: PointerEvent<HTMLDivElement>) {
    if (!selecting || !loaded || event.button !== 0) return;
    const target = event.target as HTMLElement;
    const questionKey = target.closest<HTMLElement>("[data-question-key]")?.dataset.questionKey;
    if (questionKey && onOpenRegion) { event.preventDefault(); onOpenRegion(questionKey); return; }
    const indexValue = target.closest<HTMLElement>("[data-region-index]")?.dataset.regionIndex;
    const index = indexValue === undefined ? -1 : Number(indexValue);
    const original = editableIndices.includes(index) ? regions.find(region => region.index === index)?.bbox : undefined;
    if (!original) {
      if (!allowNewRegion || !onDrawStart) return;
      onDrawStart(event, block.id); return;
    }
    const mode = target.dataset.handle || (original ? "move" : "draw");
    if (original) onSelect(index);
    drag.current = { start: point(event), mode, index, original };
    event.currentTarget.setPointerCapture(event.pointerId);
    event.preventDefault();
  }
  function move(event: PointerEvent<HTMLDivElement>) {
    const current = drag.current;
    if (!current) return;
    const bounds = surface.current!.getBoundingClientRect();
    if (block.kind === "pdf_page" && current.original && current.mode !== "move" && current.mode !== "draw"
        && onDrawStart && (event.clientY < bounds.top || event.clientY > bounds.bottom)) {
      const original = current.original;
      // Transfer a corner drag to the document controller once it leaves this page.
      onChange(current.index, original);
      drag.current = null; setTemporary(null);
      onDrawStart(event, block.id, { index: current.index, anchor: {
        x: current.mode.includes("w") ? original[2] : original[0],
        y: current.mode.includes("n") ? original[3] : original[1],
      } });
      return;
    }
    const next = point(event);
    if (current.mode === "draw") { setTemporary(rectangle(current.start, next)); return; }
    const original = current.original!;
    if (current.mode === "move") {
      onChange(current.index, translateBox(original, next.x - current.start.x, next.y - current.start.y));
    } else {
      const [left, top, right, bottom] = original;
      const box: StudyBBox = [
        current.mode.includes("w") ? Math.min(next.x, right - .005) : left,
        current.mode.includes("n") ? Math.min(next.y, bottom - .005) : top,
        current.mode.includes("e") ? Math.max(next.x, left + .005) : right,
        current.mode.includes("s") ? Math.max(next.y, top + .005) : bottom,
      ];
      onChange(current.index, box);
    }
  }
  function finish(event: PointerEvent<HTMLDivElement>) {
    const current = drag.current;
    if (!current) return;
    if (current.mode === "draw") {
      const box = rectangle(current.start, point(event));
      if (box[2] - box[0] < .005 || box[3] - box[1] < .005) onError("选区太小，请重新框选");
      else onAdd(box);
    } else onCommit();
    drag.current = null; setTemporary(null);
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  }
  function keyboard(event: KeyboardEvent, index: number, box: StudyBBox) {
    if (!selecting || !editableIndices.includes(index) || !["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
    event.preventDefault(); event.stopPropagation();
    const step = event.shiftKey ? .01 : .002;
    onChange(index, translateBox(box, event.key === "ArrowLeft" ? -step : event.key === "ArrowRight" ? step : 0,
      event.key === "ArrowUp" ? -step : event.key === "ArrowDown" ? step : 0));
    onCommit();
  }
  const url = block.supplement
    ? studyRegionUrl(documentId, { block_id: block.id })
    : studyPageUrl(documentId, block.page || 1);
  return <section className="study-source-page" data-study-block={block.id}>
    <header><span>{label}</span>{selecting && snap && bounds.data?.status === "no_text" && <span> · 无文字层，自由框选</span>}
      {selecting && snap && (bounds.isError || bounds.data?.status === "unavailable") && <span> · 文字边界不可用，自由框选</span>}</header>
    <div className="study-page-size" style={{ width: `${zoom * 100}%`, maxWidth: zoom === 1 ? "100%" : undefined }}>
      <div ref={surface} className={`study-page-canvas${selecting ? " selecting" : ""}`}
        data-page-block={block.id} data-cross-page={block.kind === "pdf_page"}
        data-text-left={textLeft} data-text-right={textRight}
        style={{ aspectRatio: `${block.width || 3} / ${block.height || 4}` }}
        onPointerDown={start} onPointerMove={move} onPointerUp={finish}
        onPointerCancel={() => { drag.current = null; setTemporary(null); onCommit(); }}>
        {near && <img src={`${url}${url.includes("?") ? "&" : "?"}retry=${attempt}`} alt={label} draggable={false}
          onLoad={() => { setLoaded(true); setFailed(false); }}
          onError={() => { setFailed(true); setLoaded(false); }} />}
        {!loaded && <div className="study-page-loading">{failed ? <>
          <span role="alert">页面加载失败</span><button className="text-button" onPointerDown={e => e.stopPropagation()}
            onClick={() => { setAttempt(n => n + 1); setFailed(false); }}><RotateCcw size={16} />重试</button>
        </> : <LoaderCircle className="spin" size={20} />}</div>}
        {selecting && savedRegions.filter(region => region.bbox).map((region, index) =>
          <div key={`saved-${index}`} className="study-saved-box" style={boxStyle(region.bbox!)}
            data-question-key={region.questionKey} role={region.questionKey ? "button" : undefined}
            aria-label={region.label} tabIndex={region.questionKey ? 0 : -1}
            onKeyDown={event => { if (region.questionKey && ["Enter", " "].includes(event.key)) {
              event.preventDefault(); onOpenRegion?.(region.questionKey);
            } }}>
            {region.label && <span className="study-selection-number">{region.label}</span>}
          </div>)}
        {selecting && snap && textBounds && <div className="study-text-guides" aria-hidden="true">
          <span style={{ left: `${textLeft! * 100}%` }} /><span style={{ left: `${textRight! * 100}%` }} />
        </div>}
        {selecting && regions.filter(region => region.bbox).map(region => <div key={region.index}
          tabIndex={selecting && editableIndices.includes(region.index) ? 0 : -1}
          role={selecting && editableIndices.includes(region.index) ? "button" : undefined} aria-label={`选区 ${region.index + 1}`}
          data-region-index={region.index} className={`study-selection-box${selectedIndex === region.index ? " active" : ""}${selecting && editableIndices.includes(region.index) ? "" : " locked"}`}
          style={boxStyle(region.bbox!)} onFocus={() => { if (selecting && editableIndices.includes(region.index)) onSelect(region.index); }}
          onKeyDown={event => keyboard(event, region.index, region.bbox!)}>
          <span className="study-selection-number">{questionLabel || `区域 ${region.index + 1}`}</span>
          {selecting && editableIndices.includes(region.index) && ["nw", "ne", "sw", "se"].map(handle =>
            <span key={handle} data-handle={handle} className={`study-selection-handle ${handle}`} />)}
        </div>)}
        {(drawing || temporary) && <div className="study-selection-box is-drawing" style={boxStyle((drawing || temporary)!)} />}
      </div>
    </div>
  </section>;
}
