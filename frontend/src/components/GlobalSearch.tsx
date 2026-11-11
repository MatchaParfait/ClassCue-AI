import { useShortcuts, matchesShortcut, shortcutLabel, shortcutTitle, shortcutAria, isShortcutInput } from "../shortcuts";
import { Fragment, createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode, type RefObject } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMatch, useNavigate } from "react-router-dom";
import { ArrowUpRight, FileVideo, LoaderCircle, RotateCcw, Search, X } from "lucide-react";
import { getLibrary, getLibraryStatus, searchSubtitles } from "../api/client";
import { searchSemanticSubtitles, type SemanticCoverage } from "../api/semantic";
import type { SubtitleSearchHit, SubtitleSearchResult } from "../types";
import { formatTime } from "../utils";
import { useModalFocus } from "../hooks/useModalFocus";
import { AnimatePresence, AnimatedOverlay, AnimatedPopup, useIsPresent } from "./Motion";
import { SearchFramePreview, type SearchPreviewTarget } from "./SearchFramePreview";
import "../search.css";

interface SearchFolder { id: number; name: string }
const SearchContext = createContext<((folder?: SearchFolder) => void) | null>(null);
const hitKey = (hit: SubtitleSearchHit) => hit.result_id ?? `${hit.video_id}:${hit.segment_id}`;

function useReviewVideoId() {
  const match = useMatch("/review/:videoId");
  const value = match?.params.videoId;
  const id = value && /^\d+$/.test(value) ? Number(value) : NaN;
  return Number.isSafeInteger(id) && id > 0 ? id : null;
}

export function GlobalSearchButton() {
  const open = useContext(SearchContext);
  const shortcuts = useShortcuts();
  const videoId = useReviewVideoId();
  const label = videoId === null ? "搜索所有视频的字幕" : "搜索字幕 · 本视频优先";
  if (!open) return null;
  return <button type="button" className="global-search-trigger" onClick={() => open()} aria-label="全局搜索字幕" title={shortcutTitle(label, shortcuts, "search")} aria-keyshortcuts={shortcutAria(shortcuts, "search")}>
    <Search size={17} aria-hidden="true" /><span>{label}</span>{shortcuts.search.length > 0 && <kbd>{shortcutLabel(shortcuts, "search")}</kbd>}
  </button>;
}

export function FolderSubtitleSearchButton({ folderId, name }: { folderId: number; name: string }) {
  const open = useContext(SearchContext);
  if (!open) return null;
  return <button type="button" className="secondary-button compact" aria-label="搜索文件夹字幕"
    title={`搜索“${name}”及所有子文件夹的字幕`} onClick={() => open({ id: folderId, name })}>
    <Search size={16} aria-hidden="true" />搜索字幕
  </button>;
}

export function GlobalSearchProvider({ children }: { children: ReactNode }) {
  // Share the library identity/status observer, including on review routes.
  const library = useQuery({ queryKey: ["library-status"], queryFn: getLibraryStatus,
    staleTime: 15_000, refetchInterval: 15_000, refetchIntervalInBackground: false });
  const scope = library.data?.library_id ?? "";
  return <SearchSession scope={scope} libraryId={library.data?.library_id}>{children}</SearchSession>;
}

