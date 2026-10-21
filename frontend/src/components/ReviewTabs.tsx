import { createContext, useCallback, useContext, useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { ChevronLeft, ChevronRight, Library, Play, X } from "lucide-react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import type { Video } from "../types";

type OpenVideo = { id: number; title: string };
type ReviewTabsContextValue = {
  tabs: OpenVideo[];
  register: (video: Video) => void;
  setLibrary: (libraryId: string) => void;
  remove: (id: number) => void;
  positions: Map<string, number>;
};
const ReviewTabsContext = createContext<ReviewTabsContextValue | null>(null);

export function ReviewTabsProvider({ children }: { children: ReactNode }) {
  const [library, setLibrary] = useState("");
  const [libraries, setLibraries] = useState<Record<string, OpenVideo[]>>({});
  const positions = useRef(new Map<string, number>());
  const register = useCallback((video: Video) => {
    if (!video.library_id) return;
    const scope = video.library_id;
    setLibrary(scope);
    setLibraries(current => {
      const tabs = current[scope] ?? [];
      const previous = tabs.find(tab => tab.id === video.id);
      if (previous?.title === video.display_name) return current;
      return { ...current, [scope]: previous
        ? tabs.map(tab => tab.id === video.id ? { ...tab, title: video.display_name } : tab)
        : [...tabs, { id: video.id, title: video.display_name }] };
    });
  }, []);
  const remove = useCallback((id: number) => {
    setLibraries(current => ({ ...current, [library]: (current[library] ?? []).filter(tab => tab.id !== id) }));
  }, [library]);
  return <ReviewTabsContext.Provider value={{ tabs: libraries[library] ?? [], register, setLibrary, remove, positions: positions.current }}>
    {children}
  </ReviewTabsContext.Provider>;
}

export function useReviewTabs() {
  return useContext(ReviewTabsContext);
}

export function ReviewTabs() {
  const context = useReviewTabs();
  const location = useLocation();
  const navigate = useNavigate();
  const list = useRef<HTMLDivElement>(null);
  const [edges, setEdges] = useState({ left: false, right: false });
  const tabs = context?.tabs ?? [];
  const match = /^\/review\/(\d+)$/.exec(location.pathname);
  const activeId = match ? Number(match[1]) : null;
  const libraryActive = location.pathname === "/" || location.pathname.startsWith("/library/folder/");

  useEffect(() => {
    const element = list.current;
    if (!element) return;
    const update = () => setEdges({
      left: element.scrollLeft > 1,
      right: element.scrollLeft + element.clientWidth < element.scrollWidth - 1,
    });
    const active = element.querySelector<HTMLElement>('[aria-current="page"]');
    if (active) {
      const item = active.parentElement!;
      const left = item.offsetLeft, right = left + item.offsetWidth;
      if (left < element.scrollLeft) element.scrollLeft = left;
      else if (right > element.scrollLeft + element.clientWidth) element.scrollLeft = right - element.clientWidth;
    }
    update();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(update);
    observer?.observe(element);
    element.addEventListener("scroll", update, { passive: true });
    return () => { observer?.disconnect(); element.removeEventListener("scroll", update); };
  }, [activeId, tabs]);

  if (!context || !tabs.length) return null;
  const close = (id: number, focus: boolean) => {
    const index = tabs.findIndex(tab => tab.id === id);
    const next = tabs[index + 1] ?? tabs[index - 1];
    context.remove(id);
    if (id === activeId) navigate(next ? `/review/${next.id}` : "/", { replace: true });
    if (focus) {
      const targetId = id === activeId ? next?.id : activeId ?? next?.id;
      window.requestAnimationFrame(() => {
        const target = list.current?.querySelector<HTMLAnchorElement>(`a[href="/review/${targetId}"]`)
          ?? document.querySelector<HTMLAnchorElement>('.review-tabs-library, .workbench-brand');
        target?.focus({ preventScroll: true });
      });
    }
  };
  const keydown = (event: KeyboardEvent<HTMLAnchorElement>, id: number) => {
    if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey || event.nativeEvent.isComposing) return;
    if (event.key === "Delete") { event.preventDefault(); close(id, true); return; }
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const links = [...(list.current?.querySelectorAll<HTMLAnchorElement>(".review-tab-link") ?? [])];
    const index = links.indexOf(event.currentTarget);
    const next = event.key === "Home" ? 0 : event.key === "End" ? links.length - 1
      : (index + (event.key === "ArrowRight" ? 1 : -1) + links.length) % links.length;
    links[next]?.focus({ preventScroll: true });
    const item = links[next]?.parentElement;
    if (item && list.current) {
      list.current.scrollLeft = Math.max(0, item.offsetLeft - (list.current.clientWidth - item.offsetWidth) / 2);
    }
  };
  return <nav className="review-tabs" aria-label="已打开的视频">
    <Link className="review-tabs-library" to="/" aria-current={libraryActive ? "page" : undefined} title="返回视频库">
      <Library size={15} aria-hidden="true" /><span>视频库</span>
    </Link>
    <button type="button" className="review-tabs-scroll" aria-label="向左滚动视频标签" title="向左滚动视频标签"
      disabled={!edges.left} onClick={() => list.current?.scrollBy({ left: -300, behavior: "auto" })}><ChevronLeft size={16} /></button>
    <div ref={list} className="review-tabs-list">
      {tabs.map(tab => <div className={`review-tab${tab.id === activeId ? " is-active" : ""}`} key={tab.id}>
        <Link to={`/review/${tab.id}`} className="review-tab-link" title={tab.title}
          aria-current={tab.id === activeId ? "page" : undefined} onKeyDown={event => keydown(event, tab.id)}>
          <Play size={13} aria-hidden="true" /><span>{tab.title}</span>
        </Link>
        <button type="button" className="review-tab-close" aria-label={`关闭视频标签：${tab.title}`} title={`关闭标签：${tab.title}`}
          onClick={() => close(tab.id, true)}><X size={13} /></button>
      </div>)}
    </div>
    <button type="button" className="review-tabs-scroll" aria-label="向右滚动视频标签" title="向右滚动视频标签"
      disabled={!edges.right} onClick={() => list.current?.scrollBy({ left: 300, behavior: "auto" })}><ChevronRight size={16} /></button>
  </nav>;
}
