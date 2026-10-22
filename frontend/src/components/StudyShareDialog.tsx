import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { AlertCircle, CheckCircle2, FileInput, FileVideo, LoaderCircle, RefreshCw, X } from "lucide-react";
import { importStudyShare, previewStudyShare } from "../api/client";
import { useModalFocus } from "../hooks/useModalFocus";
import type { StudySharePreview } from "../types";
import { formatTime } from "../utils";
import { AnimatedOverlay, AnimatedPopup } from "./Motion";

const MAX_BYTES = 32 * 1024 * 1024;

export function StudyShareDialog({ libraryId, returnFocus, onClose }: {
  libraryId?: string; returnFocus: HTMLElement | null; onClose: () => void;
}) {
  const dialog = useRef<HTMLDivElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const picker = useRef<HTMLInputElement>(null);
  const submitting = useRef(false);
  const committing = useRef(false);
  const previewRequest = useRef<AbortController | null>(null);
  const alive = useRef(true);
  const [file, setFile] = useState<File | null>(null);
  const [plan, setPlan] = useState<StudySharePreview | null>(null);
  const [phase, setPhase] = useState<"idle" | "preview" | "import">("idle");
  const [error, setError] = useState("");
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const close = () => { if (!committing.current) onClose(); };
  useModalFocus(true, dialog, cancel, close, undefined, returnFocus);
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; previewRequest.current?.abort(); };
  }, []);
  const inspect = async (selected: File) => {
    if (submitting.current) return;
    setFile(selected);
    setPlan(null);
    setError("");
    if (!selected.size || selected.size > MAX_BYTES) {
      setError(selected.size ? "分享包超过 32 MB" : "分享包为空");
      return;
    }
    submitting.current = true;
    setPhase("preview");
    const request = new AbortController();
    previewRequest.current = request;
    try {
      const result = await previewStudyShare(selected, request.signal);
      if (!alive.current) return;
      if (result.library_id !== libraryId) throw new Error("当前视频库已切换，请关闭后重新导入");
      setPlan(result);
    } catch (reason) {
      if (alive.current && !request.signal.aborted) setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      submitting.current = false;
      if (alive.current) setPhase("idle");
    }
  };
  const confirm = async () => {
    if (submitting.current || !file || !plan?.can_import || plan.library_id !== libraryId) return;
    submitting.current = true;
    committing.current = true;
    setPhase("import");
    setError("");
    try {
      const result = await importStudyShare(file, plan);
      for (const queryKey of [
        ["videos"], ["library"], ["library-status"], ["settings", "recall-schemes"],
        ["study", "video", result.video.id], ["study", "subtitles", result.video.id],
        ["study", "markers", result.video.id], ["study", "analysis-runs", result.video.id],
        ["process", "options", result.video.id], ["process", "video", result.video.id],
      ]) void queryClient.invalidateQueries({ queryKey });
      if (alive.current) {
        onClose();
        navigate(`/review/${result.video.id}`);
      }
    } catch (reason) {
      if (alive.current) {
        setError(reason instanceof Error ? reason.message : String(reason));
        setPlan(null);
      }
    } finally {
      submitting.current = false;
      committing.current = false;
      if (alive.current) setPhase("idle");
    }
  };
  const busy = phase !== "idle";
  return createPortal(
    <AnimatedOverlay className="modal-backdrop study-share-backdrop" onClick={event => {
      if (event.target === event.currentTarget) close();
    }}>
      <AnimatedPopup ref={dialog} className="study-share-dialog" role="dialog" aria-modal="true"
        aria-labelledby="study-share-title" aria-busy={busy} tabIndex={-1}>
        <div className="study-share-heading">
          <FileInput size={21} aria-hidden="true" /><h2 id="study-share-title">导入分享包</h2>
          <button className="icon-button" title="关闭" aria-label="关闭分享包" disabled={phase === "import"} onClick={close}><X size={18} /></button>
        </div>
        <div className="study-share-file">
          <span title={file?.name}>{file?.name ?? "尚未选择分享包"}</span>
          <button className="secondary-button compact" type="button" disabled={busy} onClick={() => picker.current?.click()}>
            <FileInput size={15} />{file ? "更换文件" : "选择分享包"}
          </button>
          <input ref={picker} type="file" accept=".studyshare" hidden aria-label="分享包文件" onChange={event => {
            const selected = event.currentTarget.files?.[0];
            event.currentTarget.value = "";
            if (selected) void inspect(selected);
          }} />
        </div>
        {phase === "preview" && <p className="study-share-progress" role="status">
          <LoaderCircle className="spin" size={17} />正在校验分享包和本地视频…
        </p>}
        {plan && <div className="study-share-preview">
          <div className="study-share-target"><FileVideo size={24} aria-hidden="true" /><div>
            <span>匹配的本地视频</span><strong>{plan.video_name}</strong>
            {plan.shared_video_name !== plan.video_name && <span>分享名称：{plan.shared_video_name}</span>}
          </div><span className="study-share-duration">{formatTime(plan.duration_ms)}</span></div>
          <dl className="study-share-counts">
            <div><dt>字幕</dt><dd>{plan.subtitle_count} 条</dd></div>
            <div><dt>考点</dt><dd>{plan.marker_count} 个</dd></div>
            <div><dt>召回方案</dt><dd>{plan.analysis_runs.length} 种</dd></div>
          </dl>
          <ul className="study-share-modes" aria-label="分享的召回方案">
            {plan.analysis_runs.map(run => <li key={run.analysis_mode}><CheckCircle2 size={14} />{run.name}</li>)}
          </ul>
          {plan.already_imported && <p className="study-share-matched" role="status"><CheckCircle2 size={16} />此分享包已导入，不会重复添加或覆盖。</p>}
          {plan.block_reason && <p className="field-error study-share-block" role="alert"><AlertCircle size={16} />{plan.block_reason}</p>}
        </div>}
        {error && <p className="field-error study-share-block" role="alert"><AlertCircle size={16} />{error}</p>}
        {phase === "import" && <p className="study-share-progress" role="status"><LoaderCircle className="spin" size={17} />正在导入字幕和考点…</p>}
        <div className="study-share-actions">
          <button ref={cancel} className="secondary-button" type="button" disabled={phase === "import"} onClick={close}>取消</button>
          {file && !busy && (!plan || !plan.can_import) && <button className="secondary-button" type="button" onClick={() => void inspect(file)}>
            <RefreshCw size={16} />重新校验
          </button>}
          <button className="primary-button" type="button" disabled={busy || !plan?.can_import || plan.library_id !== libraryId}
            onClick={() => void confirm()}>
            {phase === "import" ? <LoaderCircle className="spin" size={16} /> : <CheckCircle2 size={16} />}
            {phase === "import" ? "正在导入…" : plan?.already_imported ? "打开复习" : "导入并复习"}
          </button>
        </div>
      </AnimatedPopup>
    </AnimatedOverlay>, document.body,
  );
}
