import { ArrowLeft, X } from "lucide-react";
import { useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from "react";
import { useModalFocus } from "../hooks/useModalFocus";
import { motion, useMotionTransition, useReducedMotion } from "./Motion";

interface Props {
  active: boolean;
  title: string;
  scrollKey: string;
  onClose: () => void;
  onBack?: () => void;
  backgroundRef: RefObject<HTMLDivElement>;
  children: ReactNode;
}

export function WorkbenchPanel({ active, title, scrollKey, onClose, onBack, backgroundRef, children }: Props) {
  const [present, setPresent] = useState(active);
  if (active && !present) setPresent(true);
  const transition = useMotionTransition(active ? .18 : .13);
  const reduced = useReducedMotion();
  const activeRef = useRef(active);
  activeRef.current = active;
  const previousContent = useRef(children);
  if (active) previousContent.current = children;
  const dialog = useRef<HTMLElement>(null);
  const backdrop = useRef<HTMLDivElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const pressedBackdrop = useRef(false);
  const content = useRef<HTMLDivElement>(null);
  const positions = useRef<Record<string, number>>({});
  useLayoutEffect(() => { if (backdrop.current) backdrop.current.inert = !active; }, [active]);
  useLayoutEffect(() => {
    if (active && content.current) content.current.scrollTop = positions.current[scrollKey] ?? 0;
  }, [active, scrollKey]);
  useModalFocus(active, dialog, closeButton, onClose, backgroundRef);
  return (
    <motion.div className="workbench-panel-backdrop motion-overlay" hidden={!active && !present} aria-hidden={!active || undefined}
      initial={false} animate={{ opacity: active ? 1 : 0 }} transition={transition}
      onAnimationComplete={() => { if (!activeRef.current) setPresent(false); }}
      ref={backdrop} onMouseDown={(event) => {
      pressedBackdrop.current = event.target === event.currentTarget;
    }} onClick={(event) => {
      if (event.target === event.currentTarget && pressedBackdrop.current) onClose();
      pressedBackdrop.current = false;
    }}>
      <motion.section className="workbench-panel motion-surface" ref={dialog} role={active ? "dialog" : undefined}
        initial={false} animate={{ x: active || reduced ? 0 : 12 }} transition={transition}
        aria-modal={active || undefined} aria-labelledby="workbench-panel-title" tabIndex={-1}>
        <header className="workbench-panel-heading">
          {onBack && <button className="icon-button" title="返回上一个面板" aria-label="返回上一个面板" onClick={onBack}><ArrowLeft size={18} /></button>}
          <h2 id="workbench-panel-title">{title}</h2>
          <button ref={closeButton} className="icon-button" title="关闭面板" aria-label="关闭面板" onClick={onClose}><X size={18} /></button>
        </header>
        <div className="workbench-panel-content" ref={content} onScroll={(event) => {
          if (active) positions.current[scrollKey] = event.currentTarget.scrollTop;
        }}>{active ? children : previousContent.current}</div>
      </motion.section>
    </motion.div>
  );
}
