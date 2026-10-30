import { useRef, useState } from "react";
import { createPortal } from "react-dom";
import { X, ZoomIn, ZoomOut } from "lucide-react";
import { studyRegionUrl, type StudyBlock, type StudyRegion } from "../api/studyMaterials";
import { useModalFocus } from "../hooks/useModalFocus";
import { StudyMarkdown } from "./StudyMarkdown";

export function StudyQuestionContent({ documentId, regions, blocks, onPreview }: {
  documentId: string; regions: StudyRegion[]; blocks: StudyBlock[]; onPreview?: () => void;
}) {
  const parts: { key: number; text?: string; region?: StudyRegion }[] = [];
  for (const [index, region] of regions.entries()) {
    const block = blocks.find(item => item.id === region.block_id);
    if (!block) continue;
    if (block.kind === "markdown") {
      const text = (block.text || "").slice(region.start ?? 0, region.end);
      const previous = parts.at(-1);
      if (previous?.text !== undefined) previous.text += `\n\n${text}`;
      else parts.push({ key: index, text });
    } else parts.push({ key: index, region });
  }
  return <div className="study-question-content">{parts.map(part => part.text !== undefined
    ? <StudyMarkdown key={part.key} text={part.text} />
    : onPreview ? <button key={part.key} className="study-region-zoom" type="button" aria-label={`放大选区 ${part.key + 1}`}
      aria-haspopup="dialog" onClick={onPreview}>
      <img src={studyRegionUrl(documentId, part.region!)} alt={`题目选区 ${part.key + 1}`} />
    </button> : <img key={part.key} src={studyRegionUrl(documentId, part.region!)} alt={`题目选区 ${part.key + 1}`} />)}</div>;
}

export function StudyQuestionPreview({ documentId, title, regions, blocks, onClose }: {
  documentId: string; title: string; regions: StudyRegion[]; blocks: StudyBlock[]; onClose: () => void;
}) {
  const dialog = useRef<HTMLDivElement>(null), close = useRef<HTMLButtonElement>(null);
  const [zoom, setZoom] = useState(1);
  useModalFocus(true, dialog, close, onClose);
  return createPortal(<div className="study-preview-backdrop" onClick={onClose}>
    <div ref={dialog} className="study-preview-dialog" role="dialog" aria-modal="true" aria-labelledby="study-preview-title"
      tabIndex={-1} onClick={event => event.stopPropagation()}>
      <header><h2 id="study-preview-title">{title}</h2><div className="study-preview-tools">
        <button className="icon-button" title="缩小预览" aria-label="缩小预览" disabled={zoom <= .5}
          onClick={() => setZoom(value => Math.max(.5, value - .25))}><ZoomOut size={18} /></button>
        <span>{Math.round(zoom * 100)}%</span>
        <button className="icon-button" title="放大预览" aria-label="放大预览" disabled={zoom >= 2}
          onClick={() => setZoom(value => Math.min(2, value + .25))}><ZoomIn size={18} /></button>
        <button ref={close} className="icon-button" title="关闭预览" aria-label="关闭预览" onClick={onClose}><X size={19} /></button>
      </div></header>
      <div className="study-preview-scroll"><div className="study-preview-content" style={{ width: `${zoom * 100}%`, fontSize: `${zoom * 16}px` }}>
        <StudyQuestionContent documentId={documentId} regions={regions} blocks={blocks} />
      </div></div>
    </div>
  </div>, document.body);
}
