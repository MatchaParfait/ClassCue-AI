import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Camera, Image as ImageIcon, ImageOff, LoaderCircle, RefreshCw, Unlink, X } from "lucide-react";
import { useRef, useState } from "react";
import { createPortal } from "react-dom";
import { questionCaptureUrl } from "../api/questions";
import { autoQuestionImage, getQuestionVisual, visualLabel, type QuestionVisual } from "../api/questionVisuals";
import { useModalFocus } from "../hooks/useModalFocus";
import type { Marker } from "../types";
import { formatTime } from "../utils";
import "./QuestionVisualButton.css";

interface Props {
  marker: Marker;
  state?: QuestionVisual;
  busy: boolean;
  onCapture?: () => Promise<void>;
  onRemove?: () => Promise<void>;
}

export function QuestionVisualButton({ marker, state, busy, onCapture, onRemove }: Props) {
  const [open, setOpen] = useState(false);
  return <>
    <button className={`icon-button question-visual-button${state?.binding ? " has-image" : ""}`}
      type="button" title={`查看题图 · ${visualLabel(state)}`} aria-label={`查看题图：${marker.title}`}
      aria-haspopup="dialog" onClick={() => setOpen(true)}><ImageIcon size={15} /></button>
    {onCapture && <button className="icon-button question-capture-current" type="button"
      title="截取当前视频帧作为题图" aria-label={`截取当前视频帧作为题图：${marker.title}`}
      disabled={busy} onClick={() => void onCapture()}><Camera size={15} /></button>}
    {open && <QuestionImageDialog marker={marker} busy={busy} onCapture={onCapture}
      onRemove={onRemove} onClose={() => setOpen(false)} />}
  </>;
}

function QuestionImageDialog({ marker, busy, onCapture, onRemove, onClose }: Omit<Props, "state"> & { onClose: () => void }) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const [failedImage, setFailedImage] = useState<string | null>(null);
  const queryClient = useQueryClient();
  useModalFocus(true, dialogRef, closeRef, onClose);
  const state = useQuery({ queryKey: ["question-visual", marker.id],
    queryFn: () => getQuestionVisual(marker.id), retry: false });
  const automatic = useMutation({ mutationFn: () => autoQuestionImage(marker.id), onSuccess: value => {
    queryClient.setQueryData(["question-visual", marker.id], value);
    for (const queryKey of [["question-visuals", marker.video_id], ["question", marker.id], ["bilibili", marker.id]])
      void queryClient.invalidateQueries({ queryKey });
  } });
  const value = state.data;
  const binding = value?.binding;
  const url = binding && !binding.missing ? questionCaptureUrl(marker.id, binding.capture_id) : null;
  const pending = busy || automatic.isPending;
  return createPortal(<div className="modal-backdrop question-visual-backdrop" onMouseDown={event => {
    if (event.target === event.currentTarget) onClose();
  }}>
    <div ref={dialogRef} className="question-visual-dialog" role="dialog" aria-modal="true"
      aria-labelledby={`question-visual-title-${marker.id}`} tabIndex={-1}>
      <header className="question-visual-heading">
        <div><h2 id={`question-visual-title-${marker.id}`}>{marker.title}</h2>
          <span className="bili-muted">{visualLabel(value)}</span></div>
        <button ref={closeRef} className="icon-button" type="button" title="关闭题图" aria-label="关闭题图"
          onClick={onClose}><X size={20} /></button>
      </header>
      <div className="question-visual-content">
        {state.isPending && <p role="status"><LoaderCircle className="spin" size={16} />正在读取题图</p>}
        {[state.error, automatic.error].filter(Boolean).map((error, index) =>
          <p className="field-error" role="alert" key={index}>{error?.message}</p>)}
        {state.isError && <button className="secondary-button compact" onClick={() => void state.refetch()}>
          <RefreshCw size={15} />重新读取</button>}
        {value && <>
          <div className="question-visual-preview">
            {url && failedImage !== url ? <img src={url} alt="关联题图" onError={() => setFailedImage(url)} />
              : <div className="question-visual-empty"><ImageOff size={28} />
                <span>{binding ? "题图不可用" : "暂无题图"}</span></div>}
          </div>
          {binding && <p className="question-visual-caption">截图 {formatTime(binding.representative_ms)}</p>}
          <div className="question-visual-actions">
            {onCapture && <button className="icon-button" title="截取当前视频帧作为题图" aria-label="截取当前视频帧作为题图"
              disabled={pending} onClick={() => void onCapture()}><Camera size={17} /></button>}
            <button className="icon-button" title="按时间自动配图" aria-label="按时间自动配图"
              disabled={pending} onClick={() => automatic.mutate()}>
              {automatic.isPending ? <LoaderCircle className="spin" size={17} /> : <RefreshCw size={17} />}</button>
            {binding && onRemove && <button className="icon-button" title="取消题图" aria-label="取消题图"
              disabled={pending} onClick={() => void onRemove()}><Unlink size={17} /></button>}
          </div>
        </>}
      </div>
    </div>
  </div>, document.body);
}
