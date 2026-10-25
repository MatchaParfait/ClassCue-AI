import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Camera, ImageOff, LoaderCircle, RotateCcw, Trash2, X, ZoomIn } from "lucide-react";
import { getSlideImageUrl, getVideoSlides } from "../api/client";
import { questionCaptureUrl, type QuestionCapture, type QuestionMaterial } from "../api/questions";
import type { Marker, VideoSlide } from "../types";
import { formatTime } from "../utils";

export function QuestionMaterials({ marker, material, onChange, disabled, captures = [], onCapture, onDeleteCapture }: {
  marker: Marker; material: QuestionMaterial; onChange: (value: QuestionMaterial) => void; disabled: boolean;
  captures?: QuestionCapture[]; onCapture?: () => void; onDeleteCapture?: (id: string) => Promise<void>;
}) {
  const slides = useQuery({ queryKey: ["question-slides", marker.video_id],
    queryFn: () => getVideoSlides(marker.video_id, { include_auxiliary: true }) });
  const [scope, setScope] = useState("nearby");
  const [previewId, setPreviewId] = useState<number | null>(null);
  const [capturePreview, setCapturePreview] = useState<string | null>(null);
  const capturedPreview = captures.find(item => item.id === capturePreview);
  const selectedCount = material.slide_ids.length + (material.capture_ids?.length ?? 0);
  const all = [...(slides.data ?? [])].sort((a, b) => a.start_ms - b.start_ms || a.id - b.id);
  const nearby = all.filter(slide => slide.end_ms > marker.start_ms - 20000 && slide.start_ms < marker.end_ms + 20000);
  const shown = scope === "all" ? all : scope === "selected" ? all.filter(slide => material.slide_ids.includes(slide.id)) : nearby;
  const preview = all.find(slide => slide.id === previewId);
  const missing = slides.isSuccess ? material.slide_ids.filter(id => !all.some(slide => slide.id === id)) : [];
  const toggle = (id: number) => onChange({ ...material, slide_ids: material.slide_ids.includes(id)
    ? material.slide_ids.filter(value => value !== id) : selectedCount < 4 ? [...material.slide_ids, id] : material.slide_ids });

  return <>
    <section className="question-slides" aria-label="视频截图">
      <div className="bili-section-heading"><h3>视频截图 <small className="bili-muted">题图合计 {selectedCount} / 4</small></h3>
        <button className="secondary-button compact" disabled={disabled || !onCapture || selectedCount >= 4}
          onClick={onCapture}><Camera size={16} />从视频截图</button>
      </div>
      <div className="question-slide-grid">{captures.map(capture => <article className={`question-slide${material.capture_ids?.includes(capture.id) ? " is-selected" : ""}`} key={capture.id}>
        <button className="question-thumbnail" aria-label={`放大截图 ${formatTime(capture.captured_at_ms)}`} title="放大截图"
          onClick={() => setCapturePreview(capture.id)}><img src={questionCaptureUrl(marker.id, capture.id)} alt={`视频截图 ${formatTime(capture.captured_at_ms)}`} loading="lazy" /><ZoomIn size={17} /></button>
        <label><input type="checkbox" aria-label={`选择截图 ${formatTime(capture.captured_at_ms)}`} checked={Boolean(material.capture_ids?.includes(capture.id))}
          disabled={disabled || (!material.capture_ids?.includes(capture.id) && selectedCount >= 4)} onChange={() => onChange({ ...material,
            capture_ids: material.capture_ids?.includes(capture.id) ? material.capture_ids.filter(id => id !== capture.id) : [...(material.capture_ids ?? []), capture.id],
          })} /><span>截图 {formatTime(capture.captured_at_ms)}<small>{capture.width} × {capture.height}</small></span></label>
        <button className="icon-button question-capture-delete" title="删除截图" aria-label={`删除截图 ${formatTime(capture.captured_at_ms)}`}
          disabled={disabled} onClick={() => void onDeleteCapture?.(capture.id)}><Trash2 size={14} /></button>
      </article>)}</div>
      {capturedPreview && <figure className="question-preview"><figcaption><span>视频截图 {formatTime(capturedPreview.captured_at_ms)}</span>
        <button className="icon-button" title="收起截图预览" aria-label="收起截图预览" onClick={() => setCapturePreview(null)}><X size={16} /></button></figcaption>
        <img src={questionCaptureUrl(marker.id, capturedPreview.id)} alt="题目截图预览" /></figure>}
    </section>
    <section className="question-slides" aria-label="题目 PPT">
      <div className="bili-section-heading"><h3>题目 PPT <small className="bili-muted">已选 {material.slide_ids.length} / 4</small></h3>
        <label>范围<select aria-label="PPT 范围" value={scope} onChange={event => setScope(event.target.value)}>
          <option value="nearby">考点附近 ({nearby.length})</option><option value="all">全部 ({all.length})</option>
          <option value="selected">已选 ({material.slide_ids.length})</option>
        </select></label>
      </div>
      {slides.isPending && <p className="bili-feedback" role="status"><LoaderCircle size={16} className="spin" />正在读取 PPT</p>}
      {slides.isError && <p className="field-error" role="alert">PPT 读取失败：{slides.error.message}
        <button className="text-button" onClick={() => void slides.refetch()}><RotateCcw size={14} />重新读取 PPT</button></p>}
      {slides.isSuccess && !all.length && <p className="bili-muted">当前视频尚无 PPT 截图。</p>}
      {slides.isSuccess && all.length > 0 && !shown.length && <p className="bili-muted">此范围没有 PPT。</p>}
      {missing.map(id => <p className="bili-warning" key={id}>已选 PPT #{id} 已不存在
        <button className="text-button" disabled={disabled} onClick={() => toggle(id)}><X size={14} />移除失效选择</button></p>)}
      <div className="question-slide-grid">{shown.map(slide => <article className={`question-slide${material.slide_ids.includes(slide.id) ? " is-selected" : ""}`} key={slide.id}>
        <button className="question-thumbnail" title={`放大 PPT ${slide.id}`} aria-label={`放大 PPT ${slide.id}`}
          onClick={() => setPreviewId(slide.id)}><SlideImage slide={slide} /><ZoomIn size={17} /></button>
        <label><input type="checkbox" aria-label={`选择 PPT ${slide.id}`} checked={material.slide_ids.includes(slide.id)}
          disabled={disabled || (!material.slide_ids.includes(slide.id) && selectedCount >= 4)} onChange={() => toggle(slide.id)} />
          <span>{formatTime(slide.start_ms)} ~ {formatTime(slide.end_ms)}<small>{slide.is_llm_preferred ? "精选 · " : ""}截图 {formatTime(slide.representative_ms)}</small></span>
        </label>
      </article>)}</div>
      {preview && <figure className="question-preview" aria-label={`PPT ${preview.id} 预览`}>
        <figcaption><span>截图 {formatTime(preview.representative_ms)} · 展示 {formatTime(preview.start_ms)} ~ {formatTime(preview.end_ms)}</span>
          <button className="icon-button" title="收起预览" aria-label="收起预览" onClick={() => setPreviewId(null)}><X size={16} /></button></figcaption>
        <SlideImage key={preview.id} slide={preview} />
      </figure>}
    </section>
  </>;
}

function SlideImage({ slide }: { slide: VideoSlide }) {
  const [failed, setFailed] = useState(false);
  return failed ? <span className="question-image-error"><ImageOff size={22} />图片不可用</span>
    : <img src={getSlideImageUrl(slide)} alt={`PPT ${slide.id}`} loading="lazy" onError={() => setFailed(true)} />;
}
