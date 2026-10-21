import { ImageOff, LoaderCircle } from "lucide-react";
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from "react";
import { getSearchPreviewUrl } from "../api/client";
import type { SubtitleSearchHit } from "../types";
import { formatTime } from "../utils";
import { placeSearchPreview, type PreviewPlacement } from "../searchPreviewPlacement";
import { useEntryMotion } from "./Motion";

export type SearchPreviewTarget = {
  hit: SubtitleSearchHit;
  libraryId: string;
  anchor: HTMLElement;
  source: "pointer" | "keyboard";
};

export function SearchFramePreview({ target, dialog, onDismiss }: {
  target: SearchPreviewTarget; dialog: RefObject<HTMLDivElement>; onDismiss: () => void;
}) {
  const { hit, libraryId, anchor } = target;
  const key = `${libraryId}:${hit.video_id}:${hit.segment_id}:${hit.start_ms}`;
  const [shown, setShown] = useState("");
  const [loaded, setLoaded] = useState("");
  const [failed, setFailed] = useState("");
  const [placement, setPlacement] = useState<PreviewPlacement | null>(null);
  useEffect(() => {
    const timer = window.setTimeout(() => setShown(key), 200);
    return () => window.clearTimeout(timer);
  }, [key]);
  useLayoutEffect(() => {
    const position = () => {
      const dialogNode = dialog.current;
      if (!anchor.isConnected || !dialogNode) { onDismiss(); return; }
      const row = anchor.getBoundingClientRect();
      const scroll = anchor.closest(".global-search-scroll")?.getBoundingClientRect();
      if (scroll && (row.bottom <= scroll.top || row.top >= scroll.bottom)) { onDismiss(); return; }
      setPlacement(placeSearchPreview(row, dialogNode.getBoundingClientRect(), { width: innerWidth, height: innerHeight }));
    };
    const onScroll = () => {
      if (target.source === "pointer") onDismiss();
      else position();
    };
    position();
    window.addEventListener("resize", position);
    window.addEventListener("scroll", onScroll, true);
    return () => {
      window.removeEventListener("resize", position);
      window.removeEventListener("scroll", onScroll, true);
    };
  }, [anchor, dialog, key, target.source, onDismiss]);
  if (shown !== key || !placement) return null;
  const ready = loaded === key, unavailable = failed === key;
  return <PreviewSurface key={key} placement={placement}>
    <div className="global-search-preview-frame">
      {!unavailable && <img key={key} className={ready ? "is-ready" : ""} draggable={false} alt=""
        src={getSearchPreviewUrl(hit.video_id, hit.start_ms, libraryId)}
        onLoad={() => setLoaded(key)} onError={() => setFailed(key)} />}
      {!ready && !unavailable && <div className="global-search-preview-placeholder"><LoaderCircle className="spin" size={24} /><span>正在读取此处画面…</span></div>}
      {unavailable && <div className="global-search-preview-placeholder"><ImageOff size={26} /><span>画面暂不可用，仍可点击字幕跳转</span></div>}
      <time className="global-search-preview-time">{formatTime(hit.start_ms)}</time>
    </div>
    <div className="global-search-preview-caption">
      <strong>{hit.video_name || hit.filename}</strong>
      <span>{hit.text}</span>
    </div>
  </PreviewSurface>;
}

function PreviewSurface({ children, placement }: { children: ReactNode; placement: PreviewPlacement }) {
  const surface = useRef<HTMLDivElement>(null);
  useEntryMotion(surface, { distance: 3, duration: .12 });
  return <div ref={surface} className="global-search-preview" aria-hidden="true" data-placement={placement.side}
    style={{ left: placement.left, top: placement.top, width: placement.width }}>{children}</div>;
}