function SearchSession({ children, libraryId, scope }: { children: ReactNode; libraryId?: string; scope: string }) {
  const shortcuts = useShortcuts();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [composing, setComposing] = useState(false);
  const [folder, setFolder] = useState<SearchFolder | null>(null);
  const [fromFolder, setFromFolder] = useState(false);
  const [allVideos, setAllVideos] = useState(false);
  const [mode, setMode] = useState<"literal" | "semantic">("literal");
  const [data, setData] = useState<(SubtitleSearchResult & {
    preferredVideoId: number | null; folderId: number | null; mode: "literal" | "semantic";
    snapshot_id?: string; semantic_coverage?: SemanticCoverage;
  }) | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [selected, setSelected] = useState(0);
  const [preview, setPreview] = useState<SearchPreviewTarget | null>(null);
  const controller = useRef<AbortController>();
  const generation = useRef(0);
  const background = useRef<HTMLDivElement>(null);
  const previousScope = useRef(scope);
  const navigate = useNavigate();
  const reviewVideoId = useReviewVideoId();
  const rootRoute = useMatch("/");
  const showFolderPrefix = Boolean(rootRoute) && !fromFolder;
  const rootLibrary = useQuery({
    queryKey: ["library", "search-folders", scope], queryFn: () => getLibrary(),
    enabled: open && showFolderPrefix, staleTime: 15_000,
  });
  const rootFolders = (rootLibrary.data?.items ?? []).flatMap(item =>
    item.type === "folder" && item.folder && item.folder.parent_id == null ? [item.folder] : []);
  const folderId = folder && !allVideos ? folder.id : null;
  const preferredVideoId = folderId === null && mode === "literal" ? reviewVideoId : null;
  const client = useQueryClient();
  const trimmed = query.trim();
  const queryLimit = mode === "semantic" ? 1000 : 120;
  const valid = trimmed.length > 0 && [...trimmed].length <= queryLimit;
  const sameLibrary = !libraryId || data?.library_id === libraryId;
  const sameVideo = data?.preferredVideoId === preferredVideoId;
  const sameFolder = data?.folderId === folderId;
  const current = data?.query === trimmed && sameLibrary && sameVideo && sameFolder
    && data.mode === mode && !composing ? data : null;
  const items = current?.items ?? [];
  const show = useCallback((target?: SearchFolder) => {
    setFolder(target ?? null); setFromFolder(Boolean(target)); setAllVideos(false); setOpen(true);
  }, []);
  const clearPreview = useCallback(() => setPreview(null), []);
  const close = () => { clearPreview(); setOpen(false); setComposing(false); };
  const previewHit = (hit: SubtitleSearchHit, anchor: HTMLElement, source: SearchPreviewTarget["source"]) => {
    if (!current) return;
    setPreview(previous => previous?.anchor === anchor && previous.source === source && previous.libraryId === current.library_id
      && hitKey(previous.hit) === hitKey(hit) && previous.hit.start_ms === hit.start_ms ? previous
      : { hit, anchor, source, libraryId: current.library_id });
  };

  useEffect(() => {
    if (previousScope.current !== scope && previousScope.current !== "") {
      controller.current?.abort(); ++generation.current;
      setOpen(false); setPreview(null); setQuery(""); setData(null); setError(""); setBusy(false); setSelected(0); setComposing(false);
      setFolder(null); setFromFolder(false); setAllVideos(false);
      setMode("literal");
    }
    previousScope.current = scope;
  }, [scope]);

  useEffect(() => {
    const shortcut = (event: KeyboardEvent) => {
      const target = event.target instanceof Element ? event.target : null;
      if (event.repeat || target?.closest("[data-shortcut-recorder]") || event.isComposing || event.defaultPrevented) return;
      // Modifier shortcuts can still open search from a form; single keys never interrupt typing.
      if (isShortcutInput(event.target) && !event.ctrlKey && !event.metaKey && !event.altKey) return;
      if (matchesShortcut(event, shortcuts, "search")) {
        event.preventDefault(); show();
      }
    };
    document.addEventListener("keydown", shortcut);
    return () => document.removeEventListener("keydown", shortcut);
  }, [show, shortcuts]);

  const load = async (offset: number, token: number, signal: AbortSignal) => {
    setBusy(true); setError("");
    try {
      const result = mode === "semantic"
        ? await searchSemanticSubtitles(trimmed, offset, signal, folderId ?? undefined,
          offset > 0 ? data?.snapshot_id : undefined)
        : folderId !== null
        ? await searchSubtitles(trimmed, offset, signal, undefined, folderId)
        : await searchSubtitles(trimmed, offset, signal, preferredVideoId ?? undefined);
      if (signal.aborted || token !== generation.current) return;
      if ((libraryId && result.library_id !== libraryId) || (offset > 0 && data?.library_id !== result.library_id)) {
        setData(null);
        void client.invalidateQueries({ queryKey: ["library-status"] });
        throw new Error("视频库连接已变化，请重新搜索。");
      }
      setData((previous) => {
        if (!offset || !previous || previous.library_id !== result.library_id || previous.query !== result.query
            || previous.mode !== mode || previous.preferredVideoId !== preferredVideoId
            || previous.folderId !== folderId) return { ...result, preferredVideoId, folderId, mode };
        const merged = new Map(previous.items.map((hit) => [hitKey(hit), hit]));
        result.items.forEach((hit) => merged.set(hitKey(hit), hit));
        return { ...result, preferredVideoId, folderId, mode, items: [...merged.values()] };
      });
    } catch (failure) {
      if (!signal.aborted && token === generation.current) setError(failure instanceof Error ? failure.message : "搜索失败，请重试。");
    } finally {
      if (!signal.aborted && token === generation.current) setBusy(false);
    }
  };

  useEffect(() => {
    controller.current?.abort();
    const token = ++generation.current;
    setBusy(false); setError(""); setPreview(null);
    if (!valid || composing) { setData(null); setSelected(0); return; }
    if (!open || (data?.query === trimmed && sameLibrary && sameVideo && sameFolder && data.mode === mode)) return;
    setData(null); setSelected(0); setBusy(true);
    const request = new AbortController();
    controller.current = request;
    const timer = window.setTimeout(() => { void load(0, token, request.signal); }, 250);
    return () => { window.clearTimeout(timer); request.abort(); };
    // Data updates and polling must not replay searches or reset pagination.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [trimmed, composing, open, revision, scope, preferredVideoId, folderId, mode]);
  useEffect(() => () => { controller.current?.abort(); }, []);

  const loadMore = () => {
    if (busy || !current?.has_more) return;
    controller.current?.abort();
    const request = new AbortController(); controller.current = request;
    void load(current.offset + current.limit, ++generation.current, request.signal);
  };
  const refresh = () => { clearPreview(); setData(null); setRevision((value) => value + 1); };
  const choose = (hit: SubtitleSearchHit) => {
    if (!current || composing) return;
    const params = new URLSearchParams({ start_ms: String(hit.start_ms), library_id: current.library_id });
    close();
    navigate(`/review/${hit.video_id}?${params}`);
  };
  const groups = new Map<number, SubtitleSearchHit[]>();
  items.forEach((hit) => { const group = groups.get(hit.video_id) ?? []; group.push(hit); groups.set(hit.video_id, group); });

  return <SearchContext.Provider value={show}>
    <div ref={background} className="search-page-background">{children}</div>
    <AnimatePresence>
      {open && <SearchDialog key="global-search" background={background} onClose={close} onDismissPreview={clearPreview}
        preview={current && preview?.libraryId === current.library_id && items.some(hit => hitKey(hit) === hitKey(preview.hit))
          ? preview : null}>
        <div className="global-search-heading"><div><span className="global-search-eyebrow">{folderId !== null ? "当前文件夹及所有子文件夹" : preferredVideoId === null ? "跨视频查找" : "本视频优先 · 跨视频查找"}</span><h2 id="global-search-title">{folderId !== null ? "文件夹字幕搜索" : "全局字幕搜索"}</h2></div>
          <button type="button" className="icon-button" aria-label="关闭全局搜索" onClick={close}><X size={20} /></button>
        </div>
        <div className="global-search-scope">
          <div className="global-search-mode" aria-label="字幕搜索方式">
            <button type="button" aria-pressed={mode === "literal"} onClick={() => setMode("literal")}>原文</button>
            <button type="button" aria-pressed={mode === "semantic"} onClick={() => setMode("semantic")}>语义</button>
          </div>
        </div>
        {fromFolder && folder && <div className="global-search-scope">
          <span className="global-search-folder-name" title={folder.name}>{folder.name}</span>
          <div className="global-search-range" aria-label="字幕搜索范围">
            <button type="button" aria-pressed={!allVideos} onClick={() => setAllVideos(false)}>当前文件夹（含子文件夹）</button>
            <button type="button" aria-pressed={allVideos} onClick={() => setAllVideos(true)}>全部视频</button>
          </div>
        </div>}
        <div className={`global-search-field${showFolderPrefix ? " has-folder-prefix" : ""}`}>
          {showFolderPrefix && <select className="global-search-folder-prefix" aria-label="搜索文件夹范围"
            title={folder?.name ?? "全部视频"} value={folder?.id ?? ""}
            onChange={event => {
              const selectedFolder = rootFolders.find(candidate => String(candidate.id) === event.target.value);
              setFolder(selectedFolder ? { id: selectedFolder.id, name: selectedFolder.name } : null);
              setAllVideos(false);
            }}>
            <option value="">全部视频</option>
            {rootLibrary.isLoading && <option disabled>正在加载文件夹…</option>}
            {folder && !rootFolders.some(candidate => candidate.id === folder.id)
              && <option value={folder.id} disabled>{folder.name}</option>}
            {rootFolders.map(candidate => <option key={candidate.id} value={candidate.id}>{candidate.name}</option>)}
          </select>}
          <Search size={21} aria-hidden="true" />
          <input autoComplete="off" spellCheck={false} role="combobox" aria-label={folderId !== null ? "搜索当前文件夹及子文件夹的字幕" : "搜索所有视频的字幕"} aria-autocomplete="list"
            aria-expanded={items.length > 0} aria-controls="global-search-results"
            aria-activedescendant={items[selected] ? `search-hit-${hitKey(items[selected])}` : undefined}
            placeholder={mode === "semantic" ? "描述你要找的课堂讲解…" : folderId !== null ? "输入关键词，查找文件夹内的字幕…" : preferredVideoId === null ? "输入课堂上说过的关键词…" : "输入关键词，优先查找本视频字幕…"} value={query}
            onChange={(event) => { clearPreview(); setQuery(event.target.value); }}
            onCompositionStart={() => { clearPreview(); setComposing(true); }} onCompositionEnd={(event) => { setQuery(event.currentTarget.value); setComposing(false); }}
            onKeyDown={(event) => {
              if (event.nativeEvent.isComposing || composing) return;
              if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                event.preventDefault();
                if (items.length) {
                  const next = Math.max(0, Math.min(items.length - 1, selected + (event.key === "ArrowDown" ? 1 : -1)));
                  setSelected(next);
                  const anchor = document.getElementById(`search-hit-${hitKey(items[next])}`);
                  if (anchor) previewHit(items[next], anchor, "keyboard");
                }
              } else if (event.key === "Enter") {
                event.preventDefault(); if (items[selected]) choose(items[selected]);
              }
            }} />
          {query && <button type="button" className="icon-button" aria-label="清空搜索" onClick={(event) => {
            clearPreview(); setQuery(""); event.currentTarget.parentElement?.querySelector("input")?.focus();
          }}><X size={16} /></button>}
        </div>
        {showFolderPrefix && rootLibrary.isError && <div className="global-search-folder-error" role="alert">
          <span>文件夹列表加载失败</span>
          <button type="button" className="text-button" onClick={() => void rootLibrary.refetch()}>重试</button>
        </div>}
        <div className="global-search-summary" role="status" aria-live="polite">
          {!valid && trimmed ? `${mode === "semantic" ? "搜索内容" : "关键词"}最多 ${queryLimit} 个字符` : busy ? <><LoaderCircle className="spin" size={14} />正在查找…</>
            : current ? `${current.total} 处${mode === "semantic" ? "候选" : "字幕"} · ${current.total_videos} 个视频` : folderId !== null ? "仅搜索当前文件夹及所有子文件夹中已保存的字幕" : "搜索视频库所有文件夹中已保存的字幕"}
          {current && !busy && <button type="button" className="secondary-button compact global-search-refresh" onClick={refresh}><RotateCcw size={14} aria-hidden="true" />刷新结果</button>}
        </div>
        <div className="global-search-scroll">
          {mode === "semantic" && current?.semantic_coverage?.pending_videos ? <div className="global-search-folder-error" role="status">
            <span>{current.semantic_coverage.pending_videos} 个字幕视频尚未建立索引</span>
            <button type="button" className="text-button" onClick={() => {
              close(); navigate("/settings?section=semantic");
            }}>课程检索设置</button>
          </div> : null}
          {error && <div className="global-search-error" role="alert"><span>搜索失败：{error}</span>
            <button className="text-button" type="button" onClick={mode === "semantic" ? refresh : current ? loadMore : refresh}>重试</button>
            {mode === "semantic" && <button className="text-button" type="button" onClick={() => {
              close(); navigate("/settings?section=semantic");
            }}>课程检索设置</button>}
          </div>}
          <div id="global-search-results" role="listbox" aria-label="字幕搜索结果">
            {current && preferredVideoId !== null && <div className="global-search-section-label">
              <strong>本视频</strong><span>{groups.get(preferredVideoId)?.[0].video_match_count ?? 0} 处匹配</span>
            </div>}
            {current && preferredVideoId !== null && !groups.has(preferredVideoId)
              && <p className="global-search-local-empty">本视频暂无匹配字幕</p>}
            {[...groups].map(([videoId, hits], groupIndex, entries) => <Fragment key={videoId}>
              {preferredVideoId !== null && videoId !== preferredVideoId
                && (groupIndex === 0 || entries[groupIndex - 1][0] === preferredVideoId)
                && <div className="global-search-section-label is-other" role="separator" aria-label="其他视频"><strong>其他视频</strong></div>}
              <div className="global-search-group" role="group" aria-labelledby={`search-video-${videoId}`}>
              <div className="global-search-video" id={`search-video-${videoId}`}><FileVideo size={19} aria-hidden="true" />
                <div><strong>{hits[0].video_name || hits[0].filename}</strong><span>{hits[0].folder_name || "视频库"} · {mode === "semantic"
                  ? `已加载 ${hits.length} 处候选` : `${hits[0].video_match_count} 处匹配`}</span></div>
              </div>
              {hits.map((hit) => {
                const index = items.indexOf(hit);
                return <SearchHit key={hitKey(hit)} hit={hit} selected={selected === index} onChoose={() => choose(hit)} onSelect={() => setSelected(index)}
                  onPreview={(anchor) => previewHit(hit, anchor, "pointer")}
                  onLeave={() => setPreview(previous => previous?.source === "pointer" && hitKey(previous.hit) === hitKey(hit) ? null : previous)} />;
              })}
            </div></Fragment>)}
          </div>
          {!busy && !error && (!current || !items.length) && <div className="global-search-empty"><Search size={32} aria-hidden="true" />
            <strong>{mode === "semantic" ? current ? "暂无语义候选" : "查找课堂讲解"
              : current ? "没有找到匹配字幕" : "记得一句话，就能找到那堂课"}</strong>
            <p>{mode === "semantic" ? current?.semantic_coverage?.pending_videos
              ? "当前范围的索引尚未完整建立。" : "请在课程检索设置中检查索引状态。"
              : current ? "试试更短的关键词，或检查视频是否已完成转写。" : "支持中文短词与英文，按原文匹配。尚未转写的视频不会出现在结果中。"}</p>
          </div>}
          {current?.has_more && <button type="button" className="secondary-button global-search-more" disabled={busy} onClick={loadMore}>{busy ? "正在加载…" : "加载更多字幕"}</button>}
        </div>
        <div className="global-search-footer"><span>悬停 / ↑ ↓ 预览 · Enter 跳转 · Esc 关闭</span><span>{mode === "semantic" ? "语义候选 · 字幕原文" : "仅搜索字幕原文"}</span></div>
      </SearchDialog>}
    </AnimatePresence>
  </SearchContext.Provider>;
}

function SearchDialog({ children, background, onClose, preview, onDismissPreview }: {
  children: ReactNode; background: RefObject<HTMLDivElement>; onClose: () => void;
  preview: SearchPreviewTarget | null; onDismissPreview: () => void;
}) {
  const dialog = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLInputElement | null>(null);
  const present = useIsPresent();
  // The input belongs to the persistent session's content; resolve it before trapping focus.
  useEffect(() => { input.current = dialog.current?.querySelector("input") ?? null; });
  useModalFocus(present, dialog, input, onClose, background);
  return <AnimatedOverlay className="global-search-backdrop" duration={.16} exitDuration={.12} onClick={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <AnimatedPopup className="global-search-dialog" ref={dialog} role="dialog" aria-modal="true" aria-labelledby="global-search-title" duration={.16} exitDuration={.12} distance={4}>
      {children}
    </AnimatedPopup>
    {preview && <SearchFramePreview target={preview} dialog={dialog} onDismiss={onDismissPreview} />}
  </AnimatedOverlay>;
}

function SearchHit({ hit, selected, onChoose, onSelect, onPreview, onLeave }: {
  hit: SubtitleSearchHit; selected: boolean; onChoose: () => void; onSelect: () => void;
  onPreview: (anchor: HTMLElement) => void; onLeave: () => void;
}) {
  const row = useRef<HTMLDivElement>(null);
  useEffect(() => { if (selected) row.current?.scrollIntoView?.({ block: "nearest" }); }, [selected]);
  return <div ref={row} id={`search-hit-${hitKey(hit)}`} role="option" aria-selected={selected} tabIndex={-1}
    className={`global-search-hit${selected ? " is-selected" : ""}`} onClick={onChoose}
    onPointerMove={(event) => {
      if (event.pointerType === "touch") return;
      onSelect();
      if (event.pointerType === "mouse" || event.pointerType === "pen") onPreview(event.currentTarget);
    }}
    onPointerLeave={onLeave}>
    <time>{formatTime(hit.start_ms)}</time><span>{hit.snippet_parts.map((part, index) => part.match ? <mark key={index}>{part.text}</mark> : <span key={index}>{part.text}</span>)}</span>
    <ArrowUpRight size={16} aria-hidden="true" />
  </div>;
}
