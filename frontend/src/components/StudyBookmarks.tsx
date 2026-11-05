import { ArrowUpRight, Bookmark, BookmarkPlus, ChevronRight, FileVideo, FolderOpen, LoaderCircle, Pencil, Play, RotateCcw, Save, Trash2, X } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { createStudyBookmark, deleteStudyBookmark, editStudyBookmark, getStudyBookmarks, studyBookmarksKey,
  studyNavigationKey, studyVideoUrl, type StudyBookmark, type StudyFolder, type StudyVideo } from "../api/studyBookmarks";
import { useModalFocus } from "../hooks/useModalFocus";
import { canOpenReview, formatTime } from "../utils";
import { AnimatedOverlay, motion, useIsPresent, useMotionTransition, useReducedMotion } from "./Motion";

export type BookmarkScope = { title: string; folderId?: number; videoId?: number; positionMs?: number | null; create?: boolean };

export function StudyResume({ latest, onOpen, compact = false }: {
  latest: StudyVideo; onOpen: () => void; compact?: boolean;
}) {
  const label = `${latest.completed ? "上次看完" : "上次看到"} ${formatTime(latest.position_ms ?? 0)}`;
  return <button type="button" className={`study-resume${compact ? " is-compact" : ""}`} onClick={onOpen}
    title={`${[...latest.path.map(folder => folder.name), latest.display_name].join(" › ")} · ${label}`}
    aria-label={`继续复习：${latest.display_name}，${label}`}>
    <span className="study-resume-icon"><Play size={compact ? 13 : 17} aria-hidden="true" /></span>
    <span className="study-resume-copy">
      <span className="study-resume-title"><small>{compact ? "上次" : "继续复习"}</small><strong>{latest.display_name}</strong></span>
      {!compact && latest.path.length > 0 && <span className="study-resume-path"><FolderOpen size={12} aria-hidden="true" />
        <span>{latest.path.map(folder => folder.name).join(" › ")}</span></span>}
    </span>
    <span className="study-resume-position">
      {!compact && !latest.completed && <small>上次看到</small>}
      <time>{latest.completed ? "已看完" : formatTime(latest.position_ms ?? 0)}</time>
    </span>
    {!compact && <ChevronRight className="study-resume-arrow" size={17} aria-hidden="true" />}
  </button>;
}

export function FolderStudyHints({ summary, onResume, onBookmarks }: {
  summary?: StudyFolder; onResume: (video: StudyVideo) => void; onBookmarks: () => void;
}) {
  if (!summary || (!summary.latest && !summary.bookmark_count)) return null;
  return <div className="folder-study-hints" onPointerDown={event => event.stopPropagation()}
    onDragStart={event => { event.preventDefault(); event.stopPropagation(); }}>
    {summary.latest && <StudyResume compact latest={summary.latest} onOpen={() => onResume(summary.latest!)} />}
    {summary.bookmark_count > 0 && <button className="study-bookmark-count" type="button"
      title="查看此文件夹及子文件夹的书签" aria-label={`查看文件夹书签（${summary.bookmark_count}）`} aria-haspopup="dialog"
      onClick={onBookmarks}><Bookmark size={13} /><span>书签 {summary.bookmark_count}</span></button>}
  </div>;
}

