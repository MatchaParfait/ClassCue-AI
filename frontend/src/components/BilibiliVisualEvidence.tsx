import { useState } from "react";
import { Camera, ExternalLink, ImageOff, Maximize2, X } from "lucide-react";
import { biliFrameUrl } from "../api/bilibili";
import type { BiliFrame, BiliUnit } from "../api/bilibili";
import { formatTime } from "../utils";

const DIMENSIONS = [["knowledge", "同知识点"], ["problem_type", "类似题型"], ["explanation", "解题讲解"]] as const;
const LABELS = { yes: "已确认", partial: "部分符合", no: "不符合", unknown: "证据不足" };

export function BilibiliVisualEvidence({ markerId, runId, unit }: { markerId: number; runId: string; unit: BiliUnit }) {
  const [preview, setPreview] = useState<BiliFrame>();
  const [failed, setFailed] = useState<string[]>([]);
  const frames = unit.frames ?? [];
  return <div className="bili-visual">
    {unit.dimensions && <dl className="bili-dimensions">
      {DIMENSIONS.map(([key, label]) => {
        const dimension = unit.dimensions![key];
        return <div key={key}>
          <dt>{label}<strong data-match-status={dimension.status}>{LABELS[dimension.status]}</strong></dt>
          <dd>{dimension.reason}</dd>
          {!!dimension.frame_ids.length && <dd className="bili-dimension-refs">{dimension.frame_ids.map(source => {
            const frame = frames.find(item => `frame:${item.id}` === source);
            return frame ? <button key={source} className="text-button" title={`查看截图 ${formatTime(frame.captured_ms)}`}
              onClick={() => setPreview(frame)}><Camera size={13} />{formatTime(frame.captured_ms)}</button> : null;
          })}</dd>}
        </div>;
      })}
    </dl>}
    {unit.visual?.error && <p className="bili-warning" role="status">{unit.visual.error}</p>}
    {frames.length > 0 && <details className="bili-frame-evidence" open>
      <summary>候选视频截图 · {frames.length}{unit.visual?.rounds === 2 ? " · 已补拍" : ""}</summary>
      <div className="bili-frame-grid">{frames.map(frame => <figure key={frame.id}>
        <button className="question-thumbnail" type="button" onClick={() => setPreview(frame)}
          aria-label={`查看候选截图 ${formatTime(frame.captured_ms)}`} disabled={failed.includes(frame.id)}>
          {failed.includes(frame.id) ? <span className="question-image-error"><ImageOff size={18} />截图不可用</span>
            : <><img src={biliFrameUrl(markerId, runId, frame.id)} alt={`候选视频 ${formatTime(frame.captured_ms)} 画面`}
              loading="lazy" onError={() => setFailed(current => [...new Set([...current, frame.id])])} /><Maximize2 size={14} /></>}
        </button>
        <figcaption><span>{formatTime(frame.captured_ms)} · {frame.time_source === "decoded_frame" ? "解码帧" : "播放器时间"}</span>
          {frame.url && <a href={frame.url} target="_blank" rel="noreferrer noopener" title="打开该时刻的 B 站视频"
            aria-label={`打开候选视频 ${formatTime(frame.captured_ms)}`}><ExternalLink size={14} /></a>}
        </figcaption>
      </figure>)}</div>
    </details>}
    {preview && <figure className="question-preview">
      <figcaption><span>候选题图 · {formatTime(preview.captured_ms)}</span>
        <button className="icon-button" type="button" title="收起候选题图" aria-label="收起候选题图"
          onClick={() => setPreview(undefined)}><X size={16} /></button></figcaption>
      {failed.includes(preview.id) ? <p className="question-image-error">截图不可用</p>
        : <img src={biliFrameUrl(markerId, runId, preview.id)} alt={`放大的候选题图 ${formatTime(preview.captured_ms)}`}
          onError={() => setFailed(current => [...new Set([...current, preview.id])])} />}
    </figure>}
  </div>;
}
