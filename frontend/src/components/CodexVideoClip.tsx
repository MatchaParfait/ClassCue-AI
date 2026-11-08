import { useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Clock3, ExternalLink, LoaderCircle, MessageSquare, Plus, RotateCcw, X } from "lucide-react";
import { Link } from "react-router-dom";
import { attachCodexDiscussion, getCodexClipDiscussions, openCodexDiscussion, prepareCodexClip,
  previewCodexClip, type CodexClipPreview, type CodexDiscussion } from "../api/codexBridge";
import { useDesktopAPI } from "../hooks/useDesktopAPI";
import { useModalFocus } from "../hooks/useModalFocus";
import { formatTime } from "../utils";
import "./CodexVideoClip.css";

function timeInput(milliseconds: number) {
  const rounded = Math.max(0, Math.round(milliseconds));
  const seconds = Math.floor(rounded / 1000), fraction = rounded % 1000;
  return `${String(Math.floor(seconds / 3600)).padStart(2, "0")}:${String(Math.floor(seconds / 60) % 60).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}${fraction ? `.${String(fraction).padStart(3, "0")}` : ""}`;
}

export function parseClipTime(value: string): number | null {
  const match = /^(\d{1,3}):([0-5]\d):([0-5]\d(?:\.\d{1,3})?)$/.exec(value.trim());
  return match ? Math.round((Number(match[1]) * 3600 + Number(match[2]) * 60 + Number(match[3])) * 1000) : null;
}

function createdTime(value?: string) {
  if (!value) return "";
  const iso = value.replace(" ", "T");
  const date = new Date(/(?:Z|[+-]\d\d:\d\d)$/i.test(iso) ? iso : `${iso}Z`);
  return Number.isFinite(date.getTime()) ? date.toLocaleString("zh-CN", { hour12: false }) : "";
}

export function CodexVideoClip({ videoId, libraryId, title, durationMs, getCurrentTimeMs }: {
  videoId: number; libraryId: string; title: string; durationMs: number; getCurrentTimeMs: () => number;
}) {
  const desktop = useDesktopAPI();
  const [position, setPosition] = useState<number | null>(null);
  if (!desktop) return null;
  return <>
    <button type="button" className="secondary-button compact" aria-haspopup="dialog" onClick={() => {
      const now = getCurrentTimeMs();
      setPosition(Number.isFinite(now) ? Math.min(durationMs, Math.max(0, now)) : 0);
    }}><MessageSquare size={15} />讲解片段</button>
    {position !== null && <CodexClipDialog videoId={videoId} libraryId={libraryId} title={title}
      durationMs={durationMs} positionMs={position} getCurrentTimeMs={getCurrentTimeMs}
      onClose={() => setPosition(null)} />}
  </>;
}

