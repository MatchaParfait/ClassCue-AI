import { useEffect, useId, useRef, useState } from "react";
import { ArrowLeftRight, Check, LoaderCircle, SlidersHorizontal, Trash2, Undo2 } from "lucide-react";
import { createPicker, getSecondaryView, importSecondaryView, pollPicker, removeSecondaryView, setSecondaryViewAudio, setSecondaryViewOffset, startSecondaryAlignment, uploadSecondaryView } from "../api/client";
import { useDesktopAPI } from "../hooks/useDesktopAPI";
import { readAutoAlignSecondary } from "../secondaryViewPreferences";
import type { SecondaryView } from "../types";
import { AnimatePresence, AnimatedPopup } from "./Motion";
import { SecondaryAlignmentControl, useSecondaryAlignment } from "./SecondaryAlignmentControl";

interface Props {
  videoId: number;
  demo: boolean;
  secondary: SecondaryView | null;
  active: boolean;
  switching: boolean;
  onChange: (view: SecondaryView | null) => void;
  onSwitch: () => void;
}

export function SecondaryViewControl({ videoId, demo, secondary, active, switching, onChange, onSwitch }: Props) {
  const id = useId();
  const desktop = useDesktopAPI();
  const containerRef = useRef<HTMLDivElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const alive = useRef(false);
  const videoIdRef = useRef(videoId);
  videoIdRef.current = videoId;
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;
  const busyRef = useRef(false);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(!demo);
  const [open, setOpen] = useState(false);
  const [offset, setOffset] = useState("0");
  const [error, setError] = useState<string | null>(null);
  const alignment = useSecondaryAlignment({ videoId, secondary, onChange });

  useEffect(() => {
    alive.current = true;
    let cancelled = false;
    if (!demo) {
      void getSecondaryView(videoId).then(view => {
        if (!cancelled) onChangeRef.current(view);
      }).catch(failure => {
        if (!cancelled) setError(message(failure));
      }).finally(() => { if (!cancelled) setLoading(false); });
    }
    return () => { cancelled = true; alive.current = false; };
  }, [videoId, demo]);

  useEffect(() => { setOffset(String((secondary?.offset_ms ?? 0) / 1000)); }, [secondary?.offset_ms]);
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => {
      if (!containerRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") { setOpen(false); buttonRef.current?.focus(); }
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);

  const run = async (operation: () => Promise<void>) => {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setError(null);
    try { await operation(); }
    catch (failure) { if (alive.current && videoIdRef.current === videoId) { setError(message(failure)); setOpen(true); } }
    finally { busyRef.current = false; if (alive.current) setBusy(false); }
  };
  const offsetMs = Math.round(Number(offset) * 1000);
  const validOffset = offset.trim() !== "" && Number.isSafeInteger(offsetMs);
  const label = secondary ? active ? "切换到主视角" : "切换到副视角" : "导入副视角";
  const showImported = async (imported: Awaited<ReturnType<typeof importSecondaryView>>) => {
    const isCurrent = () => alive.current && videoIdRef.current === videoId;
    if (isCurrent()) {
      onChangeRef.current(imported.secondary_view);
      setOpen(true);
      if (imported.video.storage_warning) setError(imported.video.storage_warning);
    }
    if (!readAutoAlignSecondary()) return;
    try {
      const started = await startSecondaryAlignment(videoId, imported.secondary_view);
      if (isCurrent()) {
        onChangeRef.current({ ...imported.secondary_view, alignment_job_id: started.job_id });
      }
    } catch (failure) {
      if (isCurrent()) {
        setError([imported.video.storage_warning, `副视角已导入，自动对齐未启动：${message(failure)}`].filter(Boolean).join("；"));
      }
    }
  };

  return <div ref={containerRef} className="secondary-view-control">
    <input ref={fileRef} type="file" hidden accept=".mp4,.mov,.mkv,.avi" aria-label="选择副视角视频"
      onChange={event => {
        const file = event.currentTarget.files?.[0];
        event.currentTarget.value = "";
        if (!file) return;
        void run(async () => {
          await showImported(await uploadSecondaryView(videoId, file));
        });
      }} />
    <button className={`secondary-view-button secondary-view-switch${active ? " is-active" : ""}`} type="button"
      aria-label={label} aria-pressed={active} title={demo ? "演示模式暂不支持导入副视角" : label}
      disabled={demo || loading || busy || switching}
      onClick={() => {
        setError(null);
        if (secondary) onSwitch();
        else if (desktop) void run(async () => {
          const path = await pollPicker(await createPicker("file"), 15 * 60_000);
          if (path && alive.current && videoIdRef.current === videoId) await showImported(await importSecondaryView(videoId, path));
        });
        else fileRef.current?.click();
      }}>
      {busy || loading || switching ? <LoaderCircle className="spin" size={13} /> : <ArrowLeftRight size={14} />}
      <span>{secondary ? "切换视角" : "添加副视角"}</span>
    </button>
    {(secondary || error) && <button ref={buttonRef} className={`secondary-view-button secondary-view-settings${open ? " is-active" : ""}`}
      type="button" title="副视角设置" aria-label="副视角设置" aria-expanded={open} aria-controls={id}
      onClick={() => setOpen(value => !value)}><SlidersHorizontal size={13} /></button>}
    <AnimatePresence>{open && <AnimatedPopup id={id} className="secondary-view-popover" role="group" aria-label="副视角设置">
      {secondary && <>
        <strong className="secondary-view-filename" title={secondary.filename}>{secondary.filename}</strong>
        <form onSubmit={event => {
          event.preventDefault();
          if (!validOffset || busy || switching) return;
          void run(async () => {
            const view = await setSecondaryViewOffset(videoId, offsetMs);
            if (alive.current) onChangeRef.current(view);
          });
        }}>
          <label htmlFor={`${id}-offset`}>时间偏移（秒）</label>
          <div className="secondary-view-offset">
            <input id={`${id}-offset`} type="number" step="0.1" value={offset} aria-label="副视角时间偏移（秒）"
              disabled={busy || switching} onChange={event => setOffset(event.target.value)} />
            <button className="secondary-view-button secondary-view-save" type="submit" aria-label="保存时间偏移" title="保存时间偏移"
              disabled={!validOffset || busy || switching || offsetMs === secondary.offset_ms}><Check size={15} /></button>
            {offsetMs !== secondary.offset_ms && <button type="button" className="secondary-view-button secondary-view-save"
              aria-label="恢复未保存的时间偏移" title="恢复未保存的时间偏移" disabled={busy || switching}
              onClick={() => setOffset(String(secondary.offset_ms / 1000))}><Undo2 size={14} /></button>}
          </div>
        </form>
        <span className="secondary-view-mapping">副视角时间 = 主视频时间 {secondary.offset_ms < 0 ? "-" : "+"} {Math.abs(secondary.offset_ms) / 1000} 秒</span>
        <SecondaryAlignmentControl alignment={alignment}
          disabled={demo || busy || switching || loading || !validOffset || offsetMs !== secondary.offset_ms}
          onStart={() => setError(null)}
        />
        <label className="secondary-view-audio-option">
          <input type="checkbox" checked={Boolean(secondary.use_primary_audio)} disabled={busy || switching}
            onChange={event => {
              const enabled = event.target.checked;
              void run(async () => {
                const view = await setSecondaryViewAudio(videoId, enabled);
                if (alive.current && videoIdRef.current === videoId) onChangeRef.current(view);
              });
            }} />
          <span>使用主视频音频</span>
        </label>
        <button className="secondary-view-remove" type="button" aria-label="删除副视角" title="删除副视角关联，保留视频文件"
          disabled={busy || switching} onClick={() => void run(async () => {
            await removeSecondaryView(videoId);
            if (alive.current) { onChangeRef.current(null); setOpen(false); }
          })}><Trash2 size={15} />删除副视角</button>
      </>}
      {error && <span className="secondary-view-error" role="alert">{error}</span>}
    </AnimatedPopup>}</AnimatePresence>
  </div>;
}

function message(failure: unknown) {
  return failure instanceof Error ? failure.message : "副视角保存失败，请重试";
}
