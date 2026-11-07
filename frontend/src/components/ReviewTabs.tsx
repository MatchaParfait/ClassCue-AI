import { createContext, useCallback, useContext, useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { ChevronLeft, ChevronRight, FileText, Library, Play, X } from "lucide-react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import type { Video } from "../types";
import { useUnsavedLeave } from "./UnsavedChanges";

type OpenTab = { key: string; kind: "video" | "materials"; title: string; href: string };
type ReviewTabsContextValue = {
  tabs: OpenTab[];
  register: (video: Video) => void;
  registerStudyMaterials: (libraryId: string, href: string) => void;
  setLibrary: (libraryId: string) => void;
  remove: (key: string) => void;
  positions: Map<string, number>;
};
const ReviewTabsContext = createContext<ReviewTabsContextValue | null>(null);

export function ReviewTabsProvider({ children }: { children: ReactNode }) {
  const [library, setLibrary] = useState("");
  const [libraries, setLibraries] = useState<Record<string, OpenTab[]>>({});
  const positions = useRef(new Map<string, number>());
  const registerTab = useCallback((scope: string, tab: OpenTab) => {
    if (!scope) return;
    setLibrary(scope);
    setLibraries(current => {
      const tabs = current[scope] ?? [];
      const previous = tabs.find(item => item.key === tab.key);
      if (previous?.title === tab.title && previous.href === tab.href) return current;
      return { ...current, [scope]: previous
        ? tabs.map(item => item.key === tab.key ? tab : item) : [...tabs, tab] };
    });
  }, []);
  const register = useCallback((video: Video) => {
    if (!video.library_id) return;
    registerTab(video.library_id, {
      key: `video:${video.id}`, kind: "video", title: video.display_name, href: `/review/${video.id}`,
    });
  }, [registerTab]);
  const registerStudyMaterials = useCallback((libraryId: string, href: string) => {
    registerTab(libraryId, {
      key: "study-materials", kind: "materials", title: "学习资料", href,
    });
  }, [registerTab]);
  const remove = useCallback((key: string) => {
    setLibraries(current => ({ ...current, [library]: (current[library] ?? []).filter(tab => tab.key !== key) }));
  }, [library]);
  return <ReviewTabsContext.Provider value={{ tabs: libraries[library] ?? [], register, registerStudyMaterials, setLibrary, remove, positions: positions.current }}>
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
  const leave = useUnsavedLeave();
  const list = useRef<HTMLDivElement>(null);
  const [edges, setEdges] = useState({ left: false, right: false });
  const tabs = context?.tabs ?? [];
  const match = /^\/review\/(\d+)$/.exec(location.pathname);
  const activeKey = match ? `video:${Number(match[1])}`
    : location.pathname === "/study-materials" ? "study-materials" : null;
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
  }, [activeKey, tabs]);

  if (!context || !tabs.length) return null;
  const close = (key: string, focus: boolean) => {
    const index = tabs.findIndex(tab => tab.key === key);
    const next = tabs[index + 1] ?? tabs[index - 1];
    const finish = () => {
      context.remove(key);
      if (key === activeKey) navigate(next?.href ?? "/", { replace: true });
      if (focus) {
        const targetKey = key === activeKey ? next?.key : activeKey ?? next?.key;
        window.requestAnimationFrame(() => {
          const target = [...(list.current?.querySelectorAll<HTMLAnchorElement>(".review-tab-link") ?? [])]
            .find(link => link.dataset.tabKey === targetKey)
            ?? document.querySelector<HTMLAnchorElement>('.review-tabs-library, .workbench-brand');
          target?.focus({ preventScroll: true });
        });
      }
    };
    if (key === activeKey) leave(finish);
    else finish();
  };
  const keydown = (event: KeyboardEvent<HTMLAnchorElement>, key: string) => {
    if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey || event.nativeEvent.isComposing) return;
    if (event.key === "Delete") { event.preventDefault(); close(key, true); return; }
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
  return <nav className="review-tabs" aria-label="已打开的视频与资料">
    <Link className="review-tabs-library" to="/" aria-current={libraryActive ? "page" : undefined} title="返回视频库">
      <Library size={15} aria-hidden="true" /><span>视频库</span>
    </Link>
    <button type="button" className="review-tabs-scroll" aria-label="向左滚动标签" title="向左滚动标签"
      disabled={!edges.left} onClick={() => list.current?.scrollBy({ left: -300, behavior: "auto" })}><ChevronLeft size={16} /></button>
    <div ref={list} className="review-tabs-list">
      {tabs.map(tab => <div className={`review-tab${tab.key === activeKey ? " is-active" : ""}`} key={tab.key}>
        <Link to={tab.href} className="review-tab-link" title={tab.title} data-tab-key={tab.key}
          aria-current={tab.key === activeKey ? "page" : undefined} onKeyDown={event => keydown(event, tab.key)}>
          {tab.kind === "video" ? <Play size={13} aria-hidden="true" /> : <FileText size={13} aria-hidden="true" />}<span>{tab.title}</span>
        </Link>
        <button type="button" className="review-tab-close" aria-label={`关闭${tab.kind === "video" ? "视频" : "资料"}标签：${tab.title}`} title={`关闭标签：${tab.title}`}
          onClick={() => close(tab.key, true)}><X size={13} /></button>
      </div>)}
    </div>
    <button type="button" className="review-tabs-scroll" aria-label="向右滚动标签" title="向右滚动标签"
      disabled={!edges.right} onClick={() => list.current?.scrollBy({ left: 300, behavior: "auto" })}><ChevronRight size={16} /></button>
  </nav>;
}
