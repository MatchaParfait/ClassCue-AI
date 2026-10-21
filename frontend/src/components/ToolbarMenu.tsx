import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { AnimatePresence, AnimatedPopup } from "./Motion";

export function ToolbarMenu({ label, icon, primary = false, children, disabled = false }: {
  label: string; icon: ReactNode; primary?: boolean; children: ReactNode; disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [above, setAbove] = useState(false);
  const menu = useRef<HTMLDivElement>(null);
  const wrapper = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const id = useId();
  useLayoutEffect(() => {
    if (!open) return;
    const position = () => {
      if (!trigger.current || !menu.current) return;
      const anchor = trigger.current.getBoundingClientRect();
      const scroller = trigger.current.closest('.folder-window-content')?.getBoundingClientRect();
      const below = Math.min(innerHeight, scroller?.bottom ?? innerHeight) - anchor.bottom - 8;
      const over = anchor.top - Math.max(72, scroller?.top ?? 72) - 8;
      setAbove(below < menu.current.scrollHeight && over > below);
      menu.current.style.maxHeight = `${Math.max(80, Math.min(320, Math.max(below, over)))}px`;
    };
    position();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(position);
    if (menu.current) observer?.observe(menu.current);
    window.addEventListener('resize', position);
    window.addEventListener('scroll', position, true);
    return () => { observer?.disconnect(); window.removeEventListener('resize', position); window.removeEventListener('scroll', position, true); };
  }, [open]);
  useEffect(() => {
    if (!open) return;
    wrapper.current?.querySelector<HTMLElement>('[role="menuitem"]:not(:disabled)')?.focus();
    const outside = (event: PointerEvent) => {
      if (!wrapper.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, [open]);
  return (
    <div className="toolbar-menu" ref={wrapper} onKeyDown={(event) => {
      if (event.key === "Escape" && open) { event.preventDefault(); event.stopPropagation(); setOpen(false); trigger.current?.focus(); }
      if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
        event.preventDefault();
        if (!open) { setOpen(true); return; }
        const items = [...(wrapper.current?.querySelectorAll<HTMLElement>('[role="menuitem"]:not(:disabled)') ?? [])];
        const index = items.indexOf(document.activeElement as HTMLElement);
        const next = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1
          : (index + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
        items[next]?.focus();
      }
      if (event.key === "Tab") setOpen(false);
    }}>
      <button ref={trigger} type="button" className={primary ? "primary-button import-button" : "icon-button"}
        disabled={disabled}
        title={label} aria-label={label} aria-haspopup="menu" aria-expanded={open} aria-controls={id}
        onClick={() => setOpen((value) => !value)}>{icon}{primary && <span>{label}</span>}</button>
      <AnimatePresence>{open && <AnimatedPopup ref={menu} className={`toolbar-menu-items${above ? " is-above" : ""}`} id={id} role="menu" aria-label={label}
        onClick={(event) => {
          const item = (event.target as Element).closest('[role="menuitem"]');
          if (item && !item.hasAttribute("data-menu-keep-open")) { setOpen(false); trigger.current?.focus(); }
        }}>{children}</AnimatedPopup>}</AnimatePresence>
    </div>
  );
}