export function CodexClipDialog({ videoId, libraryId, title, durationMs, positionMs, getCurrentTimeMs, onClose }: {
  videoId: number; libraryId: string; title: string; durationMs: number; positionMs: number;
  getCurrentTimeMs: () => number; onClose: () => void;
}) {
  const id = useId();
  const dialog = useRef<HTMLElement>(null);
  const initial = useRef<HTMLInputElement>(null);
  const busyRef = useRef(false);
  const [operation, setOperation] = useState<"preview" | "prepare" | "open" | "attach" | null>(null);
  const busy = operation !== null;
  const [start, setStart] = useState(timeInput(Math.max(0, positionMs - 60_000)));
  const [end, setEnd] = useState(timeInput(Math.min(durationMs, positionMs || 60_000)));
  const [preview, setPreview] = useState<CodexClipPreview | null>(null);
  const [selected, setSelected] = useState<number[]>([]);
  const [result, setResult] = useState<CodexDiscussion | null>(null);
  const [resultForSelection, setResultForSelection] = useState(false);
  const [attemptedPrepare, setAttemptedPrepare] = useState(false);
  const [failedImages, setFailedImages] = useState<number[]>([]);
  const [error, setError] = useState("");
  const cache = useQueryClient();
  const historyKey = ["codex-video-clips", libraryId, videoId];
  const history = useQuery({ queryKey: historyKey, queryFn: () => getCodexClipDiscussions(videoId), retry: false });
  useModalFocus(true, dialog, initial, () => { if (!busyRef.current) onClose(); });
  const startMs = parseClipTime(start), endMs = parseClipTime(end);
  const validRange = startMs !== null && endMs !== null && startMs < endMs && endMs <= durationMs;
  const changed = () => { setPreview(null); setSelected([]); setFailedImages([]); setResult(null); setResultForSelection(false); setAttemptedPrepare(false); setError(""); };
  const run = async (kind: NonNullable<typeof operation>, action: () => Promise<void>) => {
    if (busyRef.current) return;
    busyRef.current = true; setOperation(kind); setError("");
    try { await action(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "片段准备失败，请重试"); }
    finally {
      busyRef.current = false; setOperation(null);
      if (kind !== "preview") void cache.invalidateQueries({ queryKey: historyKey });
    }
  };
  const native = result?.native_transfer;
  const status = native?.status;
  const completed = status === "pasted" || status === "attached";
  const uncertain = status === "unknown" || status === "inserting";
  const selectedImageFailed = selected.some(value => failedImages.includes(value));
  const canPrepare = !busy && Boolean(preview) && validRange && selected.length <= 23 && !selectedImageFailed;
  const prepare = (newDiscussion = false) => void run("prepare", async () => {
    if (!preview) return;
    setAttemptedPrepare(true); setResult(null); setResultForSelection(false);
    const value = await prepareCodexClip({ video_id: videoId, start_ms: preview.start_ms, end_ms: preview.end_ms,
      fingerprint: preview.fingerprint, slide_ids: selected, ...(newDiscussion ? { new_discussion: true } : {}) });
    setResult(value); setResultForSelection(true);
  });
  const operationLabel = operation === "preview" ? "正在读取片段…"
    : operation === "open" ? "正在打开讨论…" : operation === "attach" ? "正在追加片段…" : "正在准备草稿…";
  return createPortal(<div className="modal-backdrop codex-clip-backdrop" onClick={event => {
    if (event.target === event.currentTarget && !busyRef.current) onClose();
  }}>
    <section ref={dialog} className="codex-clip-dialog" role="dialog" aria-modal="true" aria-busy={busy} aria-labelledby={`${id}-title`} tabIndex={-1}>
      <header className="dialog-heading">
        <MessageSquare size={20} />
        <div><h2 id={`${id}-title`}>讲解视频片段</h2><p>{title}</p></div>
        <button className="icon-button" type="button" aria-label="关闭片段讲解" title="关闭" disabled={busy} onClick={onClose}><X size={18} /></button>
      </header>
      <div className="codex-clip-content">
        <fieldset disabled={busy} className="codex-clip-range">
          <legend>原视频时间 · 总长 {formatTime(durationMs)}</legend>
          {(["start", "end"] as const).map(side => <label key={side}>
            <span>{side === "start" ? "开始时间" : "结束时间"}</span>
            <div>
              <input ref={side === "start" ? initial : undefined} type="text" inputMode="decimal"
                aria-label={side === "start" ? "开始时间" : "结束时间"} placeholder="00:00:00"
                value={side === "start" ? start : end} onChange={event => {
                  changed(); (side === "start" ? setStart : setEnd)(event.target.value);
                }} />
              <button type="button" className="icon-button" aria-label={`将当前播放位置设为${side === "start" ? "开始" : "结束"}时间`}
                title="使用当前播放位置" onClick={() => {
                  const now = getCurrentTimeMs();
                  if (!Number.isFinite(now)) { setError("当前播放位置不可用，请稍后重试"); return; }
                  changed(); (side === "start" ? setStart : setEnd)(timeInput(Math.min(durationMs, Math.max(0, now))));
                }}><Clock3 size={16} /></button>
            </div>
          </label>)}
        </fieldset>
        {!validRange && <p className="field-error" role="alert">请输入 00:00:00 格式的时间，结束须晚于开始且不超过视频时长。</p>}
        <div className="codex-clip-preview-action">
          <button className="secondary-button compact" type="button" disabled={busy || !validRange} onClick={() => void run("preview", async () => {
            changed();
            const value = await previewCodexClip({ video_id: videoId, start_ms: startMs!, end_ms: endMs! });
            setPreview(value); setSelected(value.images.map(image => image.slide_id)); setResult(null);
          })}>{operation === "preview" ? <LoaderCircle size={15} className="spin" /> : <RotateCcw size={15} />}
            {operation === "preview" ? "正在读取片段…" : "预览片段"}</button>
          {preview && <span>{preview.subtitles.length} 条字幕 · {selected.length} 张精选 PPT</span>}
        </div>
        {busy && <p className="codex-clip-progress" role="status"><LoaderCircle className="spin" size={15} />{operationLabel}</p>}
        {preview && <>
          <details className="codex-clip-subtitles" open><summary>教师原话</summary><pre>{preview.subtitle_srt}</pre></details>
          <section className="codex-clip-slides" aria-label="片段精选 PPT">
            <h3>精选 PPT</h3>
            {preview.images.length ? <div className="codex-clip-images">{preview.images.map((image, index) =>
              <label key={image.slide_id}>
                <img src={image.image_url} alt={`精选 PPT ${index + 1}`} loading="lazy"
                  onError={() => setFailedImages(values => values.includes(image.slide_id) ? values : [...values, image.slide_id])} />
                <span><input type="checkbox" checked={selected.includes(image.slide_id)} disabled={busy} onChange={event => {
                  setSelected(values => event.target.checked ? [...values, image.slide_id] : values.filter(value => value !== image.slide_id));
                  setResult(null); setResultForSelection(false); setAttemptedPrepare(false);
                }} />图片 {index + 1} · {formatTime(image.time_instances[0].visible_from_ms)}</span>
                {failedImages.includes(image.slide_id) && <span className="field-error">图片读取失败</span>}
              </label>)}</div> : <p>此范围没有精选 PPT，可仅用字幕讲解。</p>}
          </section>
          {selected.length > 23 && <p className="field-error" role="alert">一次最多 23 张 PPT，请取消部分图片。</p>}
          {selectedImageFailed && <p className="field-error" role="alert">所选图片读取失败，请取消选择后继续，或重新预览重试。</p>}
        </>}
        {error && <p className="field-error" role="alert">{error}</p>}
        {result && <div className="codex-clip-result" role="status">
          {!resultForSelection && <p>历史片段：{formatTime(result.start_ms ?? 0)} – {formatTime(result.end_ms ?? 0)}</p>}
          {completed ? "已向 Codex 输入框粘贴过片段，请检查文字和图片，编辑后自行发送。"
            : native?.error || "讨论已准备，请检查 Codex 输入框。"}
          <div className="codex-clip-result-actions">
            <button className="text-button compact" type="button" disabled={busy} onClick={() => void run("open", async () => {
              setResult(await openCodexDiscussion(result.request_id));
            })}><ExternalLink size={14} />打开原讨论</button>
            {native && <button className="text-button compact" type="button" disabled={busy}
              title="会再次追加一套字幕与图片，请先确认 Codex 草稿中的已有内容"
              onClick={() => void run("attach", async () => { setResult(await attachCodexDiscussion(result.request_id)); })}>
              检查草稿后重新追加片段</button>}
          </div>
        </div>}
        {Boolean(history.data?.length) && <details className="codex-clip-history"><summary>片段讨论记录（{history.data!.length}）</summary>
          <ul>{history.data!.map((entry, index) => <li key={entry.request_id}><button className="text-button compact" type="button" disabled={busy}
            aria-label={`打开片段讨论 ${history.data!.length - index}：${formatTime(entry.start_ms ?? 0)} – ${formatTime(entry.end_ms ?? 0)}`}
            onClick={() => void run("open", async () => {
              const value = await openCodexDiscussion(entry.request_id);
              setResult(value); setResultForSelection(false);
            })}>
            <ExternalLink size={14} />讨论 {history.data!.length - index} · {formatTime(entry.start_ms ?? 0)} – {formatTime(entry.end_ms ?? 0)}
          </button><small>{entry.image_count ?? 0} 张 PPT{createdTime(entry.created_at) && ` · ${createdTime(entry.created_at)}`}</small></li>)}</ul>
        </details>}
        {history.isPending && <p className="codex-clip-progress" role="status">正在读取讨论记录…</p>}
        {history.error && <div className="field-error" role="alert">讨论记录读取失败：{history.error.message}
          <button className="text-button compact" type="button" disabled={busy} onClick={() => void history.refetch()}>
            <RotateCcw size={14} />重试读取记录</button></div>}
      </div>
      <footer>
        <Link to="/settings?section=codex" className="text-button compact" aria-disabled={busy} tabIndex={busy ? -1 : undefined}
          onClick={event => { if (busyRef.current) event.preventDefault(); else onClose(); }}>Codex 设置</Link>
        {attemptedPrepare && <button className="secondary-button compact" type="button" disabled={!canPrepare}
          title="使用当前片段新建独立讨论" onClick={() => prepare(true)}><Plus size={15} />新建独立讨论</button>}
        <button className="primary-button compact" type="button"
          disabled={!canPrepare || (resultForSelection && (uncertain || completed))}
          onClick={() => prepare()}>{operation === "prepare" ? <LoaderCircle size={16} className="spin" /> : <MessageSquare size={16} />}
          {operation === "prepare" ? "正在准备草稿…" : resultForSelection && completed ? "已准备草稿" : "在 Codex 中准备草稿"}</button>
      </footer>
    </section>
  </div>, document.body);
}