export function StudyBookmarksDialog({ libraryId, scope, onClose, onSeek }: {
  libraryId: string; scope: BookmarkScope; onClose: () => void; onSeek?: (positionMs: number) => void;
}) {
  const titleId = useId();
  const noteId = useId();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const dialog = useRef<HTMLElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  const editorInput = useRef<HTMLTextAreaElement>(null);
  const [creating, setCreating] = useState(Boolean(scope.create && scope.videoId));
  const [editing, setEditing] = useState<StudyBookmark | null>(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [error, setError] = useState("");
  const [removed, setRemoved] = useState<StudyBookmark | null>(null);
  const present = useIsPresent();
  const reduced = useReducedMotion();
  const transition = useMotionTransition(.2);
  useModalFocus(present, dialog, close, () => { if (!busyRef.current) onClose(); });
  useEffect(() => { if (creating || editing) editorInput.current?.focus(); }, [creating, editing]);
  const bookmarks = useQuery({
    queryKey: [...studyBookmarksKey(libraryId), scope.folderId ?? null, scope.videoId ?? null],
    queryFn: () => getStudyBookmarks(libraryId, scope),
  });
  const invalidate = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: studyBookmarksKey(libraryId) }),
      queryClient.invalidateQueries({ queryKey: studyNavigationKey(libraryId) }),
    ]);
  };
  const mutate = async (action: () => Promise<unknown>, done?: () => void) => {
    if (busyRef.current) return;
    busyRef.current = true; setBusy(true); setError("");
    try { await action(); done?.(); await invalidate(); }
    catch (failure) { setError(failure instanceof Error ? failure.message : "书签保存失败，请重试"); }
    finally { busyRef.current = false; setBusy(false); }
  };
  const open = (bookmark: StudyBookmark) => {
    if (onSeek && bookmark.video_id === scope.videoId && bookmark.position_ms !== null) {
      onSeek(bookmark.position_ms); onClose();
    } else {
      onClose();
      navigate(bookmark.status && !canOpenReview(bookmark.status, Boolean(bookmark.has_transcript))
        ? `/process?videoId=${bookmark.video_id}` : studyVideoUrl(libraryId, bookmark.video_id, bookmark.position_ms));
    }
  };
  const editor = creating || editing;
  return createPortal(<AnimatedOverlay className="modal-backdrop study-bookmarks-backdrop" role="presentation" duration={.2} exitDuration={.16}
    onClick={event => { if (event.target === event.currentTarget && !busyRef.current) onClose(); }}>
    <motion.section ref={dialog} className="study-bookmarks-dialog" role="dialog" aria-modal="true" aria-labelledby={titleId} tabIndex={-1}
      initial={{ opacity: 0, y: reduced ? 0 : 12, scale: reduced ? 1 : .985 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ opacity: 0, y: reduced ? 0 : 8, scale: reduced ? 1 : .99, transition: { ...transition, duration: reduced ? 0 : .16 } }}
      transition={transition}>
      <header className="dialog-heading">
        <span className="study-bookmarks-heading-icon"><Bookmark size={20} aria-hidden="true" /></span>
        <div className="study-bookmarks-heading-copy"><h2 id={titleId}>复习书签</h2><p>{scope.title}</p></div>
        {bookmarks.isSuccess && <span className="study-bookmarks-total">{bookmarks.data.length} 个书签</span>}
        <button ref={close} type="button" className="icon-button" title="关闭书签" aria-label="关闭书签" disabled={busy} onClick={onClose}><X size={18} /></button>
      </header>
      <div className="study-bookmarks-content">
        {editor && <form className="study-bookmark-editor" aria-label={editing ? "编辑书签" : "添加书签"} onSubmit={event => {
          event.preventDefault();
          void mutate(() => editing ? editStudyBookmark(libraryId, editing.id, note)
            : createStudyBookmark(libraryId, scope.videoId!, scope.positionMs ?? null, note), () => {
            setCreating(false); setEditing(null); setNote(""); setRemoved(null);
          });
        }}>
          <label htmlFor={noteId}>{editing ? "编辑提醒" : "添加提醒"}<span>{(editing ? editing.position_ms : scope.positionMs) != null
            ? formatTime((editing ? editing.position_ms : scope.positionMs)!) : "整个视频"}</span></label>
          <textarea ref={editorInput} id={noteId} value={note} maxLength={2000} rows={3} disabled={busy}
            placeholder="这个视频没看懂，下次再看看" onChange={event => setNote(event.target.value)} />
          <div className="study-bookmark-editor-actions">
            <span className="study-bookmark-character-count">{note.length} / 2000</span>
            <button className="text-button compact" type="button" disabled={busy} onClick={() => { setCreating(false); setEditing(null); setError(""); }}>取消</button>
            <button className="primary-button compact" type="submit" disabled={busy}>
              {busy ? <LoaderCircle className="spin" size={14} /> : <Save size={14} />}保存书签</button>
          </div>
        </form>}
        {error && <p className="local-error" role="alert">{error}</p>}
        {removed && <div className="study-bookmark-removed" role="status"><span>书签已删除</span>
          <button type="button" className="text-button compact" disabled={busy} onClick={() => void mutate(
            () => createStudyBookmark(libraryId, removed.video_id, removed.position_ms, removed.note),
            () => setRemoved(null),
          )}><RotateCcw size={14} />撤销</button></div>}
        {bookmarks.isPending ? <div className="study-bookmark-empty" role="status"><LoaderCircle className="spin" size={18} />正在读取书签…</div>
          : bookmarks.isError ? <div className="study-bookmark-empty" role="alert"><span>书签读取失败</span>
            <button className="secondary-button compact" type="button" onClick={() => void bookmarks.refetch()}><RotateCcw size={14} />重试</button></div>
          : bookmarks.data?.length ? <ul className="study-bookmark-list">{bookmarks.data.map(bookmark => <li key={bookmark.id}>
            <button type="button" className="study-bookmark-open" onClick={() => open(bookmark)} disabled={busy}
              title={bookmark.position_ms === null ? "打开视频" : `跳转到 ${formatTime(bookmark.position_ms)}`}>
              <span className="study-bookmark-path"><FolderOpen size={12} aria-hidden="true" />
                <span>{bookmark.path?.map(folder => folder.name).join(" › ") || "视频库"}</span></span>
              <span className="study-bookmark-reminder">
                <span className="study-bookmark-field-label">我的提醒</span>
                <span className={`study-bookmark-note${bookmark.note ? "" : " is-empty"}`}>{bookmark.note || "未填写提醒"}</span>
              </span>
              <span className="study-bookmark-meta">
                <span className="study-bookmark-source">
                  <FileVideo size={13} aria-hidden="true" /><span className="study-bookmark-field-label">视频</span>
                  <span className="study-bookmark-video-name">{bookmark.display_name || bookmark.filename}</span>
                </span>
                <span className="study-bookmark-time">
                  <span className="study-bookmark-action" aria-hidden="true">{bookmark.position_ms === null ? "打开" : "跳转"}</span>
                  <Play size={12} aria-hidden="true" />{bookmark.position_ms === null ? "整个视频" : formatTime(bookmark.position_ms)}
                  <ArrowUpRight className="study-bookmark-jump-arrow" size={14} aria-hidden="true" />
                </span>
              </span>
            </button>
            <div className="study-bookmark-tools">
              <button type="button" className="icon-button" title="编辑提醒" aria-label={`编辑书签：${bookmark.note || bookmark.display_name}`}
                disabled={busy || Boolean(editor)} onClick={() => { setEditing(bookmark); setNote(bookmark.note); setError(""); }}><Pencil size={14} /></button>
              <button type="button" className="icon-button danger-icon" title="删除书签" aria-label={`删除书签：${bookmark.note || bookmark.display_name}`}
                disabled={busy || Boolean(editor)} onClick={() => void mutate(() => deleteStudyBookmark(libraryId, bookmark.id), () => setRemoved(bookmark))}><Trash2 size={14} /></button>
            </div>
          </li>)}</ul>
            : <div className="study-bookmark-empty"><Bookmark size={24} /><span>还没有书签</span></div>}
      </div>
      {scope.videoId !== undefined && !editor && <footer className="study-bookmarks-footer">
        <button type="button" className="secondary-button compact" disabled={busy} onClick={() => { setCreating(true); setNote(""); setError(""); }}>
          <BookmarkPlus size={15} />添加书签</button>
      </footer>}
    </motion.section>
  </AnimatedOverlay>, document.body);
}
