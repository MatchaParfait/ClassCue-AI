import { lazy, Suspense, useRef, useState } from "react";
import { Download, FileInput, LoaderCircle } from "lucide-react";
import { downloadStudyShare } from "../api/client";
import type { Video } from "../types";

const StudyShareDialog = lazy(() => import("./StudyShareDialog").then(module => ({ default: module.StudyShareDialog })));

export function StudyShareImportButton({ active, libraryId }: { active: boolean; libraryId?: string }) {
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  return <>
    <button ref={trigger} type="button" className="text-button" disabled={!active || !libraryId}
      onClick={() => setOpen(true)}><FileInput size={16} />导入分享包</button>
    {open && active && <Suspense fallback={<span role="status">正在打开分享包…</span>}>
      <StudyShareDialog libraryId={libraryId} returnFocus={trigger.current} onClose={() => setOpen(false)} />
    </Suspense>}
  </>;
}

export function StudyShareExportMenuItem({ video, blockReason }: { video: Video; blockReason?: string | null }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState(false);
  const submitting = useRef(false);
  const exportFile = async () => {
    if (submitting.current) return;
    submitting.current = true;
    setBusy(true);
    setError("");
    setDone(false);
    try { await downloadStudyShare(video); setDone(true); }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { submitting.current = false; setBusy(false); }
  };
  return <>
    <button type="button" role="menuitem" data-menu-keep-open
      disabled={busy || video.status !== "review_ready" || Boolean(blockReason)}
      title={blockReason || (video.status !== "review_ready" ? "视频处理完成后可导出" : "导出字幕和考点")}
      onClick={() => void exportFile()}>
      {busy ? <LoaderCircle className="spin" size={16} /> : <Download size={16} />}
      {busy ? "正在生成分享包…" : error ? "重试导出分享包" : "导出分享包"}
    </button>
    {done && <span className="share-menu-feedback" role="status">分享包已生成</span>}
    {error && <span className="share-menu-feedback field-error" role="alert">{error}</span>}
  </>;
}
