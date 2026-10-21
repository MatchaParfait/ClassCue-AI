import { ArrowDownAZ, ArrowLeft, ChevronRight, Clock3, FolderOpen, FolderOutput, FolderPlus, GripVertical, Library, X } from "lucide-react";
import type { VideoLibraryFolder } from "../types";
import { useEffect, useLayoutEffect, useRef, useState, type DragEvent, type ReactNode, type RefObject } from "react";
import { motion, useMotionTransition, useReducedMotion } from "./Motion";
import { lockModalEnvironment } from "../hooks/modalEnvironment";

interface FolderWindowProps {
  viewKey?: string;
  name: string;
  count: number;
  folderCount?: number;
  breadcrumbs?: VideoLibraryFolder[];
  onNavigate?: (id: string | null) => void;
  onCreateFolder?: () => void;
  outsideDropLabel?: string;
  sortMode: "manual" | "name" | "time";
  onSortChange: (mode: "manual" | "name" | "time") => void;
  onClose: () => void;
  canDropOutside: boolean;
  showDragTextHints?: boolean;
  onDropOutside: () => void;
  onDropTo?: (id: string | null) => void;
  backgroundRef: RefObject<HTMLDivElement>;
  returnFocus: HTMLElement | null;
  children: ReactNode;
  initialScroll?: number;
  onScroll?: (scroll: number) => void;
}

export function FolderWindow({ viewKey = "", name, count, folderCount = 0, breadcrumbs = [], onNavigate, onCreateFolder,
  outsideDropLabel = "移至一级目录", sortMode, onSortChange, onClose, canDropOutside, showDragTextHints = true, onDropOutside, onDropTo, backgroundRef, returnFocus, children, initialScroll = 0, onScroll }: FolderWindowProps) {
  const dialogRef = useRef<HTMLElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const scrollRestored = useRef(false);
  const previousViewKey = useRef(viewKey);
  useEffect(() => {
    if (previousViewKey.current === viewKey) return;
    previousViewKey.current = viewKey;
    scrollRestored.current = false;
  }, [viewKey]);
  useEffect(() => {
    if (scrollRestored.current || !contentRef.current?.querySelector(".folder-window-list")) return;
    contentRef.current.scrollTop = initialScroll;
    scrollRestored.current = true;
  }, [children, initialScroll]);
  const onCloseRef = useRef(onClose);
  const pressedBackdrop = useRef(false);
  const [outsideHovered, setOutsideHovered] = useState(false);
  const [hoveredDestination, setHoveredDestination] = useState<string | null | undefined>();
  const [closing, setClosing] = useState(false);
  const [origin, setOrigin] = useState("50% 50%");
  const reduced = useReducedMotion();
  const overlayTransition = useMotionTransition(closing ? .34 : .18);
  const unfoldTransition = useMotionTransition(closing ? .34 : .46);
  const requestClose = () => {
    if (closing) return;
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) onClose();
    else setClosing(true);
  };
  onCloseRef.current = requestClose;

  useLayoutEffect(() => {
    if (!returnFocus?.isConnected || !dialogRef.current) return;
    const trigger = returnFocus.getBoundingClientRect();
    const dialog = dialogRef.current.getBoundingClientRect();
    if (!dialog.width || !dialog.height) return;
    const percent = (value: number) => `${Math.max(10, Math.min(90, value * 100))}%`;
    setOrigin(`${percent((trigger.x + trigger.width / 2 - dialog.x) / dialog.width)} ${percent((trigger.y + trigger.height / 2 - dialog.y) / dialog.height)}`);
  }, [returnFocus]);

  useEffect(() => {
    if (!canDropOutside) { setOutsideHovered(false); setHoveredDestination(undefined); }
  }, [canDropOutside, viewKey]);
  const parentId = breadcrumbs.at(-1)?.parentId ?? null;
  const parentName = breadcrumbs.at(-2)?.name ?? "视频库";
  const destinationProps = (id: string | null) => ({
    "data-drop-destination": id ?? "root",
    "data-drop-active": (canDropOutside && hoveredDestination === id) || undefined,
    onDragOver: (event: DragEvent<HTMLElement>) => {
      if (!canDropOutside || !onDropTo) return;
      event.preventDefault();
      event.stopPropagation();
      event.dataTransfer.dropEffect = "move";
      pressedBackdrop.current = false;
      setOutsideHovered(false);
      setHoveredDestination(id);
    },
    onDragLeave: (event: DragEvent<HTMLElement>) => {
      if (event.relatedTarget instanceof Node && event.currentTarget.contains(event.relatedTarget)) return;
      setHoveredDestination(undefined);
    },
    onDrop: (event: DragEvent<HTMLElement>) => {
      if (!canDropOutside || !onDropTo) return;
      event.preventDefault();
      event.stopPropagation();
      pressedBackdrop.current = false;
      setOutsideHovered(false);
      setHoveredDestination(undefined);
      onDropTo(id);
    },
  });
  const unfoldOrigin = `${origin.split(" ")[0]} 0%`;

  useEffect(() => {
    const background = backgroundRef.current;
    const opener = returnFocus ?? (document.activeElement instanceof HTMLElement ? document.activeElement : null);
    const release = lockModalEnvironment(background);
    closeRef.current?.focus();

    const focusable = () => [...(dialogRef.current?.querySelectorAll<HTMLElement>(
      'button:not([disabled]), a[href], input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ) ?? [])].filter((element) => !element.closest("[hidden], [inert]"));
    const isTopDialog = () => [...document.querySelectorAll<HTMLElement>('[role="dialog"][aria-modal="true"]')]
      .filter(element => !element.closest("[hidden], [inert]")).at(-1) === dialogRef.current;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.isComposing || !isTopDialog()) return;
      if (event.key === "Escape") {
        event.preventDefault();
        onCloseRef.current();
      } else if (event.key === "Tab") {
        const controls = focusable();
        const first = controls[0], last = controls[controls.length - 1];
        const active = document.activeElement;
        if (!first) {
          event.preventDefault();
          dialogRef.current?.focus();
        } else if (event.shiftKey && (active === first || !dialogRef.current?.contains(active))) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && (active === last || !dialogRef.current?.contains(active))) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    const keepFocusInside = (event: FocusEvent) => {
      if (!isTopDialog()) return;
      if (event.target instanceof Node && !dialogRef.current?.contains(event.target)) closeRef.current?.focus();
    };
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("focusin", keepFocusInside);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("focusin", keepFocusInside);
      release();
      if (opener?.isConnected && !opener.closest("[hidden], [inert]")) opener.focus({ preventScroll: true });
    };
  }, [backgroundRef, returnFocus]);

  return (
    <motion.div initial={{ opacity: 0 }} animate={{ opacity: closing ? 0 : 1 }} transition={overlayTransition}
      onAnimationComplete={() => { if (closing) onClose(); }}
      className={`modal-backdrop folder-window-backdrop motion-overlay${closing ? " is-leaving" : ""}${canDropOutside && outsideHovered ? " is-outside-drop" : ""}`} role="presentation"
      onDragStartCapture={() => { pressedBackdrop.current = false; }}
      onDragOver={(event) => {
        setHoveredDestination(undefined);
        const inside = event.target instanceof Node && dialogRef.current?.contains(event.target);
        setOutsideHovered(canDropOutside && !inside);
        if (!canDropOutside || inside) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = "move";
        pressedBackdrop.current = false;
      }}
      onDragLeave={(event) => {
        if (!(event.relatedTarget instanceof Node) || !event.currentTarget.contains(event.relatedTarget)) setOutsideHovered(false);
      }}
      onDrop={(event) => {
        if (!canDropOutside || (event.target instanceof Node && dialogRef.current?.contains(event.target))) return;
        event.preventDefault();
        event.stopPropagation();
        pressedBackdrop.current = false;
        setOutsideHovered(false);
        onDropOutside();
      }}
      onMouseDown={(event) => { pressedBackdrop.current = event.target === event.currentTarget; }}
      onClick={(event) => {
        if (event.target === event.currentTarget && pressedBackdrop.current) requestClose();
        pressedBackdrop.current = false;
      }}>
      {canDropOutside && showDragTextHints && <div className="folder-outside-drop-label" role="status"><FolderOutput size={15} /><span>{outsideDropLabel}</span></div>}
      <motion.div
        className="folder-window-unfold"
        initial={reduced ? false : { clipPath: "inset(0 0 100% 0 round 16px)", y: -12, scaleX: .985 }}
        animate={reduced ? {} : {
          clipPath: closing ? "inset(0 0 100% 0 round 16px)" : "inset(0 0 0% 0 round 16px)",
          y: closing ? -12 : 0,
          scaleX: closing ? .985 : 1,
        }}
        transition={unfoldTransition}
        style={{ transformOrigin: unfoldOrigin, perspective: 1200 }}>
        <motion.section
          initial={reduced ? false : { rotateX: -12 }}
          animate={reduced ? {} : { rotateX: closing ? -12 : 0 }}
          transition={unfoldTransition}
          style={{ transformOrigin: unfoldOrigin, transformStyle: "preserve-3d", backfaceVisibility: "hidden" }}
          className="folder-window motion-surface" ref={dialogRef} role="dialog" aria-modal="true" aria-labelledby="folder-window-title" tabIndex={-1}>
        <header className="folder-window-heading">
          <FolderOpen size={22} />
          <div><h2 id="folder-window-title">{name}</h2><span>{count} 个视频{folderCount > 0 ? ` · ${folderCount} 个子文件夹` : ""}</span></div>
          <button ref={closeRef} className="icon-button folder-window-close" type="button" onClick={requestClose} title="关闭文件夹" aria-label="关闭文件夹"><X size={18} /></button>
          {canDropOutside && onDropTo && <div className="folder-drop-destinations" role="group" aria-label="移出当前文件夹">
            <div className="folder-drop-destination" {...destinationProps(parentId)}>
              <FolderOutput size={22} />
              <span><small>{parentId ? "移至上一级" : "移至视频库"}</small><strong title={parentName}>{parentName}</strong></span>
              <ArrowLeft size={18} />
            </div>
            {parentId && <div className="folder-drop-destination" {...destinationProps(null)}>
              <Library size={22} />
              <span><small>移至根目录</small><strong>视频库</strong></span>
              <ArrowLeft size={18} />
            </div>}
          </div>}
        </header>
        <div className="folder-window-toolbar">
          {onNavigate && <nav className="folder-breadcrumbs" aria-label="文件夹路径">
            <button className="icon-button" type="button" aria-label="返回上一级" {...destinationProps(parentId)} onClick={() => onNavigate(parentId)}><ArrowLeft size={16} /></button>
            <button className="text-button" type="button" {...destinationProps(null)} onClick={() => onNavigate(null)}>视频库</button>
            {breadcrumbs.map((folder, index) => <span className="folder-breadcrumb" key={folder.id}>
              <ChevronRight size={14} />
              {index === breadcrumbs.length - 1 ? <span aria-current="page" title={folder.name}>{folder.name}</span>
                : <button className="text-button" type="button" {...destinationProps(folder.id)} onClick={() => onNavigate(folder.id)} title={folder.name}>{folder.name}</button>}
            </span>)}
          </nav>}
          <label className="sort-control">
            <span>排序</span>
            <select value={sortMode} aria-label="文件夹内排序" onChange={(event) => onSortChange(event.target.value as FolderWindowProps["sortMode"])}>
              <option value="manual">自定义位置</option>
              <option value="time">最近更新</option>
              <option value="name">名称 A-Z</option>
            </select>
            {sortMode === "name" ? <ArrowDownAZ size={15} /> : sortMode === "time" ? <Clock3 size={15} /> : <GripVertical size={15} />}
          </label>
          {onCreateFolder && <button className="secondary-button compact" type="button" onClick={onCreateFolder}><FolderPlus size={16} />新建文件夹</button>}
        </div>
        <div className="folder-window-content" ref={contentRef} onScroll={(event) => onScroll?.(event.currentTarget.scrollTop)}>{children}</div>
        </motion.section>
      </motion.div>
    </motion.div>
  );
}
